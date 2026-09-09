#!/usr/bin/env bash
# Unifideck Plugin Build Script - new-architecture branch
# 
# This script is responsible for preparing and packaging the Unifideck plugin
# for Decky Loader. It handles both "production" and "development" builds,
# pre-build requirements (like downloading external binaries and compiling
# locale files), and packages the final structure into a .zip file.
# 
# It supports two build strategies:
#   1. Docker/Podman with the Decky CLI (preferred, matches upstream CI).
#   2. Local bash/pnpm fallback (useful for direct Steam Deck builds without containers).
#
# Reflects the 5-layer package restructure (v0.7+)

# Exit immediately if a command exits with a non-zero status.
set -e

# Establish absolute paths to ensure script works regardless of where it's called from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLI_LOCATION="$SCRIPT_DIR/cli"
OUTPUT_DIR="$SCRIPT_DIR/out"

# ── Colors ──────────────────────────────────────────────────
# Standard ANSI color codes for readable terminal output.
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; NC='\033[0m'
log_info()    { echo -e "${BLUE}[INFO]${NC} $1"; }
log_success() { echo -e "${GREEN}[✓]${NC} $1"; }
log_warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error()   { echo -e "${RED}[ERROR]${NC} $1"; }

# ── Argument parsing ─────────────────────────────────────────
usage() {
    cat <<'EOF'
Usage: ./build-plugin.sh [dev|prod] [install|quick-install] [push]

The mode is optional and defaults to dev, so it can be left off entirely.
If you do name one, it must come first.

  dev (default)     development build; stays entirely on this machine
  prod              production build (uses package.json version)

  install           after build, full reinstall to ~/homebrew/plugins/Unifideck
                    (rm -rf + unzip + chown - ~30s)
  quick-install     skip build entirely, rsync source → install in seconds.
                    Use after editing Python / config / bundled binaries.
                    Includes defaults/ so config never drifts. For frontend
                    edits, run `pnpm run build` first to refresh dist/.
  push              dev only: publish the built zip to GitHub as a new
                    "Dev-*" prerelease and retire the previous one. WITHOUT
                    this argument nothing is ever uploaded to GitHub.
  -h, --help        show this message

Examples:
  ./build-plugin.sh                  # local dev zip, no install, no upload
  ./build-plugin.sh quick-install    # fastest deploy to the live plugin
  ./build-plugin.sh install push     # rebuild, install, publish for testers
  ./build-plugin.sh push             # dev build, publish it for testers
  ./build-plugin.sh prod             # release zip in out/
EOF
}

# `push` is deliberately opt-in. Publishing creates a remote tag + prerelease
# AND deletes the previous dev release, so an ordinary local iteration build
# must never touch the repo of its own accord (see publish_dev_release).
case "${1:-}" in
    -h|--help|help)
        usage
        exit 0
        ;;
esac

# The mode is OPTIONAL, not merely defaulted: only consume $1 when it actually
# names one. Eating $1 unconditionally made every mode-less invocation die on
# "Unknown mode: push" - a dev build is the overwhelmingly common case and
# should not have to be spelled out.
ENV_MODE="dev"
case "${1:-}" in
    dev|prod)
        ENV_MODE="$1"
        shift
        ;;
esac
INSTALL_AFTER=""
PUSH_DEV_RELEASE=0

# Every argument is validated. Silently ignoring an unrecognised one (as this
# used to for $2) costs a full build cycle to notice - a typo'd "instal" simply
# never installed.
for arg in "$@"; do
    case "$arg" in
        -h|--help|help)
            usage
            exit 0
            ;;
        install|quick-install)
            if [ -n "$INSTALL_AFTER" ] && [ "$INSTALL_AFTER" != "$arg" ]; then
                log_error "Cannot combine 'install' and 'quick-install'."
                echo ""
                usage
                exit 1
            fi
            INSTALL_AFTER="$arg"
            ;;
        push)
            PUSH_DEV_RELEASE=1
            ;;
        dev|prod)
            # Only reachable when the mode is misplaced ("push dev"), since a
            # leading mode was already consumed above.
            log_error "'$arg' is a build mode: put it first, or omit it"
            log_error "  entirely for a dev build."
            exit 1
            ;;
        *)
            log_error "Unknown argument: $arg"
            echo ""
            usage
            exit 1
            ;;
    esac
done

if [ "$PUSH_DEV_RELEASE" = "1" ] && [ "$ENV_MODE" = "prod" ]; then
    log_error "'push' is a dev-only argument. Production releases go through"
    log_error "  the draft-release flow, not the dev prerelease tag."
    exit 1
fi
if [ "$PUSH_DEV_RELEASE" = "1" ] && [ "$INSTALL_AFTER" = "quick-install" ]; then
    log_error "'push' cannot be combined with 'quick-install': quick-install"
    log_error "  rsyncs the source tree and never builds a zip to publish."
    exit 1
fi

# Parse the base version from package.json (the JS/UI project).
# We use grep/sed here instead of `jq` so we don't require the user to have `jq` installed.
PACKAGE_VERSION=$(grep '"version"' "$SCRIPT_DIR/package.json" | head -1 | sed 's/.*"version": "\([^"]*\)".*/\1/')

if [[ "$ENV_MODE" == "prod" ]]; then
    # Production builds use exact version numbers.
    VERSION_TAG="v$PACKAGE_VERSION"
    ZIP_NAME="unifideck.prod.$VERSION_TAG.zip"
    PLUGIN_VERSION="$PACKAGE_VERSION"
    # Empty on purpose (see _write_dev_build_json): a prod zip still
    # ships a dev_build.json, just with a blank build_id, so installing
    # it actively clears any dev_build.json left behind by a previous
    # dev install - Decky's own plugin installer overlays the new zip's
    # files onto the existing plugin directory rather than wiping it
    # first, so a file the new zip doesn't contain is never removed.
    GIT_BRANCH=""
    GIT_SHA=""
    DEV_BUILD_ID=""
    log_info "Building in PRODUCTION mode ($VERSION_TAG)"
elif [[ "$ENV_MODE" == "dev" ]]; then
    mkdir -p "$OUTPUT_DIR"

    # ── Dev build identifier ──────────────────────────────────
    # A dev build that is pushed (the `push` argument) publishes its own
    # GitHub prerelease, tagged "Dev-<date>-<time>-<sha>" (see
    # publish_dev_release). That tag is deliberately free of any "X.Y"
    # substring, so the updater's version parser can't turn it into a semver -
    # meaning nothing in the GitHub API's version field can tell two dev builds
    # apart. We bake a self-describing identifier (branch + short commit SHA)
    # into the asset FILENAME itself, and stamp the same value into
    # dev_build.json inside the zip (see _write_dev_build_json), so the
    # pre-install dropdown (reads the filename) and the post-install "Current"
    # line (reads dev_build.json) show the same build id. Local-only builds
    # carry the same identifier - it is what tells two unpushed zips in out/
    # apart too.
    #
    # Branch name (not $PACKAGE_VERSION) leads the identifier: working
    # branches are named after the target version (e.g. "0.7.1") while
    # package.json/plugin.json intentionally stay frozen at the last
    # official release until the real version-bump commit.
    GIT_BRANCH=""
    GIT_SHA=""
    DEV_BUILD_ID=""
    if command -v git >/dev/null 2>&1 \
            && git -C "$SCRIPT_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        GIT_BRANCH=$(git -C "$SCRIPT_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
        GIT_SHA=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "")

        BRANCH_LABEL="$GIT_BRANCH"
        # Detached HEAD makes "HEAD" a useless leading component - fall
        # back to package.json's version for that segment only.
        if [ -z "$BRANCH_LABEL" ] || [ "$BRANCH_LABEL" = "HEAD" ]; then
            BRANCH_LABEL="$PACKAGE_VERSION"
        fi
        # Branch names may contain '/' (e.g. "feature/x"), which would
        # break the zip filename / GitHub asset name.
        BRANCH_LABEL=$(echo "$BRANCH_LABEL" | tr '/' '-')

        if [ -n "$GIT_SHA" ]; then
            DEV_BUILD_ID="${BRANCH_LABEL}.g${GIT_SHA}"
        fi
    fi

    if [ -z "$DEV_BUILD_ID" ]; then
        # Fallback: no git available (e.g. a source tarball with no
        # .git). A build must never fail just because we couldn't
        # compute a cosmetic identifier - reuse the old local counter.
        log_warn "git unavailable - falling back to local dev counter for build id"
        LATEST_DEV=$(ls -1 "$OUTPUT_DIR"/unifideck.dev.v*.zip 2>/dev/null | \
            sed 's/.*unifideck\.dev\.v\([0-9]*\)\.zip/\1/' | sort -n | tail -1)
        DEV_VER=$([ -z "$LATEST_DEV" ] && echo 1 || echo $((LATEST_DEV + 1)))
        DEV_BUILD_ID="v$DEV_VER"
    fi

    VERSION_TAG="$DEV_BUILD_ID"
    ZIP_NAME="unifideck.dev.$DEV_BUILD_ID.zip"
    PLUGIN_VERSION="$PACKAGE_VERSION-dev.$DEV_BUILD_ID"
    log_info "Building in DEVELOPMENT mode ($VERSION_TAG)"
else
    # Unreachable: the parser above only ever assigns "dev" or "prod". Kept as
    # a backstop so a future edit to that case can't silently build nothing.
    log_error "Unknown mode: $ENV_MODE. Use 'dev' or 'prod'."
    exit 1
fi

