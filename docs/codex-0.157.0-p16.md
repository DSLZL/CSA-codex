# Codex 0.157.0 compatibility candidate

`rust-v0.157.0-native-join-p16` targets the official
[`rust-v0.157.0`](https://github.com/openai/codex/releases/tag/rust-v0.157.0)
at `00c972ed5d6ff6499317fd41b7f23605b8e6850d`, addressing
[issue #18](https://github.com/DSLZL/CSA-codex/issues/18).
The binding is a buildable candidate with publication disabled. The accepted
route remains 0.156.1; its payload and the shared p16 additions are unchanged.

## Adaptation

- Keep exact-run completion receipts in the new shared `LocalAgentRuntime`.
  Rebinding a control to another session must retain its pending and terminal
  receipts. Spawn preserves the new upstream configuration snapshot and cleanup
  flow; resume, inspection and listing do not invent an initial-run receipt.
- Preserve the new fullscreen composer-gap layout while reserving space for the
  existing Live panel. Recompute both areas when the return-to-bottom control
  changes composer height, including after a resize. Keep upstream background
  voice sessions alive across thread switches.
- Adapt migration code to upstream's split SQLx imports and retain native SQL,
  checksum validation and official Windows line endings. All 72 SQL inputs were
  checked against the integrity-verified official 0.157.0 Windows x64 executable,
  SHA256 `ed1c7b36e44536809c868864c833af8a857f56599a7a7fe23b908a1ba1093b1f`.
- Retain the existing 39-step native contract. Add a shared-runtime completion
  regression and extend fullscreen coverage for panel/composer boundaries when
  a scrolled transcript is resized.

## Verification and limits

Local validation passed strict application of all four ordered patches to the
pinned source, comparison of all 119 patched paths with the reviewed worktree,
family/catalog validation, previous-payload immutability, workflow guards, producer
Python tests and formatting checks for the affected Rust packages. The existing
source-dependent Python fixture was skipped; exact-source preflight ran separately.
All 73 paths with unchanged upstream preimages retain identical accepted-p16
postimages. [Producer CI](https://github.com/DSLZL/CSA-codex/actions/runs/36128450673)
also passed quality and both Linux recovery jobs.

A fresh official V1 control confirms the same version-specific Wait presentation:
one live completed item in both modes, zero on cold Legacy readback and one on cold
Paginated readback. Canonical bytes stayed unchanged; all four official processes
exited normally. The [runtime procedure](cold-unplug-validation.md) pins this
observation to the exact 0.157.0 commit and official executable.

Rust compilation, the native contract, matching-binary cold-unplug acceptance and
terminal observations remain pending. The official migration-byte check is not
database round-trip acceptance. Evidence for the accepted 0.156.1 executable does
not certify this candidate.
