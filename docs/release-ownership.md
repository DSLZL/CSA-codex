# Patched Codex release ownership

This reference defines the boundary between the CSA Manager and the CSA-codex
producer.

## Repository boundary

| Concern | Canonical repository |
| --- | --- |
| Manager CLI, online installer, runtime state, activation, npm distribution | `DSLZL/CSA` |
| Compatibility payloads, build recipe, patch verification, provenance, aggregation, compatibility releases | `DSLZL/CSA-codex` |
| Native compiler execution and repository-scoped local sccache archive | One fixed `DSLZL/CSA-codex-{os}-{arch}` build shard |

Neither repository reads the other's checkout at runtime or in CI. The
integration boundary is a formal `compat-<compat-id>` release containing a
descriptor, exact payload assets, target-qualified binaries, checksums, and an
install catalog.

## Authority chain

1. `release/compatibility-index.json` selects one reviewed manifest and its
   build/acceptance authorities.
2. The manifest binds the exact upstream tag, peeled commit, preimages, ordered
   patches, toolchain, targets, and expected artifact identities.
3. Each target-pinned shard invokes the immutable reusable recipe, verifies the
   exact producer source and patch inputs, and compiles in a disposable upstream
   checkout using only that shard's local sccache directory. GitHub Actions
   restores and saves that directory as one repository-scoped cache archive.
4. The central broker binds each target to an exact child run ID, either returned
   by a new dispatch or supplied as a complete existing build set, then verifies
   the repository, request, source, target, filename, size, and SHA-256 before
   accepting the binary. Existing builds are reusable only while every build
   input remains unchanged.
5. Central packaging requires the complete target inventory and emits provenance,
   checksums, and the display-only install catalog.
6. Publication is permitted only from the reviewed default-branch commit and an
   annotated `compat-<compat-id>` tag.

Compiler caches affect duration only. They never supply compatibility identity,
artifact authority, or release eligibility.

All six shards load the shared cache action from the requested producer source
commit. Normalize `SCCACHE_DIR` once before restore, and use that exact string for
both restore and save: GitHub includes the literal path in its cache version, so
mixed Windows separators can make an existing cache invisible.
See the [cache restore contract](https://github.com/actions/cache/blob/v6.1.0/restore/README.md).

Archive keys combine target, sccache version, and a fingerprint of `rustc -Vv`,
`Cargo.lock`, workspace `Cargo.toml`, `.cargo/config.toml`, and `rust-toolchain.toml`.
Linux additionally fingerprints its generated `CC`/`CXX` wrappers and C/C++ flags,
after installing musl tools. Zig must use the stable tool-cache installation path;
a per-run extraction directory changes both wrapper contents and header paths.
Windows exports the verified upstream commit timestamp as `SOURCE_DATE_EPOCH`,
including when caching is off. LLVM's COFF linker otherwise embeds the current
time in procedural-macro DLLs, changing the inputs sccache hashes for their users.
The timestamp is also part of the Windows archive fingerprint, so the first fixed
build can save a deterministic baseline instead of repeatedly restoring the old one.
Windows x64 selects `CC=cl.exe` and `CXX=cl.exe` through the upstream MSVC environment:
cc-rs attaches `RUSTC_WRAPPER` to explicit compilers, while its automatic MSVC
discovery path in the pinned version can bypass the wrapper.
Windows ARM64 must retain dependency-specific compiler discovery: AWS-LC uses
Clang for its ARM64 C/assembly sources and cannot build them with `cl.exe`.
Repeated runs and source-only patch revisions reuse the immutable dependency
baseline; changed workspace crates still compile as needed. Compiler, dependency
or build-configuration changes create a new snapshot, with the existing prefix
fallback retaining useful older entries. Do not add a run ID to the key: that
duplicates the full archive on every run. An exact archive hit and the actual
sccache Rust hit rate are separate observations.

The producer sets no cache-size threshold and runs no automatic cache cleanup.
GitHub owns quota enforcement and eviction. Cache archive restore/save failures
remain non-fatal because an empty cache is a valid compiler-cache state.
Save an immutable baseline only after the native build and artifact upload succeed.
A failed build's partial archive must not become an exact hit that blocks later
successful builds from saving their completed cache.

The broker distinguishes a failed status request from a completed, failed build.
After `gh run watch` exits unsuccessfully, it checks the same run's terminal
status. A completed success proceeds; a completed failure or cancellation fails.
Unavailable or non-terminal status retries the same watch up to five times with
15/30/45/60-second backoff, within the existing job timeout. It never redispatches
a build to recover a status-read failure.

Each shard uploads `codex-build-diagnostics-<compat-id>-<target>` separately from
the authoritative binary bundle. This contains Cargo timing reports and sccache
statistics. macOS also records CPU/memory configuration and `/usr/bin/time -l`
resource statistics in the build log. See [build performance](build-performance.md)
for the measured incident and the limits of the available evidence.

The child repositories contain no compatibility payload or publication job and
receive no central credential. Cross-repository dispatch and artifact retrieval
use the dedicated `BUILD_FANOUT_TOKEN` only inside trusted central broker steps.

## Historical releases

Historical `compat-*` tags retain their original annotated tag objects and
peeled commit SHA values. Migration copies existing assets without rebuilding
the patched CLI and verifies filename, size, and SHA-256 equality before any new
release is considered complete. The legacy `DSLZL/CSA` releases remain
available as a fallback mirror.

## Forbidden coupling

- No `../CSA` file reads, nested clone, submodule, or subtree.
- No Manager source, runtime activation state, npm package, or Manager release
  asset in this repository.
- No fuzzy patching, three-way patch application, or mutable released payload.
- No credentials in manifests, evidence, logs, or fixtures.
- No publication, tag movement, or remote repository creation without explicit
  maintainer authorization.
