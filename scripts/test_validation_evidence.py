#!/usr/bin/env python3
"""Focused checks for patched-Codex validation evidence."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from compat_catalog import resolve
from run_patch_contract import load_contract, test_runner_argv
from validation_evidence import (
    EvidenceError,
    WORKFLOW_PATH,
    create_evidence,
    select_run,
    verify_evidence,
)
from verify_patch_payload import _load_payload, _payload_file


REPOSITORY = Path(__file__).resolve().parents[1]
TARGET = "x86_64-pc-windows-msvc"


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def passed_report(
    resolution: dict[str, object], test_runner: str = "cargo"
) -> dict[str, object]:
    payload = _load_payload((REPOSITORY / str(resolution["manifest_path"])).resolve())
    contract = load_contract(_payload_file(payload, "test-contract.json"), str(resolution["compat_id"]))
    steps = []
    for kind, contract_steps in (
        ("generation", contract["generation"]),
        ("test", contract["tests"]),
    ):
        for step in contract_steps:
            record = {
                "kind": kind,
                "name": step["name"],
                "argv": step["argv"],
                "exit_code": 0,
            }
            runner_argv = test_runner_argv(step["argv"], test_runner)
            if runner_argv != step["argv"]:
                record["runner_argv"] = runner_argv
            steps.append(record)
    return {
        "schema": 1,
        "result": "pass",
        "phase": "tests",
        "compat_id": resolution["compat_id"],
        "source_verification": {
            "compat_id": resolution["compat_id"],
            "commit": resolution["upstream_commit"],
            "applied": True,
        },
        "steps": steps,
        "known_upstream_errata": contract["known_upstream_errata"],
    }


def main() -> None:
    resolution = resolve(REPOSITORY, "current", TARGET)
    with tempfile.TemporaryDirectory(prefix="csa-validation-evidence-") as directory:
        root = Path(directory)
        resolution_path = root / "resolution.json"
        report_path = root / "test-report.json"
        evidence_path = root / "validation-result.json"
        resolution_path.write_text(json.dumps(resolution), encoding="utf-8")
        report_path.write_text(json.dumps(passed_report(resolution)), encoding="utf-8")

        created = create_evidence(
            REPOSITORY,
            resolution_path,
            report_path,
            evidence_path,
            git_head(),
            123,
            2,
        )
        assert created["validation"]["clippy"] == "passed"
        assert created["validation"]["cargo_frontend"] == "cargo"
        assert created["validation"]["test_runner"] == "cargo"
        assert created["payload"]["test_contract_sha256"]
        verified = verify_evidence(
            REPOSITORY,
            resolution_path,
            report_path,
            evidence_path,
            git_head(),
            123,
            2,
        )
        assert verified["status"] == "pass"

        nextest_report = passed_report(resolution, "nextest")
        nextest_report_path = root / "nextest-test-report.json"
        nextest_evidence_path = root / "nextest-validation-result.json"
        nextest_report_path.write_text(json.dumps(nextest_report), encoding="utf-8")
        nextest_created = create_evidence(
            REPOSITORY,
            resolution_path,
            nextest_report_path,
            nextest_evidence_path,
            git_head(),
            124,
            1,
        )
        assert nextest_created["validation"]["cargo_frontend"] == "cargo"
        assert nextest_created["validation"]["test_runner"] == "nextest"

        mixed_report = passed_report(resolution, "nextest")
        del mixed_report["steps"][0]["runner_argv"]
        mixed_report_path = root / "mixed-test-report.json"
        mixed_report_path.write_text(json.dumps(mixed_report), encoding="utf-8")
        try:
            create_evidence(
                REPOSITORY,
                resolution_path,
                mixed_report_path,
                root / "mixed-validation-result.json",
                git_head(),
                125,
                1,
            )
        except EvidenceError:
            pass
        else:
            raise AssertionError("mixed Cargo/nextest test report was accepted")

        rewritten_report = passed_report(resolution)
        rewritten_report["steps"][0]["runner_argv"] = ["mbx", "fmt"]
        rewritten_report_path = root / "rewritten-test-report.json"
        rewritten_report_path.write_text(json.dumps(rewritten_report), encoding="utf-8")
        try:
            create_evidence(
                REPOSITORY,
                resolution_path,
                rewritten_report_path,
                root / "rewritten-validation-result.json",
                git_head(),
                123,
                2,
            )
        except EvidenceError:
            pass
        else:
            raise AssertionError("rewritten Cargo command was accepted")

        tampered = json.loads(evidence_path.read_text(encoding="utf-8"))
        tampered["payload"]["tree_sha256"] = "0" * 64
        evidence_path.write_text(json.dumps(tampered), encoding="utf-8")
        try:
            verify_evidence(
                REPOSITORY,
                resolution_path,
                report_path,
                evidence_path,
                git_head(),
                123,
                2,
            )
        except EvidenceError:
            pass
        else:
            raise AssertionError("tampered validation evidence was accepted")

        runs_path = root / "runs.json"
        runs_path.write_text(
            json.dumps(
                {
                    "workflow_runs": [
                        {
                            "id": 10,
                            "run_attempt": 1,
                            "path": WORKFLOW_PATH,
                            "head_sha": git_head(),
                            "head_branch": "feature",
                            "event": "pull_request",
                            "status": "completed",
                            "conclusion": "success",
                        },
                        {
                            "id": 9,
                            "run_attempt": 3,
                            "path": WORKFLOW_PATH,
                            "head_sha": git_head(),
                            "head_branch": "main",
                            "event": "push",
                            "status": "completed",
                            "conclusion": "success",
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        assert select_run(runs_path, git_head(), "main") == {"run_id": 9, "run_attempt": 3}
        try:
            select_run(runs_path, git_head(), "main", 10)
        except EvidenceError:
            pass
        else:
            raise AssertionError("pull-request validation run was accepted for release")

    print("validation evidence tests passed")


if __name__ == "__main__":
    main()
