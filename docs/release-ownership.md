# Patched Codex release ownership

This reference defines the boundary between the CSA Manager and the CSA-codex
producer.

## Repository boundary

| Concern | Canonical repository |
| --- | --- |
| Manager CLI, online installer, runtime state, activation, npm distribution | `DSLZL/CSA` |
| Compatibility payloads, patch verification, patched builds, provenance, compatibility releases | `DSLZL/CSA-codex` |

Neither repository reads the other's checkout at runtime or in CI. The
integration boundary is a formal `compat-<compat-id>` release containing a
descriptor, exact payload assets, target-qualified binaries, checksums, and an
install catalog.

## Authority chain

1. `release/compatibility-index.json` selects one reviewed manifest and its
   build/acceptance authorities.
2. The manifest binds the exact upstream tag, peeled commit, preimages, ordered
   patches, toolchain, targets, and expected artifact identities.
3. Validation applies the complete patch sequence to a disposable exact
   upstream checkout.
4. Formal build jobs compile every declared target independently.
5. Packaging verifies the complete target inventory and emits provenance,
   checksums, and the display-only install catalog.
6. Publication is permitted only from the reviewed default-branch commit and an
   annotated `compat-<compat-id>` tag.

Compiler caches affect duration only. They never supply compatibility identity,
artifact authority, or release eligibility.

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
