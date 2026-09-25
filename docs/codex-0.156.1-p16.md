# Codex 0.156.1 and fullscreen reuse

`rust-v0.156.1-native-join-p16` targets the official stable
[`rust-v0.156.1`](https://github.com/openai/codex/releases/tag/rust-v0.156.1)
at `b412ff32c417f855c2b2d1581b77058eed87c84b`. It is the accepted/current
compatibility route, with the [Windows x64 acceptance record](../release/acceptance/rust-v0.156.1-native-join-p16/x86_64-pc-windows-msvc.json)
binding the exact A10 artifact and runtime evidence. Earlier releases remain immutable.

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

## Verification and limits

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

On source `9666a728ee38c66c09bec6b8d130608c49a04c0c`,
[A10 Windows Actions](https://github.com/DSLZL/CSA-codex-windows-x64/actions/runs/35968632530)
passed all 39 contract steps and built the release binary. This includes generated
schemas, complete TUI/configuration libraries, their Clippy checks, Join/context,
Live/Orbit, all 201 state tests, native protocol/configuration, rollout persistence,
cold child selection and shared prompts. The missing test trait import found in A9
is fixed with all assertions retained. No background panic was found in the A10 log.

The other five builds and
[producer CI](https://github.com/DSLZL/CSA-codex/actions/runs/35968656177) also passed:

| Target | A10 run |
| --- | --- |
| Windows ARM64 | [35968636478](https://github.com/DSLZL/CSA-codex-windows-arm64/actions/runs/35968636478) |
| Linux x64 | [35968640038](https://github.com/DSLZL/CSA-codex-linux-x64/actions/runs/35968640038) |
| Linux ARM64 | [35968644583](https://github.com/DSLZL/CSA-codex-linux-arm64/actions/runs/35968644583) |
| macOS x64 | [35968648505](https://github.com/DSLZL/CSA-codex-macos-x64/actions/runs/35968648505) |
| macOS ARM64 | [35968652775](https://github.com/DSLZL/CSA-codex-macos-arm64/actions/runs/35968652775) |

All six downloaded target bundles passed source/run/manifest/size/SHA256 validation.
Their manifest SHA256 is
`4196536f67032167a5f174806730eab12bdd7458f1b3c64b1e8bd14089376840`.

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

Windows x64 candidate SHA256:
`d47bc7c95a0d977c66ed3b00decb56ae46c3a14ff23d5f293e9241affe9b1004`.
On 2026-09-25, this exact executable passed all 18 Legacy/Paginated A-I cold-unplug
cells against the matching official executable. All 22 positive native processes
exited normally; the report binds 419 evidence files. The version-specific Legacy
display rule and separate resume/read-only projection checks are documented in
[the runtime procedure](cold-unplug-validation.md).

Ten fresh ConPTY launches passed the unset/auto/on/off/invalid mouse-policy checks
across inline and fullscreen modes, including cold child navigation, raw-mode
transitions, one generic invalid-value warning and terminal cleanup. Three further
live loopback captures passed inline, fullscreen and fullscreen with
`--no-alt-screen`: active child/title generation, 140/80/45-column layouts, mouse
selection after resize, return to parent, raw hiding/restoration and completion.

These are real executable observations with a local Responses fixture, not a
credentialed live-provider evaluation. Other-platform runtime UI, actual Kitty/Sixel
terminals, native clipboard integration and Unix suspend remain **NOT VERIFIED**;
their unit/snapshot coverage is separate. Promotion and release do not modify an
installed CLI or home. The formal release reuses the six A10 builds above, with
unchanged native build inputs and the same accepted Windows executable hash.
