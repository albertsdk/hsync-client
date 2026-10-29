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
#                               rpm                  RHEL-family .rpm package
#                                                    for the host arch
#                               rpm-all              .rpm for x86_64 and
#                                                    aarch64 (via Docker,
#                                                    Rocky Linux 9 baseline)
#                               rpi                  Standalone binaries for
#                                                    Raspberry Pi OS 32-bit
#                                                    (armhf) and 64-bit (arm64)
#                                                    via Docker
#   -v, --version VERSION   Override the version used for packages (default:
#                           the pyproject.toml version). Also settable via the
#                           HSYNC_VERSION environment variable. A prerelease
#                           (e.g. 1.2.0-alpha.1) is translated to the Debian
#                           form (1.2.0~alpha.1) for the .deb target.
#   -h, --help              Show this help message
#
# Requirements:
#   - native target: Python virtual environment (.venv); run 'poetry install'
#   - deb target: Python virtual environment (.venv) + dpkg-deb (available on
#     any Debian/Ubuntu system via dpkg-dev)
#   - rpm target: Python virtual environment (.venv) + rpmbuild (available on
#     any RHEL/Rocky/Fedora system via rpm-build)
#   - deb-all / rpm-all / rpi: Docker with
#     qemu-user-static (binfmt) configured (used for cross-arch emulation on
#     amd64 hosts).
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
#   rpm:
#     - dist/rpm/hsync-client-<ver>-1.<dist>.<arch>.rpm   Installable with dnf
#   rpm-all:
#     - dist/rpm/hsync-client-<ver>-1.el9.x86_64.rpm
#     - dist/rpm/hsync-client-<ver>-1.el9.aarch64.rpm
#   rpi:
#     - dist/rpi/armhf/hsync   Raspberry Pi OS 32-bit (armv7)
#     - dist/rpi/arm64/hsync   Raspberry Pi OS 64-bit (aarch64)
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

# PyInstaller flags for the RPM container build. The WD OS5 package lives in
# the hsync-wd-os5 repository and carries its own copy.
readonly PYINSTALLER_COMMON_FLAGS="--clean --onedir --noconfirm --strip \
    --name ${APP_NAME} \
    --log-level ERROR \
    --hidden-import websockets.legacy \
    --hidden-import websockets.legacy.client \
    --hidden-import websockets.client \
    --hidden-import websockets.exceptions \
    --hidden-import watchdog.observers \
    --hidden-import watchdog.observers.inotify \
    --hidden-import watchdog.observers.inotify_buffer \
    --hidden-import watchdog.events \
    --hidden-import rich._unicode_data \
    --hidden-import rich._unicode_data._versions \
    --collect-submodules pathspec \
    --collect-submodules hsync_common \
    --exclude-module pytest --exclude-module pytest_asyncio \
    --exclude-module pytest_cov --exclude-module tkinter \
    --exclude-module unittest --exclude-module pdb \
    --exclude-module doctest --exclude-module pip \
    --exclude-module setuptools --exclude-module wheel \
    --exclude-module pkg_resources --exclude-module rapidfuzz \
    ${ENTRY_POINT}"

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
    # Image pinned by digest: this runs --privileged, so it must not move.
    if ! docker run --rm --privileged \
        multiarch/qemu-user-static@sha256:fe60359c92e86a43cc87b3d906006245f77bfc0565676b80004cc666e4feb9f0 \
        --reset -p yes \
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
                            Supported: native, deb, deb-all, rpm, rpm-all, rpi
    -v, --version VERSION   Override the package version (default:
                            pyproject.toml). Also settable via HSYNC_VERSION.
    -h, --help              Show this help message

${BOLD}Examples:${RESET}
    ./compile.sh                                # Native host build
    ./compile.sh --clean                        # Clean and rebuild
    ./compile.sh --target deb                   # .deb for host arch
    ./compile.sh --target deb-all               # .deb for amd64, arm64, armhf
    ./compile.sh --target rpm                   # .rpm for host arch (RHEL-family)
    ./compile.sh --target rpm-all               # .rpm for x86_64 + aarch64
    ./compile.sh --target rpi                   # Raspberry Pi armhf + arm64

