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
`RUSTC_WRAPPER`; the explicit `CC`/`CXX` path does attach it. Windows now specifies
`cl.exe` for both, using the upstream MSVC step's target-specific PATH and SDK
environment. This allows cc-rs C/C++ work to enter sccache without changing the
compiler or compilation flags; the next native run must confirm the requests.

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
