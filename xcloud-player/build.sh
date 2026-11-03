#!/bin/bash
# Build the xCloud player page bundle into the plugin's launcher package.
# Output is committed with the source (the plugin build does not run node).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT="$HERE/../py_modules/unifideck/launcher/xcloud_player/web"
mkdir -p "$OUT"
npx --yes esbuild@0.25.0 "$HERE/src/entry.ts" --bundle --format=iife --target=chrome120 \
    --legal-comments=eof --outfile="$OUT/bundle.js" --log-level=warning
cp "$HERE/LICENSE-greenlight-player" "$OUT/LICENSE-greenlight-player"
echo "built $OUT/bundle.js"
