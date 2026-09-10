# Validate official/candidate cold-unplug compatibility

`scripts/verify_cold_unplug.py` runs matching official and candidate Windows x64
executables against new durable homes. A fixed loopback Responses service supplies
model output; the executables perform the actual native tools, persistence,
shutdown and reads. This tests interoperability, not authenticated model quality.

The command requires Python 3.14, Windows, RTK and the inputs below. It never
compiles, queries GitHub, accepts a candidate or changes release/current state.

## Obtain the exact executable

Use `DSLZL/CSA-codex-windows-x64` and its existing `build.yml` with an exact producer
commit, compatibility ID and unique request ID. The shard pins the reusable build
recipe and owns its native Windows runner and compiler cache.

For Task 06, the user prohibits compilation polling. Dispatch once and retain the
response. Wait for the user's completion notification before reading the exact
run result or downloading its artifact. Do not use `gh run watch`, a waiting build
broker, status loops or artifact-availability probes. The observed shard retains
artifacts for one day; an expired artifact requires a new build decision.

After notification, verify the run's success, workflow, recipe, repository and
request/source inputs. Download the exact target bundle, preserving this layout:

```text
download/
  target-record.json
  bin/codex.exe
```

Retain a JSON build receipt with these fields: `schema: 1`, `status: "passed"`,
`builder_repository`, `runner`, `target`, `source_commit`, `request_id`,
`workflow_run_id` (positive integer), and `recipe_commit` (full SHA). Record
`passed` only after observing successful completion and verifying those inputs.
A dispatch receipt alone is rejected. The driver reuses `verify_target_bundle`
to verify the bundle inventory and its schema-2 artifact/source/run bindings.

Also retain the matching official npm archive and official executable. The
archive must match the catalog runtime lock's SHA512 integrity. The driver
extracts only the pinned runtime members into its new run root and checks that
the supplied official executable matches the archive. It gives the candidate
those same verified helper executables.

## Run the regression

Use absolute paths. The new run root must be a direct child of
`%LOCALAPPDATA%/CSA-codex-validation/` and must not exist. Homes stay outside
`%TEMP%`, because native helper-alias installation rejects temporary roots.

```bash
rtk proxy py -3 scripts/verify_cold_unplug.py \
  --repository C:/work/CSA-codex \
  --manifest C:/work/CSA-codex/payload/codex/native-join-p15/bindings/rust-v0.153.2-native-join-p15/manifest.toml \
  --official-binary C:/validation/official/package/vendor/x86_64-pc-windows-msvc/bin/codex.exe \
  --official-archive C:/validation/official-0.153.2-win32-x64.tgz \
  --candidate-binary C:/validation/download/bin/codex.exe \
  --build-receipt C:/validation/verified-github-build.json \
  --native-source C:/validation/verified-p15-source \
  --run-root C:/Users/NAME/AppData/Local/CSA-codex-validation/cold-unplug-run-001 \
  --terminal-evidence C:/validation/terminal/receipt.json
```

`--native-source` is the reviewed, exactly patched source used to compare the
config schema with its upstream hash; the driver does not build that checkout.
Do not substitute an arbitrary version or reuse a dirty fixture home.

| Gate | Observation |
| --- | --- |
| A–B | Official seed → candidate append → official read/resume, plus candidate-created parent discovery. |
| C–E | Explorer/worker handoffs, native parent edges, optional-state inventory and original-worker busy/completed/cold continuation. |
| F | Exact source config schema, real official startup with the fixture config and separate real terminal observations. |
| G | Single and reversed-order two-child batch Join, complete canonical calls/results, official Legacy display semantics, Paginated Wait presentation and separate reprojection. |
| H | Complete schemas/migration rows, a sentinel in the native projects table, and isolated checksum-mismatch fixtures with no repair. |
| I | Explicit `fork_turns=all`, inherited context, native lineage and official continuation of that same child. |

Both Legacy and Paginated homes run. Native timeline pages arrive newest first,
with entries in ascending order inside each page. The driver checks that order
and stable ties before prepending older pages; it does not sort away corruption.
Readback compares full prior item content and turn order, not just final text.
For exact official 0.153.2 Legacy readback only, omit known completed Wait display
items associated with verified Join or ordinary `wait_agent` calls from the expected
candidate view. Use actual fixture call IDs and verify each complete canonical
call/result/Wait triple before allowing that display omission. Unknown or unfinished
Wait items and all other missing or changed items fail.
Canonical single/batch calls, arguments, complete results, child order and unique
Wait completions must remain intact; prior canonical records must remain unchanged
after official continuation. Candidate and Paginated comparisons remain exact.
Items retain their order within each turn; asynchronous child events can interleave
turns in the item stream. Item and timeline readers must agree on that global order.

Native flat listing discovers roots; parent-edge queries discover children.
For reprojection, discover the children before loading owner metadata, then resume
each child through its parent in a separate native process. This respects native
resident-thread capacity while rebuilding every child index from canonical history.

Each checksum-negative home first passes native initialization with the same
executable. After changing only one checksum, retain the native rejection and
unchanged migration rows/sentinel. The CLI can reject startup before RPC and print
only the state-runtime error. Such an expected exit 1 is recorded under H with
its successful control, separately from normal persistence processes; forced
cleanup, malformed streams or an unrelated error cannot satisfy the negative case.

## Record real terminal observations

