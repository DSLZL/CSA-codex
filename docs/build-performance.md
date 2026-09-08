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
