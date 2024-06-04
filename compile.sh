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
#                               wd-ex2-ultra-os5     WD My Cloud EX2 Ultra OS5
#                                                    (ARMv7, packaged as .bin)
#   -h, --help              Show this help message
#
# Requirements:
#   - native target: Python virtual environment (.venv); run 'poetry install'
#   - wd-ex2-ultra-os5: Docker with qemu-user-static (binfmt) configured
#
# Output:
#   native:
#     - dist/<os>-<arch>/hsync[.exe]   Standalone platform-aware executable
#       e.g. dist/linux-amd64/hsync, dist/darwin-arm64/hsync
#   wd-ex2-ultra-os5:
#     - dist/wd-ex2-ultra-os5/MyCloudEX2Ultra_hsync_<ver>.bin
#                                       Installable through the OS5 dashboard
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
readonly PYINSTALLER="${VENV_DIR}/bin/pyinstaller"

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

# Display usage information
show_help() {
    cat << EOF
${BOLD}HSYNC Client - Build Script${RESET}

${BOLD}Usage:${RESET}
    ./compile.sh [options]

${BOLD}Options:${RESET}
    -c, --clean             Clean build artifacts before building
    -t, --target TARGET     Build target (default: native)
                            Supported: native, wd-ex2-ultra-os5
    -h, --help              Show this help message

${BOLD}Examples:${RESET}
    ./compile.sh                                # Native host build
    ./compile.sh --clean                        # Clean and rebuild
    ./compile.sh --target wd-ex2-ultra-os5      # ARMv7 .bin for WD OS5

${BOLD}Output:${RESET}
    native:
        ${OUTPUT_DIR}/${APP_NAME}${BIN_EXT}   Standalone executable (platform: ${PLATFORM})
    wd-ex2-ultra-os5:
        dist/wd-ex2-ultra-os5/MyCloudEX2Ultra_${APP_NAME}_<ver>.bin
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

    if [[ -f "${SPEC_FILE}" ]]; then
        rm -f "${SPEC_FILE}"
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
        --strip \
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

# -----------------------------------------------------------------------------
# WD My Cloud EX2 Ultra (OS5) build
# -----------------------------------------------------------------------------

# Configuration for the wd-ex2-ultra-os5 target.
readonly WDOS5_DIR="packaging/wd-os5"
readonly WDOS5_SRC_APKG="${WDOS5_DIR}/Apkg"
readonly WDOS5_OUTPUT_DIR="${OUTPUT_BASE}/wd-ex2-ultra-os5"
readonly WDOS5_BUILD_DIR="${BUILD_DIR}/wdos5"
readonly WDOS5_STAGE_DIR="${WDOS5_BUILD_DIR}/Apkg"
readonly WDOS5_BUILDER_IMAGE="hsync-wdos5-builder:armv7"
readonly WDOS5_MKSAPKG_URL="https://raw.githubusercontent.com/WDCommunity/wdpksrc/master/mksapkg-OS5"
readonly WDOS5_CACHE_DIR="${WDOS5_DIR}/.cache"
readonly WDOS5_MKSAPKG="${WDOS5_CACHE_DIR}/mksapkg-OS5"
readonly WDOS5_MODEL="MyCloudEX2Ultra"

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
    log_step "Registering qemu-user-static binfmt handlers (idempotent)..."
    docker run --rm --privileged multiarch/qemu-user-static --reset -p yes \
        >/dev/null 2>&1 || log_warn "qemu-user-static registration skipped (may already be active)"

    # --- builder image ---------------------------------------------------
    log_step "Building Docker image ${WDOS5_BUILDER_IMAGE} (cached after first run)..."
    docker build \
        --platform linux/arm/v7 \
        -t "${WDOS5_BUILDER_IMAGE}" \
        -f "${WDOS5_DIR}/Dockerfile.builder" \
        "${WDOS5_DIR}"

    # --- PyInstaller in the emulated container --------------------------
    # The binary is dropped under build/wdos5/bin/ so the source tree stays
    # clean. The staging copy of Apkg/ (built below) picks it up from there.
    #
    # The repository root (parent of ``hsync-client``) is mounted as ``/repo``
    # so the ``hsync-common = {path = "../hsync-common"}`` path dependency in
    # ``pyproject.toml`` resolves inside the container. Workdir is the
    # ``hsync-client`` subdir so all the script's relative paths still work.
    local repo_root
    repo_root="$(cd .. && pwd)"
    log_step "Running PyInstaller inside the ARMv7 container..."
    # PIP_DEFAULT_TIMEOUT / PIP_RETRIES make Poetry's PEP-517 chef resilient
    # to the slow PyPI transfers we see under qemu-user emulation (the
    # cryptography build pulls maturin, setuptools, cffi from source).
    docker run --rm \
        --platform linux/arm/v7 \
        -v "${repo_root}:/repo" \
        -w /repo/hsync-client \
        -e LC_ALL=C \
        -e PYTHONWARNINGS=ignore \
        -e PIP_DEFAULT_TIMEOUT=300 \
        -e PIP_RETRIES=10 \
        -e POETRY_REQUESTS_TIMEOUT=300 \
        "${WDOS5_BUILDER_IMAGE}" \
        bash -c "
            set -euo pipefail
            poetry config virtualenvs.in-project true
            poetry config installer.max-workers 4
            # Retry transient PyPI timeouts (common under qemu-user emulation).
            install_ok=0
            for attempt in 1 2 3; do
                if poetry install --no-root --only main; then
                    install_ok=1
                    break
                fi
                echo \"poetry install failed (attempt \${attempt}/3), retrying in 10s...\" >&2
                sleep 10
            done
            if [ \"\${install_ok}\" -ne 1 ]; then
                echo 'poetry install failed after 3 attempts' >&2
                exit 1
            fi
            poetry run pyinstaller \
                --clean --onefile --strip \
                --name ${APP_NAME} \
                --distpath ${WDOS5_BUILD_DIR}/bin \
                --workpath ${WDOS5_BUILD_DIR}/work \
                --specpath ${WDOS5_BUILD_DIR}/work \
                --log-level ERROR \
                --hidden-import websockets.legacy \
                --hidden-import websockets.legacy.client \
                --hidden-import websockets.client \
                --hidden-import websockets.exceptions \
                --hidden-import watchdog.observers \
                --hidden-import watchdog.events \
                --hidden-import rich._unicode_data \
                --hidden-import rich._unicode_data._versions \
                --exclude-module pytest --exclude-module pytest_asyncio \
                --exclude-module pytest_cov --exclude-module tkinter \
                --exclude-module unittest --exclude-module pdb \
                --exclude-module doctest --exclude-module pip \
                --exclude-module setuptools --exclude-module wheel \
                --exclude-module pkg_resources --exclude-module rapidfuzz \
                ${ENTRY_POINT}
        "

    local built_bin="${WDOS5_BUILD_DIR}/bin/${APP_NAME}"
    if [[ ! -f "${built_bin}" ]]; then
        log_error "ARMv7 binary not produced at ${built_bin}"
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
    cp -f "${built_bin}" "${WDOS5_STAGE_DIR}/bin/${APP_NAME}"
    chmod 0755 "${WDOS5_STAGE_DIR}/bin/${APP_NAME}"
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

    log_step "Building OS5 .bin package..."
    local mksapkg_abs stage_abs stage_parent out_abs
    mksapkg_abs="$(cd "$(dirname "${WDOS5_MKSAPKG}")" && pwd)/$(basename "${WDOS5_MKSAPKG}")"
    stage_abs="$(cd "${WDOS5_STAGE_DIR}" && pwd)"
    stage_parent="$(dirname "${stage_abs}")"
    out_abs="$(cd "${WDOS5_OUTPUT_DIR}" && pwd)"

    # mksapkg-OS5 has no output-path flag: it runs `cd ..` and drops the
    # .bin in the parent of its current working directory. Clean any stale
    # artifact there so the lookup below picks up the fresh one.
    rm -f "${stage_parent}/${WDOS5_MODEL}_${APP_NAME}_"*.bin
    ( cd "${stage_abs}" && "${mksapkg_abs}" -E -s -m "${WDOS5_MODEL}" )

    local produced_bin bin_path
    produced_bin="$(ls -t "${stage_parent}/${WDOS5_MODEL}_${APP_NAME}_"*.bin 2>/dev/null | head -1)"
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
        *)
            log_error "Unknown target: ${target}"
            echo "Supported targets: native, wd-ex2-ultra-os5"
            exit 1
            ;;
    esac
}

main "$@"