Use an actual PTY/ConPTY and the exact candidate executable with the verified
runtime helpers. Use a separate owned fixture home containing a completed parent
and child history from a basic native smoke fixture. Prepare this receipt before
the complete regression command. Keep real user configuration and credentials out
of the fixture. Open the parent in the native TUI and exercise its subagent view
and keyboard navigation, then exit normally.

Capture five separate launches: `CSA_SUBAGENT_LIVE_MOUSE` unset, `auto`, `on`,
`off`, and a benign invalid value. Capture startup output, keyboard interaction,
mouse-capture transitions and terminal restoration. The invalid value must give
one generic mouse-mode warning and fall back to Auto; count that specific warning,
not unrelated model-metadata or feature deprecation notices. Never put the raw
invalid value or real credentials in the report.
Use the native `log_dir` override to retain an actual TUI file log. Without an
explicit log directory this upstream version does not keep the old default
`codex-tui.log`; a short capture cannot assume SQLite logs have already flushed.

The terminal receipt is JSON with `schema: 1`, `candidate_sha256`, `files` and
`terminal`. `files` maps safe relative capture paths to `{sha256, size}`. The
driver hashes the real files before importing them. `terminal.method` is `pty`
or `conpty`; `terminal.modes` contains exactly `unset`, `auto`, `on`, `off`,
`invalid`. Each mode records:

- `status: "passed"` and nonempty `evidence` references to its actual capture files;
- the executable `sha256`, `version`, timezone-aware `started_at`/`ended_at`, and
  `exit_code: 0`;
- observed `keyboard`, `capture_cleanup` and `normal_shutdown` booleans, all true;
- `warning_count` (one for invalid, zero otherwise) and `effective_mode` (`auto`
  for unset/invalid, otherwise the selected value).

Setting these fields is not a terminal test. Preserve actual captures and process
results. Redirected stdout, unit tests and forced termination cannot fill this
gate. Omitting `--terminal-evidence` allows diagnostic binary work but leaves F
unverified and returns a nonzero exit code; it cannot produce passing acceptance.
Failed terminal mode observations explicitly fail F even if another binary phase
already failed; retain all available captures in the partial report.
Verify that both stdin and stdout remain terminals at the subject executable.
If a command wrapper redirects either stream, use the project's documented
wrapper-behavior exception for that subject and retain the diagnostic evidence.

## Interpret and retain evidence

The driver prints its report path, `RUN_ROOT/evidence/evidence.json`. Exit zero
means all required cells, process shutdowns, binary identities and terminal
observations passed the shared validator. Missing, failed or unverified work
returns nonzero and preserves diagnostics. Keep raw RPC/SSE exchanges, canonical
record snapshots, complete migration histories, process receipts and terminal
captures alongside the report. Do not scrub/rewrite history to make a case pass.

The initial verified p15 Windows run passed Paginated A–I and all five ConPTY modes,
but failed the original Legacy Wait presentation requirement.
Official 0.153.2 applies its own Legacy replay allowlist and omits that Wait even
when p15 persisted it. A stock-official V1 control also loses its own live Wait
from cold Legacy presentation while preserving canonical bytes. The current gate
stayed closed for that original report. The user approved official Legacy display
semantics after this control. The driver now pins that rule to exact upstream
0.153.2 and requires fresh evidence with `G.presentation` equal to `official-native`
for Legacy or `native-wait` for Paginated, plus `G.canonical_join_verified: true`.
The original failed report is not relabeled or reused as passing evidence.

The fresh p15 run on 2026-09-08 passed all 18 A–I cells across both modes and all
five ConPTY modes against GitHub build `34133546274`. All 15 positive persistence
processes exited normally. Each mode checked seven histories, three Join calls and
three ordinary waits; Paginated also rebuilt all seven histories from canonical
records. Four checksum-negative/control pairs passed. The report retains 398 hashed
supporting files and is bound to executable SHA256
`a22dfdc868db8c4047151c2ae2fa11810cb9fdd34e7fa308de46dc8c1f4b48fb`.
Native Rust tests and snapshots were not executed by the CLI-only build recipe.

Fresh acceptance on 2026-09-10 repeated all 18 A–I cells and five ConPTY modes for
Windows build `34440926437`. All 15 positive persistence processes exited normally;
the report retains 402 hashed supporting files and binds executable SHA256
`df4c06d2a8f5ef2f5b878dc23b570c8a7ce5a9bf001c8d7867b4198b6a9f90a7`.
The [accepted record](../release/acceptance/rust-v0.153.2-native-join-p15/x86_64-pc-windows-msvc.json)
also includes the independently verified native Linux official/p15/official
database round trip. Both runs use real executables with a local Responses fixture;
they do not establish live authenticated provider coverage or native Rust test coverage.

For native-join p14 and later, `compat_catalog.py accept` requires this complete
`evidence.cold_unplug` report and verifies its referenced files before writing
acceptance/catalog state. Its build provenance must match the candidate record's
`provider=github`, `pipeline=<run ID>`, and exact `source_commit`. Accepted-record
resolution also validates the portable report and its identity/completeness;
p1–p13 retain their prior acceptance contract. Existing accepted records do not
require a copy of temporary observation files on every resolving host.

Actual promotion and publication remain separate actions. A different rebuilt
executable requires fresh evidence bound to its own artifact identity. A native
p14 defect retains its failing inputs and needs a reviewed follow-on payload;
do not rewrite the immutable p14 payload or relax a required gate.
