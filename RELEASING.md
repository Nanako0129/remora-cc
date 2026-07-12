# Releasing remora

> A release is complete only when tests pass, the tag matches both version declarations, GitHub publishes an attested archive, and the pinned one-prompt URL resolves.

## Release contract

| Source | Required value for `vX.Y.Z` |
|---|---|
| `VERSION` | `X.Y.Z` |
| `src/remora.py` | `VERSION = "X.Y.Z"` |
| `CHANGELOG.md` | A dated `X.Y.Z` entry |
| README one-prompt URLs | `.../vX.Y.Z/install/AGENT-INSTALL.md` |

## Local gate

Run the full suite and inspect the generated payload before tagging:

```bash
make check
make package
tar -tzf dist/remora-cc-X.Y.Z.tar.gz
git diff --check
git status --short
```

The suite includes installer isolation, unrelated-executable collision protection, offline bootstrap installation, and checksum rejection. No test may require a real gateway or modify the developer's `~/.claude` directory.

## Publish

Commit the reviewed tree, create a signed tag when signing is configured, and push the branch and tag:

```bash
git tag -s vX.Y.Z -m "remora vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

The tag starts `.github/workflows/release.yml`. That workflow reruns `make check`, verifies the tag against `VERSION`, builds the archive, creates a GitHub provenance attestation, and publishes the archive plus `checksums.txt`.

## Post-publish verification

| Check | Expected result |
|---|---|
| GitHub Actions | Release workflow is green |
| Release assets | Archive and `checksums.txt` are present |
| Provenance | `gh attestation verify` succeeds for the downloaded archive |
| Pinned runbook | Raw `vX.Y.Z/install/AGENT-INSTALL.md` URL returns the tagged content |
| Clean-room install | Pinned one-prompt flow installs and `remora doctor` passes |
| Native isolation | Clean-room `~/.claude` manifest is unchanged |

Do not move an existing release tag. If a published artifact or runbook is wrong, fix it in a new patch release so the reviewed tag remains immutable.