# The final absolute path where the .zip file will be saved.
OUTPUT_FILE="$OUTPUT_DIR/$ZIP_NAME"

echo "========================================="
echo "Unifideck Plugin Build Script (v0.7+)"
echo "========================================="
echo "Mode:   $ENV_MODE"
echo "Target: $OUTPUT_FILE"
echo ""

# ── Binary versions (sourced from package.json remote_binary) ─
# To add a new binary:
#   1. Add it to "remote_binary" in package.json.
#   2. Add the URL below.
#   3. Add a check step inside the `prebuild_binaries` function.
# These must stay in sync with package.json "remote_binary" entries:
# tests/unit/_tooling/test_binary_manifest_sync.py fails the build if they drift.
#
# NOTE: legendary >=0.20.40 and gogdl >=1.2.2 ship a Python ZIPAPP for Linux,
# not a PyInstaller ELF. They need a `python3` on PATH (shebang) and a writable
# HOME - they extract native modules to ~/.cache/{legendary,heroic_gogdl}/
# on first run. That applies to this build host too, since _download_bin
# validates by executing the file it just downloaded.
LEGENDARY_URL="https://github.com/Heroic-Games-Launcher/legendary/releases/download/0.20.43/legendary_linux_x86_64"
GOGDL_URL="https://github.com/Heroic-Games-Launcher/heroic-gogdl/releases/download/v1.3.0/gogdl_linux_x86_64"
NILE_URL="https://github.com/imLinguin/nile/releases/download/v1.1.2/nile_linux_x86_64"
COMET_URL="https://github.com/imLinguin/comet/releases/download/v0.3.2/comet-x86_64-unknown-linux-gnu"
WINETRICKS_URL="https://raw.githubusercontent.com/Winetricks/winetricks/20260125/src/winetricks"

