#!/bin/bash

# Unifideck Plugin Build Script
# Automatically packages the plugin for Decky Loader installation

set -e  # Exit on any error

# Configuration
PROJECT_DIR="/home/deck/Documents/Projects/unifideck-main"
SOURCE_DIR="$PROJECT_DIR/unifideck-decky"
BUILD_DIR="$PROJECT_DIR/temp-build"
OUTPUT_DIR="$PROJECT_DIR"

# Get version number from argument or auto-increment
if [ -z "$1" ]; then
    # Find highest version number and increment
    LATEST=$(ls -1 "$OUTPUT_DIR"/unifideck-plugin-v*.zip 2>/dev/null | \
             sed 's/.*v\([0-9]*\)\.zip/\1/' | \
             sort -n | \
             tail -1)

    if [ -z "$LATEST" ]; then
        VERSION="1"
    else
        VERSION=$((LATEST + 1))
    fi
    echo "Auto-detected version: v$VERSION"
else
    VERSION="$1"
    echo "Using specified version: v$VERSION"
fi

OUTPUT_FILE="$OUTPUT_DIR/unifideck-plugin-v$VERSION.zip"

echo "========================================="
echo "Unifideck Plugin Build Script"
echo "========================================="
echo "Source: $SOURCE_DIR"
echo "Output: $OUTPUT_FILE"
echo ""

# Check if source directory exists
if [ ! -d "$SOURCE_DIR" ]; then
    echo "ERROR: Source directory not found: $SOURCE_DIR"
    exit 1
fi

# Always compile TypeScript frontend before packaging
echo "Compiling TypeScript frontend..."
cd "$SOURCE_DIR"
pnpm run build
if [ $? -ne 0 ]; then
    echo "ERROR: Frontend compilation failed"
    exit 1
fi
cd "$PROJECT_DIR"
echo "✓ Frontend compiled successfully"
echo ""

# Sync version from package.json to plugin.json
echo "Syncing version from package.json..."
VERSION_NUM=$(grep '"version"' "$SOURCE_DIR/package.json" | head -1 | sed 's/.*"version": "\([^"]*\)".*/\1/')
if [ -n "$VERSION_NUM" ]; then
    # Update plugin.json with version from package.json
    if grep -q '"version"' "$SOURCE_DIR/plugin.json"; then
        # Replace existing version
        sed -i "s/\"version\": \"[^\"]*\"/\"version\": \"$VERSION_NUM\"/" "$SOURCE_DIR/plugin.json"
    else
        # Add version after name field
        sed -i "/\"name\"/a\\  \"version\": \"$VERSION_NUM\"," "$SOURCE_DIR/plugin.json"
    fi
    echo "✓ Set plugin version to $VERSION_NUM"
else
    echo "WARNING: Could not extract version from package.json"
fi
echo ""

# Clean previous build artifacts
echo "Cleaning build directory..."
rm -rf "$BUILD_DIR"
mkdir -p "$BUILD_DIR/unifideck-decky"

# Copy files to build directory
echo "Copying files..."
cp -r "$SOURCE_DIR/lib" "$BUILD_DIR/unifideck-decky/"
cp -r "$SOURCE_DIR/dist" "$BUILD_DIR/unifideck-decky/"
cp -r "$SOURCE_DIR/bin" "$BUILD_DIR/unifideck-decky/"
cp -r "$SOURCE_DIR/defaults" "$BUILD_DIR/unifideck-decky/"
cp -r "$SOURCE_DIR/py_modules" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/main.py" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/plugin.json" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/package.json" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/vdf_utils.py" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/steamgriddb_client.py" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/download_manager.py" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/requirements.txt" "$BUILD_DIR/unifideck-decky/"
cp "$SOURCE_DIR/LICENSE" "$BUILD_DIR/unifideck-decky/"

# Verify critical files exist
echo "Verifying files..."
CRITICAL_FILES=(
    "main.py"
    "download_manager.py"
    "plugin.json"
    "dist/index.js"
    "lib/vdf/__init__.py"
    "lib/websockets/__init__.py"
    "lib/steamgrid/__init__.py"
    "bin/legendary"
    "bin/unifideck-launcher"
    "bin/unifideck-runner"
    "bin/umu/umu/umu-run"
    "bin/innoextract"
    "py_modules/pip/__init__.py"
    "steamgriddb_client.py"
    "vdf_utils.py"
)

for file in "${CRITICAL_FILES[@]}"; do
    if [ ! -e "$BUILD_DIR/unifideck-decky/$file" ]; then
        echo "ERROR: Missing critical file: $file"
        exit 1
    fi
done

# Check plugin.json has api_version
if ! grep -q '"api_version"' "$BUILD_DIR/unifideck-decky/plugin.json"; then
    echo "WARNING: plugin.json missing api_version field!"
    echo "This will prevent frontend from loading!"
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Create zip file
echo "Creating package..."
cd "$BUILD_DIR"
zip -r "$OUTPUT_FILE" unifideck-decky \
    -x "unifideck-decky/.git/*" \
    -x "unifideck-decky/__pycache__/*" \
    -x "unifideck-decky/node_modules/*" \
    -x "unifideck-decky/.gitignore" \
    -x "unifideck-decky/**/*.pyc" \
    -q

if [ $? -ne 0 ]; then
    echo "ERROR: Failed to create zip file"
    exit 1
fi

# Cleanup
cd "$PROJECT_DIR"
rm -rf "$BUILD_DIR"

# Display results
FILE_SIZE=$(ls -lh "$OUTPUT_FILE" | awk '{print $5}')
FILE_COUNT=$(unzip -l "$OUTPUT_FILE" | tail -1 | awk '{print $2}')

echo ""
echo "========================================="
echo "Build Complete!"
echo "========================================="
echo "Package: $OUTPUT_FILE"
echo "Size: $FILE_SIZE"
echo "Files: $FILE_COUNT"
echo ""
echo "To install:"
echo "1. QAM → Decky → Settings → Developer → Install from ZIP"
echo "2. Select: $OUTPUT_FILE"
echo "3. After install, restart Steam:"
echo "   killall steam && sleep 5 && steam &"
echo ""
echo "To verify contents:"
echo "   unzip -l '$OUTPUT_FILE' | less"
echo "========================================="
