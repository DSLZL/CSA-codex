#!/usr/bin/env python3
"""Audit p10 migration checksums; optionally publish recovered copies, never edit inputs."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request

from verify_patch_payload import VerificationError, _load_payload, _payload_file


FAMILIES = {
    "state_5.sqlite": "migrations",
    "logs_2.sqlite": "logs_migrations",
    "goals_1.sqlite": "goals_migrations",
    "memories_1.sqlite": "memory_migrations",
    "queue_1.sqlite": "queue_migrations",
    "thread_history_1.sqlite": "thread_history_migrations",
}
TARGETS = {
    "x86_64-unknown-linux-musl": "linux-x64",
    "aarch64-unknown-linux-musl": "linux-arm64",
    "x86_64-apple-darwin": "darwin-x64",
    "aarch64-apple-darwin": "darwin-arm64",
    "x86_64-pc-windows-msvc": "win32-x64",
    "aarch64-pc-windows-msvc": "win32-arm64",
}
MIGRATION_COLUMNS = [
    ("version", "BIGINT", 0, None, 1),
    ("description", "TEXT", 1, None, 0),
    ("installed_on", "TIMESTAMP", 1, "CURRENT_TIMESTAMP", 0),
    ("success", "BOOLEAN", 1, None, 0),
    ("checksum", "BLOB", 1, None, 0),
    ("execution_time", "BIGINT", 1, None, 0),
]


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def absolute(path: Path, *, directory: bool = False) -> Path:
    require(path.is_absolute(), f"path must be absolute: {path}")
    for part in (path, *path.parents):
        metadata = part.lstat()
        require(not stat.S_ISLNK(metadata.st_mode) and not (
            getattr(metadata, "st_file_attributes", 0) & 0x400
        ), f"symlinks/reparse points are unsupported: {part}")
    require(path.is_dir() if directory else path.is_file(), f"wrong path type: {path}")
    return path.resolve(strict=True)


def file_record(path: Path) -> dict:
    absolute(path)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"sha256": digest, "size": path.stat().st_size}


def git(source: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "--no-optional-locks", "-C", str(source), *args], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
    ).stdout


def official_binary(archive: Path, version: str, target: str) -> tuple[bytes, dict]:
    """Bind the supplied archive to the exact official npm package, then read one member."""
    archive = absolute(archive)
    package_version = version + "-" + TARGETS[target]
    url = "https://registry.npmjs.org/@openai%2fcodex/" + package_version
    with urllib.request.urlopen(url, timeout=30) as response:
        require(response.geturl() == url, "official metadata redirected unexpectedly")
        metadata = json.load(response)
    require(metadata.get("name") == "@openai/codex" and metadata.get("version") == package_version,
            "official npm package identity differs")
    dist = metadata["dist"]
    require(dist.get("tarball") == (
        "https://registry.npmjs.org/@openai/codex/-/codex-" + package_version + ".tgz"
    ), "unexpected official archive URL")
    with archive.open("rb") as stream:
        integrity = "sha512-" + base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()
    require(integrity == dist.get("integrity"), "official archive integrity differs from npm")
    before = file_record(archive)
    member = f"package/vendor/{target}/bin/codex" + (".exe" if "windows" in target else "")
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        require(len({entry.name for entry in members}) == len(members), "duplicate archive members")
        for name in ("package/package.json", member):
            require(bundle.getmember(name).isfile(), f"official member is not a regular file: {name}")
        package = json.load(bundle.extractfile("package/package.json"))
        require(package.get("name") == "@openai/codex" and package.get("version") == package_version,
                "archive package identity differs")
        binary = bundle.extractfile(member).read()
    require(file_record(archive) == before, "official archive changed during verification")
    return binary, {"metadata_url": url, "archive_url": dist["tarball"], "integrity": integrity,
                    "archive": before, "binary_member": member,
                    "binary_sha256": hashlib.sha256(binary).hexdigest()}


def load_oracle(manifest_path: Path, source: Path, archive: Path, target: str) -> tuple[dict, dict]:
    manifest_path = absolute(manifest_path)
    source = absolute(source, directory=True)
    payload = _load_payload(manifest_path)
    manifest = payload.manifest
    version = manifest["codex_version"]
    require(version in {"0.153.0", "0.153.2"} and
            manifest["compat_id"] == f"rust-v{version}-native-join-p10",
            "recovery supports only the reviewed 0.153.0/0.153.2 p10 bindings")
    for patch in manifest["patches"]:
        require(file_record(_payload_file(payload, patch["path"]))["sha256"] == patch["sha256"],
                "p10 payload hash differs")
    commit = manifest["upstream_commit"]
    require(git(source, "rev-parse", f"refs/tags/{manifest['upstream_tag']}^{{commit}}").decode().strip()
            == commit, "upstream tag/commit differs from the binding")

    def blob(path: str) -> bytes:
        return git(source, "show", f"{commit}:codex-rs/{path}")

    require(tomllib.loads(blob("Cargo.toml").decode())["workspace"]["package"]["version"] == version,
            "upstream workspace version differs")
    sqlite_source = blob("state/src/sqlite.rs").decode()
    migrators = blob("state/src/migrations.rs").decode()
    binary, identity = official_binary(archive, version, target)
    oracle = {}
    line_endings = set()
    for filename, directory in FAMILIES.items():
        require(f'"{filename}"' in sqlite_source and f'sqlx::migrate!("./{directory}")' in migrators,
                f"native database mapping differs: {filename}")
        prefix = f"codex-rs/state/{directory}/"
        paths = git(source, "ls-tree", "-r", "--name-only", commit, "--", prefix).decode().splitlines()
        require(bool(paths), f"missing native migrations: {directory}")
        migrations = {}
        for path in paths:
            match = re.fullmatch(r"(\d+)_(.+)\.sql", path.removeprefix(prefix))
            require(match is not None, f"unsupported migration filename: {path}")
            number = int(match[1])
            require(number not in migrations, f"duplicate native migration: {path}")
            lf = git(source, "show", f"{commit}:{path}")
            require(b"\r" not in lf and b"\n" in lf, f"unexpected native SQL line endings: {path}")
            crlf = lf.replace(b"\n", b"\r\n")
            found_lf, found_crlf = lf in binary, crlf in binary
            require(found_lf != found_crlf, f"official SQL input is missing or ambiguous: {path}")
            ending = "LF" if found_lf else "CRLF"
            line_endings.add(ending)
            migrations[number] = {
                "description": match[2].replace("_", " "), "sql": lf.decode("utf-8"),
                "expected": hashlib.sha384(lf if found_lf else crlf).digest(),
                "counterpart": hashlib.sha384(crlf if found_lf else lf).digest(),
            }
        oracle[filename] = migrations
    require(len(line_endings) == 1, "mixed official migration line endings are unsupported")
    identity.update(compat_id=manifest["compat_id"], codex_version=version, upstream_commit=commit,
                    manifest_sha256=file_record(manifest_path)["sha256"], target=target,
                    official_line_endings=line_endings.pop(),
                    migration_counts={name: len(rows) for name, rows in oracle.items()})
    return oracle, identity


def input_records(directory: Path) -> dict:
    records = {}
    allowed = {name + suffix for name in FAMILIES for suffix in ("", "-wal", "-shm", "-journal")}
    for path in sorted(directory.iterdir()):
        if ".sqlite" not in path.name:
            continue
        require(path.name in allowed, f"unrecognized database file: {path.name}")
        records[path.name] = file_record(path)
    databases = set(records) & set(FAMILIES)
    require(bool(databases), "no supported databases in the explicit directory")
    require(all(any(name == db or name.startswith(db + "-") for db in databases) for name in records),
            "orphan SQLite sidecar")
    return records


def schema(connection: sqlite3.Connection) -> list:
    rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT GLOB 'sqlite_*' AND name != '_sqlx_migrations' ORDER BY type,name"
    ).fetchall()
    # SQLite retains the migration's line endings in DDL. Compare both native byte forms;
    # the preservation digest below still requires the original stored schema to stay exact.
    return [(kind, name, table, sql.replace("\r\n", "\n") if sql else sql)
            for kind, name, table, sql in rows]


def preserved_digest(connection: sqlite3.Connection) -> str:
    digest = hashlib.sha256()
    for line in connection.iterdump():
        if not line.startswith('INSERT INTO "_sqlx_migrations" VALUES('):
            digest.update(line.encode("utf-8") + b"\n")
    for pragma in ("user_version", "application_id"):
        digest.update(repr(connection.execute("PRAGMA " + pragma).fetchall()).encode())
    return digest.hexdigest()


def inspect_database(connection: sqlite3.Connection, migrations: dict) -> tuple[list, list]:
    connection.execute("PRAGMA trusted_schema=OFF")
    require(connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "SQLite integrity check failed")
    columns = connection.execute("PRAGMA table_xinfo(_sqlx_migrations)").fetchall()
    require([tuple(row[1:6]) for row in columns] == MIGRATION_COLUMNS and
            all(row[6] == 0 for row in columns), "unexpected _sqlx_migrations schema")
    require(not connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name='_sqlx_migrations'"
    ).fetchall(), "migration table triggers are unsupported")
    rows = connection.execute("SELECT * FROM _sqlx_migrations ORDER BY version").fetchall()
    require(bool(rows), "empty migration history is not a checksum recovery case")
    changes = []
    expected_schema = sqlite3.connect(":memory:")
    try:
        for version, description, _installed, success, checksum, _elapsed in rows:
            require(type(version) is int and version in migrations, f"unknown migration version: {version}")
            migration = migrations[version]
            require(success == 1 and description == migration["description"],
                    f"dirty or unexpected migration metadata: {version}")
            require(checksum in (migration["expected"], migration["counterpart"]),
                    f"unexplained migration checksum: {version}")
            expected_schema.executescript(migration["sql"])
            if checksum != migration["expected"]:
                changes.append({"version": version, "before": checksum.hex(),
                                "after": migration["expected"].hex()})
        require(schema(connection) == schema(expected_schema), "database schema differs from applied native migrations")
    finally:
        expected_schema.close()
    return rows, changes


def recover(directory: Path, output: Path | None, oracle: dict, identity: dict) -> dict:
    directory = absolute(directory, directory=True)
    if output is not None:
        require(output.is_absolute(), "output directory must be absolute")
        parent = absolute(output.parent, directory=True)
        require(not output.exists() and not output.is_symlink(), "output already exists")
        require(not output.resolve().is_relative_to(directory) and not directory.is_relative_to(output.resolve()),
                "output and source directories must not overlap")
    else:
        parent = None
    # Private copies are retained as evidence, including on failure; never open an input with SQLite.
    stage = Path(tempfile.mkdtemp(prefix="csa-migration-recovery-", dir=parent)).resolve()
    report = {"schema": 1, "status": "failed", "mode": "recover" if output else "audit",
              "source": str(directory), "work_directory": str(stage), "official": identity,
              "databases": {}}
    connections = []
    try:
        before = input_records(directory)
        report["inputs"] = before
        snapshot, working = stage / "snapshot", stage / "recovered"
        snapshot.mkdir()
        working.mkdir()
        for name, record in before.items():
            shutil.copyfile(directory / name, snapshot / name)
            require(file_record(snapshot / name) == record, f"input changed while copying: {name}")
            # SHM is a rebuildable WAL index; preserve it only in the untouched snapshot.
            if not name.endswith("-shm"):
                shutil.copyfile(snapshot / name, working / name)
        require(input_records(directory) == before, "source changed during snapshot; stop every writer")
        plans = []
        for name in sorted(set(before) & set(FAMILIES)):
            connection = sqlite3.connect(working / name)
            connections.append(connection)
            rows, changes = inspect_database(connection, oracle[name])
            plans.append((name, connection, rows, changes))
            report["databases"][name] = {"applied_migrations": len(rows), "changes": changes}
        for name, connection, rows, changes in plans:
            if output is None or not changes:
                continue
            preserved = preserved_digest(connection)
            with connection:
                for change in changes:
                    cursor = connection.execute(
                        "UPDATE _sqlx_migrations SET checksum=? WHERE version=? AND checksum=?",
                        (bytes.fromhex(change["after"]), change["version"], bytes.fromhex(change["before"])),
                    )
                    require(cursor.rowcount == 1, "migration changed during recovery")
                after_rows, remaining = inspect_database(connection, oracle[name])
                expected_rows = [(*row[:4], oracle[name][row[0]]["expected"], row[5]) for row in rows]
                require(not remaining and after_rows == expected_rows, "recovery changed migration metadata")
                require(preserved_digest(connection) == preserved, "recovery changed schema or user data")
        for connection in connections:
            connection.close()
        connections.clear()
        require(input_records(directory) == before, "source changed during audit/recovery; stop every writer")
        require(all(file_record(snapshot / name) == record for name, record in before.items()), "snapshot changed")
        report["status"] = "recovered" if output else "audited"
        report["outputs"] = input_records(working)
        report["work_directory"] = str(output or stage)
        (stage / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if output is not None:
            require(not output.exists(), "output appeared during recovery")
            stage.rename(output)
        return report
    except Exception as error:
        report.update(status="failed", error=str(error), work_directory=str(stage))
        (stage / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        raise ValueError(f"{error}; diagnostic copies retained at {stage}") from error
    finally:
        for connection in connections:
            connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("manifest", "source", "official-archive", "database-dir"):
        parser.add_argument("--" + option, type=Path, required=True)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--writers-stopped", action="store_true", required=True,
                        help="Confirm every Codex process using these databases has stopped")
    parser.add_argument("--output-dir", type=Path, help="Fresh directory for snapshot, recovered copies, and report")
    args = parser.parse_args()
    try:
        oracle, identity = load_oracle(args.manifest, args.source, args.official_archive, args.target)
        print(json.dumps(recover(args.database_dir, args.output_dir, oracle, identity), indent=2))
        return 0
    except (ValueError, OSError, KeyError, sqlite3.Error, tarfile.TarError,
            subprocess.SubprocessError, VerificationError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