# ── Pre-build: download/verify bundled binaries ───────────────
# Decky Loader expects all dependencies to be included in the zip file.
# This function pulls down the large third-party store clients.
prebuild_binaries() {
    log_info "Running pre-build binary checks..."

    # Helper function: Downloads to a `.new` file, marks executable,
    # and runs a quick validation command (usually `--version`) before
    # swapping it in place. This prevents corrupt downloads from breaking the plugin.
    #
    # Skips the download entirely when the binary we already have was fetched
    # from this exact URL. Every URL in the manifest is version-pinned (e.g.
    # .../download/0.20.43/legendary_linux_x86_64), so "same URL" *is* "same
    # version" - a bump changes the URL and re-downloads on its own.
    #
    # This matters more than it looks: the five binaries are ~28 MB combined and
    # were re-fetched on EVERY build. GitHub's release-asset throughput to a
    # Deck is not dependable (measured at ~100 KB/s, turning prebuild alone into
    # 274 s, while the same assets can arrive in seconds on a good day). That
    # single step was dominating total build time and made it look like the
    # whole script had regressed.
    _download_bin() {
        local name="$1" url="$2" dest="$3" validate_cmd="$4"
        local stamp="$dest.url"
        log_info "Checking $name..."

        # Cache hit: binary present, executable, and stamped with this URL.
        if [ -x "$dest" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$url" ]; then
            log_success "$name up to date (cached)"
            return 0
        fi

        if curl -fsSL "$url" -o "$dest.new"; then
            chmod +x "$dest.new"
            if eval "$validate_cmd" > /dev/null 2>&1; then
                mv "$dest.new" "$dest"
                printf '%s\n' "$url" > "$stamp"
                log_success "$name downloaded/verified"
            else
                rm -f "$dest.new"
                log_warn "$name: downloaded binary failed validation, keeping existing"
            fi
        else
            log_warn "$name: download failed, keeping existing"
        fi
    }

    # Legendary: Epic Games Store CLI.
    _download_bin "legendary" "$LEGENDARY_URL" "$SCRIPT_DIR/bin/legendary" \
        '"$SCRIPT_DIR/bin/legendary.new" --version'

    # Gogdl: GOG download manager (developed by Heroic).
    _download_bin "gogdl" "$GOGDL_URL" "$SCRIPT_DIR/bin/gogdl" \
        '"$SCRIPT_DIR/bin/gogdl.new" --version --auth-config-path /dev/null'

    # Nile: Amazon Games CLI.
    _download_bin "nile" "$NILE_URL" "$SCRIPT_DIR/bin/nile" \
        '"$SCRIPT_DIR/bin/nile.new" --version'

    # Comet: GOG Galaxy online services wrapper.
    _download_bin "comet" "$COMET_URL" "$SCRIPT_DIR/bin/comet" \
        '"$SCRIPT_DIR/bin/comet.new" --version'

    # Winetricks is a shell script, so it doesn't have a reliable --version flag.
    # Validated by checking for the "WINETRICKS_VERSION" string instead, which is
    # why it cannot reuse _download_bin - but it gets the same URL stamp so a
    # rebuild skips it too.
    log_info "Checking winetricks..."
    if [ -x "$SCRIPT_DIR/bin/winetricks" ] \
            && [ "$(cat "$SCRIPT_DIR/bin/winetricks.url" 2>/dev/null)" \
                 = "$WINETRICKS_URL" ]; then
        log_success "winetricks up to date (cached)"
    elif curl -fsSL "$WINETRICKS_URL" -o "$SCRIPT_DIR/bin/winetricks.new"; then
        chmod +x "$SCRIPT_DIR/bin/winetricks.new"
        if grep -q "WINETRICKS_VERSION" "$SCRIPT_DIR/bin/winetricks.new"; then
            mv "$SCRIPT_DIR/bin/winetricks.new" "$SCRIPT_DIR/bin/winetricks"
            printf '%s\n' "$WINETRICKS_URL" > "$SCRIPT_DIR/bin/winetricks.url"
            log_success "winetricks downloaded/verified"
        else
            rm -f "$SCRIPT_DIR/bin/winetricks.new"
            log_warn "winetricks: downloaded file invalid, keeping existing"
        fi
    else
        log_warn "winetricks: download failed, keeping existing"
    fi

    echo ""
}

# ── Pre-build: requirements check ────────────────────────────
# Decky's backend expects a requirements.txt file to install Python dependencies.
# We keep requirements.in as the source of truth, so this step ensures
# it is correctly mirrored to requirements.txt for the build system.
check_requirements() {
    if [ ! -f "$SCRIPT_DIR/requirements.txt" ] && [ -f "$SCRIPT_DIR/requirements.in" ]; then
        log_info "requirements.txt missing - copying from requirements.in..."
        cp "$SCRIPT_DIR/requirements.in" "$SCRIPT_DIR/requirements.txt"
        log_success "Created requirements.txt"
    elif [ ! -f "$SCRIPT_DIR/requirements.txt" ]; then
        log_warn "requirements.txt missing and requirements.in not found!"
    fi
}

# ── Pre-build: vendor Python deps into py_modules/ ──────────
# Decky Loader is *supposed* to pip-install requirements.txt at
# plugin load time, but in practice this is unreliable across
# Loader versions. We vendor the wheels into py_modules/ ourselves
# so the install zip is self-contained and doesn't depend on the
# Loader's pip behaviour.
#
# We download manylinux wheels for Python 3.11 (Decky Loader's bundled
# Python, which runs the plugin backend), regardless of the host's
# Python. ``--only-binary :all:`` refuses sdists so we never accidentally
# compile against the host's libpython.
#
# NOTE: the Steam shortcut launcher (bin/unifideck-launcher) does NOT run
# under Decky's Python - it runs under the *system* /usr/bin/python3, whose
# minor version varies by distro (SteamOS/Bazzite/CachyOS are 3.13 today, but
# CachyOS is rolling and Arch will bump to 3.14). Any ABI-specific C extension
# that a launcher code path imports must therefore be vendored for Decky's
# Python AND every system Python we want to support. Today that's cffi's
# ``_cffi_backend`` (pulled in by cryptography via the cloud-save / token
# paths): see vendor_launcher_cffi(), which loops LAUNCHER_PYTHON_VERSIONS.
# Without a matching .so the launcher now degrades cloud-save gracefully
# (see launcher/dispatcher.py) rather than aborting every game launch.
#
# Idempotent: if a package is already in py_modules/ we leave it
# alone (--upgrade-strategy only-if-needed). Disk-cheap and fast
# (~2s when fully cached, ~10s on first run).
DECK_PYTHON_VERSION="3.11"
# The launcher runs under the HOST system Python, whose minor version differs
# across distros (SteamOS/Bazzite/CachyOS are 3.13 today, but CachyOS is rolling
# and Arch will bump to 3.14; an older Fedora rebase could still be on 3.12).
# We vendor _cffi_backend for EVERY version in this list so whichever
# /usr/bin/python3 the host ships finds a matching ABI .so. Versions with no
# published cffi wheel are skipped gracefully. See vendor_launcher_cffi().
# Keep this range in sync with ACCEPTED_VERSIONS in
# py_modules/unifideck/launcher/proton/infrastructure/selector.py.
LAUNCHER_PYTHON_VERSIONS=(3.10 3.11 3.12 3.13 3.14)
DECK_PLATFORM_TAG="manylinux2014_x86_64"

vendor_deps() {
    [ -f "$SCRIPT_DIR/requirements.txt" ] || {
        log_warn "requirements.txt not found - skipping vendor step"
        return 0
    }
    log_info "Vendoring Python deps into py_modules/ (Python $DECK_PYTHON_VERSION, $DECK_PLATFORM_TAG)..."

    # Use a quiet cache dir so repeated builds don't re-download.
    local cache_dir="$SCRIPT_DIR/.cache/pip-vendor"
    mkdir -p "$cache_dir"

    # --target installs into py_modules/ instead of site-packages.
    # --platform + --python-version + --only-binary force the
    # SteamOS-compatible wheel set regardless of host interpreter.
    # --upgrade-strategy only-if-needed avoids churning unchanged deps.
    if python3 -m pip install \
            --quiet \
            --target "$SCRIPT_DIR/py_modules" \
            --platform "$DECK_PLATFORM_TAG" \
            --python-version "$DECK_PYTHON_VERSION" \
            --only-binary ":all:" \
            --upgrade \
            --upgrade-strategy only-if-needed \
            --cache-dir "$cache_dir" \
            -r "$SCRIPT_DIR/requirements.txt" 2>&1 | tail -20; then
        log_success "Python deps vendored"
    else
        log_warn "vendor_deps failed - the zip may be missing required Python deps"
        log_warn "(check that you have pip and a network connection)"
    fi

    prune_stale_dist_info

    # Sanity-check that the four runtime deps actually landed.
    local missing_deps=()
    for dep in aiohttp websockets cryptography jsonschema; do
        if ! ls "$SCRIPT_DIR/py_modules/$dep" >/dev/null 2>&1 \
                && ! ls "$SCRIPT_DIR/py_modules/${dep}-"*.dist-info >/dev/null 2>&1; then
            missing_deps+=("$dep")
        fi
    done
    if [ "${#missing_deps[@]}" -gt 0 ]; then
        log_warn "Missing vendored deps after pip install: ${missing_deps[*]}"
        log_warn "Plugin features depending on these will be disabled at runtime."
    fi
    echo ""
}

# Drop *.dist-info directories left behind by earlier vendoring runs.
#
# ``pip install --target --upgrade`` overwrites a package's .py files but
# NEVER removes the previous version's metadata directory. Over many builds
# py_modules/ accumulates one dist-info per version ever vendored - we had
# SIX for aiohttp (3.13.3 through 3.14.3), four each for websockets/yarl/idna,
# and two for cffi. Two things break because of that:
#
#   * pip-audit (quality.yml) walks this directory and reports CVEs against
#     versions that are not the ones actually importable, so the CVE floors
#     documented in requirements.txt are gated against phantom data.
#   * vendor_launcher_cffi() below derived the cffi version from a glob whose
#     alphabetically-first match was the STALE dist-info - so it vendored
#     _cffi_backend 2.0.0 next to the cffi 2.1.0 package it had just
#     installed, and every FFI() construction raised "Version mismatch".
#
# pip writes a package's dist-info at install time, so the most recently
# modified one is the live copy (verified: newest mtime == the version in the
# package's own __version__, for aiohttp/cffi/cryptography alike). Keep that
# one, delete its predecessors.
prune_stale_dist_info() {
    local pruned=0 pkg newest d
    # Group by distribution name: strip the trailing -<version>.dist-info.
    while IFS= read -r pkg; do
        # -t sorts by mtime, newest first; everything after the first is stale.
        newest=1
        while IFS= read -r d; do
            if [ "$newest" = "1" ]; then
                newest=0
                continue
            fi
            rm -rf "$d"
            pruned=$((pruned + 1))
        done < <(ls -dt "$SCRIPT_DIR/py_modules/${pkg}-"*.dist-info 2>/dev/null)
    done < <(
        ls -d "$SCRIPT_DIR"/py_modules/*.dist-info 2>/dev/null \
            | sed -E 's#.*/([^/]+)-[^-]+\.dist-info$#\1#' | sort -u
    )
    if [ "$pruned" -gt 0 ]; then
        log_success "Pruned $pruned stale dist-info director$([ "$pruned" = 1 ] && echo y || echo ies)"
    fi
}

# Vendor cffi's ABI-specific _cffi_backend for the LAUNCHER's Python too.
#
# vendor_deps() above targets Decky's Python ($DECK_PYTHON_VERSION) and so
# only produces _cffi_backend.cpython-311-*.so. But bin/unifideck-launcher
# runs under the system /usr/bin/python3, whose minor version varies by host
# (see LAUNCHER_PYTHON_VERSIONS). The cloud-save / token-refresh paths import
# cryptography → cffi → _cffi_backend at load time; under a system Python with
# no matching .so the import raises and (historically) the launcher aborted:
# killing ALL game launches. cryptography's own binding is abi3
# (version-agnostic) and the rest of cffi is pure-python, so the ONLY
# ABI-specific piece we need for the system interpreter is _cffi_backend.
#
# To stay portable across distros (SteamOS/Bazzite/CachyOS, and future Python
# bumps) we vendor _cffi_backend for EVERY version in LAUNCHER_PYTHON_VERSIONS.
# CPython's import machinery auto-selects the .so whose ABI tag matches the
# running interpreter, so no runtime code change is needed. Versions with no
# published cffi wheel (e.g. a not-yet-released CPython) are skipped with a
# warning. Pure-python files are identical across versions so they're left
# untouched.
vendor_launcher_cffi() {
    local cffi_ver
    # Read the version from the PACKAGE, not from a dist-info glob. cffi's own
    # api.py compares its __version__ against the compiled backend's and raises
    # on any difference, so the package is the only source of truth that
    # matters. (The old glob took the alphabetically-first dist-info, which
    # after an upgrade was the stale one - it pinned the backend to the version
    # we had just replaced. See prune_stale_dist_info above.)
    cffi_ver=$(sed -nE 's/^__version__ *= *"([^"]+)".*/\1/p' \
        "$SCRIPT_DIR/py_modules/cffi/__init__.py" 2>/dev/null | head -1)
    if [ -z "$cffi_ver" ]; then
        log_info "cffi not vendored (no cloud-save crypto path) - skipping launcher cffi"
        return 0
    fi

    # The .so files carry no readable version, so record which cffi they were
    # built for. Build-local (never shipped): a fresh clone simply re-vendors.
    # Without this the "already present" check below was version-blind and
    # happily kept a mismatched backend forever.
    local stamp="$SCRIPT_DIR/.cache/cffi-backend-version"
    local stamped=""
    [ -f "$stamp" ] && stamped=$(cat "$stamp" 2>/dev/null)
    if [ "$stamped" != "$cffi_ver" ]; then
        if [ -n "$stamped" ] || ls "$SCRIPT_DIR"/py_modules/_cffi_backend.*.so \
                >/dev/null 2>&1; then
            log_warn "cffi backend stamp '${stamped:-none}' != package $cffi_ver - re-vendoring backends"
        fi
        rm -f "$SCRIPT_DIR"/py_modules/_cffi_backend.*.so
    fi

    local vendored=() skipped=()
    local ver abitag tmp
    for ver in "${LAUNCHER_PYTHON_VERSIONS[@]}"; do
        abitag="cpython-3${ver#3.}"
        # Already present AND known to match cffi_ver (stamp verified above).
        if ls "$SCRIPT_DIR"/py_modules/_cffi_backend.${abitag}-*.so \
                >/dev/null 2>&1; then
            vendored+=("$ver")
            continue
        fi
        tmp=$(mktemp -d)
        if python3 -m pip install \
                --quiet \
                --target "$tmp" \
                --platform "$DECK_PLATFORM_TAG" \
                --python-version "$ver" \
                --only-binary ":all:" \
                --no-deps \
                --cache-dir "$SCRIPT_DIR/.cache/pip-vendor" \
                "cffi==$cffi_ver" 2>&1 | tail -5 \
                && cp -f "$tmp"/_cffi_backend.${abitag}-*.so \
                    "$SCRIPT_DIR/py_modules/" 2>/dev/null; then
            vendored+=("$ver")
        else
            # No wheel for this Python (e.g. unreleased) - non-fatal.
            skipped+=("$ver")
        fi
        rm -rf "$tmp"
    done
    if [ "${#vendored[@]}" -gt 0 ]; then
        mkdir -p "$(dirname "$stamp")"
        printf '%s\n' "$cffi_ver" > "$stamp"
        log_success "Vendored launcher cffi backends (cffi==$cffi_ver) for Python: ${vendored[*]}"
    fi
    if [ "${#skipped[@]}" -gt 0 ]; then
        log_warn "No cffi wheel for Python: ${skipped[*]} - cloud-save degrades gracefully on those hosts"
    fi
    echo ""
}

# ── Pre-build: generate src/i18n/locales.generated.ts ────────
# The frontend uses i18next for localization, but ES modules cannot natively
# do dynamic imports from JSON without breaking the bundler configuration.
# This python script reads our supported languages config and generates
# a `.ts` file with static imports that Rollup can consume.
gen_locales() {
    log_info "Generating src/i18n/locales.generated.ts..."
    if cd "$SCRIPT_DIR/scripts" && python3 gen_locale_imports.py \
        --config "$SCRIPT_DIR/defaults/config.json" \
        --output "$SCRIPT_DIR/src/i18n/locales.generated.ts" 2>&1; then
        cd "$SCRIPT_DIR"
        log_success "locales.generated.ts ready"
    else
        cd "$SCRIPT_DIR"
        log_warn "Locale generation failed - build may fail if file is missing"
    fi
}

# ── Pre-build: read version from plugin.json ─────────────────
# Grab the plugin version defined in Decky's plugin manifest for our logs.
sync_version() {
    PLUGIN_VERSION=$(grep '"version"' "$SCRIPT_DIR/plugin.json" | head -1 | sed 's/.*"version": "\([^"]*\)".*/\1/')
    log_info "Plugin version (plugin.json): $PLUGIN_VERSION"
    echo ""
}

# ── Decky CLI detection ───────────────────────────────────────
# The Decky CLI handles building the UI and packaging everything securely.
# These functions download the appropriate CLI binary for the host OS.
# Asset names are ``decky-<os>-<arch>`` — a BARE binary, not an archive,
# and the arch is spelled ``x86_64``/``aarch64`` (uname's spelling), not
# ``x64``/``arm64``. Getting either wrong 404s.
#
# This was wrong in both respects and nobody noticed, because a working
# ``cli/decky`` was committed to the repo and ``check_decky_cli`` returns
# early when the file is already there. Deleting that binary (PR #270) is
# what surfaced it: the download 404s, the build silently falls back to
# the local path, and on a clean checkout there is no CLI at all.
get_decky_cli_url() {
    local os arch base="https://github.com/SteamDeckHomebrew/cli/releases/latest/download"
    case "$(uname -s)" in Linux*) os="linux";; Darwin*) os="macOS";; CYGWIN*|MINGW*|MSYS*) os="windows";; *) os="linux";; esac
    case "$(uname -m)" in x86_64|amd64) arch="x86_64";; arm64|aarch64) arch="aarch64";; *) arch="x86_64";; esac
    if [ "$os" = "windows" ]; then echo "${base}/decky-${os}-${arch}.exe"
    else echo "${base}/decky-${os}-${arch}"; fi
}

