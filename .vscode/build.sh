#!/usr/bin/env bash
# Build the Unifideck plugin using Decky CLI
set -e

SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
CLI_LOCATION="$ROOT_DIR/cli"

cd "$ROOT_DIR"

# Ensure frontend is built
echo "Building frontend..."
pnpm run build

# Check if Decky CLI exists
if [ ! -f "$CLI_LOCATION/decky" ]; then
    echo "Decky CLI not found. Please run the 'setup' task first."
    exit 1
fi

# Build plugin with Decky CLI
echo "Building plugin with Decky CLI..."
"$CLI_LOCATION/decky" plugin build "$ROOT_DIR"

echo "Build complete! Plugin zip created in:"
ls -lh "$ROOT_DIR/out/"*.zip 2>/dev/null || echo "No zip files found in out/"
