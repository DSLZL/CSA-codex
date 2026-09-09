#!/usr/bin/env python3
"""Runnable standard-library checks for standalone producer tooling."""

from __future__ import annotations

import hashlib
import json
import os
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
from patch_family import PatchFamilyError, verify_family  # noqa: E402
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
    assert family["status"] == "pass" and family["bindings"] == 3
    p14 = root / "native-join-p14"
    shutil.copytree(REPOSITORY / "payload/codex/native-join-p14", p14)
    assert verify_family(p14)["status"] == "pass"
    manifest_path = p14 / "bindings/rust-v0.153.2-native-join-p14/manifest.toml"
    before = manifest_path.read_bytes()
    text = before.decode("utf-8")
    adapter = text[text.rindex("[[patches]]"):text.index("[preimage]")]
    reordered = text.replace(adapter, "").replace("[[patches]]", adapter + "[[patches]]", 1)
    # Keep lexical ordering valid so the ordered-ownership check rejects this fixture.
    reordered = reordered.replace(
        '"patches/1600-cold-unplug-persistence-adapter.patch"',
        '"patches/0000-cold-unplug-persistence-adapter.patch"',
    ).encode("utf-8")
    manifest_path.write_bytes(reordered)
    family_path = p14 / "family.toml"
    family_path.write_bytes(family_path.read_bytes().replace(
        hashlib.sha256(before).hexdigest().encode(),
        hashlib.sha256(reordered).hexdigest().encode(),
    ))
    expect_error(lambda: verify_family(p14), PatchFamilyError)
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
        "--no-fail-fast",
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
        "--no-fail-fast",
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
        "--no-fail-fast",
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
    cache_prefix = "csa-sccache-local-v3-${{ inputs.target }}-${{ inputs.sccache_version }}-${{ steps.config.outputs.environment_fingerprint }}-"
    assert f"key: {cache_prefix}${{{{ steps.config.outputs.fingerprint }}}}" in cache_setup
    assert f"restore-keys: |\n          {cache_prefix}\n" in cache_setup
    assert "csa-sccache-local-v2-" not in cache_setup
    assert "github.run_id" not in cache_setup and "github.run_attempt" not in cache_setup
    assert "${{ steps.config.outputs.fingerprint }}" in cache_setup
    # actions/cache hashes path spelling into its version, before resolving filesystem paths.
    assert "path: ${{ env.SCCACHE_DIR }}" in cache_setup
    assert "path: ${{ env.SCCACHE_DIR }}" in target
    assert "path: ${{ inputs.sccache_dir }}" not in cache_setup

    assert "uses: ./.github/actions/setup-codex-rust-cache" in target
    assert target.index("dtolnay/rust-toolchain@") < target.index(
        "uses: ./.github/actions/setup-codex-rust-cache"
    )
    toolchain_step = target.split("      - name: Install exact compatibility Rust toolchain\n", 1)[1].split("\n      - name:", 1)[0]
    assert "          toolchain:" not in toolchain_step
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
    save_step = target.split("      - name: Save local compiler cache\n", 1)[1]
    assert "if: ${{ success() &&" in save_step and "!cancelled()" not in save_step
    assert "use-tool-cache: true" in target
    assert target.index("Install upstream musl build tools") < target.index("- id: compiler_cache")
    assert "Upload build diagnostics" in target and "/cargo-timings/" in target
    assert '/usr/bin/time -l "${build[@]}"' in target
    assert "inputs.cache_mode == 'read-write'" in target
    assert target.count("continue-on-error: true") >= 2
    assert "CARGO_FRONTEND" not in target and "--cargo-frontend" not in target
    assert target.count("--timings") == 2
    assert TARGET_BUILDERS["aarch64-apple-darwin"]["runner"] == "macos-26"
    assert TARGET_BUILDERS["x86_64-apple-darwin"]["runner"] == "macos-26-intel"
    mac_fetch = target.index("      - name: Prefetch Cargo dependencies")
    mac_shutdown = target.index("      - name: Disable macOS security scanning")
    cli_build = target.index("      - name: Build patched Codex CLI")
    assert target.index("Configure rusty_v8 artifact overrides") < mac_fetch < mac_shutdown < cli_build
    for step in (target[mac_fetch:mac_shutdown], target[mac_shutdown:cli_build]):
        assert "if: runner.os == 'macOS'" in step
    assert 'cargo fetch --locked --target "$TARGET"' in target[mac_fetch:mac_shutdown]
    assert "scripts/disable-macos-ci-security-services.sh" in target[mac_shutdown:cli_build]
    assert "disable-macos-ci-security-services.sh" not in cache_setup
    diagnostics = target.split("      - name: Upload build diagnostics\n", 1)[1].split("\n      - name:", 1)[0]
    assert "/c/apple-toolchain.txt" in diagnostics and "/c/macos-security.log" in diagnostics
    assert "retention-days: 7" in diagnostics

    assert "matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}" in release
    assert "matrix.repository" in release and "matrix.workflow" in release
    assert 'X-GitHub-Api-Version: 2026-03-10' in release
    assert "      reuse_builds:" in release
    assert 'response.get("id" if reuse else "workflow_run_id")' in release
    assert "Existing builds cannot be reused after their build inputs changed." in release
    assert "scripts/disable-macos-ci-security-services.sh" in release.split("git diff --quiet", 1)[1].split("||", 1)[0]
    assert "reused target set differs" in release
    assert "needs.plan.outputs.artifact_source_commit" in release
    assert "gh run watch" in release and "gh run download" in release
    assert "scripts/compat_release.py verify-target" in release
    assert "needs: [plan, build]" in release
    assert "needs.validate" not in release
    assert release.count("${{ secrets.BUILD_FANOUT_TOKEN }}") == 3
    assert "timeout-minutes: 360" in release
    assert 'request_id="${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"' in release

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


