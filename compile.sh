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
#   -c, --clean    Clean build artifacts before building
#   -h, --help     Show this help message
#
# Requirements:
#   - Python virtual environment (.venv) with dependencies installed
#   - Run 'poetry install' before first build
#
# Output:
#   - dist/hsync   Standalone executable binary
#
# Author: Alberto Basso <alberto.sdk [at] gmail [dot] com>
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------

# Script directory (resolves symlinks)
readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Build configuration
readonly APP_NAME="hsync"
readonly ENTRY_POINT="hsync_client/main.py"
readonly OUTPUT_DIR="dist"
readonly BUILD_DIR="build"
readonly VENV_DIR=".venv"
readonly PYINSTALLER="${VENV_DIR}/bin/pyinstaller"

# -----------------------------------------------------------------------------
# Colors (disabled if not a terminal)
# -----------------------------------------------------------------------------

if [[ -t 1 ]]; then
    readonly RED='\033[0;31m'
    readonly GREEN='\033[0;32m'
    readonly YELLOW='\033[0;33m'
    readonly BLUE='\033[0;34m'
    readonly CYAN='\033[0;36m'
    readonly BOLD='\033[1m'
    readonly DIM='\033[2m'
    readonly RESET='\033[0m'
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
    -c, --clean    Clean build artifacts before building
    -h, --help     Show this help message

${BOLD}Examples:${RESET}
    ./compile.sh           # Build the binary
    ./compile.sh --clean   # Clean and rebuild

${BOLD}Output:${RESET}
    ${OUTPUT_DIR}/${APP_NAME}   Standalone executable
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

    if [[ -d "${OUTPUT_DIR}" ]]; then
        rm -rf "${OUTPUT_DIR}"
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

    log_step "Building ${APP_NAME} v${version}..."

    # Verify PyInstaller is available
    if [[ ! -x "${PYINSTALLER}" ]]; then
        log_error "PyInstaller not found at ${PYINSTALLER}"
        log_error "Run 'poetry install' first to install dependencies"
        exit 1
    fi

    # Run PyInstaller with optimized settings
    # --strip: Remove debug symbols to reduce binary size
    # --hidden-import: Include modules not detected by static analysis
    # --exclude-module: Exclude unnecessary modules to reduce size
    "${PYINSTALLER}" \
        --clean \
        --onefile \
        --strip \
        --name "${APP_NAME}" \
        --log-level WARN \
        --hidden-import "websockets.legacy" \
        --hidden-import "websockets.legacy.client" \
        --hidden-import "websockets.client" \
        --hidden-import "websockets.exceptions" \
        --hidden-import "watchdog.observers" \
        --hidden-import "watchdog.events" \
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
        "${ENTRY_POINT}"

    # Verify output
    if [[ -f "${OUTPUT_DIR}/${APP_NAME}" ]]; then
        local size
        size="$(du -h "${OUTPUT_DIR}/${APP_NAME}" | cut -f1)"

        echo
        log_ok "Build successful!"
        echo
        echo -e "  ${BOLD}Binary:${RESET}  ${OUTPUT_DIR}/${APP_NAME}"
        echo -e "  ${BOLD}Size:${RESET}    ${size}"
        echo -e "  ${BOLD}Version:${RESET} ${version}"
        echo
        echo -e "${DIM}Run with: ./${OUTPUT_DIR}/${APP_NAME} --help${RESET}"
    else
        log_error "Build failed: output binary not found"
        exit 1
    fi
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------

main() {
    local do_clean=false

    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -c|--clean)
                do_clean=true
                shift
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

    # Check prerequisites
    check_prerequisites

    # Clean if requested
    if [[ "${do_clean}" == true ]]; then
        clean_artifacts
        echo
    fi

    # Build
    build_binary
}

main "$@"
