# Contributing

This guide is for maintainers adding or validating patched Codex compatibility
payloads.

## Before changing a payload

1. Work from an exact formal `openai/codex` tag and peeled commit.
2. Use a disposable upstream checkout outside the CSA-codex worktree.
3. Leave existing payloads, bindings, shared patches, and released tags
   byte-identical.
4. If a preimage or patch no longer applies exactly, create a new binding or
   patch revision. Do not use fuzzy or three-way application.
5. Keep Manager, runtime activation, and npm distribution changes in
   `DSLZL/CSA`.

## Validate a change

```powershell
py -3 -m compileall -q scripts
py -3 scripts/test_verify_patch_payload.py
py -3 scripts/test_compat_catalog.py
py -3 scripts/test_validation_evidence.py
py -3 scripts/test_verify_release_asset_set.py
py -3 scripts/test_producer_tools.py
py -3 scripts/compat_catalog.py validate --repository .
```

For a real payload, also run `scripts/run_patch_contract.py` against the exact
disposable upstream checkout and preserve its machine-readable evidence.

## Pull requests

- Keep each commit to one logical change and use Conventional Commit titles.
- Include the exact compatibility ID, upstream tag/commit, and validation
  evidence in the pull request.
- Never commit credentials, authentication state, build outputs, or local
  upstream checkouts.
- Pull requests and candidate workflows do not authorize tag creation, GitHub
  Release publication, or historical release mutation.
