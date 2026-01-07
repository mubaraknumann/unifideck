#!/usr/bin/env bash
CLI_LOCATION="$(pwd)/cli"
echo "Building plugin in $(pwd)"

if ! test -f "$CLI_LOCATION/decky"; then
    echo "Decky CLI tool not found. Please run the setup task first."
    exit 1
fi

echo "Building plugin..."
$CLI_LOCATION/decky plugin build $(pwd)
