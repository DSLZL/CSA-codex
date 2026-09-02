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
from run_patch_contract import ContractError, cargo_frontend_argv, load_contract  # noqa: E402


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


def test_cargo_frontend_routing() -> None:
    assert cargo_frontend_argv(["cargo", "test", "-p", "codex-core"], "mbx") == [
        "mbx",
        "test",
        "-p",
        "codex-core",
    ]
    assert cargo_frontend_argv(["cargo", "clippy", "--workspace"], "mbx") == [
        "mbx",
        "clippy",
        "--workspace",
    ]
    assert cargo_frontend_argv(["cargo", "build", "--release"], "mbx") == [
        "mbx",
        "build",
        "--release",
    ]
    assert cargo_frontend_argv(["cargo", "build", "--release"], "cargo") == [
        "cargo",
        "build",
        "--release",
    ]
    assert cargo_frontend_argv(["cargo", "fmt", "--check"], "mbx") == [
        "cargo",
        "fmt",
        "--check",
    ]
    assert cargo_frontend_argv(["cargo", "xwin", "build"], "mbx") == [
        "cargo",
        "xwin",
        "build",
    ]
    expect_error(lambda: cargo_frontend_argv(["rustc", "--version"], "mbx"), ContractError)


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
    watcher = workflows["watch-codex-release.yml"]
    ci = workflows["ci.yml"]
    cargo_cache = (REPOSITORY / ".github/actions/setup-codex-rust-cache/action.yml").read_text(
        encoding="utf-8"
    )
    bundle_builder = (REPOSITORY / "scripts/build_patched_codex_bundle.sh").read_text(
        encoding="utf-8"
    )

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
    mbx_action = (
        "jdx/mr-boxington-action@4fd4eab077dde1d635a289366f62c80cf6f11e6f"
    )
    for owner in (validation, target):
        assert mbx_action in owner
        assert 'version: "1.4.1"' in owner
        assert "mbx doctor" in owner and "mbx cache stats --json" in owner
        assert "cache-generation: csa-patched-codex-v2-" in owner
        assert "MBX_SUMMARY: full" in owner
        assert "MBX_BYPASS_LOG" in owner and "unknown-codegen-option" in owner
        assert owner.index("dtolnay/rust-toolchain@") < owner.index(mbx_action)
        assert "uses: ./.github/actions/setup-codex-rust-cache" in owner
        assert "minimum-rust-hit-rate" not in owner and "require-requests" not in owner
        assert "max-size:" not in owner
        assert "RUSTFLAGS" not in owner and "CARGO_PROFILE_" not in owner
        assert "steps.mbx.outputs.cache-primary-key" in owner
        assert "steps.mbx.outputs.mbx-version" in owner
    for former_owner in (validation, target, cargo_cache, bundle_builder):
        assert "sccache" not in former_owner.lower()
        assert "RUSTC_WRAPPER" not in former_owner and "SCCACHE_" not in former_owner
    assert not (REPOSITORY / "scripts/check_sccache_stats.py").exists()
    assert 'save-on-workflow-dispatch: "true"' in validation
    assert "save-on-workflow-dispatch: ${{ inputs.compiler_cache_save }}" in target
    assert "Verify compiler-cache save authority" in target
    assert "test \"$GITHUB_EVENT_NAME\" = workflow_dispatch" in target
    assert target.count("inputs.target != 'x86_64-apple-darwin'") == 4
    assert "Record Intel macOS Cargo fallback" in target
    assert "MBX 1.4.1 does not publish an x86_64-apple-darwin binary" in target
    assert "COMPILER_CACHE_ENABLED: ${{ inputs.compiler_cache }}" in validation
    cache_policy = "compiler_cache: ${{ needs.plan.outputs.publish_requested != 'true' }}"
    assert release.count(cache_policy) == 2
    save_policy = "compiler_cache_save: ${{ needs.plan.outputs.publish_requested != 'true' }}"
    assert save_policy in release
    assert "compiler_cache: true" in windows and "compiler_cache_save: false" in windows
    assert "--cargo-frontend $env:CARGO_FRONTEND" in validation
    assert '"$CARGO_FRONTEND" build' in target
    assert target.count("--timings") == 2
    assert "registry/index" in cargo_cache and "registry/cache" in cargo_cache
    assert "git/db" in cargo_cache and "cargo-target" not in cargo_cache
    all_workflows = "\n".join(workflows.values())
    assert "spctl developer-mode" not in all_workflows
    assert "DevToolsSecurity" not in all_workflows
    for command in (
        "test_verify_patch_payload.py",
        "test_compat_catalog.py",
        "test_validation_evidence.py",
        "test_verify_release_asset_set.py",
        "test_producer_tools.py",
        "compat_catalog.py validate",
    ):
        assert command in ci


def main() -> int:
    test_repository_boundary()
    test_cargo_frontend_routing()
    with tempfile.TemporaryDirectory(prefix="csa-codex-producer-") as directory:
        root = Path(directory)
        test_payload_and_contract_authority(root / "payload")
        test_release_matrix_and_pack(root / "pack")
        test_release_notes(root / "notes")
    test_workflow_contracts()
    print(json.dumps({"schema": 1, "result": "pass"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
