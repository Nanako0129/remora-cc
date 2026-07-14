#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(tr -d '[:space:]' < "$ROOT/VERSION")
DIST=${REMORA_DIST_DIR:-"$ROOT/dist"}
ARCHIVE="remora-cc-$VERSION.tar.gz"

case "$VERSION" in
  ''|*[!0-9A-Za-z.+-]*)
    echo "invalid VERSION: $VERSION" >&2
    exit 1
    ;;
esac

python3 - "$ROOT/src/remora.py" "$VERSION" <<'PY'
import importlib.util
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
expected = sys.argv[2]
spec = importlib.util.spec_from_file_location("remora_release_check", path)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
if module.VERSION != expected:
    raise SystemExit(f"VERSION mismatch: VERSION={expected}, src/remora.py={module.VERSION}")
PY

rm -rf "$DIST"
mkdir -p "$DIST/stage/remora-cc-$VERSION"

for path in \
  VERSION LICENSE README.md README.zh-TW.md SECURITY.md CONTRIBUTING.md CHANGELOG.md \
  Makefile RELEASING.md config.example.toml bootstrap.sh install.sh uninstall.sh \
  agents benchmarks bin docs install scripts src tests
do
  cp -R "$ROOT/$path" "$DIST/stage/remora-cc-$VERSION/"
done

rm -rf "$DIST/stage/remora-cc-$VERSION/__pycache__" \
  "$DIST/stage/remora-cc-$VERSION/src/__pycache__" \
  "$DIST/stage/remora-cc-$VERSION/tests/__pycache__"

COPYFILE_DISABLE=1 tar -czf "$DIST/$ARCHIVE" -C "$DIST/stage" "remora-cc-$VERSION"
rm -rf "$DIST/stage"

if command -v sha256sum >/dev/null 2>&1; then
  (cd "$DIST" && sha256sum "$ARCHIVE" > checksums.txt)
else
  (cd "$DIST" && shasum -a 256 "$ARCHIVE" > checksums.txt)
fi

printf '%s\n' "$DIST/$ARCHIVE" "$DIST/checksums.txt"