${BOLD}Output:${RESET}
    native:
        ${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}   Standalone executable (platform: ${PLATFORM})
    deb:
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_<arch>.deb
    deb-all:
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_amd64.deb
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_arm64.deb
        ${OUTPUT_BASE}/deb/${DEB_PKG_NAME}_<ver>_armhf.deb
    rpm:
        ${OUTPUT_BASE}/rpm/${DEB_PKG_NAME}-<ver>-1.<dist>.<arch>.rpm
    rpm-all:
        ${OUTPUT_BASE}/rpm/${DEB_PKG_NAME}-<ver>-1.el9.x86_64.rpm
        ${OUTPUT_BASE}/rpm/${DEB_PKG_NAME}-<ver>-1.el9.aarch64.rpm
    rpi:
        dist/rpi/armhf/${APP_NAME}   Raspberry Pi OS 32-bit (armv7)
        dist/rpi/arm64/${APP_NAME}   Raspberry Pi OS 64-bit (aarch64)
EOF
}

# Clean build artifacts
clean_artifacts() {
    log_step "Cleaning build artifacts..."

    local cleaned=0

    if [[ -d "${BUILD_DIR}" ]]; then
        rm -rf "${BUILD_DIR}"
        ((cleaned++)) || true
    fi

    # Clean the whole dist/ tree so stale platform directories don't accumulate
    if [[ -d "${OUTPUT_BASE}" ]]; then
        rm -rf "${OUTPUT_BASE}"
        ((cleaned++)) || true
    fi

    if [[ -f "${APP_NAME}.spec" ]]; then
        rm -f "${APP_NAME}.spec"
        ((cleaned++)) || true
    fi

    # Clean PyInstaller cache
    if [[ -d "__pycache__" ]]; then
        find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
        ((cleaned++)) || true
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

# Version override, set by --version in main() and/or the HSYNC_VERSION
# environment variable. Mutable (not readonly) so main() can assign it.
VERSION_OVERRIDE=""

# Get the source (SemVer) version: --version flag > HSYNC_VERSION env >
# pyproject.toml. May be a prerelease (e.g. 1.2.0-alpha.1); the .deb build
# translates it to the Debian form via _semver_to_debian below.
get_version() {
    if [[ -n "${VERSION_OVERRIDE}" ]]; then
        printf '%s' "${VERSION_OVERRIDE}"
    elif [[ -n "${HSYNC_VERSION:-}" ]]; then
        printf '%s' "${HSYNC_VERSION}"
    else
        grep -E '^version\s*=' pyproject.toml | head -1 | sed 's/.*"\(.*\)".*/\1/'
    fi
}

# Translate a SemVer version into a Debian policy version so a prerelease sorts
# *before* the stable release. Debian uses '~' as the "sorts before everything"
# separator while '-' denotes the debian revision, so the first '-' (the SemVer
# prerelease separator) becomes '~': 1.2.0-alpha.1 -> 1.2.0~alpha.1. Build
# metadata ('+') is left untouched (Debian uses '+' too).
_semver_to_debian() {
    printf '%s' "$1" | sed 's/-/~/'
}

# Translate a SemVer version into an RPM Version: field. RPM (>= 4.10) also uses
# '~' as the "sorts before everything" separator, so the mapping is the same as
# Debian: the first '-' (SemVer prerelease separator) becomes '~'
# (1.2.0-alpha.1 -> 1.2.0~alpha.1). The Release: field stays a plain number
# (`1%{?dist}`), so the prerelease marker must live in Version, not Release.
_semver_to_rpm() {
    _semver_to_debian "$1"
}

# PyInstaller flags shared by the native (host) and rpi (container) onefile
# builds. --strip is added by the caller: omitted on Windows hosts, always used
# inside the Linux build containers. --distpath is also added by the caller.
# Backslash continuations (not embedded newlines) keep the expansion on a
# single line so it is safe to interpolate inside a ``bash -c`` string.
_onefile_pyi_flags() {
    printf '%s' "--clean --onefile --noconfirm \
        --name ${APP_NAME} \
        --log-level ERROR \
        --hidden-import websockets.legacy \
        --hidden-import websockets.legacy.client \
        --hidden-import websockets.client \
        --hidden-import websockets.exceptions \
        --hidden-import watchdog.observers \
        --hidden-import watchdog.events \
        --hidden-import rich._unicode_data \
        --hidden-import rich._unicode_data.unicode17-0-0 \
        --hidden-import rich._unicode_data.unicode16-0-0 \
        --hidden-import rich._unicode_data.unicode15-1-0 \
        --hidden-import rich._unicode_data.unicode15-0-0 \
        --hidden-import rich._unicode_data.unicode14-0-0 \
        --hidden-import rich._unicode_data.unicode13-0-0 \
        --hidden-import rich._unicode_data.unicode12-1-0 \
        --hidden-import rich._unicode_data.unicode12-0-0 \
        --hidden-import rich._unicode_data._versions \
        --exclude-module pytest --exclude-module pytest_asyncio \
        --exclude-module pytest_cov --exclude-module tkinter \
        --exclude-module unittest --exclude-module pdb \
        --exclude-module doctest --exclude-module pip \
        --exclude-module setuptools --exclude-module wheel \
        --exclude-module pkg_resources --exclude-module rapidfuzz"
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
    # --hidden-import / --exclude-module: see _onefile_pyi_flags above
    #
    # LC_ALL=C: Force English locale to avoid ldconfig localization warnings
    # PYTHONWARNINGS=ignore: Suppress Python warnings during build
    LC_ALL=C PYTHONWARNINGS=ignore "${PYINSTALLER}" \
        --distpath "${OUTPUT_DIR}" \
        ${STRIP_FLAG} \
        $(_onefile_pyi_flags) \
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

# Map uname -m to RPM architecture name. RHEL/Rocky is 64-bit only, so only
# x86_64 and aarch64 are supported (no armv7hl); anything else is echoed as-is.
_arch_to_rpm() {
    case "$1" in
        x86_64|amd64)   echo "x86_64" ;;
        aarch64|arm64)  echo "aarch64" ;;
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
    # Sensitive configuration (API/p2p keys): restrict to root only so the
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
    # Resolve the binary to an absolute path so it still exists after we cd
    # into the temporary control dir.
    local bin_abs
    bin_abs="$(cd "$(dirname "${bin}")" && pwd)/${APP_NAME}"
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
        "${bin_abs}" 2>/dev/null \
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
    local version deb_version deb_arch
    version="$(get_version)"
    deb_version="$(_semver_to_debian "${version}")"
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
    local stage_dir="${DEB_BUILD_DIR}/${DEB_PKG_NAME}_${deb_version}_${deb_arch}"
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
    log_step "Injecting version ${deb_version} and arch ${deb_arch} into control..."
    sed -i.bak \
        -e "s/__VERSION__/${deb_version}/g" \
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

    local deb_name="${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb"
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
    echo -e "  ${BOLD}Version:${RESET}  ${deb_version}"
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
# Docker cross-compilation is used — one build per arch (the builder base is
# the multi-arch ``python:3.12-bookworm`` image, selected via --platform):
#
#   amd64 → linux/amd64
#   arm64 → linux/arm64
#   armhf → linux/arm/v7
#
# Output: dist/deb/hsync-client_<ver>_<arch>.deb
build_deb_all() {
    local version deb_version
    version="$(get_version)"
    deb_version="$(_semver_to_debian "${version}")"

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
    # NOTE: backslash continuations (not embedded newlines) are used so the
    # variable expands to a single line.  When this variable is later
    # interpolated inside a ``bash -c "…"`` string, embedded newlines would
    # be treated as command separators, splitting ``pyinstaller`` away from
    # its arguments and triggering "error: the following arguments are
    # required: scriptname".
    local pyinstaller_flags="--clean --onedir --noconfirm --strip \
        --name ${APP_NAME} \
        --log-level ERROR \
        --hidden-import websockets.legacy \
        --hidden-import websockets.legacy.client \
        --hidden-import websockets.client \
        --hidden-import websockets.exceptions \
        --hidden-import watchdog.observers \
        --hidden-import watchdog.observers.inotify \
        --hidden-import watchdog.observers.inotify_buffer \
        --hidden-import watchdog.events \
        --hidden-import rich._unicode_data \
        --hidden-import rich._unicode_data._versions \
        --collect-submodules pathspec \
        --collect-submodules hsync_common \
        --exclude-module pytest --exclude-module pytest_asyncio \
        --exclude-module pytest_cov --exclude-module tkinter \
        --exclude-module unittest --exclude-module pdb \
        --exclude-module doctest --exclude-module pip \
        --exclude-module setuptools --exclude-module wheel \
        --exclude-module pkg_resources --exclude-module rapidfuzz \
        ${ENTRY_POINT}"

    # Architecture definitions: deb_arch → docker_platform. The builder base
    # image is the multi-arch ``python:3.12-bookworm`` (Dockerfile default), so
    # ``--platform`` alone selects the architecture.
    local -A arch_docker_platform
    arch_docker_platform["amd64"]="linux/amd64"
    arch_docker_platform["arm64"]="linux/arm64"
    arch_docker_platform["armhf"]="linux/arm/v7"

    local -a deb_archs=("amd64" "arm64" "armhf")
    local out_abs
    out_abs="$(cd "${DEB_ALL_OUTPUT_DIR}" && pwd)"

    for deb_arch in "${deb_archs[@]}"; do
        local docker_platform="${arch_docker_platform[${deb_arch}]}"
        local builder_tag="${DEB_ALL_BUILDER_IMAGE_PREFIX}:${deb_arch}"
        local arch_build_dir="${DEB_ALL_BUILD_DIR}/${deb_arch}"
        local bin_dir="${arch_build_dir}/pyinstaller"
        local work_dir="${arch_build_dir}/work"
        local stage_dir="${arch_build_dir}/${DEB_PKG_NAME}_${deb_version}_${deb_arch}"
        mkdir -p "${bin_dir}" "${work_dir}"

        echo
        log_step "─── ${deb_arch} build ───────────────────────────────────────"

        # --- builder image ----------------------------------------------------
        log_step "Building Docker image ${builder_tag}..."
        DOCKER_BUILDKIT=1 docker build \
            --platform "${docker_platform}" \
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
        stage_abs="$(cd "${stage_dir}" && pwd)"

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
            -e "s/__VERSION__/${deb_version}/g" \
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
            -v "${stage_abs}:/pkg" \
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
                chmod 0644 /out/${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb
                chown -R \"\${HOST_UID}:\${HOST_GID}\" /pkg \"/out/${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb\"
            "

        local deb_name="${DEB_PKG_NAME}_${deb_version}_${deb_arch}.deb"
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
    echo -e "  ${BOLD}Version:${RESET}   ${deb_version}"
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
# RPM package build (RHEL/Rocky/Alma/Fedora)
# -----------------------------------------------------------------------------

# Configuration for the rpm / rpm-all targets. The .spec is rendered from
# packaging/rpm/hsync-client.spec (__VERSION__ replaced); the payload reuses the
# Debian source tree (man page, systemd unit, /etc conffile) but the binary is a
# separate PyInstaller build on the Rocky 9 baseline (glibc 2.34), never the
# Debian-built binary.
readonly RPM_DIR="packaging/rpm"
readonly RPM_SPEC="${RPM_DIR}/hsync-client.spec"
readonly RPM_PKG_NAME="hsync-client"
readonly RPM_OUTPUT_DIR="${OUTPUT_BASE}/rpm"
readonly RPM_BUILD_DIR="${BUILD_DIR}/rpm"
readonly RPM_ALL_BUILD_DIR="${BUILD_DIR}/rpm-all"
readonly RPM_BUILDER_IMAGE_PREFIX="hsync-rpm-builder"

# Stage a buildroot for rpmbuild: Debian payload (etc/, usr/) + the PyInstaller
# --onedir bundle under /usr/lib/hsync + a /usr/bin symlink + a gzipped man
# page, with permissions normalised. Shared by build_rpm (host) and
# build_rpm_all (container).
# $1 = stage_dir (absolute)   $2 = built onedir dir (absolute)
_stage_rpm_buildroot() {
    local stage_dir="$1" built_dir="$2"
    rm -rf "${stage_dir}"
    mkdir -p "${stage_dir}"

    # Debian payload (etc/, usr/) minus DEBIAN/ and the Dockerfile.
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

    # Relative symlink: /usr/bin/hsync -> ../lib/hsync/hsync
    mkdir -p "${stage_dir}/usr/bin"
    ln -sf "../lib/${APP_NAME}/${APP_NAME}" "${stage_dir}/usr/bin/${APP_NAME}"

    # Normalise permissions, gzip the man page, restrict the conffile.
    find "${stage_dir}/etc" "${stage_dir}/usr" -type d -exec chmod 0755 {} + 2>/dev/null || true
    find "${stage_dir}/etc" "${stage_dir}/usr" -type f -exec chmod 0644 {} + 2>/dev/null || true
    chmod 0755 "${stage_dir}/usr/lib/${APP_NAME}/${APP_NAME}"
    local man="${stage_dir}/usr/share/man/man1/${APP_NAME}.1"
    [[ -f "${man}" ]] && gzip -n -9 -f "${man}"
    chmod 0750 "${stage_dir}/etc/hsync" 2>/dev/null || true
    [[ -f "${stage_dir}/etc/hsync/hsync.conf" ]] && chmod 0600 "${stage_dir}/etc/hsync/hsync.conf"
}

# Render the spec with the resolved RPM version and echo its absolute path.
# $1 = version (SemVer)   $2 = work dir
_render_rpm_spec() {
    local version="$1" work_dir="$2"
    local rpm_version rendered
    rpm_version="$(_semver_to_rpm "${version}")"
    rendered="${work_dir}/hsync-client.spec"
    sed "s/__VERSION__/${rpm_version}/g" "${RPM_SPEC}" > "${rendered}"
    printf '%s' "${rendered}"
}

# Build a .rpm package for the host architecture (native build, no Docker).
# Only meaningful on a RHEL-family host with rpmbuild installed.
build_rpm() {
    local version rpm_version rpm_arch
    version="$(get_version)"
    rpm_arch="$(_arch_to_rpm "${ARCH_NAME}")"

    log_step "Building ${RPM_PKG_NAME} v${version} for ${rpm_arch} (host build)..."

    check_prerequisites
    if ! command -v rpmbuild >/dev/null 2>&1; then
        log_error "rpmbuild is required for the rpm target."
        log_info "Install it with: dnf install rpm-build (RHEL/Rocky/Fedora)."
        exit 1
    fi
    mkdir -p "${RPM_OUTPUT_DIR}" "${RPM_BUILD_DIR}"

    # --- PyInstaller --onedir on host ------------------------------------
    local onedir_dist="${RPM_BUILD_DIR}/pyinstaller"
    mkdir -p "${onedir_dist}"
    log_step "Compiling ${APP_NAME} with PyInstaller (--onedir)..."
    LC_ALL=C PYTHONWARNINGS=ignore "${PYINSTALLER}" \
        --clean --onedir --noconfirm --strip \
        --name "${APP_NAME}" \
        --distpath "${onedir_dist}" \
        --workpath "${RPM_BUILD_DIR}/work" \
        --specpath "${RPM_BUILD_DIR}/work" \
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
    [[ -d "${built_dir}" ]] || { log_error "PyInstaller --onedir output not found at ${built_dir}"; exit 1; }

    # --- stage buildroot + render spec -----------------------------------
    _stage_rpm_buildroot "${RPM_BUILD_DIR}/pkg" "${built_dir}"
    local stage_dir
    stage_dir="$(cd "${RPM_BUILD_DIR}/pkg" && pwd)"
    local spec_path
    spec_path="$(_render_rpm_spec "${version}" "${RPM_BUILD_DIR}")"

    # --- rpmbuild --------------------------------------------------------
    # rpmbuild must receive an ABSOLUTE _topdir: it chdir()s into the build
    # directory, so a relative _topdir makes %{buildroot} resolve against the
    # wrong CWD and every %files entry is reported missing. _dbpath points at a
    # writable directory so a non-root rpmbuild does not touch /var/lib/rpm.
    log_step "Building .rpm with rpmbuild..."
    local spec_abs topdir_abs dbpath_abs
    spec_abs="$(cd "$(dirname "${spec_path}")" && pwd)/$(basename "${spec_path}")"
    mkdir -p "${RPM_BUILD_DIR}/rpmbuild" "${RPM_BUILD_DIR}/rpmdb"
    topdir_abs="$(cd "${RPM_BUILD_DIR}/rpmbuild" && pwd)"
    dbpath_abs="$(cd "${RPM_BUILD_DIR}/rpmdb" && pwd)"
    rpmbuild -bb "${spec_abs}" \
        --define "pkgdir ${stage_dir}" \
        --define "_topdir ${topdir_abs}" \
        --define "_dbpath ${dbpath_abs}"

    local rpm_glob="${RPM_BUILD_DIR}/rpmbuild/RPMS/*/*.rpm"
    local rpm_file
    rpm_file="$(ls ${rpm_glob} 2>/dev/null | head -1)"
    [[ -n "${rpm_file}" ]] || { log_error "rpmbuild produced no package"; exit 1; }
    mv -f "${rpm_file}" "${RPM_OUTPUT_DIR}/"

    rm -rf "${RPM_BUILD_DIR}/work" "${RPM_BUILD_DIR}/pyinstaller" "${APP_NAME}.spec" 2>/dev/null || true

    local rpm_name rpm_path size
    rpm_name="$(basename "$(ls "${RPM_OUTPUT_DIR}"/*.rpm 2>/dev/null | head -1)")"
    rpm_path="${RPM_OUTPUT_DIR}/${rpm_name}"
    size="$(du -h "${rpm_path}" | cut -f1)"
    echo
    log_ok "RPM package built!"
    echo
    echo -e "  ${BOLD}Package:${RESET}  ${rpm_name}"
    echo -e "  ${BOLD}Arch:${RESET}     ${rpm_arch}"
    echo -e "  ${BOLD}Size:${RESET}     ${size}"
    echo -e "  ${BOLD}Version:${RESET}  ${version}"
    echo
    echo -e "${DIM}Install with:${RESET}"
    echo -e "${DIM}  sudo dnf install ${rpm_path}${RESET}"
}

# Build .rpm packages for x86_64 and aarch64 via Docker + qemu, on the Rocky 9
# baseline. Separate from the Debian build: the binary is compiled inside the
# Rocky 9 image so its glibc requirement is <= 2.34 (RHEL 9 compatible).
build_rpm_all() {
    local version
    version="$(get_version)"

    log_step "Building ${RPM_PKG_NAME} v${version} for x86_64 and aarch64..."

    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the rpm-all target."
        exit 1
    fi
    mkdir -p "${RPM_OUTPUT_DIR}"

    local repo_root
    repo_root="$(cd .. && pwd)"
    local out_abs
    out_abs="$(cd "${RPM_OUTPUT_DIR}" && pwd)"

    ensure_qemu_binfmt qemu-aarch64

    local -A arch_docker_platform
    arch_docker_platform["x86_64"]="linux/amd64"
    arch_docker_platform["aarch64"]="linux/arm64"

    local -a rpm_archs=("x86_64" "aarch64")

    for rpm_arch in "${rpm_archs[@]}"; do
        local docker_platform="${arch_docker_platform[${rpm_arch}]}"
        local builder_tag="${RPM_BUILDER_IMAGE_PREFIX}:${rpm_arch}"
        local arch_build_dir="${RPM_ALL_BUILD_DIR}/${rpm_arch}"
        local bin_dir="${arch_build_dir}/pyinstaller"
        local work_dir="${arch_build_dir}/work"
        mkdir -p "${bin_dir}" "${work_dir}"

        echo
        log_step "─── ${rpm_arch} build ───────────────────────────────────────"

        # --- builder image ----------------------------------------------------
        log_step "Building Docker image ${builder_tag}..."
        DOCKER_BUILDKIT=1 docker build \
            --platform "${docker_platform}" \
            --build-arg TARGET_PLATFORM="${docker_platform}" \
            -t "${builder_tag}" \
            -f "${RPM_DIR}/Dockerfile.builder" \
            "${RPM_DIR}"

        # --- PyInstaller in container ----------------------------------------
        log_step "Compiling ${rpm_arch} binary inside container..."
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
                    ${PYINSTALLER_COMMON_FLAGS} \
                    --distpath ${bin_dir} \
                    --workpath ${work_dir} \
                    --specpath ${work_dir}
            "

        local built_dir="${bin_dir}/${APP_NAME}"
        [[ -d "${built_dir}" ]] || { log_error "${rpm_arch} build directory not produced at ${built_dir}"; exit 1; }
        log_ok "${rpm_arch} binary ready ($(du -sh "${built_dir}" | cut -f1))"

        # --- stage buildroot + render spec -----------------------------------
        local stage_dir="${arch_build_dir}/pkg"
        local spec_path
        spec_path="$(_render_rpm_spec "${version}" "${arch_build_dir}")"
        _stage_rpm_buildroot "${stage_dir}" "${built_dir}"
        stage_dir="$(cd "${stage_dir}" && pwd)"
        local spec_abs
        spec_abs="$(cd "$(dirname "${spec_path}")" && pwd)/$(basename "${spec_path}")"

        # --- rpmbuild in container -------------------------------------------
        log_step "Building .rpm for ${rpm_arch}..."
        docker run --rm \
            --platform "${docker_platform}" \
            -u "$(id -u):$(id -g)" \
            -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
            -v "${stage_dir}:/pkg:ro" \
            -v "${spec_abs}:/hsync.spec:ro" \
            -v "${out_abs}:/out" \
            -w /tmp \
            "${builder_tag}" \
            bash -c "
                set -euo pipefail
                topdir=/tmp/rpmbuild-\$\$
                mkdir -p \"\${topdir}/db\"
                rpmbuild -bb /hsync.spec \
                    --define 'pkgdir /pkg' \
                    --define \"_topdir \${topdir}\" \
                    --define \"_dbpath \${topdir}/db\"
                out_rpm=\$(ls \${topdir}/RPMS/*/*.rpm | head -1)
                cp \"\${out_rpm}\" /out/
                chmod 0644 \"/out/\$(basename \"\${out_rpm}\")\"
                chown \"\${HOST_UID}:\${HOST_GID}\" \"/out/\$(basename \"\${out_rpm}\")\"
                rm -rf \"\${topdir}\"
            "

        local rpm_glob="${out_abs}/${RPM_PKG_NAME}-"*".${rpm_arch}.rpm"
        local rpm_file
        rpm_file="$(ls ${rpm_glob} 2>/dev/null | head -1)"
        [[ -n "${rpm_file}" ]] || { log_error ".rpm not produced for ${rpm_arch}"; exit 1; }
        log_ok "  → $(basename "${rpm_file}") ($(du -h "${rpm_file}" | cut -f1))"
    done

    echo
    log_ok "RPM packages built!"
    echo
    echo -e "  ${BOLD}Version:${RESET}   ${version}"
    echo -e "  ${BOLD}Output:${RESET}    ${out_abs}/"
    echo
    echo -e "${DIM}Install with:  sudo dnf install ${out_abs}/${RPM_PKG_NAME}-${version}-1.el9.<arch>.rpm${RESET}"
}

# -----------------------------------------------------------------------------
# Raspberry Pi OS (armhf + arm64) — standalone executables via Docker
# -----------------------------------------------------------------------------

# Configuration for the rpi target. The Debian builder image already carries
# Python 3.12 + Poetry + the armhf source-build toolchain (Rust, libssl-dev,
# …) required by the project's native dependencies, so it doubles as the
# Raspberry Pi cross-build image. Raspberry Pi OS Bookworm is Debian 12
# (glibc 2.36), matching the bookworm base images.
readonly RPI_OUTPUT_DIR="${OUTPUT_BASE}/rpi"
readonly RPI_DOCKERFILE="${DEB_DIR}/Dockerfile.builder"
readonly RPI_BUILDER_IMAGE_PREFIX="hsync-client-rpi-builder"

# Build standalone executables for Raspberry Pi OS, one per supported
# architecture: armhf (32-bit, armv7) and arm64 (64-bit, aarch64). PyInstaller
# cannot cross-compile, so each binary is produced by running it inside an
# emulated python:3.12-bookworm container for the target CPU family.
#
# Output: dist/rpi/<arch>/hsync
build_rpi() {
    local version
    version="$(get_version)"

    log_step "Building ${APP_NAME} v${version} for Raspberry Pi OS (armhf, arm64)..."

    if ! command -v docker >/dev/null 2>&1; then
        log_error "docker is required for the rpi target."
        exit 1
    fi
    if [[ ! -f "${RPI_DOCKERFILE}" ]]; then
        log_error "Builder Dockerfile missing: ${RPI_DOCKERFILE}"
        exit 1
    fi

    local repo_root
    repo_root="$(cd .. && pwd)"

    ensure_qemu_binfmt qemu-arm qemu-aarch64

    local -A arch_docker_platform
    arch_docker_platform["armhf"]="linux/arm/v7"
    arch_docker_platform["arm64"]="linux/arm64"

    local -a rpi_archs=("armhf" "arm64")
    local deb_arch
    for deb_arch in "${rpi_archs[@]}"; do
        local docker_platform="${arch_docker_platform[${deb_arch}]}"
        local builder_tag="${RPI_BUILDER_IMAGE_PREFIX}:${deb_arch}"
        local out_dir="${RPI_OUTPUT_DIR}/${deb_arch}"
        local out_abs
        mkdir -p "${out_dir}"
        out_abs="$(cd "${out_dir}" && pwd)"

        echo
        log_step "─── ${deb_arch} build ───────────────────────────────────────"

        log_step "Building Docker image ${builder_tag}..."
        DOCKER_BUILDKIT=1 docker build \
            --platform "${docker_platform}" \
            --build-arg TARGET_PLATFORM="${docker_platform}" \
            -t "${builder_tag}" \
            -f "${RPI_DOCKERFILE}" \
            "${DEB_DIR}"

        # Dependencies are installed into a container-local venv (HOME=/tmp) so
        # the mounted source tree stays pristine; only the produced executable
        # is written back to the host under /out.
        log_step "Compiling ${deb_arch} binary inside container..."
        docker run --rm \
            --platform "${docker_platform}" \
            -u "$(id -u):$(id -g)" \
            -e HOME=/tmp -e XDG_CACHE_HOME=/tmp \
            -v "${repo_root}:/repo" \
            -v "${out_abs}:/out" \
            -w /repo/hsync-client \
            -e LC_ALL=C -e PYTHONWARNINGS=ignore \
            -e PIP_DEFAULT_TIMEOUT=300 -e PIP_RETRIES=10 \
            -e POETRY_REQUESTS_TIMEOUT=300 \
            "${builder_tag}" \
            bash -c "
                set -euo pipefail
                poetry config virtualenvs.in-project false
                poetry config installer.max-workers 4
                poetry install --no-root --without dev
                poetry run pyinstaller --strip \
                    $(_onefile_pyi_flags) \
                    --distpath /out \
                    --workpath /tmp/work \
                    --specpath /tmp/work \
                    ${ENTRY_POINT}
            "

        local bin_path="${out_dir}/${APP_NAME}"
        if [[ ! -f "${bin_path}" ]]; then
            log_error "${deb_arch} binary not produced at ${bin_path}"
            exit 1
        fi
        log_ok "  → ${RPI_OUTPUT_DIR}/${deb_arch}/${APP_NAME} ($(du -h "${bin_path}" | cut -f1))"
    done

    echo
    log_ok "Raspberry Pi executables built!"
    echo
    echo -e "  ${BOLD}Version:${RESET}  ${version}"
    echo -e "  ${BOLD}Output:${RESET}   ${RPI_OUTPUT_DIR}/<arch>/${APP_NAME}"
    echo
    local sep="────────────────────────────────────────────────────────────"
    echo -e "${GREEN}${BOLD}${sep}${RESET}"
    echo -e "${GREEN}${BOLD}  Raspberry Pi executables:${RESET}"
    for deb_arch in "${rpi_archs[@]}"; do
        echo -e "${GREEN}${BOLD}      $(pwd)/${RPI_OUTPUT_DIR}/${deb_arch}/${APP_NAME}${RESET}"
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
            -v|--version)
                [[ $# -ge 2 ]] || { log_error "--version requires a value"; exit 1; }
                VERSION_OVERRIDE="$2"
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
        rpi)
            # Only pyproject.toml is required (poetry runs inside the container).
            if [[ ! -f "pyproject.toml" ]]; then
                log_error "pyproject.toml not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning rpi artifacts..."
                rm -rf "${RPI_OUTPUT_DIR}"
                docker image rm -f \
                    "${RPI_BUILDER_IMAGE_PREFIX}:armhf" \
                    "${RPI_BUILDER_IMAGE_PREFIX}:arm64" >/dev/null 2>&1 || true
                echo
            fi
            build_rpi
            ;;
        rpm)
            check_prerequisites
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning rpm artifacts..."
                rm -rf "${RPM_OUTPUT_DIR}" "${RPM_BUILD_DIR}"
                echo
            fi
            build_rpm
            ;;
        rpm-all)
            if [[ ! -f "pyproject.toml" ]]; then
                log_error "pyproject.toml not found. Run from the project root."
                exit 1
            fi
            if [[ "${do_clean}" == true ]]; then
                log_step "Cleaning rpm-all artifacts..."
                rm -rf "${RPM_OUTPUT_DIR}" "${RPM_ALL_BUILD_DIR}"
                docker image rm -f \
                    "${RPM_BUILDER_IMAGE_PREFIX}:x86_64" \
                    "${RPM_BUILDER_IMAGE_PREFIX}:aarch64" >/dev/null 2>&1 || true
                echo
            fi
            build_rpm_all
            ;;
        *)
            log_error "Unknown target: ${target}"
            echo "Supported targets: native, deb, deb-all, rpm, rpm-all, rpi"
            exit 1
            ;;
    esac
}

main "$@"
