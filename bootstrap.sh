#!/usr/bin/env sh
set -eu

REPO=${REMORA_REPO:-Nanako0129/remora-cc}
ALLOW_CHECKSUM_ONLY=${REMORA_ALLOW_CHECKSUM_ONLY:-0}
VERSION=${REMORA_VERSION:-}

log() {
  printf '%s\n' "$*" >&2
}

fail() {
  log "remora bootstrap: $*"
  exit 1
}

require() {
  command -v "$1" >/dev/null 2>&1 || fail "missing required command: $1"
}

require curl
require python3
require tar

if [ -z "$VERSION" ]; then
  METADATA=$(curl --fail --silent --show-error --location \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$REPO/releases/latest") || \
    fail "could not resolve the latest release"
  VERSION=$(printf '%s' "$METADATA" | python3 -c \
    'import json,sys; print(json.load(sys.stdin).get("tag_name", "").removeprefix("v"))')
fi

case "$VERSION" in
  ''|*[!0-9A-Za-z.+-]*) fail "invalid release version: $VERSION" ;;
esac

TAG="v$VERSION"
ARCHIVE="remora-cc-$VERSION.tar.gz"
BASE_URL=${REMORA_RELEASE_BASE_URL:-"https://github.com/$REPO/releases/download/$TAG"}
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

log "Downloading remora $VERSION"
curl --fail --silent --show-error --location \
  "$BASE_URL/$ARCHIVE" -o "$TMP/$ARCHIVE" || fail "archive download failed"
curl --fail --silent --show-error --location \
  "$BASE_URL/checksums.txt" -o "$TMP/checksums.txt" || fail "checksum download failed"

EXPECTED=$(awk -v name="$ARCHIVE" '$2 == name {print $1}' "$TMP/checksums.txt")
case "$EXPECTED" in
  [0-9A-Fa-f][0-9A-Fa-f]*) ;;
  *) fail "release checksum does not contain $ARCHIVE" ;;
esac
[ "${#EXPECTED}" -eq 64 ] || fail "invalid SHA-256 in checksums.txt"

if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL=$(sha256sum "$TMP/$ARCHIVE" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  ACTUAL=$(shasum -a 256 "$TMP/$ARCHIVE" | awk '{print $1}')
else
  fail "sha256sum or shasum is required"
fi
[ "$EXPECTED" = "$ACTUAL" ] || fail "SHA-256 verification failed"
log "Verified SHA-256: $ACTUAL"

if [ "$ALLOW_CHECKSUM_ONLY" = "1" ]; then
  log "WARNING: proceeding with checksum verification only by explicit override"
elif command -v gh >/dev/null 2>&1; then
  gh attestation verify "$TMP/$ARCHIVE" --repo "$REPO" >/dev/null || \
    fail "GitHub artifact attestation verification failed"
  log "Verified GitHub artifact attestation for $REPO"
else
  fail "GitHub CLI is required for provenance verification; install gh or explicitly set REMORA_ALLOW_CHECKSUM_ONLY=1"
fi

mkdir "$TMP/extracted"
python3 - "$TMP/$ARCHIVE" "remora-cc-$VERSION" <<'PY'
import pathlib
import sys
import tarfile

archive = sys.argv[1]
root = sys.argv[2]
with tarfile.open(archive, "r:gz") as bundle:
    for member in bundle.getmembers():
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != root:
            raise SystemExit(f"unsafe archive member: {member.name}")
        if member.issym() or member.islnk():
            raise SystemExit(f"archive links are not allowed: {member.name}")
PY
tar -xzf "$TMP/$ARCHIVE" -C "$TMP/extracted"
SOURCE="$TMP/extracted/remora-cc-$VERSION"
[ -x "$SOURCE/install.sh" ] || fail "verified archive has an unexpected layout"

log "Installing verified remora $VERSION"
"$SOURCE/install.sh"
log "remora $VERSION installation complete"