def test_build_wait_retries(root: Path) -> None:
    """Run the actual broker shell with deterministic network/child outcomes."""
    root.mkdir()
    workflow = (REPOSITORY / ".github/workflows/release-patched-codex.yml").read_text(encoding="utf-8")
    step = workflow.split("      - name: Wait for the exact child run\n", 1)[1].split("\n      - ", 1)[0]
    block = step.split("        run: |\n", 1)[1]
    body = "\n".join(line.removeprefix("          ") for line in block.splitlines())
    stub = r'''
watch_calls=0
gh() {
  [[ "$1 $3 $4 $5" == "run 1234 --repo DSLZL/CSA-codex-macos-x64" ]] || exit 99
  if [[ "$2" == watch ]]; then
    watch_calls=$((watch_calls + 1))
    printf 'watch\n' >> "$EVENTS"
    return "${WATCH_RESULTS[watch_calls-1]}"
  fi
  [[ "$2" == view ]] || exit 99
  if [[ "$6" == --log-failed ]]; then
    printf 'log-failed\n' >> "$EVENTS"
    return 0
  fi
  state="${VIEW_RESULTS[watch_calls-1]:-unavailable}"
  [[ "$state" != unavailable ]] || return 1
  printf '%s\n' "$state"
}
sleep() { printf 'sleep %s\n' "$1" >> "$EVENTS"; }
'''
    cases = (
        ((0,), (), 0, ()),
        ((1, 0), ("in_progress/",), 0, (15,)),
        ((1, 0), ("completed/",), 0, (15,)),
        ((1,), ("completed/success",), 0, ()),
        ((1,), ("completed/failure",), 1, ()),
        ((1,), ("completed/cancelled",), 1, ()),
        ((1, 1, 0), ("unavailable", "in_progress/"), 0, (15, 30)),
        ((1, 1, 1, 1, 1), (), 1, (15, 30, 45, 60)),
    )
    for index, (watches, states, exit_code, delays) in enumerate(cases):
        script = root / f"wait-{index}.sh"
        events = root / f"events-{index}"
        script.write_text(
            "WATCH_RESULTS=(" + " ".join(map(str, watches)) + ")\n"
            + "VIEW_RESULTS=(" + " ".join(states) + ")\n" + stub + body + "\n",
            encoding="utf-8", newline="\n",
        )
        result = subprocess.run(
            ["bash", str(script)], capture_output=True, text=True,
            env={**os.environ, "EVENTS": events.as_posix(), "RUN_ID": "1234",
                 "CHILD_REPOSITORY": "DSLZL/CSA-codex-macos-x64"},
        )
        assert result.returncode == exit_code, (index, result.stdout, result.stderr)
        calls = events.read_text(encoding="utf-8").splitlines()
        assert calls.count("watch") == len(watches), (index, calls)
        assert [line for line in calls if line.startswith("sleep ")] == [f"sleep {delay}" for delay in delays]
        assert ("log-failed" in calls) == (states in (("completed/failure",), ("completed/cancelled",)))


