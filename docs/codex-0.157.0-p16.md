# Codex 0.157.0 compatibility candidate

`rust-v0.157.0-native-join-p16` targets the official
[`rust-v0.157.0`](https://github.com/openai/codex/releases/tag/rust-v0.157.0)
at `00c972ed5d6ff6499317fd41b7f23605b8e6850d`, addressing
[issue #18](https://github.com/DSLZL/CSA-codex/issues/18).
The binding is a candidate with builds enabled and publication disabled. The accepted
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
postimages. [Producer CI](https://github.com/DSLZL/CSA-codex/actions/runs/36139781340)
also passed quality and both Linux recovery jobs.

A fresh official V1 control confirms the same version-specific Wait presentation:
one live completed item in both modes, zero on cold Legacy readback and one on cold
Paginated readback. Canonical bytes stayed unchanged; all four official processes
exited normally. The [runtime procedure](cold-unplug-validation.md) pins this
observation to the exact 0.157.0 commit and official executable.

The first six-platform native attempt on `d0e4130` failed compiling `codex-core`:
the adapter's `tokio::sync::watch` import collided with upstream's new `mod watch`.
Importing the leaf `Receiver` type removes the collision without changing the
completion channel type. The failed runs remain separate from the corrected build.

The second attempt on `fdc98a7` passed the first 13 Windows contract steps, then
failed compiling `codex-tui`. All six targets reported E0061 in
`bottom_pane_desired_height`: the adapter omitted the new `composer_gap` argument.
The height helper now passes `None`, matching inline rendering; fullscreen
rendering continues to pass its per-frame gap. All callers were checked, and the
existing complete-TUI contract includes the inline panel placement regression.

The third attempt on `1f67659` built successfully on Windows ARM64, both Linux
targets and both macOS targets. Windows x64 passed 25 contract steps; the Live
panel step passed 35 tests and failed the new resize case. That fixture still had
an active native text selection, which intentionally hides the return-to-bottom
control. It now returns to latest before exercising reading-mode resize, while
retaining the button, panel-boundary and selection assertions.

The same run also logged a Tokio shutdown panic in a passing core follow-up test.
That fixture now awaits its remaining parent thread's shutdown before returning;
native revalidation must confirm the cleanup. The original failed log is retained.

The corrected fixtures, full Windows contract, matching-binary cold-unplug
acceptance and terminal observations still require passing native results.
The official migration-byte check is not database round-trip acceptance. Evidence
for the accepted 0.156.1 executable does not certify this candidate.
