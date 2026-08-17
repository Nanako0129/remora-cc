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

Before tagging, confirm that repository Settings > Releases reports **Immutable releases: Enabled**. Commit the reviewed tree, create a signed tag when signing is configured, and push the branch and tag:

```bash
git tag -s vX.Y.Z -m "remora vX.Y.Z"
git push origin main
git push origin vX.Y.Z
```

The tag starts `.github/workflows/release.yml`. Its third-party actions are pinned to reviewed full commit SHAs. The workflow reruns `make check`, verifies the tag against `VERSION`, builds and attests the archive, creates a verified-tag draft with generated notes, uploads the archive and `checksums.txt`, and only then publishes the draft. A failed upload therefore leaves a draft instead of a partial immutable release.

## Post-publish verification

| Check | Expected result |
|---|---|
| GitHub Actions | Release workflow is green |
| Release assets | Archive and `checksums.txt` are present |
| Provenance | Strict `gh attestation verify` succeeds for the downloaded archive |
| Release attestation | `gh release verify vX.Y.Z --repo Nanako0129/remora-cc` succeeds |
| Immutability | Release API reports `immutable: true` |
| Pinned runbook | Raw `vX.Y.Z/install/AGENT-INSTALL.md` URL returns the tagged content |
| Clean-room install | Pinned one-prompt flow installs and `remora doctor` passes |
| Native isolation | Clean-room `~/.claude` manifest is unchanged |

Do not move an existing release tag. If a published artifact or runbook is wrong, fix it in a new patch release so the reviewed tag remains immutable.

Download both assets, resolve the exact tagged commit through authenticated GitHub CLI, and require every attested identity constraint:

```bash
REPO=Nanako0129/remora-cc
TAG=vX.Y.Z
SOURCE_DIGEST=$(gh api "repos/$REPO/commits/$TAG" --jq .sha)
case "$SOURCE_DIGEST" in ''|*[!0-9A-Fa-f]*) exit 1 ;; esac
[ "${#SOURCE_DIGEST}" -eq 40 ]

gh attestation verify "remora-cc-X.Y.Z.tar.gz" \
  --repo "$REPO" \
  --signer-workflow "$REPO/.github/workflows/release.yml" \
  --source-ref "refs/tags/$TAG" \
  --source-digest "$SOURCE_DIGEST"
gh release verify "$TAG" --repo "$REPO"
test "$(gh api "repos/$REPO/releases/tags/$TAG" --jq .immutable)" = true
```

Run installation and native-isolation checks in a scrubbed temporary environment only after that real attestation verification. `REMORA_ALLOW_CHECKSUM_ONLY=1` below is limited to this install/isolation exercise; it makes no online or gateway claim.

```bash
REAL_HOME=$HOME
CLEAN_ROOT=$(mktemp -d)
mkdir -p "$CLEAN_ROOT/bin" "$CLEAN_ROOT/home/.claude" "$CLEAN_ROOT/xdg/runtime"
ln -s "$(command -v claude)" "$CLEAN_ROOT/bin/claude"
chmod 700 "$CLEAN_ROOT/xdg/runtime"
BOOTSTRAP="$CLEAN_ROOT/bootstrap.sh"
TAGGED_BOOTSTRAP="$CLEAN_ROOT/bootstrap.tagged.sh"
curl --proto "=https" --tlsv1.2 -fsSL \
  "https://raw.githubusercontent.com/Nanako0129/remora-cc/$TAG/bootstrap.sh" \
  --output "$BOOTSTRAP"
test "$(git rev-parse "${TAG}^{commit}")" = "$SOURCE_DIGEST"
git show "${TAG}:bootstrap.sh" > "$TAGGED_BOOTSTRAP"
cmp "$TAGGED_BOOTSTRAP" "$BOOTSTRAP"

manifest() {
  python3 - "$@" <<'PY'
import hashlib, os, pathlib, stat, sys

for name in sys.argv[1:]:
    root = pathlib.Path(name)
    paths = [root]
    if root.is_dir() and not root.is_symlink():
        paths.extend(sorted(root.rglob("*")))
    for path in paths:
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            print(f"{path}\tmissing\t-\t-")
            continue
        kind = "symlink" if stat.S_ISLNK(mode) else "file" if stat.S_ISREG(mode) else "directory" if stat.S_ISDIR(mode) else "other"
        target = os.readlink(path) if kind == "symlink" else "-"
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if kind == "file" else "-"
        print(f"{path}\t{kind}\t{target}\t{digest}")
PY
}

manifest "$REAL_HOME/.claude" "$REAL_HOME/.local/bin/remora" \
  "$REAL_HOME/.local/share/remora-cc" "$REAL_HOME/.config/remora-cc" \
  > "$CLEAN_ROOT/real.before"
manifest "$CLEAN_ROOT/home/.claude" > "$CLEAN_ROOT/temp-claude.before"

env -i \
  CLEAN_ROOT="$CLEAN_ROOT" \
  BOOTSTRAP="$BOOTSTRAP" \
  HOME="$CLEAN_ROOT/home" \
  REMORA_PREFIX="$CLEAN_ROOT/prefix" \
  XDG_CONFIG_HOME="$CLEAN_ROOT/xdg/config" \
  XDG_DATA_HOME="$CLEAN_ROOT/xdg/data" \
  XDG_STATE_HOME="$CLEAN_ROOT/xdg/state" \
  XDG_CACHE_HOME="$CLEAN_ROOT/xdg/cache" \
  XDG_RUNTIME_DIR="$CLEAN_ROOT/xdg/runtime" \
  CODEX_HOME="$CLEAN_ROOT/codex" \
  PATH="$CLEAN_ROOT/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  REMORA_VERSION=X.Y.Z \
  REMORA_ALLOW_CHECKSUM_ONLY=1 \
  REMORA_AUTH_TOKEN=clean-room-test-placeholder \
  sh -c '
    for target in "$BOOTSTRAP" "$HOME" "$REMORA_PREFIX" "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" \
      "$XDG_STATE_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR" "$CODEX_HOME"
    do
      case "$target" in "$CLEAN_ROOT"/*) ;; *) exit 1 ;; esac
    done
    test -f "$BOOTSTRAP"
    sh "$BOOTSTRAP"
    REMORA="$REMORA_PREFIX/bin/remora"
    case "$REMORA" in "$CLEAN_ROOT"/*) ;; *) exit 1 ;; esac
    test "$("$REMORA" version)" = "remora $REMORA_VERSION"
    "$REMORA" agents >/dev/null
    "$REMORA" dry-run >/dev/null
    "$REMORA" doctor
  '

manifest "$REAL_HOME/.claude" "$REAL_HOME/.local/bin/remora" \
  "$REAL_HOME/.local/share/remora-cc" "$REAL_HOME/.config/remora-cc" \
  > "$CLEAN_ROOT/real.after"
manifest "$CLEAN_ROOT/home/.claude" > "$CLEAN_ROOT/temp-claude.after"
cmp "$CLEAN_ROOT/real.before" "$CLEAN_ROOT/real.after"
cmp "$CLEAN_ROOT/temp-claude.before" "$CLEAN_ROOT/temp-claude.after"
```

Because `env -i` starts empty, no Remora/Codex configuration or token variables are inherited; `REMORA_AUTH_TOKEN` is a non-secret offline test placeholder. The manifest columns are path, file type, symlink target, and SHA-256 for regular files. This check never runs `doctor --online` and makes no gateway-reachability claim. Remove `CLEAN_ROOT` only after inspecting the installed temporary payload and both comparisons.
