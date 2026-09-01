# CSA-codex

CSA-codex is the producer repository for version-pinned, patched Codex CLI
compatibility releases. It owns the reviewed patch payloads, exact build
metadata, verification tools, and patched-Codex release workflows consumed by
the [CSA Manager](https://github.com/DSLZL/CSA).

> [!IMPORTANT]
> CSA-codex never reads files from a sibling CSA checkout. The repositories
> integrate only through committed release metadata and immutable GitHub
> Release assets.

## Repository layout

| Path | Ownership |
| --- | --- |
| `payload/codex/` | Exact upstream bindings, preimage hashes, patches, and test contracts |
| `release/` | Compatibility routing, build profiles, runtime locks, and acceptance records |
| `scripts/` | Patch verification, catalog, provenance, and release tooling |
| `.github/workflows/` | Candidate validation and formal patched-Codex release automation |
| `tests/ui/` | Disposable UI development harness for patch work |

Manager/runtime code, npm packages, activation logic, and Manager releases stay
in `DSLZL/CSA`.

## Local verification

Use Python 3.11 or newer and Git:

```powershell
py -3 -m compileall -q scripts
py -3 scripts/test_verify_patch_payload.py
py -3 scripts/test_compat_catalog.py
py -3 scripts/test_validation_evidence.py
py -3 scripts/test_verify_release_asset_set.py
py -3 scripts/test_producer_tools.py
py -3 scripts/compat_catalog.py validate --repository .
```

The patch-contract runner additionally needs a disposable exact upstream Codex
checkout outside this repository.

## Release model

Formal releases use the `compat-<compat-id>` namespace only. Every release is
bound to an exact upstream tag and commit, reviewed payload bytes, build target,
artifact digest, provenance descriptor, and checksums. See
[release ownership](docs/release-ownership.md) for the authority and migration
rules.