def test_compiler_cache_configuration(root: Path) -> None:
    """Execute the real action setup, including Windows mixed-path normalization."""
    pwsh = shutil.which("pwsh")
    assert pwsh, "PowerShell 7 is required to check the shared build-cache action"
    source = root / "source"
    files = ("Cargo.lock", "Cargo.toml", ".cargo/config.toml", "rust-toolchain.toml")
    for name in files:
        path = source / "codex-rs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture {name}\n", encoding="utf-8")
    action = (REPOSITORY / ".github/actions/setup-codex-rust-cache/action.yml").read_text(encoding="utf-8")
    block = action.split("      run: |\n", 1)[1].split("\n    - ", 1)[0]
    script = root / "cache-setup.ps1"
    script.write_text(
        "function rustc { $global:LASTEXITCODE = 0; $env:TEST_COMPILER }\n"
        + "function git { $global:LASTEXITCODE = [int]$env:TEST_GIT_EXIT; $env:TEST_SOURCE_EPOCH }\n"
        + "function sw_vers { $global:LASTEXITCODE = [int]$env:TEST_APPLE_EXIT; if ($args[0] -eq '-productVersion') { $env:TEST_MACOS_VERSION } else { $env:TEST_MACOS_BUILD } }\n"
        + "function xcodebuild { $global:LASTEXITCODE = [int]$env:TEST_APPLE_EXIT; $env:TEST_XCODE_VERSION }\n"
        + "function xcrun { $global:LASTEXITCODE = [int]$env:TEST_APPLE_EXIT; if ($args[0] -eq 'clang') { $env:TEST_CLANG_VERSION } else { $env:TEST_SDK_VERSION } }\n"
        + "\n".join(line.removeprefix("        ") for line in block.splitlines()),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "RUNNER_TEMP": str(root),
        "CSA_CARGO_HOME": str(root) + "/c/h",
        "CSA_SCCACHE_DIR": str(root) + "/c/k",
        "CSA_SOURCE_ROOT": str(source),
        "CSA_TARGET": TARGET,
        "CSA_CACHE_MODE": "read-write",
        "CSA_DEFAULT_BRANCH": "main",
        "CSA_EVENT_NAME": "workflow_dispatch",
        "CSA_REF": "refs/heads/main",
        "GITHUB_ENV": str(root / "github-env"),
        "GITHUB_OUTPUT": str(root / "github-output"),
        "TEST_COMPILER": "rustc pinned-compiler-a",
        "TEST_SOURCE_EPOCH": "1770000000",
        "TEST_GIT_EXIT": "0",
        "RUNNER_OS": "Windows",
        "RUNNER_ARCH": "X64",
        "TEST_APPLE_EXIT": "0",
        "TEST_MACOS_VERSION": "26.0",
        "TEST_MACOS_BUILD": "25A354",
        "TEST_XCODE_VERSION": "Xcode 26.0\nBuild version 17A324",
        "TEST_CLANG_VERSION": "Apple clang version 17.0.0",
        "TEST_SDK_VERSION": "26.0",
    }

    def configure(**overrides: str) -> tuple[dict[str, str], dict[str, str]]:
        paths = [Path(environment[name]) for name in ("GITHUB_ENV", "GITHUB_OUTPUT")]
        for path in paths:
            path.write_text("", encoding="utf-8")
        result = subprocess.run(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(script)],
            env={**environment, **overrides}, capture_output=True, text=True, encoding="utf-8",
        )
        if result.returncode:
            raise ValueError(result.stderr)
        return tuple(dict(line.split("=", 1) for line in path.read_text(encoding="utf-8-sig").splitlines()) for path in paths)

    settings, first = configure(GITHUB_RUN_ID="1")
    assert settings["SCCACHE_DIR"] == str((root / "c/k").resolve())
    assert settings["CARGO_HOME"] == str((root / "c/h").resolve())
    assert settings["SOURCE_DATE_EPOCH"] == "1770000000"
    assert settings["CC"] == settings["CXX"] == "cl.exe"
    arm_settings, _ = configure(CSA_TARGET="aarch64-pc-windows-msvc")
    assert arm_settings["SOURCE_DATE_EPOCH"] == settings["SOURCE_DATE_EPOCH"]
    assert not {"CC", "CXX"}.intersection(arm_settings), "Preserve AWS-LC's ARM64 Clang selection"
    mac_env = {"CSA_TARGET": "aarch64-apple-darwin", "RUNNER_OS": "macOS", "RUNNER_ARCH": "ARM64"}
    mac_settings, mac_fingerprint = configure(**mac_env)
    assert not {"SOURCE_DATE_EPOCH", "CC", "CXX"}.intersection(mac_settings)
    assert configure(**mac_env, TEST_GIT_EXIT="1", GITHUB_RUN_ID="new-run")[1] == mac_fingerprint
    for name in ("TEST_MACOS_VERSION", "TEST_MACOS_BUILD", "TEST_XCODE_VERSION", "TEST_CLANG_VERSION", "TEST_SDK_VERSION"):
        changed = configure(**mac_env, **{name: environment[name] + "-changed"})[1]
        assert changed["environment_fingerprint"] != mac_fingerprint["environment_fingerprint"], name
        assert changed["fingerprint"] != mac_fingerprint["fingerprint"], name
        expect_error(lambda: configure(**mac_env, **{name: ""}), ValueError)
    expect_error(lambda: configure(**mac_env, TEST_APPLE_EXIT="1"), ValueError)
    assert configure(**{**mac_env, "RUNNER_ARCH": "X64"})[1]["environment_fingerprint"] != mac_fingerprint["environment_fingerprint"]
    assert configure(**mac_env, TEST_COMPILER="changed compiler")[1]["environment_fingerprint"] == mac_fingerprint["environment_fingerprint"]
    assert "MACOS_VERSION=26.0" in (root / "c/apple-toolchain.txt").read_text(encoding="utf-8")
    assert re.fullmatch(r"[0-9a-f]{64}", first["fingerprint"])
    assert configure(GITHUB_RUN_ID="2", GITHUB_RUN_ATTEMPT="2")[1] == first
    assert configure(TEST_COMPILER="rustc pinned-compiler-b")[1] != first
    assert configure(TEST_SOURCE_EPOCH="1770000001")[1] != first
    expect_error(lambda: configure(TEST_SOURCE_EPOCH=""), ValueError)
    expect_error(lambda: configure(TEST_SOURCE_EPOCH="not-a-timestamp"), ValueError)
    expect_error(lambda: configure(TEST_GIT_EXIT="1"), ValueError)
    for name in files:
        path = source / "codex-rs" / name
        before = path.read_bytes()
        path.write_bytes(before + b"changed\n")
        assert configure()[1] != first, name
        path.write_bytes(before)
    assert configure()[1] == first
    wrappers = [root / "zigcc", root / "zigcxx"]
    for path in wrappers:
        path.write_text(f"exec /opt/toolcache/zig/0.14.0/zig {path.name}\n", encoding="utf-8")
    linux_env = {"CSA_TARGET": "x86_64-unknown-linux-musl", "RUNNER_OS": "Linux", "CC": str(wrappers[0]),
                 "CXX": str(wrappers[1]), "CFLAGS": "-pthread", "CXXFLAGS": "-pthread"}
    linux = configure(**linux_env)[1]
    assert linux != first
    assert not {"SOURCE_DATE_EPOCH", "CC", "CXX"}.intersection(configure(**linux_env)[0])
    assert configure(**linux_env, TEST_GIT_EXIT="1")[1] == linux
    assert configure(**linux_env, GITHUB_RUN_ID="another-run")[1] == linux
    for path in wrappers:
        before = path.read_bytes()
        path.write_bytes(before + b"changed compiler path\n")
        assert configure(**linux_env)[1] != linux
        path.write_bytes(before)
    assert configure(**{**linux_env, "CFLAGS": "-pthread -DCHANGED"})[1] != linux
    assert configure(**linux_env)[1] == linux
    expect_error(lambda: configure(**{**linux_env, "CC": str(root / "missing")}), ValueError)
    off_settings, off_outputs = configure(CSA_CACHE_MODE="off")
    assert off_settings["SOURCE_DATE_EPOCH"] == "1770000000" and off_outputs == {}
    expect_error(lambda: configure(CSA_REF="refs/heads/untrusted"), ValueError)
    expect_error(lambda: configure(CSA_SCCACHE_DIR=str(root.parent / "outside")), ValueError)


