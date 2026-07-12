#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
PREFIX=${REMORA_PREFIX:-"$HOME/.local"}
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
INSTALL_DIR="$DATA_HOME/remora-cc"
CONFIG_DIR="$CONFIG_HOME/remora-cc"
BIN_DIR="$PREFIX/bin"
LINK="$BIN_DIR/remora"

fail() {
  echo "remora install: $*" >&2
  exit 1
}

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "remora requires Python 3.11 or newer." >&2
  exit 1
}
command -v claude >/dev/null 2>&1 || {
  echo "Claude Code is not on PATH. Install it before remora." >&2
  exit 1
}

if [ -L "$INSTALL_DIR" ]; then
  fail "refusing to replace symlinked install directory: $INSTALL_DIR"
fi
if [ -e "$LINK" ] || [ -L "$LINK" ]; then
  if [ ! -L "$LINK" ]; then
    fail "refusing to replace unrelated executable: $LINK"
  fi
  CURRENT_TARGET=$(readlink "$LINK")
  case "$CURRENT_TARGET" in
    "$INSTALL_DIR"/*) ;;
    *) fail "refusing to replace unrelated symlink: $LINK -> $CURRENT_TARGET" ;;
  esac
fi

mkdir -p "$DATA_HOME" "$CONFIG_DIR" "$BIN_DIR"
rm -rf "$INSTALL_DIR.new"
mkdir -p "$INSTALL_DIR.new"
cp -R "$ROOT/bin" "$ROOT/src" "$ROOT/agents" "$ROOT/docs" "$ROOT/LICENSE" \
  "$ROOT/README.md" "$ROOT/README.zh-TW.md" "$ROOT/SECURITY.md" \
  "$ROOT/CHANGELOG.md" "$ROOT/config.example.toml" "$ROOT/uninstall.sh" \
  "$INSTALL_DIR.new/"
OLD_DIR="$INSTALL_DIR.old.$$"
rm -rf "$OLD_DIR"
if [ -d "$INSTALL_DIR" ]; then
  mv "$INSTALL_DIR" "$OLD_DIR"
fi
if ! mv "$INSTALL_DIR.new" "$INSTALL_DIR"; then
  [ ! -d "$OLD_DIR" ] || mv "$OLD_DIR" "$INSTALL_DIR"
  fail "atomic installation failed; previous installation restored"
fi
rm -rf "$OLD_DIR"

if [ ! -f "$CONFIG_DIR/config.toml" ]; then
  cp "$ROOT/config.example.toml" "$CONFIG_DIR/config.toml"
  echo "Created $CONFIG_DIR/config.toml"
else
  echo "Kept existing $CONFIG_DIR/config.toml"
fi

chmod +x "$INSTALL_DIR/bin/remora" "$INSTALL_DIR/src/remora.py" "$INSTALL_DIR/uninstall.sh"
[ ! -e "$LINK.new" ] && [ ! -L "$LINK.new" ] || fail "temporary launcher path already exists: $LINK.new"
ln -s "$INSTALL_DIR/src/remora.py" "$LINK.new"
mv -f "$LINK.new" "$LINK"

echo "Installed remora in $INSTALL_DIR"
echo "Launcher: $LINK"
echo "Native claude configuration was not modified."
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Add $BIN_DIR to PATH before running remora." ;;
esac
