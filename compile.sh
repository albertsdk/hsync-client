#!/usr/bin/env bash
#
# HSYNC Client - Build Script
#
# Compiles the HSYNC client into a standalone binary using PyInstaller.
# The resulting binary is self-contained and doesn't require Python.
#
# Usage:
#   ./compile.sh [options]
#
# Options:
#   -c, --clean             Clean build artifacts before building
#   -t, --target TARGET     Build target (default: native)
#                           Supported:
#                               native               host OS / arch
#                               deb                  Debian/Ubuntu .deb
#                                                    package for host arch
#                               deb-all              .deb for amd64, arm64,
#                                                    and armhf (via Docker)
#                               wd-ex2-ultra-os5     WD My Cloud EX2 Ultra OS5
#                                                    (ARMv7, packaged as .bin)
#                               wd-os5-all           Every WD OS5 model
#                                                    (builds one binary per
#                                                     arch, packages all 10
#                                                     models in dist/wd-os5/)
#   -h, --help              Show this help message
#
# Requirements:
#   - native target: Python virtual environment (.venv); run 'poetry install'
#   - deb target: Python virtual environment (.venv) + dpkg-deb (available on
#     any Debian/Ubuntu system via dpkg-dev)
#   - deb-all / wd-ex2-ultra-os5 / wd-os5-all: Docker with qemu-user-static
#     (binfmt) configured (used for cross-arch emulation on amd64 hosts).
#
# Output:
#   native:
#     - dist/<os>-<arch>/hsync[.exe]   Standalone platform-aware executable
#       e.g. dist/linux-amd64/hsync, dist/darwin-arm64/hsync
#   deb:
#     - dist/deb/hsync-client_<ver>_<arch>.deb
#                                       Installable with dpkg -i / apt install
#   deb-all:
#     - dist/deb/hsync-client_<ver>_amd64.deb
#     - dist/deb/hsync-client_<ver>_arm64.deb
#     - dist/deb/hsync-client_<ver>_armhf.deb
#   wd-ex2-ultra-os5:
#     - dist/wd-ex2-ultra-os5/MyCloudEX2Ultra_hsync_<ver>.bin
#                                       Installable through the OS5 dashboard
#   wd-os5-all:
#     - dist/wd-os5/<MODEL>_hsync_<ver>.bin   one .bin per supported model
#
# Author: Alberto Basso
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Script directory (resolves symlinks)
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Platform detection (OS + architecture)
# Normalize OS name: linux | darwin | windows
case "$(uname -s)" in
    Linux*)               readonly OS_NAME="linux" ;;
    Darwin*)              readonly OS_NAME="darwin" ;;
    MINGW*|CYGWIN*|MSYS*) readonly OS_NAME="windows" ;;
    *)                    readonly OS_NAME="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac

# Normalize architecture: amd64 | arm64 | armv7 | x86 | ...
case "$(uname -m)" in
    x86_64|amd64)   readonly ARCH_NAME="amd64" ;;
    aarch64|arm64)  readonly ARCH_NAME="arm64" ;;
    armv7l|armv7)   readonly ARCH_NAME="armv7" ;;
    i386|i686)      readonly ARCH_NAME="x86" ;;
    *)              readonly ARCH_NAME="$(uname -m)" ;;
esac

readonly PLATFORM="${OS_NAME}-${ARCH_NAME}"

# Build configuration
readonly APP_NAME="hsync"
readonly ENTRY_POINT="hsync_client/main.py"
readonly BIN_EXT="$([[ "${OS_NAME}" == "windows" ]] && echo ".exe" || echo "")"
readonly OUTPUT_BASE="dist"
readonly OUTPUT_DIR="${OUTPUT_BASE}/${PLATFORM}"
readonly BUILD_DIR="build"
readonly VENV_DIR=".venv"
# PyInstaller lives under Scripts/ on Windows venvs and bin/ elsewhere.
if [[ "${OS_NAME}" == "windows" ]]; then
    readonly PYINSTALLER="${VENV_DIR}/Scripts/pyinstaller.exe"
else
    readonly PYINSTALLER="${VENV_DIR}/bin/pyinstaller"
fi
# --strip needs binutils and is unreliable on Windows; omit it there.
readonly STRIP_FLAG="$([[ "${OS_NAME}" == "windows" ]] && echo "" || echo "--strip")"

# -----------------------------------------------------------------------------
# Colors (disabled if not a terminal)
# -----------------------------------------------------------------------------

if [[ -t 1 ]]; then
    # ANSI-C quoting ($'...') so the variables hold the actual ESC byte and
    # render correctly with plain ``printf``/``cat`` as well as ``echo -e``.
    readonly RED=$'\033[0;31m'
    readonly GREEN=$'\033[0;32m'
    readonly YELLOW=$'\033[0;33m'
    readonly BLUE=$'\033[0;34m'
    readonly CYAN=$'\033[0;36m'
    readonly BOLD=$'\033[1m'
    readonly DIM=$'\033[2m'
    readonly RESET=$'\033[0m'
else
    readonly RED=''
    readonly GREEN=''
    readonly YELLOW=''
    readonly BLUE=''
    readonly CYAN=''
    readonly BOLD=''
    readonly DIM=''
    readonly RESET=''
fi

# -----------------------------------------------------------------------------
# Logging Functions
# -----------------------------------------------------------------------------

# Log informational message
log_info() { echo -e "${BLUE}ℹ${RESET} $*"; }

# Log success message
log_ok() { echo -e "${GREEN}✓${RESET} $*"; }

# Log warning message
log_warn() { echo -e "${YELLOW}⚠${RESET} $*"; }

# Log error message to stderr
log_error() { echo -e "${RED}✕${RESET} $*" >&2; }

# Log step/action message
log_step() { echo -e "${CYAN}→${RESET} ${BOLD}$*${RESET}"; }

# -----------------------------------------------------------------------------
# Helper Functions
# -----------------------------------------------------------------------------

# Report whether a binfmt_misc handler exists and is enabled.
# $1 = handler name (e.g. qemu-arm, qemu-aarch64)
qemu_handler_active() {
    local path="/proc/sys/fs/binfmt_misc/$1"
    [[ -e "${path}" ]] && grep -q '^enabled' "${path}" 2>/dev/null
}