check_decky_cli() {
    local cli="$CLI_LOCATION/decky"
    # If the CLI is already present and works, proceed.
    if test -f "$cli" && "$cli" --version > /dev/null 2>&1; then return 0; fi
    # If it's present but broken (e.g. built for the wrong architecture after moving files), clear it.
    if test -f "$cli"; then
        log_warn "Decky CLI incompatible with this platform - re-downloading..."
        rm -f "$cli"
    fi
    log_info "Downloading Decky CLI for $(uname -s)/$(uname -m)..."
    local url; url=$(get_decky_cli_url)
    mkdir -p "$CLI_LOCATION"
    # ``-f`` so a 404 is a curl failure instead of an HTML error page
    # written to disk and then chmod'd executable.
    if curl -fsSL "$url" -o "$cli"; then
        chmod +x "$cli" 2>/dev/null || true
        if "$cli" --version > /dev/null 2>&1; then log_success "Decky CLI ready"; return 0; fi
        log_warn "Downloaded Decky CLI does not run on this platform"
        rm -f "$cli"
    fi
    log_warn "Could not download Decky CLI ($url) - will use local build"
    return 1
}

# Determines whether Docker or Podman is available for the Decky CLI to use.
# Podman is the default on SteamOS.
check_container_engine() {
    if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then echo "docker"; return 0; fi
    if command -v podman &>/dev/null && podman info &>/dev/null 2>&1; then echo "podman"; return 0; fi
    return 1
}

# ── Staging directory contents ───────────────────────────────
#   Mirrors the exact runtime layout expected by Decky Loader on the Steam Deck.
#   It avoids zipping unnecessary dev files (.git, tests, etc.)
#   Directories included (relative to repo root):
#     py_modules/   - vendored deps + unifideck 5-layer package
#     bin/          - native binaries + shell wrappers (no .py scripts allowed here)
#     defaults/     - config.json schema + backend defaults
#     src/          - TypeScript source (built into dist/ by Decky CLI)
#     assets/       - plugin artwork/icons
#   Files included:
#     main.py, plugin.json, package.json, pnpm-lock.yaml,
#     tsconfig.json, rollup.config.mjs, requirements.txt,
#     LICENSE, README.md

