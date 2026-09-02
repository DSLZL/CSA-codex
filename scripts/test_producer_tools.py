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
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
REPOSITORY = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))

from compat_release import finalize, pack, release_matrix  # noqa: E402
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
    "rust-v0.151.0-native-join-p10/manifest.toml"
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
        "build-patched-codex-windows.yml",
        "ci.yml",
        "maintain-actions-cache.yml",
        "release-patched-codex.yml",
        "validate-patched-codex.yml",
        "watch-codex-release.yml",
    }

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
            assert re.fullmatch(r"[^@\s]+@[0-9a-f]{40}", value), (path, value)


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
    assert family["status"] == "pass" and family["bindings"] == 1
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
        "-p",
        "protocol",
        "generate",
        "--",
        "--ignored",
        "--nocapture",
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
    assert {row["target"] for row in matrix["include"]} == {
        "aarch64-apple-darwin",
        "aarch64-pc-windows-msvc",
        "aarch64-unknown-linux-musl",
        "x86_64-apple-darwin",
        "x86_64-pc-windows-msvc",
        "x86_64-unknown-linux-musl",
    }

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
    validation = workflows["validate-patched-codex.yml"]
    windows = workflows["build-patched-codex-windows.yml"]
    maintenance = workflows["maintain-actions-cache.yml"]
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
    evidence = (REPOSITORY / "scripts/validation_evidence.py").read_text(encoding="utf-8")

    assert 'cron: "0 * * * *"' in watcher
    assert "compat_release.py finalize" not in watcher
    assert "compat_release.py pack" not in watcher
    assert "uses: ./.github/workflows/validate-patched-codex.yml" in release
    assert "uses: ./.github/workflows/build-patched-codex-target.yml" in release
    assert "uses: ./.github/workflows/build-patched-codex-target.yml" in workflows[
        "build-patched-codex-windows.yml"
    ]
    assert "workflow_call:" in target and "workflow_call:" in validation
    assert "--stream manager" not in release and "--stream compat" not in release
    assert "scripts/generate_release_notes.py" in release
    assert "release-csa.yml" not in workflows and "publish-npm.yml" not in workflows

    sccache_action = "mozilla-actions/sccache-action@fc920bf0ec8de6ee65d409111f7ec508035751ba"
    assert sccache_action in cache_setup
    assert "version: v${{ inputs.sccache_version }}" in cache_setup
    assert "actions/cache@" not in cache_setup and "github.sha" not in cache_setup
    for guard_input in ("CSA_EVENT_NAME", "CSA_DEFAULT_BRANCH", "CSA_REF"):
        assert guard_input in cache_setup
    assert "Read-write compiler cache access requires a default-branch workflow dispatch." in (
        cache_setup
    )
    for setting in (
        "RUSTC_WRAPPER",
        "SCCACHE_BASEDIRS",
        "SCCACHE_CACHE_ZSTD_LEVEL",
        "SCCACHE_GHA_ENABLED",
        "SCCACHE_GHA_RW_MODE",
        "SCCACHE_GHA_VERSION = 'csa-codex-v1'",
    ):
        assert setting in cache_setup

    for owner in (validation, target):
        assert "uses: ./.github/actions/setup-codex-rust-cache" in owner
        assert owner.index("dtolnay/rust-toolchain@") < owner.index(
            "uses: ./.github/actions/setup-codex-rust-cache"
        )
        assert "scripts/check_sccache_stats.py" in owner
        assert "--require-requests" in owner and "--require-clean" in owner
        assert "cache_mode" in owner and "sccache_version" in owner
        assert "mr-boxington" not in owner.lower() and "MBX_" not in owner
        assert "RUSTFLAGS" not in owner and "CARGO_PROFILE_" not in owner

    nextest_install = "Install and verify exact cargo-nextest runner"
    assert nextest_install in validation
    assert 'CARGO_NEXTEST_VERSION: "0.9.143"' in validation
    assert "c670ba18e8731fd2eff33a47af33a0fa53d1afa6d0678344e82dc6f8fc7344ac" in validation
    assert "nextest-rs/nextest/releases/download/cargo-nextest-" in validation
    assert "Get-FileHash" in validation and "Expand-Archive" in validation
    assert "taiki-e/install-action@" not in validation
    assert "cargo nextest --version" in validation
    assert "--test-runner nextest" in validation
    assert validation.index("dtolnay/rust-toolchain@") < validation.index(nextest_install)
    assert validation.index(nextest_install) < validation.index("--test-runner nextest")
    assert "nextest" not in target.lower()
    assert validation.count("timeout-minutes: 300") == 1
    assert target.count("timeout-minutes: 300") == 1
    assert "timeout-minutes: 120" not in validation

    assert (REPOSITORY / "scripts/check_sccache_stats.py").is_file()
    assert "read-only|off" in target and "read-write)" in target
    assert "test \"$GITHUB_EVENT_NAME\" = workflow_dispatch" in target
    assert "refs/heads/$DEFAULT_BRANCH" in target
    cache_policy = (
        "cache_mode: ${{ needs.plan.outputs.publish_requested != 'true' && "
        "'read-write' || 'read-only' }}"
    )
    assert release.count(cache_policy) == 2
    assert "sccache_version: ${{ steps.resolve.outputs.sccache_version }}" in release
    assert "sccache_version: ${{ needs.plan.outputs.sccache_version }}" in release
    assert "cache_mode: read-write" in windows
    assert "Windows cache-warming builds must be dispatched" in windows
    assert "--cargo-frontend" not in validation and "CARGO_FRONTEND" not in target
    assert target.count("--timings") == 2

    assert "schedule:" not in maintenance and "workflow_call:" not in maintenance
    assert maintenance.count("actions: write") == 1
    assert "high-water" not in maintenance and "low-water" not in maintenance
    assert 'actions/caches/$cache_id' in maintenance
    assert "--purge-legacy" in maintenance
    assert maintenance.count("if: env.DRY_RUN != 'true'") == 2
    assert "Cache audit is a dry run; no cache IDs will be deleted." in maintenance
    assert "uses: ./.github/workflows/maintain-actions-cache.yml" not in validation
    assert "uses: ./.github/workflows/maintain-actions-cache.yml" not in windows
    assert "uses: ./.github/workflows/maintain-actions-cache.yml" not in release
    for name, workflow in workflows.items():
        assert "actions/cache@" not in workflow, name
        if name != "maintain-actions-cache.yml":
            assert "actions/caches/$cache_id" not in workflow, name
            assert "gh cache delete" not in workflow, name

    assert "sccache" in bundle_builder.lower() and "mbx" not in bundle_builder.lower()
    assert "cargo_frontend" not in contract_runner and "mbx" not in contract_runner.lower()
    assert "test_runner_argv" in contract_runner and "runner_argv" in contract_runner
    assert "runner_argv" in evidence and "mbx" not in evidence.lower()
    all_workflows = "\n".join(workflows.values())
    assert "spctl developer-mode" not in all_workflows
    assert "DevToolsSecurity" not in all_workflows
    for command in (
        "test_verify_patch_payload.py",
        "test_compat_catalog.py",
        "test_validation_evidence.py",
        "test_verify_release_asset_set.py",
        "test_actions_cache_policy.py",
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
