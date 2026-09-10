#!/usr/bin/env python3
"""Regression: p10 checksum repair must preserve data, WAL contents and original files."""

import hashlib
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import tempfile
from unittest.mock import patch

import recover_sqlite_migrations as recovery


def rejected(call, message):
    try:
        call()
    except ValueError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("unsafe recovery succeeded")


def fixture(directory, oracle, *, healthy=False):
    directory.mkdir()
    for name, migrations in oracle.items():
        with closing(sqlite3.connect(directory / name)) as db, db:
            db.execute("""CREATE TABLE _sqlx_migrations (
                version BIGINT PRIMARY KEY, description TEXT NOT NULL,
                installed_on TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                success BOOLEAN NOT NULL, checksum BLOB NOT NULL, execution_time BIGINT NOT NULL
            )""")
            for version, row in migrations.items():
                db.executescript(row["sql"] if healthy else row["sql"].replace("\n", "\r\n"))
                db.execute("INSERT INTO _sqlx_migrations VALUES(?,?,?,1,?,?)", (
                    version, row["description"], "2026-09-10 00:00:00",
                    row["expected" if healthy else "counterpart"], -1,
                ))
                db.commit()
            db.execute("INSERT INTO data VALUES (?,?)", ("kept", b"\x00\xffpayload"))


def main():
    sql = "CREATE TABLE data (\nname TEXT PRIMARY KEY,\npayload BLOB\n);\n"
    migration = {"description": "data", "sql": sql,
                 "expected": hashlib.sha384(sql.encode()).digest(),
                 "counterpart": hashlib.sha384(sql.replace("\n", "\r\n").encode()).digest()}
    oracle = {name: {1: migration} for name in recovery.FAMILIES}
    with tempfile.TemporaryDirectory(prefix="csa-recovery-test-") as temp:
        root = Path(temp).resolve()
        source = root / "source"
        fixture(source, oracle)
        # Keep a committed WAL frame uncheckpointed to prove copying only the main DB would lose data.
        wal = sqlite3.connect(source / "state_5.sqlite")
        try:
            wal.execute("PRAGMA journal_mode=WAL")
            wal.execute("PRAGMA wal_autocheckpoint=0")
            wal.execute("INSERT INTO data VALUES ('wal', X'010203')")
            wal.commit()
            before = recovery.input_records(source)
            audit = recovery.recover(source, None, oracle, {})
            assert audit["status"] == "audited" and all(row["changes"] for row in audit["databases"].values())
            assert recovery.input_records(source) == before
            output = root / "result"
            result = recovery.recover(source, output, oracle, {})
            assert result["status"] == "recovered" and recovery.input_records(source) == before
            assert recovery.input_records(output / "snapshot") == before
            with closing(sqlite3.connect(output / "recovered/state_5.sqlite")) as db, db:
                assert db.execute("SELECT payload FROM data WHERE name='wal'").fetchone() == (b"\x01\x02\x03",)
                assert db.execute("SELECT payload FROM data WHERE name='kept'").fetchone() == (b"\x00\xffpayload",)
            repeat = recovery.recover(output / "recovered", root / "repeat", oracle, {})
            assert all(not row["changes"] for row in repeat["databases"].values())
            rejected(lambda: recovery.recover(source, output, oracle, {}), "output already exists")
            rejected(lambda: recovery.recover(source, source / "nested", oracle, {}), "overlap")
        finally:
            wal.close()

        for label, mutation, reason in [
            ("dirty", "UPDATE _sqlx_migrations SET success=0", "dirty"),
            ("version", "UPDATE _sqlx_migrations SET version=99", "unknown migration"),
            ("checksum", "UPDATE _sqlx_migrations SET checksum=X'00'", "unexplained"),
            ("schema", "CREATE TABLE unrelated(x)", "schema differs"),
            ("sqlite_prefix", "CREATE TABLE sqlitex(x)", "schema differs"),
            ("trigger", "CREATE TRIGGER bad AFTER UPDATE ON _sqlx_migrations BEGIN DELETE FROM data; END", "triggers"),
        ]:
            case = root / label
            fixture(case, oracle)
            with closing(sqlite3.connect(case / "thread_history_1.sqlite")) as db, db:
                db.execute(mutation)
            before = recovery.input_records(case)
            rejected(lambda: recovery.recover(case, root / (label + "-output"), oracle, {}), reason)
            assert not (root / (label + "-output")).exists()
            assert recovery.input_records(case) == before

        healthy = root / "healthy"
        fixture(healthy, oracle, healthy=True)
        result = recovery.recover(healthy, root / "healthy-output", oracle, {})
        assert all(not row["changes"] for row in result["databases"].values())

        original_copy = shutil.copyfile
        changed = False

        def copy_with_writer(src, dst):
            nonlocal changed
            result = original_copy(src, dst)
            if not changed:
                changed = True
                with closing(sqlite3.connect(healthy / "state_5.sqlite")) as db, db:
                    db.execute("INSERT INTO data VALUES ('concurrent writer', X'01')")
            return result

        with patch.object(recovery.shutil, "copyfile", side_effect=copy_with_writer):
            rejected(lambda: recovery.recover(healthy, root / "drift-output", oracle, {}), "changed")
        assert not (root / "drift-output").exists()
    print("PASS: six families, audit, WAL, preserved originals/data, repeat, corruption and input drift")


if __name__ == "__main__":
    main()
