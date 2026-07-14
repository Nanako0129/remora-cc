#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

mkdir -p "$TMP/.claude" "$TMP/bin"
touch "$TMP/.claude/settings.json"
printf '#!/bin/sh\nexit 0\n' > "$TMP/bin/claude"
chmod +x "$TMP/bin/claude"
BEFORE=$(find "$TMP/.claude" -type f -print | sort)
BEFORE_CONTENT=$(cksum "$TMP/.claude/settings.json")

PATH="$TMP/bin:$PATH" \
HOME="$TMP" \
REMORA_PREFIX="$TMP/.local" \
XDG_DATA_HOME="$TMP/.local/share" \
XDG_CONFIG_HOME="$TMP/.config" \
  "$ROOT/install.sh" >/dev/null

AFTER=$(find "$TMP/.claude" -type f -print | sort)
test "$BEFORE" = "$AFTER"
test "$BEFORE_CONTENT" = "$(cksum "$TMP/.claude/settings.json")"
test -L "$TMP/.local/bin/remora"
test -f "$TMP/.config/remora-cc/config.toml"

printf '#!/bin/sh\n' > "$TMP/unrelated-remora"
chmod +x "$TMP/unrelated-remora"
rm "$TMP/.local/bin/remora"
cp "$TMP/unrelated-remora" "$TMP/.local/bin/remora"
if PATH="$TMP/bin:$PATH" \
  HOME="$TMP" \
  REMORA_PREFIX="$TMP/.local" \
  XDG_DATA_HOME="$TMP/.local/share" \
  XDG_CONFIG_HOME="$TMP/.config" \
    "$ROOT/install.sh" >/dev/null 2>&1
then
  echo "installer replaced an unrelated executable" >&2
  exit 1
fi
rm "$TMP/.local/bin/remora"
ln -s "$TMP/.local/share/remora-cc/src/remora.py" "$TMP/.local/bin/remora"

PATH="$TMP/bin:$PATH" \
HOME="$TMP" \
XDG_CONFIG_HOME="$TMP/.config" \
REMORA_AUTH_TOKEN=test-only \
  "$TMP/.local/bin/remora" doctor >/dev/null

# A real launch prepares integration state outside native ~/.claude, then execs
# the fake claude binary. This catches runtime writes that install-only checks miss.
PATH="$TMP/bin:$PATH" \
HOME="$TMP" \
XDG_CONFIG_HOME="$TMP/.config" \
XDG_STATE_HOME="$TMP/.local/state" \
REMORA_AUTH_TOKEN=test-only \
  "$TMP/.local/bin/remora" --continue >/dev/null
find "$TMP/.local/state/remora-cc" -name 'config-*.conf' -type f | grep -q .
AFTER_LAUNCH=$(find "$TMP/.claude" -type f -print | sort)
test "$BEFORE" = "$AFTER_LAUNCH"
test "$BEFORE_CONTENT" = "$(cksum "$TMP/.claude/settings.json")"

HOME="$TMP" \
REMORA_PREFIX="$TMP/.local" \
XDG_DATA_HOME="$TMP/.local/share" \
XDG_CONFIG_HOME="$TMP/.config" \
XDG_STATE_HOME="$TMP/.local/state" \
  "$ROOT/uninstall.sh" >/dev/null

test ! -e "$TMP/.local/share/remora-cc"
test ! -e "$TMP/.local/state/remora-cc"
test -f "$TMP/.config/remora-cc/config.toml"

# If XDG state and config homes alias, default uninstall must remove only the
# known runtime subtree and preserve config.toml as promised.
mkdir -p "$TMP/alias/remora-cc/coralline" "$TMP/alias-data/remora-cc"
touch "$TMP/alias/remora-cc/config.toml" "$TMP/alias/remora-cc/coralline/runtime"
HOME="$TMP" \
REMORA_PREFIX="$TMP/alias-prefix" \
XDG_DATA_HOME="$TMP/alias-data" \
XDG_CONFIG_HOME="$TMP/alias" \
XDG_STATE_HOME="$TMP/alias" \
  "$ROOT/uninstall.sh" >/dev/null
test -f "$TMP/alias/remora-cc/config.toml"
test ! -e "$TMP/alias/remora-cc/coralline"

AFTER_UNINSTALL=$(find "$TMP/.claude" -type f -print | sort)
test "$BEFORE" = "$AFTER_UNINSTALL"
test "$BEFORE_CONTENT" = "$(cksum "$TMP/.claude/settings.json")"
