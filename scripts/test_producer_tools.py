#!/usr/bin/env python3
"""Runnable standard-library checks for standalone producer tooling."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
REPOSITORY = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from compat_release import (  # noqa: E402
    TARGET_BUILDERS,
    file_digest,
    finalize,
    pack,
    release_matrix,
    release_target,
    verify_builder_binding,
    verify_target_bundle,
)
from compatibility_audit import AuditError, check_immutability  # noqa: E402
from generate_release_notes import ReleaseNotesError, generate  # noqa: E402
from patch_family import verify_family  # noqa: E402
from run_patch_contract import (  # noqa: E402
    ContractError,
    load_contract,
    test_runner_argv,
)


PRODUCER_REPOSITORY = "DSLZL/CSA-codex"
TARGET = "x86_64-pc-windows-msvc"
P10_MANIFEST = (
    REPOSITORY
    / "payload/codex/native-join-p10/bindings/"
    "rust-v0.153.0-native-join-p10/manifest.toml"
)


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout.strip()


def expect_error(call, error_type: type[Exception]) -> None:
    try:
        call()
    except error_type:
        return
    raise AssertionError(f"{error_type.__name__} was not raised")


def test_repository_boundary() -> None:
    for forbidden in ("src", "npm", "Cargo.toml", "Cargo.lock", "build.rs"):
        assert not (REPOSITORY / forbidden).exists(), forbidden

    workflows = {
        path.name
        for pattern in ("*.yml", "*.yaml")
        for path in (REPOSITORY / ".github/workflows").glob(pattern)
    }
    assert workflows == {
        "build-patched-codex-target.yml",
        "ci.yml",
        "release-patched-codex.yml",
        "watch-codex-release.yml",
    }
    assert not (REPOSITORY / "scripts/actions_cache_policy.py").exists()
    assert not (REPOSITORY / "scripts/test_actions_cache_policy.py").exists()

    old_repository = re.compile(r"(?i)DSLZL/CSA(?!-codex)")
    for root in (REPOSITORY / "scripts", REPOSITORY / ".github/workflows"):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".sh", ".yml", ".yaml"}:
                continue
            if path.name.startswith("test_"):
                continue
            text = path.read_text(encoding="utf-8")
            assert "../CSA" not in text and "..\\CSA" not in text
            if path.name == "compat_catalog.py":
                legacy = 'LEGACY_REPOSITORY = "DSLZL/CSA"'
                assert text.count(legacy) == 1
                text = text.replace(legacy, "")
            assert old_repository.search(text) is None, path

    action = re.compile(r"^\s*-?\s*uses:\s+([^\s#]+)", re.MULTILINE)
    for path in (REPOSITORY / ".github").rglob("*.yml"):
        for value in action.findall(path.read_text(encoding="utf-8")):
            if value.startswith("./"):
                continue
            assert re.fullmatch(r"[^@\s]+@v?[0-9]+\.[0-9]+\.[0-9]+", value), (path, value)


def test_payload_and_contract_authority(root: Path) -> None:
    root.mkdir()
    legacy = REPOSITORY / "payload/codex/rust-v0.147.0-native-join-p1"
    baseline = root / "empty"
    candidate = root / "candidate"
    baseline.mkdir()
    candidate.mkdir()
    shutil.copytree(legacy, candidate / legacy.name)
    report = check_immutability(baseline.resolve(), candidate.resolve())
    assert report["result"] == "pass"
    immutable_baseline = root / "immutable-baseline"
    shutil.copytree(candidate, immutable_baseline)
    (candidate / legacy.name / "manifest.toml").write_bytes(
        (candidate / legacy.name / "manifest.toml").read_bytes() + b"\n"
    )
    expect_error(
        lambda: check_immutability(immutable_baseline.resolve(), candidate.resolve()),
        AuditError,
    )

    family = verify_family(REPOSITORY / "payload/codex/native-join-p10")
    assert family["status"] == "pass" and family["bindings"] == 2
    contract = load_contract(P10_MANIFEST.with_name("test-contract.json"), P10_MANIFEST.parent.name)
    assert contract["schema"] == 1
    assert contract["build"]["artifact"].endswith("/release/codex.exe")


def test_nextest_runner_mapping() -> None:
    logical = [
        "cargo",
        "test",
        "-p",
        "codex-tui",
        "--lib",
        "subagent_live",
        "--",
        "--test-threads=1",
        "--format=terse",
    ]
    assert test_runner_argv(logical, "nextest") == [
        "cargo",
        "nextest",
        "run",
        "--test-threads=1",
        "-p",
        "codex-tui",
        "--lib",
        "subagent_live",
    ]
    assert test_runner_argv(
        ["cargo", "test", "-p", "protocol", "generate", "--", "--ignored", "--nocapture"],
        "nextest",
    ) == [
        "cargo",
        "nextest",
        "run",
        "--run-ignored",
        "ignored-only",
        "--no-capture",
        "-p",
        "protocol",
        "generate",
    ]
    assert test_runner_argv(
        [
            "cargo",
            "test",
            "-p",
            "codex-tui",
            "--lib",
            "--",
            "--skip",
            "first",
            "--skip",
            "second",
            "--format=terse",
        ],
        "nextest",
    ) == [
        "cargo",
        "nextest",
        "run",
        "-p",
        "codex-tui",
        "--lib",
        "--",
        "--skip",
        "first",
        "--skip",
        "second",
    ]
    doctest = ["cargo", "test", "-p", "codex-core", "--doc"]
    assert test_runner_argv(doctest, "nextest") == doctest
    formatting = ["cargo", "fmt", "--all", "--", "--check"]
    assert test_runner_argv(formatting, "nextest") == formatting
    expect_error(
        lambda: test_runner_argv(["cargo", "test", "--", "--bench"], "nextest"),
        ContractError,
    )

    cargo_test_steps = 0
    for path in sorted((REPOSITORY / "payload/codex").rglob("test-contract.json")):
        contract = json.loads(path.read_text(encoding="utf-8"))
        for section in ("generation", "tests"):
            for step in contract[section]:
                argv = step["argv"]
                mapped = test_runner_argv(argv, "nextest")
                if argv[:2] == ["cargo", "test"] and "--doc" not in argv:
                    cargo_test_steps += 1
                    assert mapped[:3] == ["cargo", "nextest", "run"], (path, argv)
                else:
                    assert mapped == argv, (path, argv)
    assert cargo_test_steps > 0


def test_release_matrix_and_pack(root: Path) -> None:
    matrix = release_matrix(P10_MANIFEST)
    assert {
        row["target"]: (row["repository"], row["runner"], row["workflow"])
        for row in matrix["include"]
    } == {
        target: (builder["repository"], builder["runner"], "build.yml")
        for target, builder in TARGET_BUILDERS.items()
    }
    for row in matrix["include"]:
        assert row["artifact_filename"] in {"codex", "codex.exe"}
        assert release_target(P10_MANIFEST, row["target"]) == row

    target = TARGET
    builder = TARGET_BUILDERS[target]
    assert verify_builder_binding(builder["repository"], target, builder["runner"])[
        "repository"
    ] == builder["repository"]
    expect_error(
        lambda: verify_builder_binding(PRODUCER_REPOSITORY, target, builder["runner"]),
        RuntimeError,
    )
    expect_error(
        lambda: verify_builder_binding("DSLZL/CSA-codex-linux-x64", target, builder["runner"]),
        RuntimeError,
    )

    bundle = root / "target-bundle"
    artifact = bundle / "bin/codex.exe"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"verified remote builder artifact")
    p10_manifest = tomllib.loads(P10_MANIFEST.read_text(encoding="utf-8"))
    record = {
        "schema": 2,
        "request_id": "acceptance-1",
        "builder_repository": builder["repository"],
        "runner": builder["runner"],
        "workflow_run_id": "123456",
        "compat_id": P10_MANIFEST.parent.name,
        "manifest_sha256": file_digest(P10_MANIFEST),
        "target": target,
        "artifact": "bin/codex.exe",
        "size": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "upstream_commit": p10_manifest["upstream_commit"],
        "source_commit": "a" * 40,
    }
    record_path = bundle / "target-record.json"
    record_path.write_text(json.dumps(record), encoding="utf-8")
    verified = verify_target_bundle(
        P10_MANIFEST,
        bundle.resolve(),
        request_id="acceptance-1",
        source_commit="a" * 40,
        repository=builder["repository"],
        runner=builder["runner"],
        target=target,
        workflow_run_id="123456",
    )
    assert verified["result"] == "verified" and verified["sha256"] == record["sha256"]

    def verify_fixture(
        *,
        repository: str = builder["repository"],
        runner: str = builder["runner"],
        expected_target: str = target,
    ) -> dict[str, object]:
        return verify_target_bundle(
            P10_MANIFEST,
            bundle.resolve(),
            request_id="acceptance-1",
            source_commit="a" * 40,
            repository=repository,
            runner=runner,
            target=expected_target,
            workflow_run_id="123456",
        )

    base_record = dict(record)
    for field, bad_value in (
        ("request_id", "stale-request"),
        ("builder_repository", "DSLZL/CSA-codex-linux-x64"),
        ("runner", "ubuntu-24.04"),
        ("workflow_run_id", "654321"),
        ("compat_id", "rust-v0.150.0-native-join-p10"),
        ("manifest_sha256", "0" * 64),
        ("target", "x86_64-unknown-linux-musl"),
        ("artifact", "bin/not-codex.exe"),
        ("upstream_commit", "b" * 40),
        ("source_commit", "b" * 40),
        ("size", base_record["size"] + 1),
        ("sha256", "0" * 64),
    ):
        record_path.write_text(
            json.dumps({**base_record, field: bad_value}), encoding="utf-8"
        )
        expect_error(verify_fixture, RuntimeError)

    record_path.write_text(json.dumps(base_record), encoding="utf-8")
    expect_error(
        lambda: verify_fixture(repository="DSLZL/CSA-codex-linux-x64"),
        RuntimeError,
    )
    expect_error(
        lambda: verify_fixture(
            repository="DSLZL/CSA-codex-linux-x64",
            runner="ubuntu-24.04",
            expected_target="x86_64-unknown-linux-musl",
        ),
        RuntimeError,
    )

    duplicate = bundle / "duplicate/target-record.json"
    duplicate.parent.mkdir()
    duplicate.write_text(json.dumps(base_record), encoding="utf-8")
    expect_error(verify_fixture, RuntimeError)
    duplicate.unlink()
    duplicate.parent.rmdir()

    record_path.unlink()
    expect_error(verify_fixture, RuntimeError)
    record_path.write_text(json.dumps(base_record), encoding="utf-8")

    artifact.write_bytes(b"tampered remote builder artifact")
    expect_error(verify_fixture, RuntimeError)
    artifact.write_bytes(b"verified remote builder artifact")
    unexpected = bundle / "unexpected.txt"
    unexpected.write_text("unexpected\n", encoding="utf-8")
    expect_error(verify_fixture, RuntimeError)
    unexpected.unlink()
    assert verify_fixture()["result"] == "verified"

    source = REPOSITORY / "payload/codex/rust-v0.148.0-native-join-p1"
    payload = root / source.name
    shutil.copytree(source, payload)
    artifact = root / "codex.exe"
    artifact.write_bytes(b"standalone producer artifact")
    finalized = finalize((payload / "manifest.toml").resolve(), artifact.resolve())
    assert finalized["artifact"]["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    output = root / "assets"
    packed = pack(
        (payload / "manifest.toml").resolve(),
        artifact.resolve(),
        "a" * 40,
        output.resolve(),
    )
    descriptor = json.loads((output / "compatibility-release.json").read_text(encoding="utf-8"))
    assert packed["release_tag"] == f"compat-{source.name}"
    assert descriptor["repository"] == PRODUCER_REPOSITORY
    assert descriptor["source_commit"] == "a" * 40
    assert (
        f"https://github.com/{PRODUCER_REPOSITORY}/releases/download/"
        in (payload / "manifest.toml").read_text(encoding="utf-8")
    )


def notes_commit(root: Path, relative: str, text: str, subject: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(root, "add", "--", relative)
    git(root, "commit", "-qm", subject)


def notes_index(compat_id: str) -> dict[str, object]:
    return {
        "schema": 1,
        "compatibilities": {
            compat_id: {
                "manifest": f"payload/codex/{compat_id}/manifest.toml",
                "targets": {
                    TARGET: {
                        "runtime_lock": f"release/runtime-locks/{compat_id}.json",
                        "acceptance": f"release/acceptance/{compat_id}/{TARGET}.json",
                    }
                },
            }
        },
    }


def test_release_notes(root: Path) -> None:
    compat_id = "rust-v1.2.3-native-join-p3"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "CSA-codex Test")
    git(root, "config", "user.email", "csa-codex@example.invalid")
    (root / "release").mkdir()
    (root / "release/compatibility-index.json").write_text(
        json.dumps(notes_index(compat_id), indent=2) + "\n",
        encoding="utf-8",
    )
    git(root, "add", "release/compatibility-index.json")
    notes_commit(root, "scripts/fixture.py", "baseline\n", "chore: fixture baseline")
    git(root, "tag", "compat-rust-v1.2.3-native-join-p2")
    notes_commit(
        root,
        f"payload/codex/{compat_id}/patches/orbit.patch",
        "orbit\n",
        "feat(patch): add square orbit",
    )
    notes_commit(
        root,
        "release/build-profiles/windows-msvc-x64.json",
        "{}\n",
        "build(patch): pin LLVM",
    )
    notes_commit(root, "README.md", "unrelated\n", "feat(docs): manager-independent readme")
    git(root, "tag", f"compat-{compat_id}")

    output = root / "notes.md"
    result = generate(
        root,
        "HEAD",
        output,
        compat_id=compat_id,
        codex_version="1.2.3",
        upstream_tag="rust-v1.2.3",
        upstream_commit="b" * 40,
        target=TARGET,
        artifact_sha256="c" * 64,
    )
    notes = output.read_text(encoding="utf-8")
    comparison = (
        "compat-rust-v1.2.3-native-join-p2..."
        "compat-rust-v1.2.3-native-join-p3"
    )
    assert result["previous_tag"] == "compat-rust-v1.2.3-native-join-p2"
    assert f"[{comparison}](https://github.com/DSLZL/CSA-codex/compare/{comparison})" in notes
    assert "## Patch Changes" in notes and "## Build & Release" in notes
    assert "Add square orbit." in notes and "Pin LLVM." in notes
    assert "manager-independent" not in notes
    assert notes.index("## Full Changelog") < notes.index("## Compatibility")
    expect_error(
        lambda: generate(
            root,
            "HEAD",
            root / "bad.md",
            compat_id=compat_id,
            codex_version="1.2.3",
            upstream_tag="rust-v1.2.3",
            upstream_commit="b" * 40,
            target=TARGET,
            artifact_sha256="bad",
        ),
        ReleaseNotesError,
    )


def test_workflow_contracts() -> None:
    workflows = {
        path.name: path.read_text(encoding="utf-8")
        for path in (REPOSITORY / ".github/workflows").glob("*.yml")
    }
    release = workflows["release-patched-codex.yml"]
    target = workflows["build-patched-codex-target.yml"]
    watcher = workflows["watch-codex-release.yml"]
    ci = workflows["ci.yml"]
    cache_setup = (REPOSITORY / ".github/actions/setup-codex-rust-cache/action.yml").read_text(
        encoding="utf-8"
    )
    bundle_builder = (REPOSITORY / "scripts/build_patched_codex_bundle.sh").read_text(
        encoding="utf-8"
    )
    contract_runner = (REPOSITORY / "scripts/run_patch_contract.py").read_text(
        encoding="utf-8"
    )

    assert 'cron: "0 * * * *"' in watcher
    assert "compat_release.py finalize" not in watcher
    assert "compat_release.py pack" not in watcher
    assert "Validate exact patched Codex contract" not in release
    assert "validate-patched-codex.yml" not in release
    assert "uses: ./.github/workflows/build-patched-codex-target.yml" not in release
    assert "workflow_call:" in target
    assert "repository: DSLZL/CSA-codex" in target
    assert "scripts/compat_release.py builder" in target
    assert "producer_args" not in target and "--allow-producer" not in target
    assert "scripts/compat_catalog.py resolve" in target
    assert '"schema": 2' in target
    for input_name in (
        "builder_repository",
        "compat_id",
        "request_id",
        "runner",
        "source_commit",
        "target",
    ):
        assert f"      {input_name}:" in target
    assert "--stream manager" not in release and "--stream compat" not in release
    assert "scripts/generate_release_notes.py" in release
    assert "release-csa.yml" not in workflows and "publish-npm.yml" not in workflows

    sccache_action = "mozilla-actions/sccache-action@v0.0.11"
    cache_restore_action = "actions/cache/restore@v6.1.0"
    cache_save_action = "actions/cache/save@v6.1.0"
    all_action_sources = "\n".join([*workflows.values(), cache_setup])
    for source in (
        "actions/checkout@v7.0.1",
        "actions/setup-python@v7.0.0",
        "actions/upload-artifact@v7.0.1",
        "actions/download-artifact@v8.0.1",
        "dtolnay/rust-toolchain@1.95.0",
        "mlugg/setup-zig@v2.2.1",
        sccache_action,
        cache_restore_action,
        cache_save_action,
    ):
        assert source in all_action_sources
    assert sccache_action in cache_setup
    assert cache_restore_action in cache_setup
    assert cache_save_action in target
    assert "version: v${{ inputs.sccache_version }}" in cache_setup
    assert "continue-on-error: true" in cache_setup
    assert "github.sha" not in cache_setup
    for guard_input in ("CSA_EVENT_NAME", "CSA_DEFAULT_BRANCH", "CSA_REF"):
        assert guard_input in cache_setup
    assert "Read-write compiler cache access requires a default-branch workflow dispatch." in (
        cache_setup
    )
    for setting in (
        "RUSTC_WRAPPER",
        "SCCACHE_BASEDIRS",
        "SCCACHE_CACHE_ZSTD_LEVEL",
        "SCCACHE_DIR",
        "SCCACHE_GHA_ENABLED",
    ):
        assert setting in cache_setup
    assert "SCCACHE_GHA_ENABLED = 'false'" in cache_setup
    assert "SCCACHE_GHA_RW_MODE" not in cache_setup
    assert "SCCACHE_GHA_VERSION" not in cache_setup
    assert "SCCACHE_CACHE_SIZE" not in cache_setup
    assert "csa-sccache-local-v2-${{ inputs.target }}-${{ inputs.sccache_version }}-" in cache_setup
    assert "${{ github.run_id }}" in cache_setup

    assert "uses: ./.github/actions/setup-codex-rust-cache" in target
    assert target.index("dtolnay/rust-toolchain@") < target.index(
        "uses: ./.github/actions/setup-codex-rust-cache"
    )
    assert "scripts/check_sccache_stats.py" in target
    assert "--require-requests" in target and "--require-clean" not in target
    assert "cache_mode" in target and "sccache_version" in target
    assert "mr-boxington" not in target.lower() and "MBX_" not in target
    assert "RUSTFLAGS" not in target and "CARGO_PROFILE_" not in target
    assert "nextest" not in target.lower()
    assert target.count("timeout-minutes: 300") == 1
    assert "$RUNNER_TEMP/c" in target and "${{ runner.temp }}/c/h" in target

    assert (REPOSITORY / "scripts/check_sccache_stats.py").is_file()
    assert "read-only|off" in target and "read-write)" in target
    assert "test \"$GITHUB_EVENT_NAME\" = workflow_dispatch" in target
    assert "refs/heads/$DEFAULT_BRANCH" in target
    assert "sccache_version: ${{ steps.authority.outputs.sccache_version }}" in target
    assert "sccache_dir: ${{ runner.temp }}/c/k" in target
    assert "target: ${{ inputs.target }}" in target
    assert "steps.compiler_cache.outputs.cache_primary_key" in target
    assert "inputs.cache_mode == 'read-write'" in target
    assert target.count("continue-on-error: true") >= 2
    assert "CARGO_FRONTEND" not in target and "--cargo-frontend" not in target
    assert target.count("--timings") == 2

    assert "matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}" in release
    assert "matrix.repository" in release and "matrix.workflow" in release
    assert 'X-GitHub-Api-Version: 2026-03-10' in release
    assert 'response.get("workflow_run_id")' in release
    assert "gh run watch" in release and "gh run download" in release
    assert "scripts/compat_release.py verify-target" in release
    assert "needs: [plan, build]" in release
    assert "needs.validate" not in release
    assert release.count("${{ secrets.BUILD_FANOUT_TOKEN }}") == 3
    assert "timeout-minutes: 360" in release
    assert "request_id=${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in release

    for name, workflow in workflows.items():
        if name == "build-patched-codex-target.yml":
            assert cache_save_action in workflow
        else:
            assert "actions/cache/" not in workflow, name
        assert "actions/caches/$cache_id" not in workflow, name
        assert "gh cache delete" not in workflow, name
        if name != "build-patched-codex-target.yml":
            assert "cargo build --target" not in workflow, name

    assert "sccache" in bundle_builder.lower() and "mbx" not in bundle_builder.lower()
    assert "cargo_frontend" not in contract_runner and "mbx" not in contract_runner.lower()
    assert "test_runner_argv" in contract_runner and "runner_argv" in contract_runner
    all_workflows = "\n".join(workflows.values())
    assert "spctl developer-mode" not in all_workflows
    assert "DevToolsSecurity" not in all_workflows
    for command in (
        "test_verify_patch_payload.py",
        "test_compat_catalog.py",
        "test_verify_release_asset_set.py",
        "test_check_sccache_stats.py",
        "test_producer_tools.py",
        "compat_catalog.py validate",
    ):
        assert command in ci


def main() -> int:
    test_repository_boundary()
    with tempfile.TemporaryDirectory(prefix="csa-codex-producer-") as directory:
        root = Path(directory)
        test_payload_and_contract_authority(root / "payload")
        test_nextest_runner_mapping()
        test_release_matrix_and_pack(root / "pack")
        test_release_notes(root / "notes")
    test_workflow_contracts()
    print(json.dumps({"schema": 1, "result": "pass"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
