#!/usr/bin/env bash
# Unifideck Plugin Build Script
# Compatible with Decky CLI approach, with local fallback for Steam Deck

set -e  # Exit on any error

# ============================================================
# Configuration
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_LOCATION="$SCRIPT_DIR/cli"
OUTPUT_DIR="$SCRIPT_DIR/out"

# ============================================================
# Helper Functions & Colors
# ============================================================
# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[✓]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# ============================================================
# Environment Setup & Argument Parsing
# ============================================================
ENV_MODE="${1:-dev}" # Default to dev
PACKAGE_VERSION=$(grep '"version"' "$SCRIPT_DIR/package.json" | head -1 | sed 's/.*"version": "\([^"]*\)".*/\1/')

if [[ "$ENV_MODE" == "prod" ]]; then
    VERSION_TAG="v$PACKAGE_VERSION"
    ZIP_NAME="unifideck.prod.$VERSION_TAG.zip"
    PLUGIN_VERSION="$PACKAGE_VERSION"
    log_info "Building in PRODUCTION mode ($VERSION_TAG)"
    
elif [[ "$ENV_MODE" == "dev" ]]; then
    # Auto-increment dev version based on existing files in output dir
    mkdir -p "$OUTPUT_DIR"
    LATEST_DEV=$(ls -1 "$OUTPUT_DIR"/unifideck.dev.v*.zip 2>/dev/null | \
        sed 's/.*unifideck\.dev\.v\([0-9]*\)\.zip/\1/' | \
        sort -n | \
        tail -1)
    
    if [ -z "$LATEST_DEV" ]; then
        DEV_VER="1"
    else
        DEV_VER=$((LATEST_DEV + 1))
    fi
    
    VERSION_TAG="v$DEV_VER"
    ZIP_NAME="unifideck.dev.$VERSION_TAG.zip"
    # For dev builds, we might want to append a build number or keep semantic version
    # keeping semantic version in plugin.json is safer for loader compat
    PLUGIN_VERSION="$PACKAGE_VERSION-dev$DEV_VER"
    
    log_info "Building in DEVELOPMENT mode ($VERSION_TAG)"
else
    log_error "Unknown mode: $ENV_MODE. Use 'dev' or 'prod'."
    exit 1
fi

OUTPUT_FILE="$OUTPUT_DIR/$ZIP_NAME"

echo "========================================="
echo "Unifideck Plugin Build Script"
echo "========================================="
echo "Building plugin in $SCRIPT_DIR"
echo "Target: $OUTPUT_FILE"
echo ""

