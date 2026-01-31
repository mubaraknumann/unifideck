#!/usr/bin/env bash
set -euo pipefail

CLI_LOCATION="$(pwd)/cli"
echo "Building plugin in $(pwd)"

# Prefer a non-sudo build for local dev.
# If your environment requires elevated permissions (e.g., Docker socket), run this script manually with sudo.
"$CLI_LOCATION/decky" plugin build "$(pwd)" || {
  echo "Build failed without sudo. If this is due to permissions (e.g. Docker), rerun: sudo -E $CLI_LOCATION/decky plugin build $(pwd)" >&2
  exit 1
}
