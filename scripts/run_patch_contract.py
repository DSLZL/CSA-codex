#!/usr/bin/env python3
"""Apply and run one immutable patch payload contract in a disposable checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from verify_patch_payload import VerificationError, _load_payload, _payload_file, verify


ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]*\Z")
FLAKY_TUI_BACKGROUND_EXIT_STEP = "TUI background exit isolation"
FAILURE_OUTPUT_TAIL_BYTES = 256 * 1024
TEST_RUNNERS = ("cargo", "nextest")
NEXTEST_PASSTHROUGH_LIBTEST_FLAGS = {
    "--exact",
    "--ignored",
    "--include-ignored",
    "--nocapture",
}


class ContractError(RuntimeError):
    pass


def cross_windows_build_argv(argv: list[str]) -> list[str]:
    if len(argv) < 2 or argv[:2] != ["cargo", "build"]:
        raise ContractError("cross-Windows build step must start with cargo build")
    return ["cargo", "xwin", *argv[1:]]


def cross_windows_build_env(env: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(env, dict) or "RUSTFLAGS" in env:
        raise ContractError("cross-Windows build flags must be runner-owned")
    return {
        **env,
        "RUSTFLAGS": "-C link-arg=/debug:none -C link-arg=/build-id:no",
    }


def test_runner_argv(argv: list[str], test_runner: str) -> list[str]:
    """Map a logical Cargo test command to one explicitly supported runner."""
    if test_runner not in TEST_RUNNERS:
        raise ContractError(f"unsupported test runner: {test_runner}")
    if test_runner == "cargo" or argv[:2] != ["cargo", "test"]:
        return list(argv)

    separator = argv.index("--") if "--" in argv else len(argv)
    cargo_args = argv[2:separator]
    if "--doc" in cargo_args:
        # cargo-nextest does not support doctests. Keep any explicit doctest
        # contract on Cargo rather than silently weakening the test gate.
        return list(argv)

    libtest_args = argv[separator + 1 :] if separator < len(argv) else []
    passthrough: list[str] = []
    test_threads: str | None = None
    index = 0
    while index < len(libtest_args):
        argument = libtest_args[index]
        if argument in NEXTEST_PASSTHROUGH_LIBTEST_FLAGS:
            passthrough.append(argument)
        elif argument == "--skip":
            if index + 1 == len(libtest_args):
                raise ContractError("nextest mapping requires a value after --skip")
            passthrough.extend((argument, libtest_args[index + 1]))
            index += 1
        elif argument.startswith("--test-threads="):
            test_threads = argument.partition("=")[2]
        elif argument == "--test-threads":
            if index + 1 == len(libtest_args):
                raise ContractError("nextest mapping requires a value after --test-threads")
            test_threads = libtest_args[index + 1]
            index += 1
        elif argument == "--format=terse":
            # Nextest already owns output formatting and does not expose
            # libtest's terse formatter.
            pass
        elif argument == "--format":
            if index + 1 == len(libtest_args) or libtest_args[index + 1] != "terse":
                raise ContractError("nextest mapping supports only libtest --format=terse")
            index += 1
        else:
            raise ContractError(
                f"nextest mapping does not support libtest argument: {argument}"
            )
        index += 1

    runner_args: list[str] = []
    if test_threads is not None:
        if not test_threads.isdecimal() or int(test_threads) < 1:
            raise ContractError("nextest test thread count must be a positive integer")
        runner_args.append(f"--test-threads={test_threads}")
    mapped = ["cargo", "nextest", "run", *runner_args, *cargo_args]
    if passthrough:
        mapped.extend(("--", *passthrough))
    return mapped


def load_contract(path: Path, compat_id: str) -> dict[str, Any]:
    try:
        contract = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read test contract: {error}") from error
    expected = {
        "schema",
        "compat_id",
        "parameters",
        "cwd",
        "common_env",
        "generation",
        "tests",
        "build",
        "known_upstream_errata",
    }
    if (
        not isinstance(contract, dict)
        or set(contract) != expected
        or contract.get("schema") != 1
        or contract.get("compat_id") != compat_id
        or not isinstance(contract.get("generation"), list)
        or not isinstance(contract.get("tests"), list)
        or not isinstance(contract.get("build"), dict)
    ):
        raise ContractError("unsupported test contract")
    return contract


def expand(value: str, source: Path, cargo_target: Path) -> str:
    return value.replace("{source}", str(source)).replace("{cargo_target}", str(cargo_target))


def environment(
    common: dict[str, Any], extra: dict[str, Any], source: Path, cargo_target: Path
) -> dict[str, str]:
    if not isinstance(common, dict) or not isinstance(extra, dict):
        raise ContractError("contract environment must be an object")
    result = os.environ.copy()
    for name, value in {**common, **extra}.items():
        if not isinstance(name, str) or not ENV_NAME.fullmatch(name) or not isinstance(value, str):
            raise ContractError("contract environment contains an invalid entry")
        result[name] = expand(value, source, cargo_target)
    return result


def run_step(
    step: dict[str, Any],
    kind: str,
    cwd: Path,
    common_env: dict[str, Any],
    source: Path,
    cargo_target: Path,
    test_runner: str = "cargo",
) -> dict[str, Any]:
    allowed = {"name", "argv", "env", "output"}
    if not isinstance(step, dict) or set(step) - allowed or not {"name", "argv"} <= set(step):
        raise ContractError(f"invalid {kind} step")
    argv = step["argv"]
    output = step.get("output", "live")
    if (
        not isinstance(step["name"], str)
        or not isinstance(argv, list)
        or not argv
        or argv[0] != "cargo"
        or any(not isinstance(value, str) or not value for value in argv)
    ):
        raise ContractError(f"{kind} step must be a named cargo argv array")
    if not isinstance(output, str) or output not in {"live", "failure-only"}:
        raise ContractError(f"invalid {kind} output policy")
    expanded = [expand(value, source, cargo_target) for value in argv]
    runner_argv = test_runner_argv(expanded, test_runner)
    attempts = 2 if step["name"] == FLAKY_TUI_BACKGROUND_EXIT_STEP else 1
    print(f"{kind} step started: {step['name']}", flush=True)
    for attempt in range(1, attempts + 1):
        options = {
            "cwd": cwd,
            "env": environment(common_env, step.get("env", {}), source, cargo_target),
            "shell": False,
        }
        if output == "failure-only":
            with tempfile.TemporaryFile() as captured_stdout:
                result = subprocess.run(
                    runner_argv,
                    **options,
                    stdout=captured_stdout,
                )
                if result.returncode and attempt == attempts:
                    captured_stdout.seek(0, os.SEEK_END)
                    captured_size = captured_stdout.tell()
                    captured_stdout.seek(max(0, captured_size - FAILURE_OUTPUT_TAIL_BYTES))
                    if captured_size > FAILURE_OUTPUT_TAIL_BYTES:
                        sys.stderr.write(
                            f"[captured stdout truncated; showing final "
                            f"{FAILURE_OUTPUT_TAIL_BYTES} bytes]\n"
                        )
                    sys.stderr.write(captured_stdout.read().decode("utf-8", errors="replace"))
                    sys.stderr.flush()
        else:
            result = subprocess.run(runner_argv, **options)
        if not result.returncode or attempt == attempts:
            break
        print(
            f"{kind} step hit the known upstream event race; retrying once: {step['name']}",
            file=sys.stderr,
        )
    if result.returncode:
        raise ContractError(f"{kind} step failed ({result.returncode}): {step['name']}")
    print(f"{kind} step passed: {step['name']}", flush=True)
    report = {"kind": kind, "name": step["name"], "argv": expanded, "exit_code": 0}
    if runner_argv != expanded:
        report["runner_argv"] = runner_argv
    return report


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_report(output: Path, report: dict[str, Any]) -> None:
    with output.open("xb") as destination:
        destination.write((json.dumps(report, indent=2, sort_keys=True) + "\n").encode())


def load_test_report(
    path: Path,
    compat_id: str,
    known_upstream_errata: object,
    expected_steps: list[tuple[str, str]],
) -> dict[str, Any]:
    try:
        report = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ContractError(f"cannot read test phase report: {error}") from error
    expected = {
        "schema",
        "result",
        "phase",
        "compat_id",
        "source_verification",
        "steps",
        "known_upstream_errata",
    }
    if (
        not isinstance(report, dict)
        or set(report) != expected
        or report.get("schema") != 1
        or report.get("result") != "pass"
        or report.get("phase") != "tests"
        or report.get("compat_id") != compat_id
        or not isinstance(report.get("source_verification"), dict)
        or not isinstance(report.get("steps"), list)
        or report.get("known_upstream_errata") != known_upstream_errata
    ):
        raise ContractError("test phase report does not match the selected contract")
    actual_steps = [
        (step.get("kind"), step.get("name"))
        for step in report["steps"]
        if isinstance(step, dict) and step.get("exit_code") == 0
    ]
    if actual_steps != expected_steps or len(actual_steps) != len(report["steps"]):
        raise ContractError("test phase report contains an incomplete step")
    return report


def execute_version(executable: Path, expected: str) -> dict[str, Any]:
    result = subprocess.run(
        [str(executable), "--version"],
        cwd=executable.parent,
        check=False,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode or result.stdout.strip() != expected:
        raise ContractError(
            f"absolute-path artifact execution failed ({result.returncode}): "
            f"expected {expected!r}, got {result.stdout.strip()!r}"
        )
    return {
        "argv": [str(executable), "--version"],
        "exit_code": 0,
        "stdout": expected,
    }


def run_contract(
    manifest_path: Path,
    source: Path,
    cargo_target: Path,
    output: Path,
    cross_windows_msvc: bool = False,
    portable_evidence: bool = False,
    phase: str = "all",
    resume: Path | None = None,
    test_runner: str = "cargo",
) -> dict[str, Any]:
    for path, label in (
        (manifest_path, "manifest"),
        (source, "source"),
        (cargo_target, "cargo target"),
        (output, "output"),
    ):
        if not path.is_absolute():
            raise ContractError(f"{label} path must be absolute")
    if phase not in {"all", "tests", "build", "release"}:
        raise ContractError(f"unsupported contract phase: {phase}")
    if test_runner not in TEST_RUNNERS:
        raise ContractError(f"unsupported test runner: {test_runner}")
    if resume is not None and not resume.is_absolute():
        raise ContractError("resume path must be absolute")
    if phase == "build" and resume is None:
        raise ContractError("build phase requires a test phase report")
    if phase != "build" and resume is not None:
        raise ContractError("resume is valid only for the build phase")
    manifest_path = manifest_path.resolve(strict=True)
    source = source.resolve(strict=True)
    if output.exists() or not output.parent.is_dir():
        raise ContractError("output must be a new file under an existing directory")
    if phase in {"all", "tests", "release"} and cargo_target.exists():
        raise ContractError("cargo target must not already exist")
    if phase == "build" and not cargo_target.is_dir():
        raise ContractError("build phase requires the existing test cargo target")
    if phase != "build":
        cargo_target.parent.mkdir(parents=True, exist_ok=True)
    payload = _load_payload(manifest_path)
    manifest = payload.manifest
    if cross_windows_msvc:
        if os.name == "nt":
            raise ContractError("cross-Windows mode requires a non-Windows host")
        if manifest["build_target"] != "x86_64-pc-windows-msvc":
            raise ContractError("cross-Windows mode requires x86_64-pc-windows-msvc")
    contract = load_contract(_payload_file(payload, "test-contract.json"), manifest["compat_id"])
    expected_cwd = expand(contract["cwd"], source, cargo_target)
    cwd = (source / "codex-rs").resolve(strict=True)
    if Path(expected_cwd).resolve(strict=True) != cwd:
        raise ContractError("contract cwd must be the candidate codex-rs directory")
    expected_test_steps = [
        (kind, step.get("name"))
        for kind, contract_steps in (
            ("generation", contract["generation"]),
            ("test", contract["tests"]),
        )
        for step in contract_steps
        if isinstance(step, dict) and isinstance(step.get("name"), str)
    ]
    if len(expected_test_steps) != len(contract["generation"]) + len(contract["tests"]):
        raise ContractError("test contract contains an unnamed step")

    if phase == "build":
        test_report = load_test_report(
            resume.resolve(strict=True),
            manifest["compat_id"],
            contract["known_upstream_errata"],
            expected_test_steps,
        )
        verification = test_report["source_verification"]
        steps = test_report["steps"]
    else:
        verification = verify(manifest_path, source, True, None)
        if portable_evidence:
            verification.update(
                {
                    "manifest": "manifest.toml",
                    "source": "{source}",
                    "patches": [patch["path"] for patch in manifest["patches"]],
                }
            )
        steps = []
        if phase != "release":
            for step in contract["generation"]:
                steps.append(
                    run_step(
                        step,
                        "generation",
                        cwd,
                        contract["common_env"],
                        source,
                        cargo_target,
                        test_runner,
                    )
                )
            for step in contract["tests"]:
                steps.append(
                    run_step(
                        step,
                        "test",
                        cwd,
                        contract["common_env"],
                        source,
                        cargo_target,
                        test_runner,
                    )
                )
            if phase == "tests":
                report = {
                    "schema": 1,
                    "result": "pass",
                    "phase": "tests",
                    "compat_id": manifest["compat_id"],
                    "source_verification": verification,
                    "steps": steps,
                    "known_upstream_errata": contract["known_upstream_errata"],
                }
                write_report(output, report)
                return report
    build = contract["build"]
    if not isinstance(build, dict) or set(build) != {"env", "argv", "artifact"}:
        raise ContractError("invalid build contract")
    build_argv = (
        cross_windows_build_argv(build["argv"]) if cross_windows_msvc else build["argv"]
    )
    build_env = cross_windows_build_env(build["env"]) if cross_windows_msvc else build["env"]
    build_step = {"name": "release build", "argv": build_argv, "env": build_env}
    steps.append(
        run_step(
            build_step,
            "build",
            cwd,
            contract["common_env"],
            source,
            cargo_target,
        )
    )
    artifact_path = Path(expand(build["artifact"], source, cargo_target)).resolve(strict=True)
    artifact = manifest["artifacts"][manifest["build_target"]]
    expected_version = f"codex-cli {manifest['codex_version']}"
    execution = (
        {
            "deferred": True,
            "reason": "cross-compiled Windows artifact requires local Windows acceptance",
        }
        if cross_windows_msvc
        else execute_version(artifact_path, expected_version)
    )
    actual_hash = digest(artifact_path)
    actual_size = artifact_path.stat().st_size
    canonical = actual_hash == artifact["sha256"] and actual_size == artifact["size"]
    report = {
        "schema": 1,
        "result": "pass",
        "compat_id": manifest["compat_id"],
        "source_verification": verification,
        "steps": steps,
        "artifact": {
            "path": build["artifact"] if portable_evidence else str(artifact_path),
            "size": actual_size,
            "sha256": actual_hash,
            "manifest_size": artifact["size"],
            "manifest_sha256": artifact["sha256"],
            "canonical_manifest_match": canonical,
            "release_eligible": canonical and not cross_windows_msvc,
            "absolute_path_execution": execution,
        },
        "known_upstream_errata": contract["known_upstream_errata"],
    }
    write_report(output, report)
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cargo-target", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cross-windows-msvc", action="store_true")
    parser.add_argument("--portable-evidence", action="store_true")
    parser.add_argument(
        "--phase", choices=("all", "tests", "build", "release"), default="all"
    )
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--test-runner", choices=TEST_RUNNERS, default="cargo")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        report = run_contract(
            args.manifest,
            args.source,
            args.cargo_target,
            args.output,
            args.cross_windows_msvc,
            args.portable_evidence,
            args.phase,
            args.resume,
            args.test_runner,
        )
    except (ContractError, OSError, VerificationError) as error:
        print(json.dumps({"schema": 1, "error": str(error)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