# ============================================================
# Pre-build: Download/Update Bundled Binaries
# ============================================================
prebuild_binaries() {
    log_info "Running pre-build tasks..."
    
    # --- Download Patched Legendary binary ---
    log_info "Checking Legendary binary..."
    LEGENDARY_URL="https://github.com/Heroic-Games-Launcher/legendary/releases/download/0.20.38/legendary_linux_x86_64"
    LEGENDARY_BIN="$SCRIPT_DIR/bin/legendary"
    if curl -sL "$LEGENDARY_URL" -o "$LEGENDARY_BIN.new"; then
        chmod +x "$LEGENDARY_BIN.new"
        if "$LEGENDARY_BIN.new" --version >/dev/null 2>&1; then
            mv "$LEGENDARY_BIN.new" "$LEGENDARY_BIN"
            log_success "Downloaded Legendary ($("$LEGENDARY_BIN" --version | head -1))"
        else
            rm -f "$LEGENDARY_BIN.new"
            log_warn "Downloaded Legendary binary invalid, keeping existing"
        fi
    else
        log_warn "Failed to download Legendary, keeping existing"
    fi

    # --- Download heroic-gogdl binary ---
    log_info "Checking gogdl binary..."
    GOGDL_VERSION="1.1.2"
    GOGDL_URL="https://github.com/Heroic-Games-Launcher/heroic-gogdl/releases/download/v${GOGDL_VERSION}/gogdl_linux_x86_64"
    GOGDL_BIN="$SCRIPT_DIR/bin/gogdl"
    if curl -sL "$GOGDL_URL" -o "$GOGDL_BIN.new"; then
        chmod +x "$GOGDL_BIN.new"
        if "$GOGDL_BIN.new" --version --auth-config-path /dev/null >/dev/null 2>&1; then
            mv "$GOGDL_BIN.new" "$GOGDL_BIN"
            log_success "Downloaded gogdl v${GOGDL_VERSION}"
        else
            rm -f "$GOGDL_BIN.new"
            log_warn "Downloaded gogdl binary invalid, keeping existing"
        fi
    else
        log_warn "Failed to download gogdl, keeping existing"
    fi

    # --- Download Winetricks ---
    log_info "Checking Winetricks..."
    WINETRICKS_URL="https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks"
    WINETRICKS_BIN="$SCRIPT_DIR/bin/winetricks"
    if curl -sL "$WINETRICKS_URL" -o "$WINETRICKS_BIN.new"; then
        chmod +x "$WINETRICKS_BIN.new"
        if grep -q "WINETRICKS_VERSION" "$WINETRICKS_BIN.new"; then
            mv "$WINETRICKS_BIN.new" "$WINETRICKS_BIN"
            log_success "Downloaded Winetricks"
        else
            rm -f "$WINETRICKS_BIN.new"
            log_warn "Downloaded Winetricks invalid, keeping existing"
        fi
    else
        log_warn "Failed to download Winetricks, keeping existing"
    fi

    # --- Ensure cabextract is present ---
    CABEXTRACT_BIN="$SCRIPT_DIR/bin/cabextract"
    if [ ! -f "$CABEXTRACT_BIN" ]; then
        log_warn "cabextract binary missing in bin/!"
        SYSTEM_CAB=$(which cabextract 2>/dev/null || true)
        if [ -n "$SYSTEM_CAB" ] && [ -f "$SYSTEM_CAB" ]; then
            cp "$SYSTEM_CAB" "$CABEXTRACT_BIN"
            log_success "Copied system cabextract to bin/"
        else
            log_error "Could not find cabextract. Please install it or copy a static binary to bin/cabextract."
        fi
    else
        log_success "cabextract binary present"
    fi

    echo ""
}

# ============================================================
# Pre-build: Sync Version
# ============================================================
sync_version() {
    # Version is now managed manually in plugin.json - no longer auto-updated
    log_info "Using version from plugin.json (no auto-sync)"
    PLUGIN_VERSION=$(grep '"version"' "$SCRIPT_DIR/plugin.json" | head -1 | sed 's/.*"version": "\([^"]*\)".*/\1/')
    log_success "Plugin version: $PLUGIN_VERSION"
    echo ""
}
# ============================================================
# Detect OS and Architecture
# ============================================================
get_decky_cli_url() {
    local os_type=""
    local arch_type=""
    
    # Detect OS
    case "$(uname -s)" in
        Linux*)     os_type="linux" ;;
        Darwin*)    os_type="darwin" ;;
        CYGWIN*|MINGW*|MSYS*) os_type="windows" ;;
        *)          os_type="unknown" ;;
    esac
    
    # Detect architecture
    case "$(uname -m)" in
        x86_64|amd64)   arch_type="x64" ;;
        arm64|aarch64)  arch_type="arm64" ;;
        *)              arch_type="x64" ;;  # Default to x64
    esac
    
    # Construct download URL
    local base_url="https://github.com/SteamDeckHomebrew/cli/releases/latest/download"
    
    if [ "$os_type" = "windows" ]; then
        echo "${base_url}/decky-${os_type}-${arch_type}.exe"
    else
        echo "${base_url}/decky-${os_type}-${arch_type}.tar.gz"
    fi
}

