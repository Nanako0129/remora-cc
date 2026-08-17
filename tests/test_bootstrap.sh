#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

mkdir -p "$TMP/bin" "$TMP/home/.claude"
touch "$TMP/bin/claude" "$TMP/home/.claude/settings.json"
chmod +x "$TMP/bin/claude"
ln -s "$(command -v python3)" "$TMP/bin/python3"
cat > "$TMP/bin/gh" <<'SH'
#!/usr/bin/env sh
set -eu

EXPECTED_REPO=Nanako0129/remora-cc
EXPECTED_TAG="v$REMORA_VERSION"
EXPECTED_WORKFLOW="$EXPECTED_REPO/.github/workflows/release.yml"
API_SOURCE_DIGEST=${FAKE_GH_API_SOURCE_DIGEST:-0123456789abcdef0123456789abcdef01234567}

case "$1" in
  api)
    [ "$#" -eq 4 ]
    [ "$2" = "repos/$EXPECTED_REPO/commits/$EXPECTED_TAG" ]
    [ "$3" = "--jq" ]
    [ "$4" = ".sha" ]
    printf '%s\n' "$API_SOURCE_DIGEST"
    ;;
  attestation)
    [ "$#" -eq 11 ]
    [ "$2" = "verify" ]
    case "$3" in
      */"remora-cc-$REMORA_VERSION.tar.gz") ;;
      *) exit 1 ;;
    esac
    [ "$4" = "--repo" ]
    [ "$5" = "$EXPECTED_REPO" ]
    [ "$6" = "--signer-workflow" ]
    [ "$7" = "${FAKE_GH_ATTESTED_SIGNER_WORKFLOW:-$EXPECTED_WORKFLOW}" ]
    [ "$8" = "--source-ref" ]
    [ "$9" = "${FAKE_GH_ATTESTED_SOURCE_REF:-refs/tags/$EXPECTED_TAG}" ]
    [ "${10}" = "--source-digest" ]
    [ "${11}" = "${FAKE_GH_ATTESTED_SOURCE_DIGEST:-$API_SOURCE_DIGEST}" ]
    ;;
  *) exit 1 ;;
esac
SH
chmod +x "$TMP/bin/gh"

python3 - "$ROOT/.github/workflows/release.yml" <<'PY'
import pathlib
import sys

workflow = pathlib.Path(sys.argv[1]).read_text()
assert "uses: actions/checkout@fbc6f3992d24b796d5a048ff273f7fcc4a7b6c09 # v5" in workflow
assert "uses: actions/attest-build-provenance@977bb373ede98d70efdf65b84cb5f73e068dcc2a # v3" in workflow
steps = [
    'gh release create "$GITHUB_REF_NAME"',
    'gh release upload "$GITHUB_REF_NAME"',
    'gh release edit "$GITHUB_REF_NAME" --draft=false --verify-tag',
]
positions = [workflow.index(step) for step in steps]
assert positions == sorted(positions)
assert workflow.index("--draft", positions[0], positions[1])
PY

REMORA_DIST_DIR="$TMP/release" "$ROOT/scripts/package-release.sh" >/dev/null
tar -tzf "$TMP/release/remora-cc-$VERSION.tar.gz" \
  | grep -qx "remora-cc-$VERSION/benchmarks/baton-compatibility/results.json"
tar -tzf "$TMP/release/remora-cc-$VERSION.tar.gz" \
  | grep -qx "remora-cc-$VERSION/agents/orchestration.md"
tar -tzf "$TMP/release/remora-cc-$VERSION.tar.gz" \
  | grep -qx "remora-cc-$VERSION/agents/agents.json"

PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
HOME="$TMP/home" \
REMORA_PREFIX="$TMP/home/.local" \
XDG_DATA_HOME="$TMP/home/.local/share" \
XDG_CONFIG_HOME="$TMP/home/.config" \
REMORA_VERSION="$VERSION" \
REMORA_RELEASE_BASE_URL="file://$TMP/release" \
  "$ROOT/bootstrap.sh" >/dev/null

test -L "$TMP/home/.local/bin/remora"
test -f "$TMP/home/.config/remora-cc/config.toml"
test "$(HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/home/.config" "$TMP/home/.local/bin/remora" version)" = "remora $VERSION"
grep -Fq 'Blocker:' "$TMP/home/.local/share/remora-cc/agents/agents.json"
grep -Fq 'After two automatic `REVISE` verdicts in one readiness-unit epoch, stop resubmitting' \
  "$TMP/home/.local/share/remora-cc/agents/orchestration.md"
