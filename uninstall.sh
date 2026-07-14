#!/usr/bin/env sh
set -eu

PREFIX=${REMORA_PREFIX:-"$HOME/.local"}
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
STATE_HOME=${XDG_STATE_HOME:-"$HOME/.local/state"}
INSTALL_DIR="$DATA_HOME/remora-cc"
CONFIG_DIR="$CONFIG_HOME/remora-cc"
STATE_DIR="$STATE_HOME/remora-cc"
LINK="$PREFIX/bin/remora"
PURGE=0

if [ "${1:-}" = "--purge" ]; then
  PURGE=1
elif [ "$#" -gt 0 ]; then
  echo "usage: ./uninstall.sh [--purge]" >&2
  exit 2
fi

if [ -L "$LINK" ]; then
  TARGET=$(readlink "$LINK")
  case "$TARGET" in
    "$INSTALL_DIR"/*) rm "$LINK" ;;
    *) echo "Kept unrelated symlink: $LINK -> $TARGET" ;;
  esac
elif [ -e "$LINK" ]; then
  echo "Kept unrelated file: $LINK"
fi

rm -rf "$INSTALL_DIR" "$STATE_DIR/coralline"
# Remove the state root only when empty. If XDG_STATE_HOME and XDG_CONFIG_HOME
# alias, config.toml keeps this directory non-empty and therefore preserved.
rmdir "$STATE_DIR" 2>/dev/null || true
if [ "$PURGE" -eq 1 ]; then
  rm -rf "$CONFIG_DIR"
  echo "Removed remora and its configuration."
else
  echo "Removed remora; kept $CONFIG_DIR/config.toml"
fi
echo "Native claude configuration was not modified."
