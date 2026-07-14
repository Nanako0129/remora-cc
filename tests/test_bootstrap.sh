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

REMORA_DIST_DIR="$TMP/release" "$ROOT/scripts/package-release.sh" >/dev/null
tar -tzf "$TMP/release/remora-cc-$VERSION.tar.gz" \
  | grep -qx "remora-cc-$VERSION/benchmarks/baton-compatibility/results.json"

PATH="$TMP/bin:/usr/bin:/bin:/usr/sbin:/sbin" \
HOME="$TMP/home" \
REMORA_PREFIX="$TMP/home/.local" \
XDG_DATA_HOME="$TMP/home/.local/share" \
XDG_CONFIG_HOME="$TMP/home/.config" \
REMORA_VERSION="$VERSION" \
REMORA_RELEASE_BASE_URL="file://$TMP/release" \
REMORA_ALLOW_CHECKSUM_ONLY=1 \
  "$ROOT/bootstrap.sh" >/dev/null

test -L "$TMP/home/.local/bin/remora"
test -f "$TMP/home/.config/remora-cc/config.toml"
test "$(HOME="$TMP/home" XDG_CONFIG_HOME="$TMP/home/.config" "$TMP/home/.local/bin/remora" version)" = "remora $VERSION"
test "$(find "$TMP/home/.claude" -type f -print | sort)" = "$TMP/home/.claude/settings.json"

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
