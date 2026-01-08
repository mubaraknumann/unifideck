#!/usr/bin/env bash
# Delegate to the main build script which handles permissions, versioning, and dependencies correctly
SCRIPT_DIR="$(dirname "$(readlink -f "$0")")"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# Execute the main build script in dev mode
cd "$ROOT_DIR"
chmod +x build-plugin.sh
./build-plugin.sh dev
