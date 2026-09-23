# Codex 0.156.1 candidate and fullscreen reuse

`rust-v0.156.1-native-join-p16` targets the official stable
[`rust-v0.156.1`](https://github.com/openai/codex/releases/tag/rust-v0.156.1)
at `b412ff32c417f855c2b2d1581b77058eed87c84b`. It remains a buildable candidate;
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

Compared with the final p15 diff against its own pinned upstream, the TUI patch
touches 60 files instead of 69. Net added TUI lines are 6,128 versus 6,048,
including the new fullscreen integration and regression test. The gain is fewer
upstream adaptation points, not a reduction in total code. The p16 family stores
the final implementation in four ordered patches; previous families are immutable.

## Verification and remaining gates

Local checks cover exact patch application/postimages, family/catalog integrity,
producer Python tests, formatting and workflow syntax. Rust compilation, schema
generation and native tests run only in GitHub Actions. The Windows x64 shard
can run the full patch contract before producing its release binary. Other
shards retain their native six-target build authority and artifact receipts.

The contract includes Join/context/follow-up tests, native state/history checks,
the complete TUI library and TUI Clippy. Its fullscreen regression covers three
widths, row clicks, unchanged composer drafts, native selection with CSA clicks
disabled and raw-mode hiding. Old version-specific test skips are not carried
forward without new evidence.

Official Windows x64 binary SHA-256:
`70bcb05f9bf1a4e7306edd0cd1b57d02af3267ad02a34b26f45c8c4bb20a3301`.
All 69 migration SQL inputs in that verified binary match the native CRLF
representation used by the Windows compatibility path.

Native build/test results, matching official/candidate/official cold-unplug runs,
real ConPTY coverage in both display modes, and terminal-specific Kitty/Sixel
observations remain **NOT VERIFIED** until their evidence is collected. Source
checks do not grant release acceptance. No installed CLI or user home is changed.