def test_macos_security_shutdown(root: Path) -> None:
    """Exercise the real script with inert OS commands; never mutate the test host."""
    root.mkdir()
    script = root / "shutdown-test.sh"
    script.write_text(r'''
record() { printf '%s\n' "$*" >> "$EVENTS"; return 1; }
sw_vers() { printf '%s\n' "$TEST_MACOS_VERSION"; }
id() { printf '1001\n'; }
sudo() { record sudo "$@"; }
defaults() { record defaults "$@"; }
launchctl() { record launchctl "$@"; }
csrutil() { record csrutil "$@"; }
spctl() { record spctl "$@"; }
mdutil() { record mdutil "$@"; }
pgrep() { record pgrep "$@"; }
source "$SHUTDOWN_SCRIPT"
''', encoding="utf-8", newline="\n")
    runner_temp = root / "runner"
    environment = {
        **os.environ, "GITHUB_ACTIONS": "true", "RUNNER_OS": "macOS",
        "RUNNER_ENVIRONMENT": "github-hosted", "TEST_MACOS_VERSION": "26.0",
        "RUNNER_TEMP": runner_temp.as_posix(), "EVENTS": (root / "events").as_posix(),
        "SHUTDOWN_SCRIPT": (SCRIPTS / "disable-macos-ci-security-services.sh").as_posix(),
    }
    for name, suffix in (("SOURCE_ROOT", "s"), ("CARGO_HOME", "h"), ("SCCACHE_DIR", "k"),
                         ("CARGO_TARGET_DIR", "t"), ("TARGET_BUNDLE", "b")):
        path = runner_temp / "c" / suffix
        environment[name] = path.as_posix()
        if suffix not in ("t", "b"):
            path.mkdir(parents=True)
    (runner_temp / "rusty_v8").mkdir()

    def execute(**overrides: str) -> tuple[subprocess.CompletedProcess, str]:
        events = Path(environment["EVENTS"])
        events.write_text("", encoding="utf-8")
        result = subprocess.run(["bash", str(script)], env={**environment, **overrides},
                                capture_output=True, text=True)
        return result, events.read_text(encoding="utf-8")

    for invalid in ({"GITHUB_ACTIONS": "false"}, {"RUNNER_ENVIRONMENT": "self-hosted"},
                    {"RUNNER_OS": "Linux"}, {"TEST_MACOS_VERSION": "15.7.9"},
                    {"SOURCE_ROOT": root.as_posix()}, {"SOURCE_ROOT": runner_temp.as_posix()}):
        result, calls = execute(**invalid)
        assert result.returncode == 2 and not calls, (invalid, result.stderr, calls)
    result, calls = execute()
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Best-effort command returned 1" in result.stdout
    assert "sudo -n spctl --global-disable" in calls and "sudo -n mdutil -a -i off" in calls
    assert "sudo -n launchctl bootout system/com.apple.XProtect.daemon.scan" in calls
    assert "launchctl bootout gui/1001/com.apple.trustd.agent" in calls
    assert "sudo -n pkill -9 -x trustd" in calls and "sudo -n pkill -9 -f XProtect" in calls
    assert calls.count("sudo -n xattr -drs com.apple.quarantine ") == 4
    assert calls.count("sudo -n xattr -drs com.apple.provenance ") == 4
    assert "csrutil status" in calls and "csrutil disable" not in calls
    assert "pgrep -lf XProtect|syspolicyd|trustd|mds|mdworker" in calls