# ============================================================
# Check for Decky CLI (with auto-download)
# ============================================================
check_decky_cli() {
    local cli_binary="$CLI_LOCATION/decky"
    
    # Check if CLI exists and is executable for this platform
    if test -f "$cli_binary"; then
        # Verify the binary is compatible with this OS
        if "$cli_binary" --version >/dev/null 2>&1; then
            return 0
        else
            log_warn "Decky CLI exists but is incompatible with this OS/architecture"
            log_info "Will attempt to download correct version..."
            rm -f "$cli_binary"
        fi
    fi
    
    # CLI doesn't exist or is incompatible - try to download
    log_info "Downloading Decky CLI for $(uname -s) $(uname -m)..."
    
    local download_url
    download_url=$(get_decky_cli_url)
    
    mkdir -p "$CLI_LOCATION"
    
    if [[ "$download_url" == *.exe ]]; then
        # Windows binary
        if curl -sL "$download_url" -o "$cli_binary.exe"; then
            log_success "Downloaded Decky CLI for Windows"
            return 0
        fi
    else
        # Linux/macOS tarball
        if curl -sL "$download_url" -o "$CLI_LOCATION/decky.tar.gz"; then
            cd "$CLI_LOCATION"
            tar -xzf decky.tar.gz 2>/dev/null
            rm -f decky.tar.gz
            chmod +x decky 2>/dev/null || true
            cd "$SCRIPT_DIR"
            
            if test -f "$cli_binary" && "$cli_binary" --version >/dev/null 2>&1; then
                log_success "Downloaded Decky CLI ($(\"$cli_binary\" --version 2>/dev/null | head -1 || echo 'unknown version'))"
                return 0
            fi
        fi
    fi
    
    log_warn "Could not download Decky CLI - will use local build method"
    return 1
}

# ============================================================
# Check for Docker/Podman
# ============================================================
check_container_engine() {
    if command -v docker &>/dev/null; then
        if docker info &>/dev/null 2>&1; then
            echo "docker"
            return 0
        fi
    fi
    if command -v podman &>/dev/null; then
        if podman info &>/dev/null 2>&1; then
            echo "podman"
            return 0
        fi
    fi
    return 1
}

# ============================================================
# Build with Decky CLI (Docker/Podman)
# ============================================================
build_with_cli() {
    local engine="$1"
    log_info "Building with Decky CLI using $engine..."
    
    # Clean output file if exists
    rm -f "$OUTPUT_FILE"
    
    # Clean dist folder to avoid permission conflicts between local/container builds
    if [ -d "$SCRIPT_DIR/dist" ]; then
        log_info "Cleaning dist folder..."
        # Try normal remove first, if fails use container to remove (for root-owned files)
        rm -rf "$SCRIPT_DIR/dist" 2>/dev/null || \
            "$engine" run --rm -v "$SCRIPT_DIR":/v -w /v alpine rm -rf dist
    fi
    
    # Create a clean staging directory with only required files
    log_info "Creating clean build staging directory..."
    STAGING_DIR=$(mktemp -d)
    STAGING_PLUGIN="$STAGING_DIR/unifideck-staging"
    mkdir -p "$STAGING_PLUGIN"
    
    # Copy only required directories
    cp -r "$SCRIPT_DIR/backend" "$STAGING_PLUGIN/"
    cp -r "$SCRIPT_DIR/bin" "$STAGING_PLUGIN/"
    cp -r "$SCRIPT_DIR/defaults" "$STAGING_PLUGIN/"
    cp -r "$SCRIPT_DIR/py_modules" "$STAGING_PLUGIN/"
    cp -r "$SCRIPT_DIR/src" "$STAGING_PLUGIN/"
    cp -r "$SCRIPT_DIR/assets" "$STAGING_PLUGIN/" 2>/dev/null || true
    
    # Copy required config files
    cp "$SCRIPT_DIR/main.py" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/plugin.json" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/package.json" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/pnpm-lock.yaml" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/tsconfig.json" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/rollup.config.mjs" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/vdf_utils.py" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/steamgriddb_client.py" "$STAGING_PLUGIN/"
    # Note: download_manager.py is now in defaults/backend/download/manager.py (included via defaults/ copy above)
    cp "$SCRIPT_DIR/cloud_save_manager.py" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/steam_user_utils.py" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/launch_options_parser.py" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/requirements.txt" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/LICENSE.txt" "$STAGING_PLUGIN/"
    cp "$SCRIPT_DIR/README.md" "$STAGING_PLUGIN/"
    
    log_success "Staged clean build directory (excluding debug/test files)"
    
    # Fix permissions so container can read all files
    log_info "Fixing permissions for container access..."
    chmod -R a+rX "$STAGING_PLUGIN" 2>/dev/null || true
    
    mkdir -p "$OUTPUT_DIR"
    
    "$CLI_LOCATION/decky" plugin build "$STAGING_PLUGIN" \
        --output-path "$OUTPUT_DIR" \
        --engine "$engine" \
        --follow-symlinks \
        --build-as-root
    
    # Cleanup staging directory
    rm -rf "$STAGING_DIR"
    
    # Rename output file to our desired versioned name
    # Decky CLI outputs as {plugin-name}.zip based on plugin.json "name" field
    EXPECTED_CLI_OUTPUT="$OUTPUT_DIR/Unifideck.zip"
    
    if [ -f "$EXPECTED_CLI_OUTPUT" ] && [ "$EXPECTED_CLI_OUTPUT" != "$OUTPUT_FILE" ]; then
        mv "$EXPECTED_CLI_OUTPUT" "$OUTPUT_FILE"
        log_success "Renamed Unifideck.zip -> $ZIP_NAME"
    elif [ -f "$OUTPUT_FILE" ]; then
        log_success "Build output already at $ZIP_NAME"
    else
        log_warn "Expected output file not found: Unifideck.zip"
    fi
    
    log_success "Build complete! Output: $OUTPUT_FILE"
}

