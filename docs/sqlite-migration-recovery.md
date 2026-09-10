# Recover p10 SQLite migration checksums

Use this procedure for [issue #10](https://github.com/DSLZL/CSA-codex/issues/10):
official Codex reports that a previously applied migration has been modified after
using a `native-join-p10` executable against the same Codex home.

p10 converted migration inputs and recognized stored checksums to CRLF on every
platform. The official Linux x64 0.153.0 and 0.153.2 executables embed LF inputs.
Their schemas and data can therefore be valid while their migration checksums no
longer match the official executable. Reinstalling Codex or uninstalling CSA does
not restore those database values.

The p15 candidate removes that rewriting and limits CRLF migration inputs to the
verified Windows x64 target. As of 2026-09-10 it is still a release-disabled
candidate. Candidate promotion and native target acceptance remain separate gates;
an upgrade alone does not repair a home previously opened by p10. Keep p10
unplugged while validating recovery with official Codex.

## Inputs and boundaries

The standard-library tool requires Python 3.11+, Git, access to the official npm
registry, and these explicit inputs:

- The producer's exact p10 binding for **0.153.0 or 0.153.2**.
- A local Git repository containing that official upstream tag and commit. A bare
  clone is sufficient; the tool reads Git objects without changing the checkout.
- The official npm platform archive for the **same version and target**. The tool
  checks its SHA-512 integrity against live official registry metadata, then finds
  every migration's exact SQL bytes in the contained native executable.
- An absolute directory containing the affected databases. No default Codex home
  is inferred. Stop every Codex process using it before passing `--writers-stopped`.

The following database filenames are recognized at the directory's top level:

| Database | Native migration directory | Migrations in both supported versions |
| --- | --- | ---: |
| `state_5.sqlite` | `state/migrations` | 52 |
| `logs_2.sqlite` | `state/logs_migrations` | 2 |
| `goals_1.sqlite` | `state/goals_migrations` | 2 |
| `memories_1.sqlite` | `state/memory_migrations` | 1 |
| `queue_1.sqlite` | `state/queue_migrations` | 2 |
| `thread_history_1.sqlite` | `state/thread_history_migrations` | 6 |

Missing families are left absent. Unknown database files, versions, checksums,
failed migrations, migration-table triggers, or unexplained schema differences
reject the entire recovery. A legacy migration-version repair, database corruption,
or a database from a newer release needs its own diagnosis. Do not use this tool
to bypass those errors.

## Audit first

Run from a CSA-codex checkout. Example for Linux x64 and official 0.153.2;
replace every `/absolute/...` path with your selected location:

```sh
git clone --bare --depth 1 --branch rust-v0.153.2 \
  https://github.com/openai/codex.git /absolute/upstream.git
curl --fail --location \
  https://registry.npmjs.org/@openai/codex/-/codex-0.153.2-linux-x64.tgz \
  --output /absolute/codex-0.153.2-linux-x64.tgz

python3 scripts/recover_sqlite_migrations.py \
  --manifest /absolute/CSA-codex/payload/codex/native-join-p10/bindings/rust-v0.153.2-native-join-p10/manifest.toml \
  --source /absolute/upstream.git \
  --official-archive /absolute/codex-0.153.2-linux-x64.tgz \
  --target x86_64-unknown-linux-musl \
  --database-dir /absolute/stopped-codex-home \
  --writers-stopped
```

The default operation prints `status: audited` and the exact proposed checksum
changes. SQLite opens only private working copies. The source databases and their
WAL/SHM/journal files are copied and hashed before inspection; any source change
aborts the operation. A private directory containing `snapshot/`, working copies,
and `report.json` is retained for review. Audit working copies are **not repaired**.

The official archive determines LF versus CRLF independently of the affected
database. If registry verification fails or the embedded SQL is ambiguous, the
tool stops instead of guessing.

## Create recovered copies

Repeat the same command with `--output-dir /absolute/new-recovery-output`. Its
parent must exist, and the output directory must be new and outside the source.
On success it contains:

- `snapshot/`: byte-identical copies of the original databases and sidecars.
- `recovered/`: databases with only recognized counterpart checksums corrected.
- `report.json`: official artifact/source identities, input/output hashes, and
  each migration's before/after checksum.

All databases are checked before any repair. Updates run in transactions and are
checked for preserved migration metadata, raw schema, user data, and SQLite
integrity. Schema comparison accepts the known LF/CRLF formatting difference but
does not rewrite stored schema text or execute missing migrations in the recovered
database. The completed output directory is published only after every check passes.
Failures retain diagnostic copies and never replace the originals.

Review the report, then test the recovered databases with the matching official
Codex in a **separate complete copy** of the Codex home, including its rollouts and
configuration. Replace that test copy's database/sidecar set with the complete
`recovered/` set; do not combine recovered main files with stale WAL files. Verify
existing history and a new turn before choosing to switch homes. The script has
no in-place repair or automatic home-switch option.

## Verification recorded on 2026-09-10

Exact official Linux x64 0.153.0 and 0.153.2 artifacts were checked, and all six
families/65 migrations passed fixture recovery and repeat-run checks. Both versions
also resumed recovered homes and completed new turns with the real official
executables in [native Linux CI](https://github.com/DSLZL/CSA-codex/actions/runs/34441569806).
Windows x64
0.153.2 was also checked against its CRLF inputs: the actual official process
resumed a recovered home containing all six databases and completed a new turn.
The regression test covers WAL
data, unchanged originals, healthy databases, unknown checksums/versions, dirty
migrations, schema drift, triggers, existing output, and changing inputs:

```sh
python3 scripts/test_recover_sqlite_migrations.py
```

Native macOS startup and candidate acceptance remain separate checks. Retained
Windows p15 acceptance evidence was separately rehashed; publication requires
acceptance evidence for the actual candidate artifact.