# Ensure the qemu-user-static binfmt handlers required for cross-arch Docker
# emulation are registered AND active. On amd64 hosts this is mandatory for
# arm/v7 and arm64 builds; when a handler is missing the emulated layers die
# deep inside the build with a cryptic "exec format error". The registration
# is genuinely idempotent: we only (re)install when a required handler is
# missing, and we fail loudly if it cannot be activated.
# $@ = required handler names (default: qemu-arm)
ensure_qemu_binfmt() {
    local handlers=("$@")
    [[ ${#handlers[@]} -eq 0 ]] && handlers=("qemu-arm")

    log_step "Ensuring qemu-user-static binfmt handlers are active: ${handlers[*]}"

    local h missing=()
    for h in "${handlers[@]}"; do
        qemu_handler_active "${h}" || missing+=("${h}")
    done
    if [[ ${#missing[@]} -eq 0 ]]; then
        log_ok "binfmt handlers already active"
        return 0
    fi

    log_info "Registering qemu-user-static binfmt handlers..."
    if ! docker run --rm --privileged multiarch/qemu-user-static --reset -p yes \
        >/dev/null 2>&1; then
        log_error "Failed to run multiarch/qemu-user-static (is the Docker daemon reachable?)."
        exit 1
    fi

    missing=()
    for h in "${handlers[@]}"; do
        qemu_handler_active "${h}" || missing+=("${h}")
    done
    if [[ ${#missing[@]} -ne 0 ]]; then
        log_error "binfmt handlers still inactive after registration: ${missing[*]}"
        log_error "Cross-arch emulation is unavailable; aborting now to avoid a"
        log_error "misleading 'exec format error' later in the build."
        exit 1
    fi
    log_ok "binfmt handlers registered and active"
}

# Display usage information
show_help() {
    cat << EOF
${BOLD}HSYNC Client - Build Script${RESET}

${BOLD}Usage:${RESET}
    ./compile.sh [options]

${BOLD}Options:${RESET}
    -c, --clean             Clean build artifacts before building
    -t, --target TARGET     Build target (default: native)
                            Supported: native, deb, deb-all,
                            wd-ex2-ultra-os5, wd-os5-all
    -h, --help              Show this help message

${BOLD}Examples:${RESET}
    ./compile.sh                                # Native host build
    ./compile.sh --clean                        # Clean and rebuild
    ./compile.sh --target deb                   # .deb for host arch
    ./compile.sh --target deb-all               # .deb for amd64, arm64, armhf
    ./compile.sh --target wd-ex2-ultra-os5      # ARMv7 .bin for WD OS5
    ./compile.sh --target wd-os5-all            # All 10 WD OS5 models

${BOLD}Output:${RESET}
    native:
        ${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}   Standalone executable (platform: ${PLATFORM})
    deb:
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_<arch>.deb
    deb-all:
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_amd64.deb
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_arm64.deb
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_armhf.deb
    wd-ex2-ultra-os5:
        dist/wd-ex2-ultra-os5/MyCloudEX2Ultra_${APP_NAME}_<ver>.bin
    wd-os5-all:
        dist/wd-os5/<MODEL>_${APP_NAME}_<ver>.bin (one per model)
EOF
}

# Clean build artifacts
clean_artifacts() {
    log_step "Cleaning build artifacts..."

    local cleaned=0

    if [[ -d "${BUILD_DIR}" ]]; then
        rm -rf "${BUILD_DIR}"
        ((cleaned++))
    fi

    # Clean the whole dist/ tree so stale platform directories don't accumulate
    if [[ -d "${OUTPUT_BASE}" ]]; then
        rm -rf "${OUTPUT_BASE}"
        ((cleaned++))
    fi

    if [[ -f "${APP_NAME}.spec" ]]; then
        rm -f "${APP_NAME}.spec"
        ((cleaned++))
    fi

    # Clean PyInstaller cache
    if [[ -d "__pycache__" ]]; then
        find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
        ((cleaned++))
    fi

    if [[ ${cleaned} -gt 0 ]]; then
        log_ok "Cleaned ${cleaned} artifact(s)"
    else
        log_info "No artifacts to clean"
    fi
}

# Check prerequisites
check_prerequisites() {
    # Ensure we're in the correct directory
    if [[ ! -f "pyproject.toml" ]]; then
        log_error "pyproject.toml not found. Run this script from the project root."
        exit 1
    fi

    # Check for virtual environment
    if [[ ! -d ".venv" ]]; then
        log_error "Virtual environment not found."
        log_info "Run 'poetry install' to create it."
        exit 1
    fi

    # Check entry point exists
    if [[ ! -f "${ENTRY_POINT}" ]]; then
        log_error "Entry point not found: ${ENTRY_POINT}"
        exit 1
    fi
}

# Get version from pyproject.toml
get_version() {
    grep -E '^version\s*=' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/'
}

# Build the binary
build_binary() {
    local version
    version="$(get_version)"

    log_step "Building ${APP_NAME} v${version} for ${PLATFORM}..."

    # Verify PyInstaller is available
    if [[ ! -x "${PYINSTALLER}" ]]; then
        log_error "PyInstaller not found at ${PYINSTALLER}"
        log_error "Run 'poetry install' first to install dependencies"
        exit 1
    fi

    # Run PyInstaller with optimized settings
    # --strip: Remove debug symbols to reduce binary size
    # --hidden-import: Include modules not detected by static analysis
    # --collect-data: Include data files from packages (e.g., Rich unicode data)
    # --exclude-module: Exclude unnecessary modules to reduce size
    # --distpath: Place the final binary under dist/<os>-<arch>/
    #
    # LC_ALL=C: Force English locale to avoid ldconfig localization warnings
    # PYTHONWARNINGS=ignore: Suppress Python warnings during build
    LC_ALL=C PYTHONWARNINGS=ignore "${PYINSTALLER}" \
        --clean \
        --onefile \
        ${STRIP_FLAG} \
        --name "${APP_NAME}" \
        --distpath "${OUTPUT_DIR}" \
        --log-level ERROR \
        --hidden-import "websockets.legacy" \
        --hidden-import "websockets.legacy.client" \
        --hidden-import "websockets.client" \
        --hidden-import "websockets.exceptions" \
        --hidden-import "watchdog.observers" \
        --hidden-import "watchdog.events" \
        --hidden-import "rich._unicode_data" \
        --hidden-import "rich._unicode_data.unicode17-0-0" \
        --hidden-import "rich._unicode_data.unicode16-0-0" \
        --hidden-import "rich._unicode_data.unicode15-1-0" \
        --hidden-import "rich._unicode_data.unicode15-0-0" \
        --hidden-import "rich._unicode_data.unicode14-0-0" \
        --hidden-import "rich._unicode_data.unicode13-0-0" \
        --hidden-import "rich._unicode_data.unicode12-1-0" \
        --hidden-import "rich._unicode_data.unicode12-0-0" \
        --hidden-import "rich._unicode_data._versions" \
        --exclude-module "pytest" \
        --exclude-module "pytest_asyncio" \
        --exclude-module "pytest_cov" \
        --exclude-module "tkinter" \
        --exclude-module "unittest" \
        --exclude-module "pdb" \
        --exclude-module "doctest" \
        --exclude-module "pip" \
        --exclude-module "setuptools" \
        --exclude-module "wheel" \
        --exclude-module "pkg_resources" \
        --exclude-module "rapidfuzz" \
        "${ENTRY_POINT}"

    # Verify output
    local bin_path="${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}"
    if [[ -f "${bin_path}" ]]; then
        local size
        size="$(du -h "${bin_path}" | cut -f1)"

        # Clean up temporary build files
        cleanup_temp_files

        echo
        log_ok "Build successful!"
        echo
        echo -e "  ${BOLD}Binary:${RESET}   ${bin_path}"
        echo -e "  ${BOLD}Platform:${RESET} ${PLATFORM}"
        echo -e "  ${BOLD}Size:${RESET}     ${size}"
        echo -e "  ${BOLD}Version:${RESET}  ${version}"
        echo
        echo -e "${DIM}Run with: ./${bin_path} --help${RESET}"
    else
        log_error "Build failed: output binary not found at ${bin_path}"
        exit 1
    fi
}

# Clean up temporary files after successful build
cleanup_temp_files() {
    log_step "Cleaning up temporary files..."

    local cleaned=0

    # Remove build directory (PyInstaller intermediate files)
    if [[ -d "${BUILD_DIR}" ]]; then
        rm -rf "${BUILD_DIR}"
        ((cleaned++)) || true
    fi

    # Remove auto-generated spec file
    if [[ -f "${APP_NAME}.spec" ]]; then
        rm -f "${APP_NAME}.spec"
        ((cleaned++)) || true
    fi

    if [[ ${cleaned} -gt 0 ]]; then
        log_ok "Removed ${cleaned} temporary file(s)"
    fi
}

# Map uname -m to Debian architecture name.
_arch_to_deb() {
    case "$1" in
        x86_64|amd64)   echo "amd64" ;;
        aarch64|arm64)  echo "arm64" ;;
        armv7l|armv7)   echo "armhf" ;;
        *)              echo "$1" ;;
    esac
}

# -----------------------------------------------------------------------------
# Debian/Ubuntu .deb package build
# -----------------------------------------------------------------------------

# Configuration for the deb target.
readonly DEB_DIR="packaging/deb"
readonly DEB_SRC_CONTROL="${DEB_DIR}/DEBIAN"
readonly DEB_SRC_DATA="${DEB_DIR}"
readonly DEB_PKG_NAME="hsync-client"
readonly DEB_OUTPUT_DIR="${OUTPUT_BASE}/deb"
readonly DEB_BUILD_DIR="${BUILD_DIR}/deb"

# Normalise a staged .deb payload so the resulting package is lintian-clean:
#   * directories 0755 / files 0644 under etc/ and usr/ (the source tree is
#     group-writable; DEBIAN/ control scripts are handled separately);
#   * the main executable keeps its 0755 bit;
#   * the man page is gzip-compressed (-n => reproducible header).
# Shared between the native (build_deb) and Docker (build_deb_all) staging.
_finalize_deb_stage() {
    local stage_dir="$1"
    find "${stage_dir}/etc" "${stage_dir}/usr" -type d -exec chmod 0755 {} + 2>/dev/null || true
    find "${stage_dir}/etc" "${stage_dir}/usr" -type f -exec chmod 0644 {} + 2>/dev/null || true
    chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"
    local man="${stage_dir}/usr/share/man/man1/${APP_NAME}.1"
    [[ -f "${man}" ]] && gzip -n -9 -f "${man}"
    # Sensitive configuration (API/LAN keys): restrict to root only so the
    # shipped package is never world-readable. postinst re-asserts ownership.
    chmod 0750 "${stage_dir}/etc/hsync" 2>/dev/null || true
    [[ -f "${stage_dir}/etc/hsync/hsync.conf" ]] && chmod 0600 "${stage_dir}/etc/hsync/hsync.conf"
}

# Compute the shared-library dependency string for the staged main executable
# using dpkg-shlibdeps, so the .deb carries the correct, arch-specific glibc
# (libc6) minimum version. Echoes the dependency list without the
# "shlibs:Depends=" prefix (e.g. "libc6 (>= 2.36), libgcc-s1 (>= 4.2)"), or an
# empty string if dpkg-shlibdeps is unavailable or resolves nothing.
# dpkg-shlibdeps must run in the target architecture's dpkg environment: on the
# host for build_deb, inside the per-arch container for build_deb_all.
# $1 = stage_dir
_compute_shlib_depends() {
    local stage_dir="$1"
    local bin="${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"
    command -v dpkg-shlibdeps >/dev/null 2>&1 || { echo ""; return 0; }
    [[ -f "${bin}" ]] || { echo ""; return 0; }
    local tmp deps
    tmp="$(mktemp -d)"
    mkdir -p "${tmp}/debian"
    # dpkg-shlibdeps insists on a debian/control in its cwd to derive the
    # package name; a minimal stub is enough when emitting to stdout (-O).
    printf 'Source: %s\n\nPackage: %s\nArchitecture: any\n' \
        "${DEB_PKG_NAME}" "${DEB_PKG_NAME}" > "${tmp}/debian/control"
    # --ignore-missing-info skips PyInstaller's private libs (no shlibs info)
    # while still resolving system libraries such as libc6.
    deps="$(cd "${tmp}" && dpkg-shlibdeps -O --ignore-missing-info \
        "${bin}" 2>/dev/null \
        | sed -n 's/^shlibs:Depends=//p')"
    rm -rf "${tmp}"
    echo "${deps}"
}

# Append the computed shared-library dependencies to the Depends: line of the
# staged DEBIAN/control. No-op (with a warning) if none could be computed.
# $1 = stage_dir
_inject_shlib_depends() {
    local stage_dir="$1"
    local deps
    deps="$(_compute_shlib_depends "${stage_dir}")"
    if [[ -z "${deps}" ]]; then
        log_warn "dpkg-shlibdeps unavailable; libc6 dependency not added to control"
        return 0
    fi
    log_step "Adding shared-library dependencies: ${deps}"
    sed -i.bak "s/^Depends: \(.*\)$/Depends: \1, ${deps}/" "${stage_dir}/DEBIAN/control"
    rm -f "${stage_dir}/DEBIAN/control.bak"
}

# Build a .deb package for the host architecture (native build, no Docker).
build_deb() {
    local version deb_arch
    version="$(get_version)"
    deb_arch="$(_arch_to_deb "${ARCH_NAME}")"

    log_step "Building ${DEB_PKG_NAME} v${version} for ${deb_arch} (host build)..."

    # --- prerequisites ---------------------------------------------------
    check_prerequisites
    if ! command -v dpkg-deb >/dev/null 2>&1; then
        log_error "dpkg-deb is required for the deb target."
        log_info "Install it with: apt install dpkg-dev"
        exit 1
    fi
    mkdir -p "${DEB_OUTPUT_DIR}" "${DEB_BUILD_DIR}"

    # --- PyInstaller --onedir on host ------------------------------------
    # We reuse the existing build infrastructure but produce a --onedir
    # layout that we can place under /usr/lib/hsync/.
    local onedir_dist="${DEB_BUILD_DIR}/pyinstaller"
    mkdir -p "${onedir_dist}"

    log_step "Compiling ${APP_NAME} with PyInstaller (--onedir)..."
    LC_ALL=C PYTHONWARNINGS=ignore "${PYINSTALLER}" \
        --clean --onedir --noconfirm --strip \
        --name "${APP_NAME}" \
        --distpath "${onedir_dist}" \
        --workpath "${DEB_BUILD_DIR}/work" \
        --specpath "${DEB_BUILD_DIR}/work" \
        --log-level ERROR \
        --hidden-import "websockets.legacy" \
        --hidden-import "websockets.legacy.client" \
        --hidden-import "websockets.client" \
        --hidden-import "websockets.exceptions" \
        --hidden-import "watchdog.observers" \
        --hidden-import "watchdog.observers.inotify" \
        --hidden-import "watchdog.observers.inotify_buffer" \
        --hidden-import "watchdog.events" \
        --hidden-import "rich._unicode_data" \
        --hidden-import "rich._unicode_data._versions" \
        --collect-submodules pathspec \
        --collect-submodules hsync_common \
        --exclude-module "pytest" --exclude-module "pytest_asyncio" \
        --exclude-module "pytest_cov" --exclude-module "tkinter" \
        --exclude-module "unittest" --exclude-module "pdb" \
        --exclude-module "doctest" --exclude-module "pip" \
        --exclude-module "setuptools" --exclude-module "wheel" \
        --exclude-module "pkg_resources" --exclude-module "rapidfuzz" \
        "${ENTRY_POINT}"

    local built_dir="${onedir_dist}/${APP_NAME}"
    if [[ ! -d "${built_dir}" ]]; then
        log_error "PyInstaller --onedir output not found at ${built_dir}"
        exit 1
    fi

    # --- stage .deb tree -------------------------------------------------
    local stage_dir="${DEB_BUILD_DIR}/${DEB_PKG_NAME}_${version}_${deb_arch}"
    rm -rf "${stage_dir}"
    mkdir -p "${stage_dir}"

    log_step "Staging .deb tree at ${stage_dir}..."

    # Copy DEBIAN control files.
    cp -a "${DEB_SRC_CONTROL}" "${stage_dir}/"
    chmod 0755 "${stage_dir}/DEBIAN"/*inst* "${stage_dir}/DEBIAN"/*rm* 2>/dev/null || true

    # Copy data files (etc/, usr/), excluding DEBIAN/ itself.
    local item
    for item in "${DEB_SRC_DATA}"/*; do
        local bn
        bn="$(basename "${item}")"
        if [[ "${bn}" == "DEBIAN" ]]; then
            continue
        fi
        cp -a "${item}" "${stage_dir}/"
    done

    # Place the PyInstaller --onedir output under /usr/lib/hsync/.
    mkdir -p "${stage_dir}/usr/lib"
    cp -a "${built_dir}" "${stage_dir}/usr/lib/${APP_NAME}"
    chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"

    # Create a relative symlink: /usr/bin/hsync -> ../lib/hsync/hsync
    mkdir -p "${stage_dir}/usr/bin"
    ln -sf "../lib/${APP_NAME}/${APP_NAME}" "${stage_dir}/usr/bin/${APP_NAME}"

    # Normalise payload permissions and compress the man page.
    _finalize_deb_stage "${stage_dir}"

    # --- inject version / arch into control ------------------------------
    log_step "Injecting version ${version} and arch ${deb_arch} into control..."
    sed -i.bak \
        -e "s/__VERSION__/${version}/g" \
        -e "s/__ARCH__/${deb_arch}/g" \
        "${stage_dir}/DEBIAN/control"
    rm -f "${stage_dir}/DEBIAN/control.bak"

    # --- inject arch-correct shared-library deps (libc6, ...) ------------
    # Host arch == target arch here, so dpkg-shlibdeps runs directly.
    _inject_shlib_depends "${stage_dir}"

    # --- build .deb with dpkg-deb ----------------------------------------
    log_step "Building .deb package..."
    # dpkg-deb needs root:root ownership for system paths like /usr/ and /etc/.
    # Use fakeroot if available, otherwise warn (build may still work).
    local deb_cmd
    if command -v fakeroot >/dev/null 2>&1; then
        deb_cmd="fakeroot"
    else
        deb_cmd=""
        log_warn "fakeroot not available — building .deb with current user ownership"
    fi
    ${deb_cmd} dpkg-deb --build "${stage_dir}" "${DEB_OUTPUT_DIR}"

    local deb_name="${DEB_PKG_NAME}_${version}_${deb_arch}.deb"
    local deb_path="${DEB_OUTPUT_DIR}/${deb_name}"
    local size deb_abs
    size="$(du -h "${deb_path}" | cut -f1)"
    deb_abs="$(cd "$(dirname "${deb_path}")" && pwd)/$(basename "${deb_path}")"

    # Clean up PyInstaller build artifacts.
    rm -rf "${DEB_BUILD_DIR}/work" "${DEB_BUILD_DIR}/pyinstaller" "${APP_NAME}.spec" 2>/dev/null || true

    echo
    log_ok "Debian package built!"
    echo
    echo -e "  ${BOLD}Package:${RESET}  ${deb_name}"
    echo -e "  ${BOLD}Arch:${RESET}     ${deb_arch}"
    echo -e "  ${BOLD}Size:${RESET}     ${size}"
    echo -e "  ${BOLD}Version:${RESET}  ${version}"
    echo
    echo -e "${DIM}Install with:${RESET}"
    echo -e "${DIM}  sudo dpkg -i ${deb_abs}${RESET}"
    echo -e "${DIM}  sudo apt install -f  # to resolve any missing dependencies${RESET}"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  📦  Installation package:${RESET}"
    echo -e "${GREEN}${BOLD}      ${deb_abs}${RESET}"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
}

# -----------------------------------------------------------------------------
# Debian/Ubuntu .deb — All architectures (via Docker)
# -----------------------------------------------------------------------------

# Configuration for the deb-all target.
readonly DEB_ALL_DIR="${DEB_DIR}"
readonly DEB_ALL_OUTPUT_DIR="${OUTPUT_BASE}/deb"
readonly DEB_ALL_BUILD_DIR="${BUILD_DIR}/deb-all"
readonly DEB_ALL_BUILDER_IMAGE_PREFIX="hsync-deb-builder"

# Build .deb packages for all 3 Debian/Ubuntu architectures (amd64, arm64, armhf).
#
# Docker cross-compilation is used — one build per arch:
#
#   amd64 → amd64/debian:bookworm
#   arm64 → arm64v8/debian:bookworm
#   armhf → arm32v7/debian:bookworm
#
# Output: dist/deb/hsync-client_<ver>_<arch>.deb
build_deb_all() {
    local version
    version="$(get_version)"

    log_step "Building ${DEB_PKG_NAME} v${version} for all 3 Debian/Ubuntu architectures..."

    # ── prerequisites ─────────────────────────────────────────────────────────
    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the deb-all target."
        exit 1
    fi
    if [[ ! -d "${DEB_SRC_CONTROL}" ]]; then
        log_error "DEBIAN control directory missing: ${DEB_SRC_CONTROL}"
        exit 1
    fi

    mkdir -p "${DEB_ALL_OUTPUT_DIR}"

    local repo_root
    repo_root="$(cd .. && pwd)"

    # ── qemu (required for arm64/armhf on amd64 hosts) ────────────────────────
    ensure_qemu_binfmt qemu-aarch64 qemu-arm

    # Common PyInstaller flags — used for all arch builds.
    local pyinstaller_flags="
        --clean --onedir --noconfirm --strip
        --name ${APP_NAME}
        --log-level ERROR
        --hidden-import websockets.legacy
        --hidden-import websockets.legacy.client
        --hidden-import websockets.client
        --hidden-import websockets.exceptions
        --hidden-import watchdog.observers
        --hidden-import watchdog.observers.inotify
        --hidden-import watchdog.observers.inotify_buffer
        --hidden-import watchdog.events
        --hidden-import rich._unicode_data
        --hidden-import rich._unicode_data._versions
        --collect-submodules pathspec
        --collect-submodules hsync_common
        --exclude-module pytest --exclude-module pytest_asyncio
        --exclude-module pytest_cov --exclude-module tkinter
        --exclude-module unittest --exclude-module pdb
        --exclude-module doctest --exclude-module pip
        --exclude-module setuptools --exclude-module wheel
        --exclude-module pkg_resources --exclude-module rapidfuzz
        ${ENTRY_POINT}"

    # Architecture definitions: deb_arch  docker_platform  base_image
    local -A arch_docker_platform
    local -A arch_base_image
    arch_docker_platform["amd64"]="linux/amd64"
    arch_base_image["amd64"]="amd64/debian:bookworm"
    arch_docker_platform["arm64"]="linux/arm64"
    arch_base_image["arm64"]="arm64v8/debian:bookworm"
    arch_docker_platform["armhf"]="linux/arm/v7"
    arch_base_image["armhf"]="arm32v7/debian:bookworm"

    local -a deb_archs=("amd64" "arm64" "armhf")
    local out_abs
    out_abs="$(cd "${DEB_ALL_OUTPUT_DIR}" && pwd)"

    for deb_arch in "${deb_archs[@]}"; do
        local docker_platform="${arch_docker_platform[${deb_arch}]}"
        local base_image="${arch_base_image[${deb_arch}]}"
        local builder_tag="${DEB_ALL_BUILDER_IMAGE_PREFIX}:${deb_arch}"
        local arch_build_dir="${DEB_ALL_BUILD_DIR}/${deb_arch}"
        local bin_dir="${arch_build_dir}/pyinstaller"
        local work_dir="${arch_build_dir}/work"
        local stage_dir="${arch_build_dir}/${DEB_PKG_NAME}_${version}_${deb_arch}"
        mkdir -p "${bin_dir}" "${work_dir}"

        echo
        log_step "─── ${deb_arch} build ───────────────────────────────────────"

        # --- builder image ----------------------------------------------------
        log_step "Building Docker image ${builder_tag}..."
        docker build \
            --platform "${docker_platform}" \
            --build-arg BASE_IMAGE="${base_image}" \
            --build-arg TARGET_PLATFORM="${docker_platform}" \
            -t "${builder_tag}" \
            -f "${DEB_ALL_DIR}/Dockerfile.builder" \
            "${DEB_ALL_DIR}"

        # --- PyInstaller in container -----------------------------------------
        # Run as the host uid:gid so the bundle written under build/deb-all and
        # the in-project .venv (created in the mounted tree) stay caller-owned;
        # otherwise the host-side cleanup (rm -rf build) fails on root files.
        # HOME/XDG_CACHE_HOME point at /tmp because an arbitrary uid has no
        # writable home for the poetry/PyInstaller caches.
        log_step "Compiling ${deb_arch} binary inside container..."
        docker run --rm \
            --platform "${docker_platform}" \
            -u "$(id -u):$(id -g)" \
            -e HOME=/tmp -e XDG_CACHE_HOME=/tmp \
            -v "${repo_root}:/repo" \
            -w /repo/hsync-client \
            -e LC_ALL=C -e PYTHONWARNINGS=ignore \
            -e PIP_DEFAULT_TIMEOUT=300 -e PIP_RETRIES=10 \
            -e POETRY_REQUESTS_TIMEOUT=300 \
            "${builder_tag}" \
            bash -c "
                set -euo pipefail
                poetry config virtualenvs.in-project true
                poetry config installer.max-workers 4
                install_ok=0
                for attempt in 1 2 3; do
                    if poetry install --no-root --without dev; then install_ok=1; break; fi
                    echo \"poetry install attempt \${attempt}/3 failed, retrying in 10s...\" >&2
                    sleep 10
                done
                [ \"\${install_ok}\" -eq 1 ] || { echo 'poetry install failed after 3 attempts' >&2; exit 1; }
                poetry run pyinstaller \
                    ${pyinstaller_flags} \
                    --distpath ${bin_dir} \
                    --workpath ${work_dir} \
                    --specpath ${work_dir}
            "

        local built_dir="${bin_dir}/${APP_NAME}"
        if [[ ! -d "${built_dir}" ]]; then
            log_error "${deb_arch} build directory not produced at ${built_dir}"
            exit 1
        fi
        log_ok "${deb_arch} binary ready ($(du -sh "${built_dir}" | cut -f1))"

        # --- stage .deb tree --------------------------------------------------
        log_step "Staging .deb tree for ${deb_arch}..."
        rm -rf "${stage_dir}"
        mkdir -p "${stage_dir}"

        # Copy DEBIAN control files.
        cp -a "${DEB_SRC_CONTROL}" "${stage_dir}/"
        chmod 0755 "${stage_dir}/DEBIAN"/*inst* "${stage_dir}/DEBIAN"/*rm* 2>/dev/null || true

        # Copy data files (etc/, usr/), excluding DEBIAN/ itself.
        local item
        for item in "${DEB_SRC_DATA}"/*; do
            local bn
            bn="$(basename "${item}")"
            if [[ "${bn}" == "DEBIAN" || "${bn}" == "Dockerfile.builder" ]]; then
                continue
            fi
            cp -a "${item}" "${stage_dir}/"
        done

        # Place the PyInstaller --onedir output under /usr/lib/hsync/.
        mkdir -p "${stage_dir}/usr/lib"
        cp -a "${built_dir}" "${stage_dir}/usr/lib/${APP_NAME}"
        chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"

        # Create a relative symlink: /usr/bin/hsync -> ../lib/hsync/hsync
        mkdir -p "${stage_dir}/usr/bin"
        ln -sf "../lib/${APP_NAME}/${APP_NAME}" "${stage_dir}/usr/bin/${APP_NAME}"

        # Normalise payload permissions and compress the man page.
        _finalize_deb_stage "${stage_dir}"

        # Inject version / arch into control.
        sed -i.bak \
            -e "s/__VERSION__/${version}/g" \
            -e "s/__ARCH__/${deb_arch}/g" \
            "${stage_dir}/DEBIAN/control"
        rm -f "${stage_dir}/DEBIAN/control.bak"

        # --- build .deb with dpkg-deb -----------------------------------------
        log_step "Building .deb for ${deb_arch}..."
        # Inside the container dpkg-deb runs as root — no fakeroot needed — so
        # the package's internal /usr and /etc paths are owned root:root.
        # dpkg-shlibdeps runs here too (not on the host) so the libc6 minimum
        # version is resolved against the target architecture's dpkg database.
        # The .deb ownership is captured by dpkg-deb at build time, so chowning
        # the host-mounted /pkg (control rewritten by root) and the produced
        # .deb back to the caller afterwards keeps the host tree cleanable
        # (rm -rf build / dist) without touching the archive's internal owners.
        docker run --rm \
            --platform "${docker_platform}" \
            -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
            -v "${stage_dir}:/pkg" \
            -v "${out_abs}:/out" \
            -w /pkg \
            "${builder_tag}" \
            bash -c "
                set -e
                bin=/pkg/usr/lib/${APP_NAME}/${APP_NAME}
                tmp=\$(mktemp -d)
                mkdir -p \"\${tmp}/debian\"
                printf 'Source: %s\n\nPackage: %s\nArchitecture: any\n' '${DEB_PKG_NAME}' '${DEB_PKG_NAME}' > \"\${tmp}/debian/control\"
                deps=\$(cd \"\${tmp}\" && dpkg-shlibdeps -O --ignore-missing-info \"\${bin}\" 2>/dev/null | sed -n 's/^shlibs:Depends=//p')
                rm -rf \"\${tmp}\"
                if [ -n \"\${deps}\" ]; then
                    sed -i \"s/^Depends: \\(.*\\)\$/Depends: \\1, \${deps}/\" /pkg/DEBIAN/control
                    echo \"Added shared-library dependencies: \${deps}\"
                else
                    echo 'WARNING: dpkg-shlibdeps resolved no dependencies' >&2
                fi
                dpkg-deb --build /pkg /out
                chmod 0644 /out/${DEB_PKG_NAME}_${version}_${deb_arch}.deb
                chown -R \"\${HOST_UID}:\${HOST_GID}\" /pkg \"/out/${DEB_PKG_NAME}_${version}_${deb_arch}.deb\"
            "

        local deb_name="${DEB_PKG_NAME}_${version}_${deb_arch}.deb"
        local deb_path="${out_abs}/${deb_name}"
        if [[ ! -f "${deb_path}" ]]; then
            log_error ".deb not produced for ${deb_arch} at ${deb_path}"
            exit 1
        fi
        local pkg_size
        pkg_size="$(du -h "${deb_path}" | cut -f1)"
        log_ok "  → ${deb_name} (${pkg_size})"
    done

    # ── Summary ───────────────────────────────────────────────────────────────
    echo
    log_ok "All 3 Debian packages built!"
    echo
    echo -e "  ${BOLD}Version:${RESET}   ${version}"
    echo -e "  ${BOLD}Output:${RESET}    ${out_abs}/"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  📦  Installation packages:${RESET}"
    for deb_arch in "${deb_archs[@]}"; do
        local f="${out_abs}/${DEB_PKG_NAME}_${version}_${deb_arch}.deb"
        [[ -f "${f}" ]] && echo -e "${GREEN}${BOLD}      $(basename "${f}")${RESET}"
    done
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo
    echo -e "${DIM}Install with:  sudo dpkg -i ${out_abs}/${DEB_PKG_NAME}_${version}_<arch>.deb${RESET}"
}

# -----------------------------------------------------------------------------
# WD My Cloud EX2 Ultra (OS5) build
# -----------------------------------------------------------------------------

# Configuration for the wd-ex2-ultra-os5 target.
readonly WDOS5_DIR="packaging/wd-os5"
readonly WDOS5_SRC_APKG="${WDOS5_DIR}/Apkg"
readonly WDOS5_OUTPUT_DIR="${OUTPUT_BASE}/wd-ex2-ultra-os5"
readonly WDOS5_BUILD_DIR="${BUILD_DIR}/wdos5"
# NOTE: the staging directory **must** be named after the package
# (``Package:`` field of apkg.rc / ``APP_NAME``) because mksapkg-OS5 tars its
# CWD basename verbatim, and WD OS5 rejects the .bin at upload time with
# ``Failed to load the app package for installation`` if the root directory
# inside the tar is not the package name.
readonly WDOS5_STAGE_DIR="${WDOS5_BUILD_DIR}/${APP_NAME}"
readonly WDOS5_BUILDER_IMAGE="hsync-wdos5-builder:armv7"
readonly WDOS5_MKSAPKG_URL="https://raw.githubusercontent.com/WDCommunity/wdpksrc/master/mksapkg-OS5"
readonly WDOS5_CACHE_DIR="${WDOS5_DIR}/.cache"
readonly WDOS5_MKSAPKG="${WDOS5_CACHE_DIR}/mksapkg-OS5"
readonly WDOS5_MODEL="MyCloudEX2Ultra"
# Runtime image used to execute mksapkg-OS5 (see ``_wdos5_build_mksapkg_image``).
readonly WDOS5_MKSAPKG_IMAGE="hsync-wdos5-mksapkg:bullseye"
readonly WDOS5_MKSAPKG_DOCKERFILE="${WDOS5_DIR}/Dockerfile.mksapkg"

# Minimal Docker build context staged by ``_wdos5_stage_context``. It mirrors
# the ``hsync-client`` / ``hsync-common`` sibling layout so the builder image
# can install the path dependency and bake the sources (see
# packaging/wd-os5/Dockerfile.builder).
readonly WDOS5_CTX_DIR="${WDOS5_BUILD_DIR}/ctx"

# PyInstaller flags shared by every WD OS5 build (single target and all-models).
# The entry point is intentionally last; ``_run_pyinstaller_in_docker`` appends
# the per-build --distpath/--workpath/--specpath after it.
readonly PYINSTALLER_COMMON_FLAGS="
    --clean --onedir --noconfirm --strip
    --name ${APP_NAME}
    --log-level ERROR
    --hidden-import websockets.legacy
    --hidden-import websockets.legacy.client
    --hidden-import websockets.client
    --hidden-import websockets.exceptions
    --hidden-import watchdog.observers
    --hidden-import watchdog.observers.inotify
    --hidden-import watchdog.observers.inotify_buffer
    --hidden-import watchdog.events
    --hidden-import rich._unicode_data
    --hidden-import rich._unicode_data._versions
    --collect-submodules pathspec
    --collect-submodules hsync_common
    --exclude-module pytest --exclude-module pytest_asyncio
    --exclude-module pytest_cov --exclude-module tkinter
    --exclude-module unittest --exclude-module pdb
    --exclude-module doctest --exclude-module pip
    --exclude-module setuptools --exclude-module wheel
    --exclude-module pkg_resources --exclude-module rapidfuzz
    ${ENTRY_POINT}"

# Build the small amd64 image that runs mksapkg-OS5. The upstream tool is a
# prebuilt x86-64 binary linked against ``libxml2.so.2`` and it shells out to
# ``openssl bf-cbc`` (a legacy cipher) to sign apkg.sign. Modern hosts no longer
# provide either — e.g. Ubuntu 26.04 ships libxml2 ``.so.16`` only, and OpenSSL 3
# hides bf-cbc behind the legacy provider — so the tool runs inside a Debian
# bullseye container that still carries libxml2.so.2 and an OpenSSL 1.1.1 with
# bf-cbc enabled by default (see Dockerfile.mksapkg). Cached after first build.
_wdos5_build_mksapkg_image() {
    log_step "Building mksapkg runtime image ${WDOS5_MKSAPKG_IMAGE} (cached after first run)..."
    DOCKER_BUILDKIT=1 docker build \
        --platform linux/amd64 \
        -t "${WDOS5_MKSAPKG_IMAGE}" \
        -f "${WDOS5_MKSAPKG_DOCKERFILE}" \
        "${WDOS5_DIR}"
}

# Run mksapkg-OS5 against a staged package tree inside the runtime image.
# mksapkg-OS5 has no output-path flag: it ``cd ..`` and drops the .bin in the
# parent of its working directory, so the staging dir's parent is mounted at
# /work and the staging dir itself is the container CWD. Running with the host
# uid keeps the produced artifacts owned by the caller.
# Args: stage_abs  model
_wdos5_run_mksapkg() {
    local stage_abs="$1" model="$2"
    local stage_parent stage_name mksapkg_abs
    stage_parent="$(dirname "${stage_abs}")"
    stage_name="$(basename "${stage_abs}")"
    mksapkg_abs="$(cd "$(dirname "${WDOS5_MKSAPKG}")" && pwd)/$(basename "${WDOS5_MKSAPKG}")"
    docker run --rm \
        --platform linux/amd64 \
        -u "$(id -u):$(id -g)" \
        -v "${stage_parent}:/work" \
        -v "${mksapkg_abs}:/usr/local/bin/mksapkg-OS5:ro" \
        -w "/work/${stage_name}" \
        "${WDOS5_MKSAPKG_IMAGE}" \
        mksapkg-OS5 -E -s -m "${model}"
}

# Stage the minimal Docker build context consumed by Dockerfile.builder.
#
# The builder image installs the project's dependencies and bakes the sources
# as cached layers, so the context must expose both projects in their relative
# ``../hsync-common`` layout. __pycache__ directories are pruned to keep the
# context (and therefore the layer cache) stable across runs.
_wdos5_stage_context() {
    log_step "Staging Docker build context at ${WDOS5_CTX_DIR}..."
    rm -rf "${WDOS5_CTX_DIR}"
    mkdir -p "${WDOS5_CTX_DIR}/hsync-client" "${WDOS5_CTX_DIR}/hsync-common"
    cp -a pyproject.toml poetry.lock README.md "${WDOS5_CTX_DIR}/hsync-client/"
    cp -a hsync_client "${WDOS5_CTX_DIR}/hsync-client/"
    cp -a ../hsync-common/pyproject.toml ../hsync-common/README.md \
        "${WDOS5_CTX_DIR}/hsync-common/"
    cp -a ../hsync-common/hsync_common "${WDOS5_CTX_DIR}/hsync-common/"
    find "${WDOS5_CTX_DIR}" -name __pycache__ -type d -prune -exec rm -rf {} +
}

# Build the WD OS5 builder image for one CPU family.
# Args: docker_platform  base_image  builder_tag
_wdos5_docker_build() {
    local docker_platform="$1" base_image="$2" builder_tag="$3"
    log_step "Building Docker image ${builder_tag} (cached after first run)..."
    DOCKER_BUILDKIT=1 docker build \
        --platform "${docker_platform}" \
        --build-arg "BASE_IMAGE=${base_image}" \
        --build-arg "TARGET_PLATFORM=${docker_platform}" \
        -t "${builder_tag}" \
        -f "${WDOS5_DIR}/Dockerfile.builder" \
        "${WDOS5_CTX_DIR}"
}

# Run PyInstaller inside the pre-built builder image. Dependencies and sources
# are already baked into the image, so this step only produces the frozen
# binary. The host ${out_dir} is mounted at /out; PyInstaller writes /out/bin
# (the onedir bundle) and /out/work (intermediates).
# Args: docker_platform  out_dir  builder_tag
_run_pyinstaller_in_docker() {
    local docker_platform="$1" out_dir="$2" builder_tag="$3"
    local out_abs
    mkdir -p "${out_dir}"
    out_abs="$(cd "${out_dir}" && pwd)"
    # Run as the host uid:gid so the produced bundle under /out (the host
    # ${out_dir}, e.g. build/wdos5) is owned by the caller and not root —
    # otherwise the host-side cleanup (rm -rf build) of a later native build
    # fails with "Permission denied". HOME/XDG_CACHE_HOME are pointed at /tmp
    # because an arbitrary uid has no writable home for PyInstaller's cache.
    docker run --rm \
        --platform "${docker_platform}" \
        -u "$(id -u):$(id -g)" \
        -e HOME=/tmp -e XDG_CACHE_HOME=/tmp \
        -v "${out_abs}:/out" \
        -w /build/hsync-client \
        -e LC_ALL=C -e PYTHONWARNINGS=ignore \
        "${builder_tag}" \
        pyinstaller \
            ${PYINSTALLER_COMMON_FLAGS} \
            --distpath /out/bin \
            --workpath /out/work \
            --specpath /out/work
}

# Build the .bin installer for WD My Cloud OS5 (ARMv7).
build_wd_os5() {
    local version
    version="$(get_version)"

    log_step "Building ${APP_NAME} v${version} for wd-ex2-ultra-os5 (ARMv7)..."

    # --- prerequisites ---------------------------------------------------
    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the wd-ex2-ultra-os5 target."
        exit 1
    fi
    if [[ ! -d "${WDOS5_SRC_APKG}" ]]; then
        log_error "Packaging tree missing: ${WDOS5_SRC_APKG}"
        exit 1
    fi
    mkdir -p "${WDOS5_OUTPUT_DIR}" "${WDOS5_CACHE_DIR}" "${WDOS5_BUILD_DIR}"

    # --- qemu-user-static (binfmt) --------------------------------------
    ensure_qemu_binfmt qemu-arm

    # --- builder image (dependencies + sources baked as cached layers) --
    _wdos5_stage_context
    _wdos5_docker_build linux/arm/v7 arm32v7/debian:buster "${WDOS5_BUILDER_IMAGE}"

    # --- PyInstaller inside the emulated container ----------------------
    # The image already carries the installed deps and sources, so this only
    # produces the frozen binary under build/wdos5/bin/ (mounted at /out),
    # keeping the source tree clean for the Apkg staging step below.
    log_step "Running PyInstaller inside the ARMv7 container..."
    _run_pyinstaller_in_docker linux/arm/v7 "${WDOS5_BUILD_DIR}" "${WDOS5_BUILDER_IMAGE}"

    local built_dir="${WDOS5_BUILD_DIR}/bin/${APP_NAME}"
    if [[ ! -d "${built_dir}" ]]; then
        log_error "ARMv7 build directory not produced at ${built_dir}"
        exit 1
    fi

    # --- stage Apkg/ for mksapkg ----------------------------------------
    # mksapkg-OS5 packs the directory in place, so we materialise a
    # disposable copy under build/ to keep the source tree pristine and to
    # avoid mutating the apkg.rc template with the rendered version.
    log_step "Staging Apkg tree at ${WDOS5_STAGE_DIR}..."
    rm -rf "${WDOS5_STAGE_DIR}"
    mkdir -p "${WDOS5_STAGE_DIR}"
    cp -a "${WDOS5_SRC_APKG}/." "${WDOS5_STAGE_DIR}/"

    mkdir -p "${WDOS5_STAGE_DIR}/bin"
    cp -a "${built_dir}" "${WDOS5_STAGE_DIR}/bin/"
    chmod 0755 "${WDOS5_STAGE_DIR}/bin/${APP_NAME}/${APP_NAME}"
    # mksapkg-OS5 tars the staged tree as-is: the lifecycle hooks (preinst,
    # install, start, stop, remove, init, clean) must be executable or the
    # install on the NAS fails silently.
    chmod 0755 "${WDOS5_STAGE_DIR}"/*.sh 2>/dev/null || true

    log_step "Injecting version ${version} into staged apkg.rc..."
    sed -i.bak -e "s/__HSYNC_VERSION__/${version}/g" \
        "${WDOS5_STAGE_DIR}/apkg.rc"
    rm -f "${WDOS5_STAGE_DIR}/apkg.rc.bak"
    echo "${version}" > "${WDOS5_STAGE_DIR}/VERSION"

    # --- mksapkg-OS5 -----------------------------------------------------
    if [[ ! -x "${WDOS5_MKSAPKG}" ]]; then
        log_step "Fetching mksapkg-OS5 from WDCommunity/wdpksrc..."
        curl -fsSL "${WDOS5_MKSAPKG_URL}" -o "${WDOS5_MKSAPKG}"
        chmod 0755 "${WDOS5_MKSAPKG}"
    fi
    _wdos5_build_mksapkg_image

    log_step "Building OS5 .bin package..."
    local stage_abs stage_parent out_abs
    stage_abs="$(cd "${WDOS5_STAGE_DIR}" && pwd)"
    stage_parent="$(dirname "${stage_abs}")"
    out_abs="$(cd "${WDOS5_OUTPUT_DIR}" && pwd)"

    # mksapkg-OS5 has no output-path flag: it runs `cd ..` and drops the
    # .bin in the parent of its current working directory. Clean any stale
    # artifact there so the lookup below picks up the fresh one.
    rm -f "${stage_parent}/${WDOS5_MODEL}_${APP_NAME}_"*.bin*
    _wdos5_run_mksapkg "${stage_abs}" "${WDOS5_MODEL}"

    local produced_bin bin_path
    produced_bin="$(ls -t "${stage_parent}/${WDOS5_MODEL}_${APP_NAME}_"*.bin* 2>/dev/null | head -1)"
    if [[ -z "${produced_bin}" || ! -f "${produced_bin}" ]]; then
        log_error "mksapkg-OS5 did not produce a .bin in ${stage_parent}/"
        exit 1
    fi
    mv -f "${produced_bin}" "${out_abs}/"
    bin_path="${out_abs}/$(basename "${produced_bin}")"

    local size bin_abs
    size="$(du -h "${bin_path}" | cut -f1)"
    bin_abs="$(cd "$(dirname "${bin_path}")" && pwd)/$(basename "${bin_path}")"

    echo
    log_ok "WD OS5 package built!"
    echo
    echo -e "  ${BOLD}Model:${RESET}    ${WDOS5_MODEL}"
    echo -e "  ${BOLD}Arch:${RESET}     linux/arm/v7 (glibc <= 2.28 / works up to 2.30)"
    echo -e "  ${BOLD}Size:${RESET}     ${size}"
    echo -e "  ${BOLD}Version:${RESET}  ${version}"
    echo
    echo -e "${DIM}Install via My Cloud OS5 dashboard:${RESET}"
    echo -e "${DIM}  Apps → Install an App manually → choose this .bin${RESET}"
    echo
    # Final highlighted block — easy to spot and copy
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  📦  Installation package:${RESET}"
    echo -e "${GREEN}${BOLD}      ${bin_abs}${RESET}"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
}

# -----------------------------------------------------------------------------
# WD My Cloud OS5 — All models
# -----------------------------------------------------------------------------

# Package a single WD OS5 model .bin installer from a pre-built binary.
#
# Args:
#   $1  model      — mksapkg-OS5 model name (e.g. MyCloudEX2Ultra)
#   $2  binary     — path to the compiled hsync binary for this arch
#   $3  version    — version string (e.g. 1.2.3)
#   $4  stage_base — directory under which a disposable <model>/<APP_NAME>/
#                    staging tree is created (the inner dir name must equal
#                    APP_NAME or OS5 rejects the .bin at upload time)
#   $5  out_abs    — absolute path to the final output directory
#
# Requires the mksapkg runtime image (``_wdos5_build_mksapkg_image``) to be
# built beforehand by the caller.
_wdos5_package_model() {
    local model="$1" binary_path="$2" version="$3"
    local stage_base="$4" out_abs="$5"

    # Disposable staging copy so the source tree stays pristine. The inner
    # directory **must** be named after the package (APP_NAME) — mksapkg-OS5
    # tars its CWD basename verbatim and OS5 rejects packages whose tar root
    # is not the Package: name with ``Failed to load the app package``.
    local stage_dir="${stage_base}/${model}/${APP_NAME}"
    rm -rf "${stage_dir}"
    mkdir -p "${stage_dir}"
    cp -a "${WDOS5_SRC_APKG}/." "${stage_dir}/"

    # Place the binary.
    mkdir -p "${stage_dir}/bin"
    cp -a "${binary_path}" "${stage_dir}/bin/"
    chmod 0755 "${stage_dir}/bin/${APP_NAME}/${APP_NAME}"
    # Lifecycle hook scripts must be executable or the install fails silently.
    chmod 0755 "${stage_dir}"/*.sh 2>/dev/null || true

    # Inject version.
    sed -i.bak -e "s/__HSYNC_VERSION__/${version}/g" "${stage_dir}/apkg.rc"
    rm -f "${stage_dir}/apkg.rc.bak"
    echo "${version}" > "${stage_dir}/VERSION"

    # mksapkg-OS5 drops the .bin in the *parent* of its CWD.
    local stage_abs stage_parent
    stage_abs="$(cd "${stage_dir}" && pwd)"
    stage_parent="$(dirname "${stage_abs}")"

    rm -f "${stage_parent}/${model}_${APP_NAME}_"*.bin*
    _wdos5_run_mksapkg "${stage_abs}" "${model}"

    local produced
    produced="$(ls -t "${stage_parent}/${model}_${APP_NAME}_"*.bin* 2>/dev/null | head -1)"
    if [[ -z "${produced}" || ! -f "${produced}" ]]; then
        log_error "mksapkg-OS5 did not produce a .bin for ${model}"
        return 1
    fi
    mv -f "${produced}" "${out_abs}/"
    log_ok "  → $(basename "${produced}")"
}

# Build .bin installers for every WD My Cloud OS5 model (10 total).
#
# Two Docker builds are performed — one per CPU family — and the same
# PyInstaller binary is shared across all models of that family:
#
#   ARMv7 (4 models) → arm32v7/debian:buster
#       MyCloudEX2Ultra  WDMyCloudMirror  WDMyCloud  WDCloud
#
#   amd64 (6 models) → amd64/debian:buster
#       WDMyCloudEX2100  WDMyCloudEX4100  WDMyCloudDL2100
#       WDMyCloudDL4100  MyCloudPR2100    MyCloudPR4100
#
# Output: dist/wd-os5/<MODEL>_hsync_<ver>.bin
build_wd_os5_all() {
    local version
    version="$(get_version)"

    log_step "Building ${APP_NAME} v${version} for all 10 WD My Cloud OS5 models..."

    # ── prerequisites ─────────────────────────────────────────────────────────
    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the wd-os5-all target."
        exit 1
    fi
    if [[ ! -d "${WDOS5_SRC_APKG}" ]]; then
        log_error "Packaging tree missing: ${WDOS5_SRC_APKG}"
        exit 1
    fi

    local all_out="${OUTPUT_BASE}/wd-os5"
    local all_build="${BUILD_DIR}/wdos5/all"
    mkdir -p "${all_out}" "${WDOS5_CACHE_DIR}"

    # ── mksapkg-OS5 ──────────────────────────────────────────────────────────
    if [[ ! -x "${WDOS5_MKSAPKG}" ]]; then
        log_step "Fetching mksapkg-OS5 from WDCommunity/wdpksrc..."
        curl -fsSL "${WDOS5_MKSAPKG_URL}" -o "${WDOS5_MKSAPKG}"
        chmod 0755 "${WDOS5_MKSAPKG}"
    fi
    _wdos5_build_mksapkg_image

    # ── qemu (ARMv7 emulation on amd64 hosts) ────────────────────────────────
    ensure_qemu_binfmt qemu-arm

    # ── Docker build context (shared by both CPU families) ───────────────────
    # Both builder images consume the same staged context; only the platform
    # and base image differ. PyInstaller flags live in PYINSTALLER_COMMON_FLAGS.
    _wdos5_stage_context

    # ── ARMv7 binary ──────────────────────────────────────────────────────────
    echo
    log_step "─── ARMv7 build ───────────────────────────────────────"
    local armv7_tag="hsync-wdos5-builder:armv7"
    local armv7_out="${all_build}/armv7"
    local armv7_binary="${armv7_out}/bin/${APP_NAME}"

    _wdos5_docker_build linux/arm/v7 arm32v7/debian:buster "${armv7_tag}"

    log_step "Compiling ARMv7 binary inside container..."
    _run_pyinstaller_in_docker linux/arm/v7 "${armv7_out}" "${armv7_tag}"

    if [[ ! -d "${armv7_binary}" ]]; then
        log_error "ARMv7 build directory not produced at ${armv7_binary}"
        exit 1
    fi
    log_ok "ARMv7 binary ready ($(du -sh "${armv7_binary}" | cut -f1))"

    # ── amd64 binary ──────────────────────────────────────────────────────────
    echo
    log_step "─── amd64 build ───────────────────────────────────────"
    local amd64_tag="hsync-wdos5-builder:amd64"
    local amd64_out="${all_build}/amd64"
    local amd64_binary="${amd64_out}/bin/${APP_NAME}"

    _wdos5_docker_build linux/amd64 amd64/debian:buster "${amd64_tag}"

    log_step "Compiling amd64 binary inside container..."
    _run_pyinstaller_in_docker linux/amd64 "${amd64_out}" "${amd64_tag}"

    if [[ ! -d "${amd64_binary}" ]]; then
        log_error "amd64 build directory not produced at ${amd64_binary}"
        exit 1
    fi
    log_ok "amd64 binary ready ($(du -sh "${amd64_binary}" | cut -f1))"

    # ── Package all 10 models ─────────────────────────────────────────────────
    local stage_base="${all_build}/stage"
    local out_abs
    out_abs="$(cd "${all_out}" && pwd)"

    local -a armv7_models=("MyCloudEX2Ultra" "WDMyCloudMirror" "WDMyCloud" "WDCloud")
    local -a amd64_models=("WDMyCloudEX2100" "WDMyCloudEX4100" "WDMyCloudDL2100" \
                            "WDMyCloudDL4100" "MyCloudPR2100" "MyCloudPR4100")

    echo
    log_step "─── Packaging ARMv7 models (${#armv7_models[@]}) ─────────────────────────────"
    for model in "${armv7_models[@]}"; do
        _wdos5_package_model "${model}" "${armv7_binary}" "${version}" \
            "${stage_base}" "${out_abs}"
    done

    echo
    log_step "─── Packaging amd64 models (${#amd64_models[@]}) ─────────────────────────────"
    for model in "${amd64_models[@]}"; do
        _wdos5_package_model "${model}" "${amd64_binary}" "${version}" \
            "${stage_base}" "${out_abs}"
    done

    # ── Summary ───────────────────────────────────────────────────────────────
    local total=$(( ${#armv7_models[@]} + ${#amd64_models[@]} ))
    echo
    log_ok "All ${total} WD OS5 packages built!"
    echo
    echo -e "  ${BOLD}Version:${RESET}   ${version}"
    echo -e "  ${BOLD}Output:${RESET}    ${out_abs}/"
    echo
    echo -e "  ${BOLD}ARMv7 (${#armv7_models[@]}):${RESET}  ${armv7_models[*]}"
    echo -e "  ${BOLD}amd64 (${#amd64_models[@]}):${RESET}  ${amd64_models[*]}"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  📦  Installation packages:${RESET}"
    for f in "${out_abs}/"*_"${APP_NAME}"_*.bin; do
        [[ -f "${f}" ]] && echo -e "${GREEN}${BOLD}      $(basename "${f}")${RESET}"
    done
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    local do_clean=false
    local target="native"

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c|--clean)
                do_clean=true
                shift
                ;;
            -t|--target)
                [[ $# -ge 2 ]] || { log_error "--target requires a value"; exit 1; }
                target="$2"
                shift 2
                ;;
            -h|--help)
                show_help
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                echo "Use --help for usage information."
                exit 1
                ;;
        esac
    done

    # Change to script directory
    cd "${SCRIPT_DIR}"

    # Print header
    echo -e "${BOLD}HSYNC Client - Build${RESET}"
    echo -e "${DIM}────────────────────${RESET}"
    echo

    case "${target}" in
        native)
            check_prerequisites
            if [[ "${do_clean}" == true ]]; then
                clean_artifacts
                echo
            fi
            build_binary
            ;;
        deb)
            check_prerequisites
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning deb artifacts..."
                rm -rf "${DEB_OUTPUT_DIR}" "${DEB_BUILD_DIR}"
                echo
            fi
            build_deb
            ;;
        deb-all)
            if [[ ! -f "pyproject.toml" ]]; then
                log_error "pyproject.toml not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning deb-all artifacts..."
                rm -rf "${DEB_ALL_OUTPUT_DIR}" "${DEB_ALL_BUILD_DIR}"
                docker image rm -f \
                    "hsync-deb-builder:amd64" \
                    "hsync-deb-builder:arm64" \
                    "hsync-deb-builder:armhf" >/dev/null 2>&1 || true
                echo
            fi
            build_deb_all
            ;;
        wd-ex2-ultra-os5)
            # Only pyproject.toml is required (poetry runs inside the container).
            if [[ ! -f "pyproject.toml" ]]; then
                log_error "pyproject.toml not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning wd-ex2-ultra-os5 artifacts..."
                rm -rf "${WDOS5_OUTPUT_DIR}" "${WDOS5_BUILD_DIR}"
                docker image rm -f "${WDOS5_BUILDER_IMAGE}" >/dev/null 2>&1 || true
                echo
            fi
            build_wd_os5
            ;;
        wd-os5-all)
            if [[ ! -f "pyproject.toml" ]]; then
                log_error "pyproject.toml not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning wd-os5-all artifacts..."
                rm -rf "${OUTPUT_BASE}/wd-os5" "${BUILD_DIR}/wdos5/all"
                docker image rm -f \
                    "hsync-wdos5-builder:armv7" \
                    "hsync-wdos5-builder:amd64" >/dev/null 2>&1 || true
                echo
            fi
            build_wd_os5_all
            ;;
        *)
            log_error "Unknown target: ${target}"
            echo "Supported targets: native, deb, deb-all, wd-ex2-ultra-os5, wd-os5-all"
            exit 1
            ;;
    esac
}

main "$@"