# ── Dev build-identity stamp ──────────────────────────────────
# Writes dev_build.json to $1, read at runtime by
# UpdaterService.get_current_build_id() so the Settings UI can show
# which specific dev build is installed. Shipped in EVERY build, dev
# and prod alike - DEV_BUILD_ID/GIT_BRANCH/GIT_SHA are simply empty
# strings for a prod build, so get_current_build_id() correctly reads
# no build id back. This is deliberate, not redundant: Decky's own
# plugin installer overlays a newly-installed zip's files onto the
# existing plugin directory instead of wiping it first, so a file
# absent from the new zip is never removed - without a prod build
# also shipping (and thereby overwriting) this file, a stale dev
# install's dev_build.json would linger and the Settings UI would
# claim a dev build is "installed" long after a prod version replaced
# it.
_write_dev_build_json() {
    local target="$1"
    cat > "$target" <<EOF
{
  "build_id": "$DEV_BUILD_ID",
  "branch": "$GIT_BRANCH",
  "commit": "$GIT_SHA",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
}

# Stamps dev_build.json into an already-built plugin ZIP. This runs
# AFTER packaging rather than being staged alongside main.py/plugin.json
# beforehand: the Decky CLI's containerized `plugin build` step does its
# own internal repackaging of the staging directory and silently drops
# any file it doesn't recognize (confirmed - pnpm-lock.yaml, tsconfig.json,
# rollup.config.mjs, and requirements.txt also don't survive into its
# output, alongside a first attempt at staging dev_build.json the same
# way). Appending the file directly to the finished ZIP sidesteps that
# entirely, and works identically for both build paths.
_inject_dev_build_json() {
    local zip_path="$1"
    local tmp; tmp=$(mktemp -d)
    mkdir -p "$tmp/Unifideck"
    _write_dev_build_json "$tmp/Unifideck/dev_build.json"
    (cd "$tmp" && zip -q "$zip_path" "Unifideck/dev_build.json")
    rm -rf "$tmp"
}

_stage_plugin_files() {
    local dest="$1"
    mkdir -p "$dest"

    for dir in py_modules bin defaults src; do
        [ -d "$SCRIPT_DIR/$dir" ] && cp -r "$SCRIPT_DIR/$dir" "$dest/"
    done
    cp -r "$SCRIPT_DIR/assets" "$dest/" 2>/dev/null || true

    for f in main.py plugin.json package.json pnpm-lock.yaml tsconfig.json \
              rollup.config.mjs requirements.txt LICENSE README.md; do
        [ -f "$SCRIPT_DIR/$f" ] && cp "$SCRIPT_DIR/$f" "$dest/"
    done

    # Build-time leftovers that must never ship: the per-binary download stamps
    # (see prebuild_binaries) and any half-finished download, plus the lint
    # caches, which the copy above otherwise drags in from py_modules/. Both
    # caches are created by simply running the CI gate block before a build,
    # so a release build is exactly when they are most likely to be present.
    rm -f "$dest"/bin/*.url "$dest"/bin/*.new
    rm -rf "$dest/py_modules/.mypy_cache" "$dest/py_modules/.import_linter_cache"
}

# ── Skip the Decky CLI's redundant binary re-download ─────────
# The CLI downloads every ``remote_binary`` entry from package.json into the
# container on EVERY build - the same ~28 MB of store CLIs that
# ``prebuild_binaries`` already fetched, verified and cached in bin/, and that
# ``_stage_plugin_files`` already copied into the staging dir. It then
# overwrites our copies with byte-identical ones.
#
# That download WAS the single largest cost of a full build. Measured A/B on
# this Deck, same tree, warm caches, minutes apart:
#
#     with the CLI download    320 s total (312 s in the container)
#     without it                47 s total ( 40 s in the container)
#
# 28 MB in 272 s is ~103 KB/s. The same asset fetched from the HOST with curl,
# the same afternoon, came down at 8.7 MB/s - roughly 85x faster. So this is not
# "GitHub is slow to a Deck", as it long appeared to be: it is the container's
# network path. Which is why it never looked like a variable-network problem -
# it was reliably, boringly slow on every single build.
#
# The CLI also has no cache, so it paid that cost whether or not a version had
# been bumped. A bump merely made it visible, because that is the one build
# where prebuild_binaries re-downloads too and you pay twice.
#
# So: strip ``remote_binary`` from the STAGED package.json - a throwaway copy in
# a temp dir - and let the CLI zip the binaries we staged. The repo's own
# package.json is never touched, so the manifest the Decky Store builds from,
# and the CI check that keeps it in sync with this script, are unaffected.
#
# The CLI's download step is also what verified those binaries' checksums, so we
# do it here instead, against the same ``sha256hash`` values from the same
# manifest. If ANY binary is missing or mismatched we leave ``remote_binary``
# alone and let the CLI fetch it the slow way - correctness first, speed second.
_stage_skip_cli_binary_download() {
    local staged="$1"
    local pkg="$staged/package.json"
    [ -f "$pkg" ] || return 0

    local verified
    if ! verified=$(STAGED_BIN_DIR="$staged/bin" python3 - "$pkg" <<'PY'
import hashlib, json, os, sys

pkg_path = sys.argv[1]
bin_dir = os.environ["STAGED_BIN_DIR"]
with open(pkg_path) as fh:
    pkg = json.load(fh)

entries = pkg.get("remote_binary") or []
if not entries:
    raise SystemExit(1)  # nothing to strip; let the CLI do whatever it does

for entry in entries:
    path = os.path.join(bin_dir, entry["name"])
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        print("missing or non-executable: " + entry["name"], file=sys.stderr)
        raise SystemExit(1)
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    expected = (entry.get("sha256hash") or "").lower()
    if not expected:
        print("no sha256hash in manifest: " + entry["name"], file=sys.stderr)
        raise SystemExit(1)
    if digest.hexdigest() != expected:
        print("checksum mismatch: " + entry["name"], file=sys.stderr)
        raise SystemExit(1)

pkg.pop("remote_binary")
with open(pkg_path, "w") as fh:
    json.dump(pkg, fh, indent=2)
    fh.write("\n")
print(len(entries))
PY
    ); then
        log_warn "Staged binaries failed verification - letting the Decky CLI"
        log_warn "  re-download them (slower, but guaranteed correct)."
        return 0
    fi
    log_success "Verified $verified bundled binaries (sha256); skipping the CLI re-download"
}

# ── Build with Decky CLI (Docker/Podman) ─────────────────────
# This is the primary build path. It stages files, runs the Decky CLI inside
# a container, compiles the frontend using Rollup, and generates a clean ZIP.
build_with_cli() {
    local engine="$1"
    log_info "Building with Decky CLI using $engine..."
    rm -f "$OUTPUT_FILE"

    # Clean dist. Because container builds might leave root-owned files behind,
    # we have to run a containerized `rm -rf` to delete them without `sudo`.
    if [ -d "$SCRIPT_DIR/dist" ]; then
        log_info "Cleaning dist/..."
        rm -rf "$SCRIPT_DIR/dist" 2>/dev/null || \
            "$engine" run --rm -v "$SCRIPT_DIR":/v -w /v alpine rm -rf dist
    fi

    # Stage files into a clean temporary directory. A trap removes it on any
    # exit path, because a CLI failure trips `set -e` and would otherwise leak
    # a full source copy into /tmp. The path is baked into the trap now since
    # function-locals are not visible to the EXIT trap.
    local staging; staging=$(mktemp -d)
    trap "rm -rf '$staging'" EXIT
    local staging_plugin="$staging/unifideck-staging"
    _stage_plugin_files "$staging_plugin"
    _stage_skip_cli_binary_download "$staging_plugin"
    chmod -R a+rX "$staging_plugin" 2>/dev/null || true

    # Fire up the Decky CLI builder. By default the CLI stages its own temp
    # copy under /tmp/decky. A previous root-context build can leave that
    # directory root-owned, and a deck build then cannot write into it, which
    # fails with "Temporary build directory already exists / Permission
    # denied". Point it at a fresh directory inside our own deck-owned staging
    # tree so a stray root-owned /tmp/decky can never block a sudo-less build.
    mkdir -p "$OUTPUT_DIR"
    "$CLI_LOCATION/decky" plugin build "$staging_plugin" \
        --output-path "$OUTPUT_DIR" \
        --tmp-output-path "$staging/decky-tmp" \
        --engine "$engine" \
        --follow-symlinks \
        --build-as-root

    # Clean up staging dir (and disarm the trap set above)
    rm -rf "$staging"
    trap - EXIT

    # The CLI hardcodes the output name to "Unifideck.zip". We rename it to our versioned format.
    local expected="$OUTPUT_DIR/Unifideck.zip"
    if [ -f "$expected" ] && [ "$expected" != "$OUTPUT_FILE" ]; then
        mv "$expected" "$OUTPUT_FILE"
        log_success "Renamed Unifideck.zip → $ZIP_NAME"
    elif [ -f "$OUTPUT_FILE" ]; then
        log_success "Build output at $ZIP_NAME"
    else
        log_warn "Expected CLI output not found: Unifideck.zip"
    fi

    # Stamped for both dev and prod builds - see _write_dev_build_json.
    if [ -f "$OUTPUT_FILE" ]; then
        _inject_dev_build_json "$OUTPUT_FILE"
    fi

    log_success "CLI build complete: $OUTPUT_FILE"
}

# ── Local build (Steam Deck / no container fallback) ─────────
# This acts as a fallback for users building directly on their Steam Deck
# without podman installed. It runs `pnpm` natively and manually zips the output.
build_local() {
    log_info "Building locally (no container engine)..."

    cd "$SCRIPT_DIR"
    log_info "Compiling TypeScript frontend..."
    if ! pnpm run build; then log_error "Frontend compilation failed"; exit 1; fi
    log_success "Frontend compiled"

    mkdir -p "$OUTPUT_DIR"
    local build_dir; build_dir=$(mktemp -d)
    trap "rm -rf '$build_dir'" EXIT
    local plugin_dir="$build_dir/Unifideck"
    _stage_plugin_files "$plugin_dir"

    # The CLI builds into `dist/` natively. For the local fallback,
    # we manually copy the newly built frontend into our staging area.
    [ -d "$SCRIPT_DIR/dist" ] && cp -r "$SCRIPT_DIR/dist" "$plugin_dir/"

    # ── Critical file verification ───────────────────────────
    # Since we are zipping manually, we verify that every critical architectural
    # component is present before packaging. If one of these is missing,
    # it indicates a structural flaw (like a missing import or broken script)
    # and we abort the build to prevent shipping a broken plugin.
    # 
    # NOTE: If you add a new service or store connector, ADD IT TO THIS LIST!
    log_info "Verifying critical files..."
    local CRITICAL_FILES=(
        # Plugin root files
        "main.py"
        "plugin.json"
        "dist/index.js"

        # Layer 1 - core/types (pure data island, no dependencies)
        "py_modules/unifideck/core/types/__init__.py"
        "py_modules/unifideck/core/types/domain.py"
        "py_modules/unifideck/core/types/events.py"
        "py_modules/unifideck/core/types/results.py"

        # Layer 2 - core infrastructure (utils, binary resolvers, file I/O)
        "py_modules/unifideck/core/__init__.py"
        "py_modules/unifideck/core/cache_manager.py"
        "py_modules/unifideck/core/sync_service.py"
        "py_modules/unifideck/core/manifest.py"
        "py_modules/unifideck/core/paths.py"
        "py_modules/unifideck/core/exe_finder.py"
        "py_modules/unifideck/core/metrics_collector.py"
        "py_modules/unifideck/core/io/__init__.py"
        "py_modules/unifideck/core/io/async_file_ops.py"
        "py_modules/unifideck/core/io/safe_file_op.py"
        "py_modules/unifideck/core/binaries/__init__.py"
        "py_modules/unifideck/core/binaries/binary_resolver.py"
        "py_modules/unifideck/core/binaries/binary_signatures.py"
        "py_modules/unifideck/core/binaries/cli_timeouts.py"

        # EventBus - Message queue and event routing
        "py_modules/unifideck/event_bus/__init__.py"
        "py_modules/unifideck/event_bus/event_bus.py"
        "py_modules/unifideck/event_bus/priority_dispatcher.py"
        "py_modules/unifideck/event_bus/event_replay.py"
        "py_modules/unifideck/event_bus/event_bus_extensions.py"
        "py_modules/unifideck/event_bus/bus_pipeline.py"

        # Config - Validation and startup schema
        "py_modules/unifideck/config/__init__.py"
        "py_modules/unifideck/config/config_manager.py"
        "py_modules/unifideck/config/schema.json"
        "py_modules/unifideck/config/validator.py"
        "py_modules/unifideck/config/startup.py"

        # Bootstrap - Dependency injection and lifecycle
        "py_modules/unifideck/bootstrap/boot.py"
        "py_modules/unifideck/bootstrap/teardown.py"
        "py_modules/unifideck/bootstrap/pipeline_factory.py"
        "py_modules/unifideck/bootstrap/cache_registry.py"

        # Layer 6 - RPC mixins (Frontend communication API)
        "py_modules/unifideck/rpc/__init__.py"
        "py_modules/unifideck/rpc/mixins/store.py"
        "py_modules/unifideck/rpc/mixins/sync.py"
        "py_modules/unifideck/rpc/mixins/download.py"
        "py_modules/unifideck/rpc/mixins/launch.py"
        "py_modules/unifideck/rpc/mixins/playtime.py"
        "py_modules/unifideck/rpc/mixins/security.py"
        "py_modules/unifideck/rpc/mixins/observability.py"
        "py_modules/unifideck/rpc/mixins/action.py"
        "py_modules/unifideck/rpc/mixins/cloud_failure.py"
        "py_modules/unifideck/rpc/mixins/config_validation.py"
        "py_modules/unifideck/rpc/mixins/storage.py"
        "py_modules/unifideck/rpc/mixins/ui.py"
        "py_modules/unifideck/rpc/mixins/updater.py"

        # Layer 4 - Store connectors (3rd party API implementations)
        "py_modules/unifideck/stores/__init__.py"
        "py_modules/unifideck/stores/epic/__init__.py"
        "py_modules/unifideck/stores/epic/store.py"
        "py_modules/unifideck/stores/gog/__init__.py"
        "py_modules/unifideck/stores/gog/store.py"
        "py_modules/unifideck/stores/amazon/__init__.py"
        "py_modules/unifideck/stores/amazon/amazon_store.py"
        "py_modules/unifideck/stores/ubisoft/__init__.py"
        "py_modules/unifideck/stores/ubisoft/store.py"
    "py_modules/unifideck/stores/battlenet/__init__.py"
    "py_modules/unifideck/stores/battlenet/store.py"
    "py_modules/unifideck/launcher/proton/handlers/battlenet.py"
    "py_modules/unifideck/launcher/wrapper_stores.py"
        "py_modules/unifideck/stores/microsoft/__init__.py"
        "py_modules/unifideck/stores/microsoft/microsoft_store.py"

        # Layer 5 - Services (Cross-cutting infrastructure like downloads/art)
        "py_modules/unifideck/services/__init__.py"
        "py_modules/unifideck/services/download/service.py"
        "py_modules/unifideck/services/playtime/service.py"
        "py_modules/unifideck/services/cloud_save/service.py"
        "py_modules/unifideck/services/shortcut/service.py"
        "py_modules/unifideck/services/artwork/service.py"
        "py_modules/unifideck/services/launcher/service.py"
        "py_modules/unifideck/services/security/service.py"
        "py_modules/unifideck/services/bootstrap/service_defs.py"
        "py_modules/unifideck/services/bootstrap/container.py"
        "py_modules/unifideck/services/metadata_service.py"
        "py_modules/unifideck/services/account_service.py"
        "py_modules/unifideck/services/proton_service.py"
        "py_modules/unifideck/services/updater/__init__.py"
        "py_modules/unifideck/services/updater/service.py"

        # Support packages
        "py_modules/unifideck/auth/__init__.py"
        "py_modules/unifideck/auth/browser.py"
        "py_modules/unifideck/auth/orchestrator.py"
        "py_modules/unifideck/steam/__init__.py"
        "py_modules/unifideck/steam/library.py"
        # steam/shortcuts.py was split into services/shortcut/* when
        # shortcuts.vdf reads/writes gained integrity checks. These three
        # are the ones a library-destroying regression would take out.
        "py_modules/unifideck/services/shortcut/vdf_read.py"
        "py_modules/unifideck/services/shortcut/write_guard.py"
        "py_modules/unifideck/services/shortcut/vdf_backup.py"
        "py_modules/unifideck/steam/steamgriddb/__init__.py"
        "py_modules/unifideck/cdp/__init__.py"
        "py_modules/unifideck/compatibility/__init__.py"
        "py_modules/unifideck/compatibility/library.py"
        "py_modules/unifideck/compatibility/proton_helpers.py"
        "py_modules/unifideck/security/__init__.py"
        "py_modules/unifideck/security/secure_token_store.py"
        "py_modules/unifideck/security/ephemeral_creds.py"
        "py_modules/unifideck/metadata/__init__.py"
        "py_modules/unifideck/metadata/metacritic.py"
        "py_modules/unifideck/metadata/unifidb.py"
        "py_modules/unifideck/utils/__init__.py"
        "py_modules/unifideck/utils/paths.py"
        "py_modules/unifideck/utils/locale.py"
        "py_modules/unifideck/launcher/__init__.py"
        "py_modules/unifideck/launcher/dispatcher.py"
        "py_modules/unifideck/actions/__init__.py"
        "py_modules/unifideck/actions/dispatch.py"

        # Vendored third-party deps (auto-installed by vendor_deps())
        "py_modules/vdf/__init__.py"
        "py_modules/websockets/__init__.py"
        "py_modules/aiohttp/__init__.py"
        "py_modules/certifi/__init__.py"
        "py_modules/cryptography/__init__.py"
        "py_modules/jsonschema/__init__.py"

        # Native binaries
        "bin/legendary"
        "bin/gogdl"
        "bin/nile"
        "bin/comet"
        "bin/winetricks"
        "bin/unifideck-launcher"
        "bin/unifideck-runner"
        "bin/EpicGamesLauncher.exe"
        "bin/stubs/GalaxyCommunication.exe"
        "bin/umu/umu/umu-run"

        # Defaults
        "defaults/config.json"
    )

    local missing=0
    for f in "${CRITICAL_FILES[@]}"; do
        if [ ! -e "$plugin_dir/$f" ]; then
            log_error "Missing critical file: $f"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -gt 0 ]; then
        log_error "$missing critical file(s) missing - aborting"
        rm -rf "$build_dir"
        exit 1
    fi
    log_success "All critical files present"

    # Set executable bits on everything in bin/ just to be safe
    find "$plugin_dir/bin" -type f -exec chmod +x {} \; 2>/dev/null || true

    # Sanity-check plugin.json for api_version (required by newer Decky Loader versions)
    grep -q '"api_version"' "$plugin_dir/plugin.json" || \
        log_warn "plugin.json missing api_version - frontend may fail to load!"

    # ── Package into ZIP ─────────────────────────────────────────
    # Exclude development files, node_modules, and python artifacts.
    log_info "Creating zip package..."
    cd "$build_dir"
    zip -r "$OUTPUT_FILE" Unifideck \
        -x "Unifideck/.git/*" \
        -x "Unifideck/**/__pycache__/*" \
        -x "Unifideck/**/*.pyc" \
        -x "Unifideck/node_modules/*" \
        -x "Unifideck/tests/*" \
        -x "Unifideck/.gitignore" \
        -x "Unifideck/decky.pyi" \
        -x "Unifideck/*.backup" \
        -x "Unifideck/vc_redist.x64.exe" \
        -x "Unifideck/antigravity-dashboard/*" \
        -q

    cd "$SCRIPT_DIR"
    rm -rf "$build_dir"
    trap - EXIT

    # Stamped for both dev and prod builds - see _write_dev_build_json.
    if [ -f "$OUTPUT_FILE" ]; then
        _inject_dev_build_json "$OUTPUT_FILE"
    fi

    # Print final summary
    local size; size=$(ls -lh "$OUTPUT_FILE" | awk '{print $5}')
    echo ""
    echo "========================================="
    log_success "Build Complete!"
    echo "========================================="
    echo "Mode:    $ENV_MODE"
    echo "Package: $OUTPUT_FILE"
    echo "Version: $PLUGIN_VERSION"
    echo "Size:    $size"
    echo ""
    echo "To install:"
    echo "  QAM → Decky → Settings → Developer → Install from ZIP"
    echo "========================================="
}

# ── Quick-install (dev sync) ──────────────────────────────────
# Fast incremental sync of the working tree into an existing Decky
# install - no zip, no unzip, no Docker. Use this for tight dev
# iteration when you've changed Python or defaults and want them
# live in seconds.
#
# Why this exists: full ``install_plugin`` does a containerised
# build → zip → sudo rm -rf → unzip → chown cycle that can take
# 30s+. ``quick_install`` rsyncs only the runtime payload
# (py_modules, defaults, dist, bin, main.py, plugin.json,
# requirements.txt) and restarts the loader. Sub-second on the
# Deck.
#
# Critically, this includes ``defaults/`` so the source-of-truth
# config can never drift out of the install - the most common
# breakage during manual dev syncs.
#
# Frontend changes still require ``pnpm run build`` first to
# refresh ``dist/index.js``; this script is for backend +
# config + bundled-binary edits where Rollup doesn't need to
# re-run.
#
# Usage: ``./build-plugin.sh dev quick-install``
quick_install() {
    local plugins_dir="$HOME/homebrew/plugins"
    local install_dir="$plugins_dir/Unifideck"

    [ -d "$plugins_dir" ] || { log_error "Decky plugins dir not found: $plugins_dir"; return 1; }

    # Verify the source has every critical bundled artefact BEFORE
    # touching the install - never half-sync a broken source tree.
    local CRITICAL_SOURCE_FILES=(
        "main.py"
        "plugin.json"
        "defaults/config.json"
        "py_modules/unifideck/bootstrap/boot.py"
    )
    local missing=0
    for f in "${CRITICAL_SOURCE_FILES[@]}"; do
        if [ ! -e "$SCRIPT_DIR/$f" ]; then
            log_error "Source missing: $f"
            missing=$((missing + 1))
        fi
    done
    if [ "$missing" -gt 0 ]; then
        log_error "$missing source file(s) missing - aborting quick-install"
        return 1
    fi
    [ -f "$SCRIPT_DIR/dist/index.js" ] || \
        log_warn "dist/index.js missing - frontend won't load. Run 'pnpm run build' first."

    echo ""
    echo "========================================="
    echo "Quick-install (dev sync)"
    echo "========================================="
    log_info "Stopping Decky plugin loader..."
    sudo systemctl stop plugin_loader 2>/dev/null || true
    sleep 1

    sudo mkdir -p "$install_dir"

    # Rsync each runtime payload with --delete so removed source
    # files are also removed from the install. Using rsync via sudo
    # because the install dir is owned by root for normal user
    # installs - this matches Decky's permission model without
    # requiring chmod 777.
    log_info "Syncing payload to $install_dir..."
    local rsync_opts=(-a --delete --no-owner --no-group --chown=deck:deck)
    for dir in py_modules bin defaults src dist; do
        if [ -d "$SCRIPT_DIR/$dir" ]; then
            sudo rsync "${rsync_opts[@]}" \
                --exclude='__pycache__' --exclude='*.pyc' \
                --exclude='.git' --exclude='node_modules' \
                "$SCRIPT_DIR/$dir/" "$install_dir/$dir/"
            log_success "synced $dir/"
        fi
    done
    sudo cp -p "$SCRIPT_DIR"/assets/* "$install_dir/assets/" 2>/dev/null || true
    for f in main.py plugin.json package.json pnpm-lock.yaml tsconfig.json \
              rollup.config.mjs requirements.txt LICENSE README.md; do
        if [ -f "$SCRIPT_DIR/$f" ]; then
            sudo cp -p "$SCRIPT_DIR/$f" "$install_dir/$f"
        fi
    done

    # quick-install bypasses _stage_plugin_files entirely (rsync/cp
    # straight from the repo), so stamp dev_build.json here too. Written
    # for both modes - see _write_dev_build_json for why a prod
    # quick-install must also overwrite (and thereby clear) this file.
    local tmp_stamp; tmp_stamp=$(mktemp)
    _write_dev_build_json "$tmp_stamp"
    sudo cp "$tmp_stamp" "$install_dir/dev_build.json"
    sudo chown deck:deck "$install_dir/dev_build.json" 2>/dev/null || true
    rm -f "$tmp_stamp"
    if [[ "$ENV_MODE" == "dev" ]]; then
        log_success "Stamped dev_build.json ($DEV_BUILD_ID)"
    fi

    # Make sure the install dir itself is writable by deck so the
    # plugin can mkdir state under it (e.g. data/cache fallback,
    # if DECKY_PLUGIN_RUNTIME_DIR is unset). Decky guarantees
    # runtime_dir is writable but defending against missing env.
    sudo chown deck:deck "$install_dir"
    sudo find "$install_dir/bin" -type f -exec chmod +x {} \; 2>/dev/null || true

    log_info "Starting Decky plugin loader..."
    sudo systemctl start plugin_loader
    log_success "Quick-install complete - tail logs at ~/homebrew/logs/Unifideck/"
    echo "========================================="
}

# ── Install to Decky plugins dir ──────────────────────────────
# When the --install flag is used, this function will directly extract
# the new build into the Decky loader's plugin directory and restart Decky.
# Very useful for rapid local iteration on a Steam Deck.
install_plugin() {
    local plugins_dir="$HOME/homebrew/plugins"
    local install_dir="$plugins_dir/Unifideck"

    [ -d "$plugins_dir" ] || { log_error "Decky plugins dir not found: $plugins_dir"; return 1; }
    [ -f "$OUTPUT_FILE" ] || { log_error "Build output not found: $OUTPUT_FILE"; return 1; }

    echo ""
    echo "========================================="
    echo "Installing Plugin"
    echo "========================================="
    log_info "Stopping Decky plugin loader..."
    sudo systemctl stop plugin_loader 2>/dev/null || true
    sleep 1

    # Extract to a staging dir FIRST, swap only once it succeeded. This used to
    # rm -rf the live install and *then* unzip, so any extraction failure left
    # the Deck with no plugin at all - which is exactly what happened: the unzip
    # ran without sudo while $plugins_dir is root-owned (Decky Loader owns it),
    # so it could not create the directory it had just deleted. quick_install
    # sudo's every write, which is why only this path was affected.
    local staging_install="$plugins_dir/.Unifideck.incoming"
    log_info "Extracting to $plugins_dir..."
    sudo rm -rf "$staging_install"
    if ! sudo unzip -qo "$OUTPUT_FILE" -d "$staging_install" \
            || [ ! -d "$staging_install/Unifideck" ]; then
        log_error "Extraction failed - existing installation left untouched"
        sudo rm -rf "$staging_install"
        sudo systemctl start plugin_loader
        return 1
    fi

    if [ -d "$install_dir" ]; then
        log_info "Removing existing installation..."
        sudo rm -rf "$install_dir"
    fi
    sudo mv "$staging_install/Unifideck" "$install_dir"
    sudo rm -rf "$staging_install"

    # Ensure permissions are correct; decky-loader runs as root, but plugin directories
    # are usually owned by deck:deck.
    sudo chown -R deck:deck "$install_dir"
    chmod -R 755 "$install_dir"
    log_success "Installed to $install_dir"

    log_info "Starting Decky plugin loader..."
    sudo systemctl start plugin_loader
    log_success "Decky restarted - plugin active shortly"
    echo "========================================="
}

# ── Publish the dev build to a NEW GitHub release ─────────────
# OPT-IN: this runs only when the ``push`` argument is passed. A plain
# ``./build-plugin.sh dev`` (or ``dev install``) never contacts GitHub. It used
# to publish on every run, which meant a throwaway iteration build silently
# created a remote tag and deleted whatever dev release testers were on.
#
# A pushed dev build gets its OWN prerelease, tagged
# ``Dev-<UTCdate>-<UTCtime>-<shortsha>`` (e.g. ``Dev-20260808-171205-47e6d28``).
# That is how testers get an unreleased build: the in-plugin updater lists
# prereleases in its version picker and installs the ``.zip`` asset.
#
# WHY A NEW RELEASE EVERY TIME, rather than rotating one asset on a fixed
# ``Dev`` tag as this used to: GitHub orders the releases page by a value fixed
# when the release object and its tag are first created. It is NOT the release
# id, and NOT the ``created_at`` the API reports. The old rolling ``Dev``
# release proved it: id 275549827 / created 2026-01-09T16:20:19Z sat *below*
# ``Release-0.4.1`` (id 275410187 / created 2026-01-09T07:53:47Z) despite
# winning on both keys. Neither rotating an asset nor moving the tag can lift a
# release back to the top of the list. Only a brand-new release object on a
# brand-new tag can. So each build creates one, then deletes the previous dev
# release, leaving exactly one in existence at any time.
#
# THE TAG FORMAT IS LOAD-BEARING: it must contain no ``X.Y`` substring.
# ``services/updater/service.py::_parse_version_from_tag`` greps tag names for
# ``(\d+\.\d+(?:\.\d+)?)``, so a tag like ``Dev-0.7.3-g47e6d28`` would parse as
# version "0.7.3" and collide with the real ``Release-0.7.3`` in the updater's
# version picker. A date/time/sha tag has no dots at all, so it falls through to
# that parser's raw-tag branch and sorts below every real release, exactly as
# the literal ``Dev`` tag used to. ``prerelease`` must likewise stay true: it is
# the ONLY field distinguishing a dev row in both the backend and the updater UI.
#
# CREDENTIALS ARE NEVER STORED IN THIS SCRIPT OR THE REPO. Resolution order:
#   1. ``$GH_TOKEN`` / ``$GITHUB_TOKEN`` from the environment;
#   2. the token the git credential helper already keeps in
#      ``~/.git-credentials`` (``credential.helper=store``).
# The value is only ever held in a local, never echoed.
#
# Skipped entirely for: any build without the ``push`` argument (the default),
# prod builds (those go to a real versioned release via the draft flow), CI (a
# runner must not publish tester builds), and whenever
# ``UNIFIDECK_SKIP_DEV_UPLOAD`` is set to anything non-empty - that env var is
# now redundant with the default-off behaviour, but is kept as a hard override
# that still wins over an explicit ``push``.
#
# A missing credential can no longer be shrugged off the way it was when
# publishing was implicit: once ``push`` is typed, failing to publish IS a
# failure. Every hard failure below sets ``DEV_PUBLISH_FAILED``, which makes
# ``main`` exit non-zero AFTER any requested install still runs. The build
# artifact itself is never affected - the zip is already on disk by then.
DEV_TAG_PREFIX="Dev-"
LEGACY_DEV_TAG="Dev"
RETIRED_DEV_NAME="Dev (retired: use the newest Dev build)"
GH_REPO_SLUG="mubaraknumann/unifideck"
# Set by publish_dev_release when an explicitly requested push did not land.
# Read by main() only after install_plugin, so a failed upload never costs you
# the install you also asked for.
DEV_PUBLISH_FAILED=0

_gh_token() {
    # Echoes the token on stdout for capture into a local. Never log this.
    if [ -n "${GH_TOKEN:-}" ]; then printf '%s' "$GH_TOKEN"; return 0; fi
    if [ -n "${GITHUB_TOKEN:-}" ]; then printf '%s' "$GITHUB_TOKEN"; return 0; fi
    [ -f "$HOME/.git-credentials" ] || return 1
    sed -n 's|https://[^:]*:\([^@]*\)@github\.com.*|\1|p' \
        "$HOME/.git-credentials" | head -1
}

# Delete a release AND the tag it created. Deleting the release alone leaves the
# tag ref behind, which would then block any later build that lands on the same
# tag name. Best-effort: never fails the caller.
_dev_release_delete() {
    local api="$1" token="$2" rid="$3" tag="$4"
    curl -sf -X DELETE -H "Authorization: Bearer $token" \
        "$api/releases/$rid" >/dev/null 2>&1 || true
    curl -sf -X DELETE -H "Authorization: Bearer $token" \
        "$api/git/refs/tags/$tag" >/dev/null 2>&1 || true
}

publish_dev_release() {
    if [[ "$ENV_MODE" == "prod" ]]; then
        return 0
    fi
    if [ "$PUSH_DEV_RELEASE" != "1" ]; then
        log_info "Not published to GitHub (local build)."
        log_info "  Re-run with 'push' to publish this build for testers."
        return 0
    fi
    if [ -n "${UNIFIDECK_SKIP_DEV_UPLOAD:-}" ]; then
        log_warn "Dev release upload skipped (UNIFIDECK_SKIP_DEV_UPLOAD set)."
        log_warn "  Unset it to let 'push' publish."
        return 0
    fi
    if [ -n "${CI:-}${GITHUB_ACTIONS:-}" ]; then
        log_info "Dev release upload skipped (CI environment)"
        return 0
    fi
    if [ ! -f "$OUTPUT_FILE" ]; then
        log_error "Cannot publish: $ZIP_NAME not found in out/."
        DEV_PUBLISH_FAILED=1
        return 0
    fi

    local token; token=$(_gh_token 2>/dev/null || true)
    if [ -z "$token" ]; then
        log_error "Cannot publish: no GitHub token found."
        log_error "  Set GH_TOKEN, or run 'git push' once so the credential"
        log_error "  helper stores one. The built zip is still in out/."
        DEV_PUBLISH_FAILED=1
        return 0
    fi

    local api="https://api.github.com/repos/$GH_REPO_SLUG"
    local tag
    tag="${DEV_TAG_PREFIX}$(date -u '+%Y%m%d-%H%M%S')-${GIT_SHA:-nosha}"
    log_info "Publishing $ZIP_NAME as a new '$tag' release..."

    # Creating a release also creates its tag, so the target commit has to exist
    # on GitHub. The old rotate-one-asset flow never had that constraint. An
    # unpushed WIP commit falls back to the repo's default branch; either way the
    # real built commit is recorded in the body and the asset filename.
    local target
    target=$(GH_API="$api" GH_TOK="$token" GH_SHA="${GIT_SHA:-}" python3 -c '
import json, os, urllib.error, urllib.request

api, tok, sha = os.environ["GH_API"], os.environ["GH_TOK"], os.environ["GH_SHA"]
NET = (urllib.error.URLError, OSError, ValueError, KeyError)


def get(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    return json.load(urllib.request.urlopen(req, timeout=30))


resolved = ""
if sha:
    try:
        resolved = get(api + "/commits/" + sha)["sha"]
    except NET:
        resolved = ""
if not resolved:
    try:
        resolved = get(api)["default_branch"]
    except NET:
        raise SystemExit(1)
print(resolved)
' 2>/dev/null) || target=""
    if [ -z "$target" ]; then
        log_error "Publish failed. Could not reach the GitHub API, or the"
        log_error "  token lacks repo scope. Nothing was changed."
        DEV_PUBLISH_FAILED=1
        return 0
    fi
    # $GIT_SHA is abbreviated but the API echoes the full sha back, so this has
    # to be a prefix test. Exact equality would warn on every successful build.
    if [ -n "${GIT_SHA:-}" ] && [[ "$target" != "$GIT_SHA"* ]]; then
        log_warn "Commit $GIT_SHA is not on GitHub, tagging '$target' instead."
        log_warn "  Push the branch first if you want an exact tag."
    fi

    # Create the release up front, carrying the same body the rolling release
    # used to get via a separate PATCH after upload.
    local release_id
    release_id=$(GH_API="$api" GH_TOK="$token" GH_TAG="$tag" \
            GH_TARGET="$target" GH_BUILD="$DEV_BUILD_ID" \
            GH_SHA="${GIT_SHA:-unknown}" GH_BR="${GIT_BRANCH:-unknown}" \
            GH_WHEN="$(date -u '+%Y-%m-%d %H:%M UTC')" python3 -c '
import json, os, urllib.error, urllib.request

body = (
    "**Latest dev build.** For testing, not general use.\n\n"
    "- Build: `" + os.environ["GH_BUILD"] + "`\n"
    "- Commit: `" + os.environ["GH_SHA"] + "` on branch `"
    + os.environ["GH_BR"] + "`\n"
    "- Built: " + os.environ["GH_WHEN"] + "\n\n"
    "Install from Unifideck settings → Check for Updates → pick this version."
)
payload = {
    "tag_name": os.environ["GH_TAG"],
    "target_commitish": os.environ["GH_TARGET"],
    "name": "Dev Build " + os.environ["GH_BUILD"],
    "body": body,
    "draft": False,
    "prerelease": True,
    "make_latest": "false",
}
req = urllib.request.Request(
    os.environ["GH_API"] + "/releases",
    data=json.dumps(payload).encode(), method="POST")
req.add_header("Authorization", "Bearer " + os.environ["GH_TOK"])
req.add_header("Accept", "application/vnd.github+json")
req.add_header("Content-Type", "application/json")
try:
    print(json.load(urllib.request.urlopen(req, timeout=30))["id"])
except (urllib.error.URLError, OSError, ValueError, KeyError):
    raise SystemExit(1)
' 2>/dev/null) || release_id=""
    if [ -z "$release_id" ]; then
        log_error "Publish failed. Could not create the '$tag' release."
        log_error "  Nothing was changed."
        DEV_PUBLISH_FAILED=1
        return 0
    fi

    # A brand-new release has no asset to collide with, so the zip goes up under
    # its real name directly, with no temp-name/delete/rename dance needed. If
    # the upload fails, roll the release back rather than leaving an empty one
    # sitting at the top of the releases page.
    if ! curl -sf -X POST \
            -H "Authorization: Bearer $token" \
            -H "Content-Type: application/zip" \
            --data-binary "@$OUTPUT_FILE" \
            "https://uploads.github.com/repos/$GH_REPO_SLUG/releases/$release_id/assets?name=$ZIP_NAME" \
            >/dev/null 2>&1; then
        log_error "Publish failed on asset upload. Rolling back the empty"
        log_error "  '$tag' release. The previous dev release is untouched."
        _dev_release_delete "$api" "$token" "$release_id" "$tag"
        DEV_PUBLISH_FAILED=1
        return 0
    fi
    log_success "Published $ZIP_NAME as the '$tag' release"

    # Housekeeping, strictly AFTER a confirmed upload so testers are never left
    # with zero dev builds: delete every older Dev-* release (and its tag), and
    # retire the legacy rolling "Dev" release once. Stripping that one's .zip is
    # what removes it from the updater's picker, because _parse_release() skips
    # any release with no installable asset. Tag, body and counts all survive.
    GH_API="$api" GH_TOK="$token" GH_KEEP="$release_id" \
            GH_PREFIX="$DEV_TAG_PREFIX" GH_LEGACY="$LEGACY_DEV_TAG" \
            GH_RETIRED="$RETIRED_DEV_NAME" python3 -c '
import json, os, urllib.error, urllib.request

api, tok = os.environ["GH_API"], os.environ["GH_TOK"]
keep, prefix = os.environ["GH_KEEP"], os.environ["GH_PREFIX"]
legacy, retired = os.environ["GH_LEGACY"], os.environ["GH_RETIRED"]
NET = (urllib.error.URLError, OSError, ValueError)


def call(url, data=None, method="GET"):
    req = urllib.request.Request(
        url, data=None if data is None else json.dumps(data).encode(),
        method=method)
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    raw = urllib.request.urlopen(req, timeout=30).read()
    return json.loads(raw) if raw else None


try:
    releases = call(api + "/releases?per_page=100") or []
except NET:
    raise SystemExit(1)

for rel in releases:
    tag = rel.get("tag_name", "")
    if tag.startswith(prefix) and str(rel.get("id")) != keep:
        try:
            call(api + "/releases/" + str(rel["id"]), method="DELETE")
            call(api + "/git/refs/tags/" + tag, method="DELETE")
            print("  removed previous dev release " + tag)
        except NET:
            print("  could not remove previous dev release " + tag)
    elif tag == legacy and rel.get("name") != retired:
        # One-time retirement. Once renamed it is never touched again, so a
        # re-run of this build step cannot keep clobbering it.
        try:
            for asset in rel.get("assets", []):
                if asset.get("name", "").endswith(".zip"):
                    call(api + "/releases/assets/" + str(asset["id"]),
                         method="DELETE")
            call(api + "/releases/" + str(rel["id"]), method="PATCH", data={
                "name": retired,
                "body": ("Retired: dev builds now get their own release. "
                         "Grab the newest `Dev-*` prerelease from the top of "
                         "the releases list."),
            })
            print("  retired the legacy \"" + legacy + "\" release")
        except NET:
            print("  could not retire the legacy \"" + legacy + "\" release")
' 2>/dev/null || log_warn "  could not tidy up older dev releases"
    echo ""
}

# ── Phase timing ──────────────────────────────────────────────
# Build time on a Deck is dominated by network and I/O, both of which vary
# wildly hour to hour. Without per-phase numbers every slowdown gets blamed on
# whatever changed most recently, which is how the ~28 MB binary re-download
# went unnoticed for so long. Printed as a summary at the end of every build.
BUILD_PHASE_LOG=""
_phase() {
    local label="$1"; shift
    local start; start=$SECONDS
    "$@"
    local rc=$?
    BUILD_PHASE_LOG+=$(printf '\n  %-26s %4ss' "$label" "$((SECONDS - start))")
    return $rc
}

_print_phase_summary() {
    [ -n "$BUILD_PHASE_LOG" ] || return 0
    echo ""
    log_info "Build phases:${BUILD_PHASE_LOG}"
    printf '  %-26s %4ss\n' "TOTAL" "$SECONDS"
    echo ""
}

# ── Main Execution Flow ───────────────────────────────────────
main() {
    # quick-install short-circuits the full build pipeline. Use it
    # when you've only edited Python / config / bundled binaries
    # and want them live in the existing Decky install in seconds.
    if [[ "$INSTALL_AFTER" == "quick-install" ]]; then
        quick_install
        return $?
    fi

    # Run pre-flight checks
    _phase "binaries" prebuild_binaries
    check_requirements
    _phase "vendor python deps" vendor_deps
    _phase "vendor launcher cffi" vendor_launcher_cffi
    _phase "locales" gen_locales
    sync_version

    # Attempt Decky CLI containerized build first
    if check_decky_cli; then
        ENGINE=$(check_container_engine || true)
        if [ -n "$ENGINE" ]; then
            chmod -R a+rwX "$SCRIPT_DIR" || true
            _phase "containerized build" build_with_cli "$ENGINE"
        else
            log_warn "No container engine available - falling back to local build"
            _phase "local build" build_local
        fi
    else
        # Fallback if Decky CLI is totally broken
        _phase "local build" build_local
    fi

    # Publish this build as a fresh dev prerelease so testers get it.
    # Opt-in: a no-op unless the 'push' argument was passed.
    _phase "github publish" publish_dev_release
    _print_phase_summary

    # Auto-install if requested
    if [[ "$INSTALL_AFTER" == "install" ]]; then
        install_plugin
    fi

    # Report a requested-but-failed push in the exit code, last, so the install
    # above still happened. The zip in out/ is valid either way.
    if [ "$DEV_PUBLISH_FAILED" = "1" ]; then
        log_error "Build succeeded but the GitHub publish did not."
        return 1
    fi
}

main "$@"
