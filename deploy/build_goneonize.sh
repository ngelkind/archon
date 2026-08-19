#!/usr/bin/env bash
# SECURITY.md hardening (M5): rebuild neonize's Go shared library from source
# and replace the prebuilt binary that ships in the wheel, removing all trust
# in the prebuilt blob. whatsmeow is hash-verified via go.sum by the Go
# toolchain itself.
#
# Run on the VM (Linux aarch64), after `uv sync`:
#   bash deploy/build_goneonize.sh
set -euo pipefail

NEONIZE_TAG="0.4.3.post0"
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

echo "Cloning neonize @ $NEONIZE_TAG ..."
git clone --depth 1 --branch "$NEONIZE_TAG" https://github.com/krypton-byte/neonize "$WORK/neonize" \
  || git clone --depth 1 https://github.com/krypton-byte/neonize "$WORK/neonize"

cd "$WORK/neonize/goneonize"
echo "Building shared library (this verifies go.sum hashes)..."
GOFLAGS=-mod=readonly go build -trimpath -buildmode=c-shared -o neonize.so main.go

SITE=$(cd /opt/archon/app && ~/.local/bin/uv run python -c \
  "import neonize, pathlib; print(pathlib.Path(neonize.__file__).parent)")
TARGET=$(ls "$SITE"/neonize-*.so 2>/dev/null | head -1)
if [ -z "$TARGET" ]; then
  echo "ERROR: could not find the bundled neonize .so in $SITE" >&2
  exit 1
fi
cp "$TARGET" "$TARGET.prebuilt.bak"
cp neonize.so "$TARGET"
echo "Replaced $TARGET with source-built library (backup: .prebuilt.bak)"
echo "Record this in SECURITY.md:"
sha256sum neonize.so
