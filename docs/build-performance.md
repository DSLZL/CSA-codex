# Build performance investigation: 2026-09-08

The six p15 native builds for producer commit
`f090cdf5519b685598e58fa2cd4e7eccef1d3349` all succeeded and uploaded their target
artifacts. The central run was
[`34186050615`](https://github.com/DSLZL/CSA-codex/actions/runs/34186050615).

## Status-request failure

The macOS x64 broker's `gh run watch` exited after the jobs endpoint returned
HTTP 504. Its child run had successfully compiled, uploaded the binary, and saved
the cache. Treating the watch exit code as the build conclusion skipped the
central aggregate. The broker now verifies the exact run's terminal state and
retries transient observation failures without dispatching another child.

## Linux C/C++ cache identity

Both Linux builds restored their compiler archives. Rust hit rates were 90.44%
(ARM64) and 90.54% (x64), but C/C++ and assembler hits were zero.

`mlugg/setup-zig@v2.2.1` defaults to disabling its tool cache on hosted runners.
The x64 log shows Zig installed under
`/home/runner/work/_temp/e4be8440-f2e1-4832-820d-19f9b6d4c1a0/`.
The exact upstream musl setup script embeds the result of `command -v zig` into
both compiler wrappers. A new random extraction directory therefore changes the
wrapper bytes and the Zig header paths. sccache hashes the compiler executable
and preprocessed input, so equal Rust inputs do not make these C/C++ inputs equal.

The recipe now sets `use-tool-cache: true`. The cache action fingerprints the
actual generated wrappers and C/C++ flags after musl setup. The corrected Linux
compiler identity creates one new immutable baseline while restoring the retained
archive through its prefix. The first corrected run can still miss old C entries;
cross-run C/C++ reuse requires a subsequent build with this same stable identity.

Sources: [pinned Zig action inputs](https://github.com/mlugg/setup-zig/blob/v2.2.1/action.yml),
[exact upstream musl setup](https://github.com/openai/codex/blob/657a993cbee87acf52d14b758ce49dbd46d1b8eb/.github/scripts/install-musl-build-tools.sh),
[sccache hash inputs](https://github.com/mozilla/sccache/blob/v0.16.0/docs/Caching.md).

## macOS build tail beyond cache lookup

| Target | CLI build duration | Last `Compiling` message to `Finished` | Rust cache hits |
| --- | --- | --- | --- |
| [ARM64](https://github.com/DSLZL/CSA-codex-macos-arm64/actions/runs/34186072240) | 85m 44s | 42m 42s | 91.67% |
| [x64](https://github.com/DSLZL/CSA-codex-macos-x64/actions/runs/34186072354) | 100m 26s | 51m 30s | 91.68% |

Both runs spent substantial time inside the final Cargo build after the last
crate-start message. Average cache-hit reads were 0.002 seconds. Archive
restore/save and upload happened outside these build durations.

The exact upstream release profile enables ThinLTO, four codegen units, line-table
debug information, and no stripping. sccache does not cache Rust `bin` crates or
other crates that invoke the linker; those are excluded from the reported Rust
hit/miss denominator. Thus a high cache hit percentage does not measure the final
CLI optimization/linking work. This is a concrete limitation of the cache metric,
not evidence of failed cache restoration.

The old recipe generated Cargo timing HTML but did not upload it, and recorded no
resource statistics. The logs alone cannot divide the long tail into remaining
code generation, ThinLTO, linker work, or memory pressure. ThinLTO/final linking is
the leading explanation to measure, not a proven allocation of all 42–51 minutes.
The next build preserves per-unit timing/concurrency reports and macOS CPU,
memory, and resource statistics. The official release profile remains unchanged
so the measurements remain comparable.

Sources: [exact upstream release profile](https://github.com/openai/codex/blob/657a993cbee87acf52d14b758ce49dbd46d1b8eb/codex-rs/Cargo.toml#L579),
[Rust cache limitations](https://github.com/mozilla/sccache/blob/v0.16.0/docs/Rust.md),
[Cargo timing interpretation](https://doc.rust-lang.org/cargo/reference/timings.html).

## Completed follow-up: 34195085244

The [follow-up run](https://github.com/DSLZL/CSA-codex/actions/runs/34195085244)
at producer commit `69617011edceafac0f162b86bf8e4382b06690bf` succeeded for all six
targets and the central aggregate. The following measurements come from its
uploaded Cargo timing HTML and sccache JSON, rather than log-message timestamps.

| Target | CLI Cargo duration | Rust hits / misses | Final `codex-cli` binary unit |
| --- | --- | --- | --- |
| Linux ARM64 | 17m 40s | 1083 / 15 | 14m 24s |
| Linux x64 | 19m 34s | 1085 / 14 | 16m 09s |
| macOS ARM64 | 45m 08s | 1069 / 0 | 42m 17s |
| macOS x64 | 69m 53s | 1070 / 0 | 63m 32s |
| Windows ARM64 | 50m 27s | 886 / 206 | 21m 08s |
| Windows x64 | 77m 55s | 882 / 208 | 38m 48s |

### Linux archive migration

Each Linux shard had two large archives: the retained pre-fix baseline and the
new baseline whose key includes the stable Zig wrappers. Their keys differed;
each run saved only its new key. The obsolete archives (IDs `7440268769` and
`7440386329`) were deleted after verifying the successful replacement archives
(`7443184625` and `7443217918`). This released 7.82 GiB. Each shard now retains one
large compiler archive plus its roughly 43–47 MiB Zig download archive.

The new run's C/assembler entries missed during this one-time migration. A later
run against the corrected archive is still needed to verify cross-run C hits.
Routine identical builds reuse the exact immutable key and do not save another
large archive. Future compiler/configuration migrations can leave an obsolete
baseline until manual cleanup or GitHub eviction.

### Windows repeated misses

Windows repeated exactly the previous 206/208 Rust misses. Archive restore worked,
cache read/write errors were zero, and local cache sizes were about 3.2 GiB, below
the 10 GiB limit. This is not archive loss or local capacity eviction.

The logs identify the selected linker as Rust 1.95's LLVM 22.1.2 `rust-lld`
(through the upstream ARM64 wrapper where needed). That exact LLVM COFF driver
defaults to `time(nullptr)` for PE/debug timestamps. sccache 0.16 hashes the full
contents of `--extern` files, including freshly linked procedural-macro DLLs, so
changing only their embedded timestamp invalidates dependent Rust cache entries.

A local link-only reproduction used the same Rust 1.95 linker and one fixed COFF
object. Two default links produced different DLL SHA-256 values and different PE
timestamps. With `SOURCE_DATE_EPOCH=1770000000`, two links produced the same
timestamp and byte-identical DLLs, including with PDB generation enabled. No Rust
compilation was used for this reproduction.

The shared action now sets `SOURCE_DATE_EPOCH` from the verified upstream Git
commit for Windows, independently of cache mode, and incorporates it into the
archive fingerprint. This removes the proven timestamp instability without
changing Rust optimization flags. The first fixed build seeds a new baseline;
the following build must measure reuse. The completed runs did not retain
per-crate cache keys or procedural-macro DLLs, so attributing every one of the 206/208
misses to this mechanism, or claiming a corrected hit rate, would be premature.

There is also a C/C++ wrapper gap: x64 recorded no C or assembler cache requests,
whereas ARM64 recorded 254 C hits and 21 assembler hits. In pinned cc-rs 1.2.55,
the automatic MSVC discovery path returns its compiler without attaching
`RUSTC_WRAPPER`; the explicit `CC`/`CXX` path does attach it. The first repair set
both variables to `cl.exe` on both Windows targets. The next run confirmed C cache
requests on x64, but exposed an ARM64 compiler-selection regression; see below.

Sources: [exact LLVM timestamp handling](https://github.com/llvm/llvm-project/blob/llvmorg-22.1.2/lld/COFF/Driver.cpp#L1958),
[sccache extern hashing](https://github.com/mozilla/sccache/blob/v0.16.0/src/compiler/rust.rs#L1384),
[upstream Windows linker selection](https://github.com/openai/codex/blob/657a993cbee87acf52d14b758ce49dbd46d1b8eb/.github/actions/setup-msvc-env/setup-msvc-env.ps1#L99),
[cc-rs compiler/wrapper selection](https://github.com/rust-lang/cc-rs/blob/cc-v1.2.55/src/lib.rs#L2904).

### Remaining final-binary cost

The final binary unit accounts for 94% of macOS ARM64 time and 91% of x64 time,
despite 100% cacheable Rust hits. During that unit, Cargo's mean host CPU samples
were 96% and 93%, respectively. ARM64 reported 3 CPUs / 7 GiB RAM and x64 reported
4 CPUs / 14 GiB RAM. `/usr/bin/time -l` recorded zero swaps, with maximum resident
sizes of 3.73 GiB and 6.73 GiB. Windows' final-unit mean host CPU samples were also
about 94–95%.

These measurements point to the final optimization/link workload on the hosted
CPUs, rather than archive transfer, as the remaining bottleneck. Cargo reports
this binary unit as one interval and cannot separate its frontend, ThinLTO, and
system-linker time. Fixing Windows dependency misses will not eliminate its
separate 21–39 minute final unit.

The official ThinLTO/four-codegen-unit profile remains intact. Reusing the
existing verified target set through `reuse_builds` avoids compilation when the
exact build inputs are unchanged. Making a fresh optimized binary substantially
faster requires measuring a different build profile or runner capacity; neither
was changed as part of this cache repair.

## Follow-up verification: 34205110729

The [next run](https://github.com/DSLZL/CSA-codex/actions/runs/34205110729), at
`66cd66ec6a63125535841493cf63326d728b5b45`, succeeded on five targets. Windows
ARM64 failed while building AWS-LC, so the central aggregate was skipped.

| Target | Result | CLI Cargo duration | Rust hits / misses | C hits / misses | Assembler hits / misses |
| --- | --- | --- | --- | --- | --- |
| Linux ARM64 | success | 15m 45s | 1097 / 1 | 1535 / 1 | 142 / 0 |
| Linux x64 | success | 20m 32s | 1098 / 1 | 1534 / 1 | 162 / 0 |
| macOS ARM64 | success | 43m 45s | 1069 / 0 | 375 / 0 | 120 / 0 |
| macOS x64 | success | 56m 45s | 1070 / 0 | 379 / 0 | 122 / 0 |
| Windows x64 | success | 74m 59s | 869 / 221 | 0 / 456 | no requests |
| Windows ARM64 | failure | incomplete | no final statistics | no final statistics | no final statistics |

Linux restored its corrected archives exactly and created no new large archives.
Both Rust hit rates were 99.91%; C/assembly reuse is now demonstrated across runs.
Each Linux shard still has one compiler archive and one small Zig archive. ARM64
also reported one C cache error, with zero archive/cache read errors, timeouts,
and cache write errors; the native build completed successfully.

Windows x64 now sends its C work through sccache (456 first-time misses), and saved
the new archive `7449183927`. This was the first build populating the deterministic
timestamp baseline, so its 79.72% Rust hit rate does not establish the subsequent
warm hit rate. A later identical-input build must verify that.

### ARM64 regression and partial baseline

Setting global `CC=cl.exe` / `CXX=cl.exe` on ARM64 overrode AWS-LC's compiler
selection. AWS-LC requires Clang on this target; the log shows `cl.exe` attempting
to preprocess `aes-xts-dec.S` and `aes-xts-enc.S`, failing with C2162. This was
introduced by the preceding cache repair, not by a missing archive or the status
watcher. The explicit compiler override is now restricted to Windows x64. Both
Windows targets retain the deterministic timestamp setting.

The failed job also saved partial archive `7446736435`, because the save condition
only excluded cancellation. An exact hit on this immutable archive would prevent
the next successful build from saving its completed cache. Saving now requires
`success()`, after the build and artifact upload. The partial ARM64 archive was
removed, while its previous usable archive `7440576603` was retained for prefix
restore. Obsolete x64 archive `7441118935` was also removed after verifying its
successful replacement. These two deletions released 4.65 GiB.

The final binary units still took 41m 10s / 52m 45s on macOS ARM64 / x64 and
36m 20s on Windows x64. Their mean host CPU samples were 96%, 91%, and 95%.
The official release profile is unchanged; these remaining durations are not
evidence that a compiler archive failed to restore.

Source: [AWS-LC Windows ARM64 compiler requirements](https://github.com/aws/aws-lc-rs/blob/main/book/src/requirements/windows.md).

## Completed verification: 34213949628

The [completed run](https://github.com/DSLZL/CSA-codex/actions/runs/34213949628),
at `816acacf8faa1c9675c1c3c995d2060552333ae4`, succeeded on all six targets and
the central aggregate. Publication was skipped. The measurements below come from
the uploaded Cargo timings and sccache statistics.

| Target | CLI Cargo duration | Rust hits / misses | C hits / misses | Final binary unit |
| --- | --- | --- | --- | --- |
| Linux ARM64 | 15m 18s | 1097 / 1 | 1535 / 1 | 13m 56s |
| Linux x64 | 19m 55s | 1098 / 1 | 1534 / 1 | 18m 05s |
| macOS ARM64 | 53m 09s | 1069 / 0 | 375 / 0 | 50m 28s |
| macOS x64 | 71m 45s | 1070 / 0 | 379 / 0 | 67m 45s |
| Windows ARM64 | 49m 46s | 886 / 206 | 254 / 0 | 21m 11s |
| Windows x64 | 74m 28s | 883 / 207 | 456 / 0 | 36m 52s |

Linux again restored the exact compiler archives without creating new large
archives. Each shard retains one compiler archive and one small Zig download
archive. As in the preceding run, Linux ARM64 reported one C cache error, with
zero cache read/write errors and timeouts; the build still succeeded.

Windows ARM64 now builds successfully with its upstream compiler selection. It
restored the old archive by prefix and saved the first successful timestamp-aware
baseline `7451735451`; its warm Rust hit rate still needs a subsequent run.
After verifying that replacement, obsolete archive `7440576603` was deleted,
releasing 2.31 GiB. Only the successful replacement remains in that shard.

Windows x64 restored its timestamp-aware baseline exactly. Its C work now hits
all 456 entries, but Rust still misses 207 entries (81.01% hits). The log confirms
both the expected Rust-bundled linker and `SOURCE_DATE_EPOCH=1788476668` in the
build environment. The timestamp change therefore has not resolved the main
recurring Rust misses. No per-crate cache-key log or procedural-macro DLLs were
retained, so these reports cannot establish which input still changes. The next
diagnostic must compare those inputs before choosing another compiler change.

macOS has 100% cacheable Rust/C/assembler hits. The final binary unit still takes
about 95% of the CLI build, with mean host CPU samples of 96.42% (ARM64) and
90.27% (x64); both report zero swaps. This interval includes frontend work,
optimization, ThinLTO, and system linking. It cannot be attributed entirely to
the system linker. The official release profile remains unchanged, and this
round does not establish a fix for the final-binary cost on macOS or Windows.

## XProtect/SIP investigation: 2026-09-09

The macOS jobs above used image `20260829.0321.1` (ARM64) and
`20260824.0482.1` (Intel), both macOS 15.7.9. Their image tags, rather than only
the current runner-images branch, were inspected. Both templates invoke
`configure-machine.sh`, which enables `DevToolsSecurity`; neither tag's macOS
scripts record an XProtect exclusion for the runner's launcher or a
`kTCCServiceDeveloperTool` grant. The conditional `csrutil status` check in that
script does not establish which SIP state the actual job had. The jobs did not
record live SIP status or XProtect process samples.

The linked Rust performance article concerns repeated scans when launching newly
built executables. Its Developer Tools exclusion is attached to the application
launching the processes. Enabling `DevToolsSecurity` alone is not evidence that
this launcher-specific permission was granted. GitHub's headless runner also
cannot be assumed to inherit a Terminal.app permission. SIP protects system files
and privileged operations; it is a separate mechanism from XProtect malware
scanning. Historical reports of disabled SIP on macOS 13 do not establish the
state of these macOS 15 images.

| Measurement in the retained reports | macOS ARM64 | macOS Intel |
| --- | ---: | ---: |
| Build-script executions | 105 | 105 |
| Median build-script duration | 0.03 s | 0.07 s |
| Build scripts below 0.2 s | 93 | 80 |
| Time before the final binary unit | 160.34 s | 240.19 s |
| Final binary unit | 3028.26 s | 4064.73 s |

This is not the article's widespread 0.48–3.88 second delay in trivial build
scripts. The longest script is AWS-LC (55.79/43.82 seconds), which performs native
build setup. Eliminating even the entire pre-binary interval would save only
5.0%/5.6% if the final unit were unchanged. The final unit's high host CPU samples
are not per-process attribution, so they cannot prove that XProtect contributes
zero CPU time. The evidence does not identify XProtect or SIP as the main cause.

The investigation recommended per-process CPU and stack sampling during the
final unit, covering rustc, the linker, XProtectService, and syspolicyd. If scans
are demonstrated to dominate, compare the actual runner launcher's Developer
Tools permission on a controlled runner. These measurements did not establish a
benefit from terminating security services. With the official release profile
preserved, a separate capacity comparison can use `macos-15-large` (12 Intel CPUs,
30 GB) or `macos-15-xlarge` (5 M2 CPUs, 14 GB). Measure elapsed time and billed cost;
neither more Cargo jobs nor more CPUs guarantees proportional speedup of the final
unit. Exact, already verified build inputs can continue to use `reuse_builds`.

Sources: [performance article](https://nnethercote.github.io/2025/09/04/faster-rust-builds-on-mac.html),
[nextest's launcher-specific Developer Tools guidance](https://nexte.st/docs/installation/macos/),
[ARM64 image configuration](https://github.com/actions/runner-images/blob/macos-15-arm64/20260829.0321/images/macos/scripts/build/configure-machine.sh),
[Intel image configuration](https://github.com/actions/runner-images/blob/macos-15/20260824.0482/images/macos/scripts/build/configure-machine.sh),
[Apple's SIP definition](https://support.apple.com/en-us/102149),
[historical macOS 13 SIP issue](https://github.com/actions/runner-images/issues/8162),
[GitHub larger runner specifications](https://docs.github.com/en/actions/reference/runners/larger-runners).

## Requested macOS 26 migration

The subsequent maintainer-requested migration selects `macos-26` for ARM64 and
`macos-26-intel` for Intel, updating both shard wrappers and the central runner
binding. These explicit labels are listed in
[runner-images](https://github.com/actions/runner-images/blob/main/README.md).
Rust targets, exact toolchain, upstream profile, and the Cargo/local-sccache/archive
architecture remain the same.

The shared archive namespace becomes v3 for all targets. macOS product/build,
SDK, Xcode, Apple clang, and runner OS/architecture form an environment fingerprint
included in the full key and its restore prefix. A changed Apple environment
cannot restore an older environment's archive through prefix fallback. Only the
two macOS repositories' old v2 archives are removed for this migration, after
publishing the new producer commit and updating their workflow pins. The first
macOS 26 builds therefore start cold; a later identical environment can reuse v3.

Before the native build, the recipe prefetches Cargo dependencies after
downloading and verifying rusty_v8. On disposable GitHub-hosted macOS 26 runners
only, it then requests XProtect, Gatekeeper/SystemPolicy, trustd, and Spotlight
shutdown and clears quarantine/provenance on validated temporary build roots.
SIP/AMFI/TCC are not disabled. Protected, absent, or relaunched services remain a
possible outcome: command results, disabled-service state, remaining processes,
and Apple toolchain identity accompany the seven-day timing diagnostics.

This is an unmeasured configuration change, not evidence that XProtect caused the
previous final-binary cost. Compare later warm v3 builds with the retained macOS
15 reports; the first cold build cannot establish the effect of shutdown alone,
and the OS/toolchain migration is another confounding change.

The first macOS 26 runs, `34308165259` (ARM64) and `34308168694` (Intel), failed
in prefetch: Cargo needed to update `Cargo.lock`, but the added `--locked` flag
prohibited it. Both selected the new images and v3 cache prefixes successfully;
neither reached security shutdown or compilation, and neither saved a compiler
archive. These failures provide no scan-performance measurement.

The repair removes the prefetch-only lock restriction, matching the existing
`cargo build` policy, and retains `cargo-lock.diff` in the seven-day diagnostic
artifact. The original failed jobs did not retain a resolved lockfile diff, so
they do not identify a specific changed dependency. A real Cargo regression
check uses a stale lockfile and local path dependency, confirms `--locked` fails,
then runs the actual prefetch step offline for both macOS targets without
compiling Rust.

## Diagnostic parser repair: 34253013907

The Windows x64 run at producer `4d0fc46036ad58621289bcc03bd89c7892b20c0f`
completed its native build, but its diagnostic report recorded 1088 unparsed
invocations and no native rows. The resulting `native Rust cache events missing`
error prevented the independent small probe from starting. Attempt 2 was
cancelled before applying this repair, as requested.

The original raw events were not retained, so the precise failure of those 1088
invocations cannot be reconstructed. A format defect was reproduced with the
official Windows sccache 0.16.0 binary: Rust Debug escapes such as `\u{200e}` are
valid log output but invalid JSON. The decoder now handles these escapes while
preserving literal Windows backslashes, and records parse failure reasons.
Native-log errors no longer prevent the small probe from running; incomplete
native evidence still produces an incomplete diagnostic result.

Selected compiler cache events now accompany the seven-day reports. Their
arguments and hit/miss/key events can be replayed locally with `--replay`, without
reading DLLs or invoking a compiler. Replay cannot establish DLL reproducibility.
Offline tests cover escaping, replay, failure isolation, and filtered log retention.
Real sccache log-format checks used a tiny C translation unit and a C-written
compiler-protocol stub that refuses compilation; no local Rust compilation was
performed. Another native run has not been started to validate this repair.