# ============================================================
# Local Build (No Docker - Steam Deck fallback)
# ============================================================
build_local() {
    log_info "Building locally (no container engine available)..."
    
    # Build frontend first
    log_info "Compiling TypeScript frontend..."
    cd "$SCRIPT_DIR"
    if ! pnpm run build; then
        log_error "Frontend compilation failed"
        exit 1
    fi
    log_success "Frontend compiled successfully"
    
    # Create output directory
    mkdir -p "$OUTPUT_DIR"
    
    # Create temp build directory
    BUILD_DIR=$(mktemp -d)
    # Use "Unifideck" folder name to match Decky CLI output format
    PLUGIN_DIR="$BUILD_DIR/Unifideck"
    mkdir -p "$PLUGIN_DIR"
    
    log_info "Copying files..."
    
    # Copy required directories
    cp -r "$SCRIPT_DIR/backend" "$PLUGIN_DIR/"
    cp -r "$SCRIPT_DIR/dist" "$PLUGIN_DIR/"
    cp -r "$SCRIPT_DIR/bin" "$PLUGIN_DIR/"
    cp -r "$SCRIPT_DIR/defaults" "$PLUGIN_DIR/"
    cp -r "$SCRIPT_DIR/py_modules" "$PLUGIN_DIR/"
    
    # Copy required files
    cp "$SCRIPT_DIR/main.py" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/plugin.json" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/package.json" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/vdf_utils.py" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/steamgriddb_client.py" "$PLUGIN_DIR/"
    # Note: download_manager.py is now in defaults/backend/download/manager.py (included via defaults/ copy above)
    cp "$SCRIPT_DIR/cloud_save_manager.py" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/steam_user_utils.py" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/launch_options_parser.py" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/requirements.txt" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/LICENSE.txt" "$PLUGIN_DIR/"
    cp "$SCRIPT_DIR/README.md" "$PLUGIN_DIR/"
    
    # Verify critical files
    log_info "Verifying critical files..."
    CRITICAL_FILES=(
        "main.py"
        "backend/download/manager.py"
        "plugin.json"
        "dist/index.js"
        "py_modules/vdf/__init__.py"
        "py_modules/websockets/__init__.py"
        "py_modules/steamgrid/__init__.py"
        "bin/legendary"
        "bin/gogdl"
        "bin/winetricks"
        "bin/cabextract"
        "bin/unifideck-launcher"
        "bin/unifideck-runner"
        "bin/umu/umu/umu-run"
        "bin/innoextract"
        "bin/EpicGamesLauncher.exe"
        "bin/fix_epic_launcher_prefix.py"
        "bin/cloud_save_sync.py"
        "bin/game_fixes.py"
        "bin/winetricks_installer.py"
        "bin/prefetch_winetricks.py"
        "bin/winetricks_gog.py"
        "bin/umu_lookup.py"
        "bin/galaxy_stub.py"
        "bin/gog_set_language.py"
        "bin/stubs/GalaxyCommunication.exe"
        "py_modules/pip/__init__.py"
        "steamgriddb_client.py"
        "steam_user_utils.py"
        "launch_options_parser.py"
        "vdf_utils.py"
    )

    for file in "${CRITICAL_FILES[@]}"; do
        if [ ! -e "$PLUGIN_DIR/$file" ]; then
            log_error "Missing critical file: $file"
            rm -rf "$BUILD_DIR"
            exit 1
        fi
    done
    log_success "All critical files present"
    
    # Set permissions on binaries
    log_info "Setting permissions..."
    find "$PLUGIN_DIR/bin" -type f -exec chmod +x {} \; 2>/dev/null || true
    
    # Check plugin.json has api_version
    if ! grep -q '"api_version"' "$PLUGIN_DIR/plugin.json"; then
        log_warn "plugin.json missing api_version field!"
        log_warn "This may prevent frontend from loading!"
    fi
     
    # Create zip file
    log_info "Creating package..."
    cd "$BUILD_DIR"
    zip -r "$OUTPUT_FILE" Unifideck \
        -x "Unifideck/.git/*" \
        -x "Unifideck/__pycache__/*" \
        -x "Unifideck/node_modules/*" \
        -x "Unifideck/.gitignore" \
        -x "Unifideck/**/*.pyc" \
        -x "Unifideck/**/__pycache__/*" \
        -x "Unifideck/debug_*.py" \
        -x "Unifideck/test_*.py" \
        -x "Unifideck/verify_*.py" \
        -x "Unifideck/read_user_json.py" \
        -x "Unifideck/compat_cache.py" \
        -x "Unifideck/decky.pyi" \
        -x "Unifideck/*.backup" \
        -x "Unifideck/vc_redist.x64.exe" \
        -x "Unifideck/proton-compatibility.md" \
        -x "Unifideck/tests/*" \
        -x "Unifideck/test_cloud_saves/*" \
        -q
    
    if [ $? -ne 0 ]; then
        log_error "Failed to create zip file"
        rm -rf "$BUILD_DIR"
        exit 1
    fi
    
    # Cleanup temp directory
    cd "$SCRIPT_DIR"
    rm -rf "$BUILD_DIR"
    
    # Display results
    FILE_SIZE=$(ls -lh "$OUTPUT_FILE" | awk '{print $5}')
    
    echo ""
    echo "========================================="
    log_success "Build Complete!"
    echo "========================================="
    echo "Mode:    $ENV_MODE"
    echo "Package: $OUTPUT_FILE"
    echo "Version: $PLUGIN_VERSION"
    echo "Size:    $FILE_SIZE"
    echo ""
    echo "To install:"
    echo "  1. QAM → Decky → Settings → Developer → Install from ZIP"
    echo "  2. Select this file"
    echo "  3. Restart Steam"
    echo "========================================="
}

# ============================================================
# Main Build Flow
# ============================================================
main() {
    # Run pre-build tasks
    prebuild_binaries
    sync_version
    
    # Check if forced to use local build (e.g., in CI)
    if [ "${FORCE_LOCAL_BUILD:-}" = "true" ]; then
        log_info "Forced local build mode..."
        build_local
        return 0
    fi
    
    # Check if Decky CLI is available
    if check_decky_cli; then
        # Check for container engine
        ENGINE=$(check_container_engine || true)
        if [ -n "$ENGINE" ]; then
            # Ensure we have write permissions for the build process
            chmod -R a+rwX "$SCRIPT_DIR" || true

            # Build the plugin using the Decky CLI
            # This runs in a container, so we need to ensure the volume mount has correct permissions
            log_info "Building plugin with Decky CLI..."
            build_with_cli "$ENGINE"
        else
            log_warn "No container engine (Docker/Podman) available or running"
            log_info "Falling back to local build..."
            build_local
        fi
    else
        log_info "Using local build method..."
        build_local
    fi
}

# Run main function
main "$@"
