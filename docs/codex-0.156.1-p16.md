# Codex 0.156.1 candidate and fullscreen reuse

`rust-v0.156.1-native-join-p16` targets the official stable
[`rust-v0.156.1`](https://github.com/openai/codex/releases/tag/rust-v0.156.1)
at `b412ff32c417f855c2b2d1581b77058eed87c84b`. It remains a development candidate;
the accepted/current route is still `rust-v0.154.0-native-join-p15`.

## Adaptation

- Move completion receipts and exact-run Join onto `LocalAgentControl`. The
  relocated `LiveAgent` carries a spawn receipt only for an actual spawn;
  inspection and listing do not invent one.
- Preserve Fresh defaults, explicit history forks, original-worker follow-up,
  handoff guidance, native Wait persistence and cold child routing. Adapt shared
  instructions in the new `codex-prompts` module and the updated tool schema API.
- Preserve the native persisted protocol/configuration formats and all six
  database migration families. The process-only `CSA_SUBAGENT_LIVE_MOUSE` policy
  does not add a stored configuration field.
- Reuse the matching official package, including its voice runtime. The build
  exports the exact upstream `STABLE_GIT_COMMIT` required by the voice helper
  handshake. The Windows runtime lock requires the existing four helper files
  plus all 39 voice files from the integrity-verified official archive.
- Reuse the config loader's trust-path normalization for active-project lookup.
  This preserves saved trust and distrust when Windows uses short or verbatim
  paths, without changing the stored configuration format.

## Fullscreen decision

The optional fullscreen mode introduced in
[`0.156.0`](https://github.com/openai/codex/releases/tag/rust-v0.156.0) can own
terminal capture, transcript navigation and selection. It does not replace the
CSA live-state reducer, panel or Orbit renderer.

| Area | p16 implementation |
| --- | --- |
| Fullscreen layout | Render the existing Live panel between the native transcript and composer; preserve the native composer footer, history search and selection. |
| Mouse events and screen transitions | Reuse native event forwarding and alternate-screen ownership; keep CSA capture only for inline mode. |
| `CSA_SUBAGENT_LIVE_MOUSE=off` | Disable CSA row clicks. Native fullscreen transcript selection remains available. |
| Orbit graphics | Share frame cleanup and drawing between inline and fullscreen frames, with the existing text fallback and error handling. |
| Native draw callers | Preserve the upstream draw interfaces; only CSA callers use the graphics variants. |
| Existing displays | Keep Subagent Live, Orbit, CSA version badge, thread navigation, raw-mode hiding and overlay ownership. |

Compared with each version's pinned upstream, the TUI patch touches 34 implementation
files instead of p15's 43, with 4,487 net added lines versus 4,513. This count excludes
standalone tests, test-support files and snapshots; embedded tests remain included.
Including all test adaptations, p16 touches 77 files and adds 6,167 net lines versus
p15's 69 files and 6,048 lines. Native ownership reduces implementation adaptation
points, while new coverage increases the total. The p16 family stores the final
implementation in four ordered patches; previous families are immutable.

## Verification and remaining gates

Local checks cover exact patch application/postimages, family/catalog integrity,
producer Python tests, formatting and workflow syntax. Rust compilation, schema
generation and native tests run only in GitHub Actions. The Windows x64 shard
can run the full patch contract before producing its release binary. Other
shards retain their native six-target build authority and artifact receipts.

The contract includes Join/context/follow-up tests, native state/history checks,
the complete TUI and configuration libraries, and Clippy for both. Its fullscreen regression covers three
widths, row clicks, unchanged composer drafts, native selection with CSA clicks
disabled and raw-mode hiding. Old version-specific test skips are not carried
forward without new evidence.

On producer commit `cc5fa44e0cf5e499f4a44f9c5b9b7a05e0ec744e`,
[A9 Windows Actions](https://github.com/DSLZL/CSA-codex-windows-x64/actions/runs/35894915787)
passed both complete TUI/configuration libraries and their Clippy checks, including
the directory/trust and session-header repairs. Native Join, Live/fullscreen
geometry, Orbit, the official runtime overlay, all 201 state tests, native protocol
and core configuration checks also passed. The contract then stopped compiling
the rollout policy test: the moved upstream `HasLegacyEvent` trait was not imported.
A10 adds that test import and retains the single/batch, Legacy/Paginated exact
persistence assertions. The remaining contract steps still need native confirmation.

Producer CI accepts an optional `upstream_tui_baseline` compatibility ID to run the
seven directory/trust cases on unpatched upstream on Windows. It resolves exact
source/compiler identity and test settings from the same catalog, checks that all
selected tests exist, records each exit code and fails on any failure. Each case
runs in a fresh process, unlike the complete suite; passing this diagnostic alone
cannot exclude shared-process effects. No candidate tests are skipped. Snapshot
diagnostics show differences while snapshot updates remain disabled.

Official Windows x64 binary SHA-256:
`70bcb05f9bf1a4e7306edd0cd1b57d02af3267ad02a34b26f45c8c4bb20a3301`.
All 69 migration SQL inputs in that verified binary match the native CRLF
representation used by the Windows compatibility path.

The remaining native contract checks, final-source platform artifacts, matching
official/candidate/official cold-unplug runs, real ConPTY coverage in both display
modes, and terminal-specific Kitty/Sixel observations remain **NOT VERIFIED**.
Source checks and earlier-source builds do not grant release acceptance. No
installed CLI or user home is changed.