test "$(find "$TMP/home/.claude" -type f -print | sort)" = "$TMP/home/.claude/settings.json"

if FAKE_GH_ATTESTED_SIGNER_WORKFLOW=attacker/example/.github/workflows/release.yml \
  PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  HOME="$TMP/home" \
  REMORA_PREFIX="$TMP/home/.local" \
  XDG_DATA_HOME="$TMP/home/.local/share" \
  XDG_CONFIG_HOME="$TMP/home/.config" \
  REMORA_VERSION="$VERSION" \
  REMORA_RELEASE_BASE_URL="file://$TMP/release" \
    "$ROOT/bootstrap.sh" >/dev/null 2>&1
then
  echo "bootstrap accepted the wrong signer workflow" >&2
  exit 1
fi

if FAKE_GH_ATTESTED_SOURCE_REF=refs/heads/main \
  PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  HOME="$TMP/home" \
  REMORA_PREFIX="$TMP/home/.local" \
  XDG_DATA_HOME="$TMP/home/.local/share" \
  XDG_CONFIG_HOME="$TMP/home/.config" \
  REMORA_VERSION="$VERSION" \
  REMORA_RELEASE_BASE_URL="file://$TMP/release" \
    "$ROOT/bootstrap.sh" >/dev/null 2>&1
then
  echo "bootstrap accepted the wrong source ref" >&2
  exit 1
fi

if FAKE_GH_ATTESTED_SOURCE_DIGEST=fedcba9876543210fedcba9876543210fedcba98 \
  PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  HOME="$TMP/home" \
  REMORA_PREFIX="$TMP/home/.local" \
  XDG_DATA_HOME="$TMP/home/.local/share" \
  XDG_CONFIG_HOME="$TMP/home/.config" \
  REMORA_VERSION="$VERSION" \
  REMORA_RELEASE_BASE_URL="file://$TMP/release" \
    "$ROOT/bootstrap.sh" >/dev/null 2>&1
then
  echo "bootstrap accepted the wrong source digest" >&2
  exit 1
fi

cp "$TMP/release/checksums.txt" "$TMP/release/checksums.good"
printf '%064d  remora-cc-%s.tar.gz\n' 0 "$VERSION" > "$TMP/release/checksums.txt"
if PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  HOME="$TMP/home" \
  REMORA_PREFIX="$TMP/home/.local" \
  XDG_DATA_HOME="$TMP/home/.local/share" \
  XDG_CONFIG_HOME="$TMP/home/.config" \
  REMORA_VERSION="$VERSION" \
  REMORA_RELEASE_BASE_URL="file://$TMP/release" \
  REMORA_ALLOW_CHECKSUM_ONLY=1 \
    "$ROOT/bootstrap.sh" >/dev/null 2>&1
then
  echo "bootstrap accepted a mismatched checksum" >&2
  exit 1
fi
mv "$TMP/release/checksums.good" "$TMP/release/checksums.txt"

python3 - "$TMP/release/remora-cc-$VERSION.tar.gz" <<'PY'
import io
import sys
import tarfile

with tarfile.open(sys.argv[1], "w:gz") as bundle:
    payload = b"must not escape"
    member = tarfile.TarInfo("../escape")
    member.size = len(payload)
    bundle.addfile(member, io.BytesIO(payload))
PY
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$TMP/release" && sha256sum "remora-cc-$VERSION.tar.gz" > checksums.txt)
else
  (cd "$TMP/release" && shasum -a 256 "remora-cc-$VERSION.tar.gz" > checksums.txt)
fi

if PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
  HOME="$TMP/home" \
  REMORA_PREFIX="$TMP/home/.local" \
  XDG_DATA_HOME="$TMP/home/.local/share" \
  XDG_CONFIG_HOME="$TMP/home/.config" \
  REMORA_VERSION="$VERSION" \
  REMORA_RELEASE_BASE_URL="file://$TMP/release" \
  REMORA_ALLOW_CHECKSUM_ONLY=1 \
    "$ROOT/bootstrap.sh" >/dev/null 2>&1
then
  echo "bootstrap accepted an unsafe archive path" >&2
  exit 1
fi
test ! -e "$TMP/escape"
