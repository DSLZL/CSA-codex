#!/usr/bin/env python3
"""Native Linux recovered-home and optional candidate cold-unplug check, using isolated fixtures."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import platform
import shutil
import sqlite3
import tarfile

import compat_release
import recover_sqlite_migrations as recovery
import verify_cold_unplug as driver


TARGET = "x86_64-unknown-linux-musl"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("manifest", "source", "official-archive", "run-root"):
        parser.add_argument("--" + option, type=Path, required=True)
    for option in ("candidate-manifest", "candidate-bundle", "candidate-run"):
        parser.add_argument("--" + option, type=Path)
    args = parser.parse_args()
    recovery.require(platform.system() == "Linux" and platform.machine() == "x86_64", "native Linux x64 is required")
    recovery.require(args.run_root.is_absolute() and not args.run_root.exists(), "run root must be new and absolute")
    candidate_inputs = (args.candidate_manifest, args.candidate_bundle, args.candidate_run)
    recovery.require(all(candidate_inputs) or not any(candidate_inputs), "candidate inputs must be supplied together")
    root = args.run_root
    root.mkdir(parents=True)
    evidence = root / "evidence"
    evidence.mkdir()
    oracle, identity = recovery.load_oracle(args.manifest, args.source, args.official_archive, TARGET)
    official = root / "official-codex"
    with tarfile.open(args.official_archive, "r:gz") as bundle:
        with bundle.extractfile(identity["binary_member"]) as source, official.open("xb") as output:
            shutil.copyfileobj(source, output)
    recovery.require(recovery.file_record(official)["sha256"] == identity["binary_sha256"], "official binary changed")
    official.chmod(0o700)
    candidate = None
    candidate_record = None
    if all(candidate_inputs):
        run = json.loads(args.candidate_run.read_text(encoding="utf-8"))
        recovery.require(run["repository"]["full_name"] == "DSLZL/CSA-codex" and
                         run["path"] == ".github/workflows/release-patched-codex.yml" and
                         run["status"] == "completed" and run["conclusion"] == "success",
                         "candidate must come from a successful central release build")
        record = json.loads((args.candidate_bundle / "target-record.json").read_text(encoding="utf-8"))
        target = compat_release.release_target(args.candidate_manifest, TARGET)
        candidate_record = compat_release.verify_target_bundle(
            args.candidate_manifest, args.candidate_bundle,
            request_id=f"{run['id']}-{run['run_attempt']}", source_commit=run["head_sha"],
            repository=target["repository"], runner=target["runner"], target=TARGET,
            workflow_run_id=record["workflow_run_id"],
        )
        recovery.require(record["upstream_commit"] == identity["upstream_commit"], "candidate upstream differs from official")
        candidate = root / "candidate-codex"
        shutil.copyfile(args.candidate_bundle / record["artifact"], candidate)
        candidate.chmod(0o700)
    home = root / "seed"
    workspace = root / "workspace"
    provider = driver.ResponsesFixture(evidence)
    server = None
    processes = []
    try:
        server = driver.AppServer(official, home, workspace, evidence, provider.url, "official", "paginated", "seed", timeout=90)
        server.initialize()
        thread = server.start()["id"]
        marker = "RECOVERY_SEED"
        provider.plan(marker, [driver.message(marker + "_reply")])
        server.turn(thread, marker)
        before_thread = driver.read_thread(server, thread)
        processes.append(server.close())
        server = None
        recovery.require(set(recovery.input_records(home)) >= set(oracle), "native process did not create all database families")
        for name, migrations in oracle.items():
            with closing(sqlite3.connect(home / name)) as db, db:
                rows = db.execute("SELECT version, checksum FROM _sqlx_migrations").fetchall()
                recovery.require({version for version, _ in rows} == set(migrations), "native migration set differs")
                for version, checksum in rows:
                    recovery.require(checksum == migrations[version]["expected"], "native checksum differs from official artifact")
                    db.execute("UPDATE _sqlx_migrations SET checksum=? WHERE version=?", (migrations[version]["counterpart"], version))
        result = recovery.recover(home, root / "output", oracle, identity)
        restored = root / "restored"
        shutil.copytree(home, restored, ignore=shutil.ignore_patterns("*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.sqlite-journal"))
        for path in (root / "output/recovered").iterdir():
            shutil.copyfile(path, restored / path.name)
        phases = [("official", official, "recovered")]
        if candidate is not None:
            phases.extend([("candidate", candidate, "candidate"), ("official", official, "unplugged")])
        for role, binary, label in phases:
            server = driver.AppServer(binary, restored, workspace, evidence, provider.url, role, "paginated", label, timeout=90)
            server.initialize()
            driver.assert_history_preserved(before_thread, server.resume(thread))
            marker = "RECOVERY_" + label.upper()
            provider.plan(marker, [driver.message(marker + "_reply")])
            server.turn(thread, marker)
            before_thread = driver.read_thread(server, thread)
            processes.append(server.close())
            server = None
            audit = recovery.recover(restored, None, oracle, identity)
            recovery.require(all(not row["changes"] for row in audit["databases"].values()), f"{label} changed native checksums")
        report = {"status": "passed", "target": TARGET, "official": identity,
                  "recovered_migrations": sum(len(row["changes"]) for row in result["databases"].values()),
                  "candidate": candidate_record, "processes": processes,
                  "history_preserved": True, "new_turn_completed": True}
        driver.write_json(evidence / "result.json", report)
        print(json.dumps(report, indent=2))
    finally:
        if server is not None:
            server.close()
        provider.close()


if __name__ == "__main__":
    main()
