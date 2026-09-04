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

The producer sets no cache-size threshold and runs no automatic cache cleanup.
GitHub owns quota enforcement and eviction. Cache archive restore/save failures
remain non-fatal because an empty cache is a valid compiler-cache state.

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