def test_cold_unplug_boundaries(root: Path) -> None:
    import copy
    import queue
    import time

    import verify_cold_unplug as cold

    root.mkdir(parents=True)
    owned = cold.fresh_root(root / "owned", root)
    expect_error(lambda: cold.fresh_root(owned, root), cold.ValidationError)
    expect_error(lambda: cold.fresh_root(root.parent / "escape", root), cold.ValidationError)
    expect_error(lambda: cold.strict_json('{"id":1,"id":2}'), cold.ValidationError)
    expect_error(lambda: cold.strict_json('{"value":NaN}'), cold.ValidationError)
    history = owned / "sessions" / "rollout.jsonl"
    history.parent.mkdir()
    history.write_text('{"type":"session_meta","payload":{"id":"fixture"}}\n', encoding="utf-8")
    assert cold.read_canonical(owned)["sessions/rollout.jsonl"]["count"] == 1
    history.write_text(history.read_text(encoding="utf-8") + '{"type":', encoding="utf-8")
    expect_error(lambda: cold.read_canonical(owned), json.JSONDecodeError)
    expect_error(lambda: cold.tool_result({"input": []}, "missing"), cold.ValidationError)
    before = {"id": "thread", "turns": [{"id": "turn", "status": "completed", "items": [{"id": "item", "type": "agentMessage", "text": "sentinel"}]}]}
    cold.assert_history_preserved(before, before)
    dropped = {"id": "thread", "turns": [{"id": "turn", "status": "completed", "items": []}]}
    expect_error(lambda: cold.assert_history_preserved(before, dropped), cold.ValidationError)
    wait = {"id": "join", "type": "collabAgentToolCall", "tool": "wait", "status": "completed"}
    with_wait = copy.deepcopy(before)
    with_wait["turns"][0]["items"].insert(0, wait)
    cold.assert_history_preserved(with_wait, before, official_legacy_wait_ids={"join"})
    expect_error(lambda: cold.assert_history_preserved(with_wait, before), cold.ValidationError)
    expect_error(lambda: cold.assert_history_preserved(with_wait, before, official_legacy_wait_ids={"unknown"}), cold.ValidationError)
    expect_error(lambda: cold.assert_history_preserved(with_wait, dropped, official_legacy_wait_ids={"join"}), cold.ValidationError)
    for key, value in (("status", "inProgress"), ("tool", "spawnAgent"), ("type", "agentMessage")):
        changed = copy.deepcopy(with_wait)
        changed["turns"][0]["items"][0][key] = value
        expect_error(lambda: cold.assert_history_preserved(changed, before, official_legacy_wait_ids={"join"}), cold.ValidationError)
    for index in (0, 1):
        changed = copy.deepcopy(with_wait)
        changed["turns"][0]["items"][index]["text"] = "changed"
        expect_error(lambda: cold.assert_history_preserved(with_wait, changed, official_legacy_wait_ids={"join"}), cold.ValidationError)

    runs = [{"target": "/root/a", "run_id": "run-a"}, {"target": "/root/b", "run_id": "run-b"}]
    terminal = [{**run, "status": "completed", "output": run["run_id"], "error": None, "reason": None} for run in reversed(runs)]
    join = {"join_call_id": "join", "runs": runs, "thread_ids": ["a", "b"], "terminal": terminal}
    rows = [
        {"type": "session_meta", "payload": {"id": "parent"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "join_agents", "call_id": "join", "arguments": json.dumps({"runs": list(reversed(runs))})}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "join", "type": "CollabAgentToolCall", "tool": "wait", "status": "completed", "sender_thread_id": "parent", "receiver_thread_ids": ["b", "a"]}}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "join", "output": json.dumps({"results": terminal})}},
    ]
    history.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    canonical = cold.read_canonical(owned)
    assert len(cold.verify_persisted_joins(canonical, "parent", [join])) == 1
    cold.assert_canonical_preserved(canonical, canonical)
    relative = next(iter(canonical))
    for index in (1, 2, 3):
        changed = copy.deepcopy(canonical)
        changed[relative]["rows"].pop(index)
        expect_error(lambda: cold.verify_persisted_joins(changed, "parent", [join]), cold.ValidationError)
        expect_error(lambda: cold.assert_canonical_preserved(canonical, changed), cold.ValidationError)
    for index, key, value in ((1, "arguments", json.dumps({"runs": runs})), (3, "output", json.dumps({"results": list(reversed(terminal))}))):
        changed = copy.deepcopy(canonical)
        changed[relative]["rows"][index]["payload"][key] = value
        expect_error(lambda: cold.verify_persisted_joins(changed, "parent", [join]), cold.ValidationError)
        expect_error(lambda: cold.assert_canonical_preserved(canonical, changed), cold.ValidationError)
    changed = copy.deepcopy(canonical)
    changed[relative]["rows"].append(copy.deepcopy(rows[2]))
    expect_error(lambda: cold.verify_persisted_joins(changed, "parent", [join]), cold.ValidationError)
    with_event_wait = copy.deepcopy(canonical)
    with_event_wait[relative]["rows"].extend([
        {"type": "response_item", "payload": {"type": "function_call", "name": "wait_agent", "call_id": "event-wait", "arguments": '{"timeout_ms":1000}'}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {"id": "event-wait", "type": "CollabAgentToolCall", "tool": "wait", "status": "completed", "sender_thread_id": "parent", "receiver_thread_ids": []}}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "event-wait", "output": "native event notification"}},
    ])
    assert len(cold.verify_persisted_joins(with_event_wait, "parent", [join], {"event-wait"})) == 2
    expect_error(lambda: cold.verify_persisted_joins(canonical, "parent", [join], {"event-wait"}), cold.ValidationError)
    with_event_wait[relative]["rows"][-2]["payload"]["item"]["status"] = "in_progress"
    expect_error(lambda: cold.verify_persisted_joins(with_event_wait, "parent", [join], {"event-wait"}), cold.ValidationError)
    child_id = "01a07b65-03a5-72a0-ab67-a5154f4b1879"
    spawn = {"turns": [{"items": [{"type": "subAgentActivity", "kind": "started", "id": "spawn", "agentThreadId": child_id}]}]}
    assert cold.spawn_thread_id(spawn, "spawn") == child_id
    expect_error(lambda: cold.spawn_thread_id(spawn, "other"), cold.ValidationError)
    server = cold.AppServer.__new__(cold.AppServer)
    server.home = owned
    sent = []
    server.send = sent.append
    server.call = lambda _method, _params: {"codexHome": str(root)}
    expect_error(server.initialize, cold.ValidationError)
    assert not sent
    server.call = lambda _method, _params: {"codexHome": str(owned)}
    server.initialize()
    assert sent == [{"method": "initialized", "params": {}}]
    server.label = "unit-timeout"
    server.messages = queue.Queue()
    expect_error(lambda: server.receive(time.monotonic()), cold.ValidationError)
    server.messages.put(None)
    expect_error(lambda: server.receive(time.monotonic() + 1), cold.ValidationError)
    server.call = lambda _method, _params: {"data": [], "nextCursor": "repeated"}
    expect_error(lambda: server.pages("thread/turns/list", {}), cold.ValidationError)
    start = {"type": "turnStarted", "position": 1, "turnId": "turn"}
    item = {"type": "item", "position": 1, "turnId": "turn", "item": {"id": "item"}}
    end = {"type": "turnCompleted", "position": 2, "turnId": "turn"}
    pages = iter([{"data": [item, end], "nextCursor": "older"}, {"data": [start], "nextCursor": None}])
    server.call = lambda _method, _params: next(pages)
    assert server.pages("thread/timeline/list", {}) == [start, item, end]
    server.call = lambda _method, _params: {"data": [end, item], "nextCursor": None}
    expect_error(lambda: server.pages("thread/timeline/list", {}), cold.ValidationError)
    turns = [{"id": "first", "status": "completed", "items": [{"id": "message"}, {"id": "late-child-completion"}]}, {"id": "second", "status": "completed", "items": [{"id": "next-message"}]}]
    items = [{"turnId": "first", "item": turns[0]["items"][0]}, {"turnId": "second", "item": turns[1]["items"][0]}, {"turnId": "first", "item": turns[0]["items"][1]}]
    timeline = [{"type": "item", "position": index, **row} for index, row in enumerate(items)]
    server.pages = lambda method, _params: {"thread/turns/list": turns, "thread/items/list": items, "thread/timeline/list": timeline}[method]
    thread = {"id": "thread", "turns": turns}
    assert cold.check_paginated_readers(server, thread)["items"] == items
    timeline[1], timeline[2] = timeline[2], timeline[1]
    expect_error(lambda: cold.check_paginated_readers(server, thread), cold.ValidationError)
    parents = [{"id": "official-parent"}, {"id": "candidate-parent"}]
    children = [{"id": f"child-{index}"} for index in range(5)]
    expected = [row["id"] for row in parents + children]
    cold.assert_native_discovery(parents, children, expected)
    expect_error(lambda: cold.assert_native_discovery(parents[:1], children, expected), cold.ValidationError)
    expect_error(lambda: cold.assert_native_discovery(parents + parents[:1], children, expected), cold.ValidationError)
    expect_error(lambda: cold.assert_native_discovery(parents, children[:-1], expected), cold.ValidationError)
    expect_error(lambda: cold.assert_native_discovery(parents, children[:-1] + children[:1], expected), cold.ValidationError)


def main() -> int:
    test_repository_boundary()
    with tempfile.TemporaryDirectory(prefix="csa-codex-producer-") as directory:
        root = Path(directory)
        test_payload_and_contract_authority(root / "payload")
        test_nextest_runner_mapping()
        test_release_matrix_and_pack(root / "pack")
        test_release_notes(root / "notes")
        test_compiler_cache_configuration(root / "cache")
        test_macos_security_shutdown(root / "macos-security")
        test_build_wait_retries(root / "wait")
        test_cold_unplug_boundaries(root / "cold-unplug")
    test_workflow_contracts()
    print(json.dumps({"schema": 1, "result": "pass"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
