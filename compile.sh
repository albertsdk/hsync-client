#!/usr/bin/env bash
#
# HSYNC Client - Build Script
# Compiles the client into a standalone binary using PyInstaller.
#
set -euo pipefail

# -----------------------------------------------------------------------------
# Colors
# -----------------------------------------------------------------------------
readonly RED='\033[0;31m'
readonly GREEN='\033[0;32m'
readonly YELLOW='\033[0;33m'
readonly BLUE='\033[0;34m'
readonly CYAN='\033[0;36m'
readonly BOLD='\033[1m'
readonly DIM='\033[2m'
readonly RESET='\033[0m'

# -----------------------------------------------------------------------------
# Functions
# -----------------------------------------------------------------------------
log_info()  { echo -e "${BLUE}ℹ${RESET} $*"; }
log_ok()    { echo -e "${GREEN}✓${RESET} $*"; }
log_warn()  { echo -e "${YELLOW}⚠${RESET} $*"; }
log_error() { echo -e "${RED}✕${RESET} $*" >&2; }
log_step()  { echo -e "${CYAN}→${RESET} ${BOLD}$*${RESET}"; }

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
    echo -e "${BOLD}HSYNC Client - Build${RESET}"
    echo -e "${DIM}────────────────────${RESET}"
    echo

    # Check for virtual environment
    if [[ ! -d ".venv" ]]; then
        log_error "Virtual environment not found. Run 'poetry install' first."
        exit 1
    fi

    log_step "Activating virtual environment..."
    # shellcheck source=/dev/null
    source .venv/bin/activate

    log_step "Building standalone binary..."
    pyinstaller --clean --onefile hsync_client/main.py --name hsync

    echo
    if [[ -f "dist/hsync" ]]; then
        log_ok "Build successful: ${GREEN}dist/hsync${RESET}"
        echo -e "${DIM}Run with: ./dist/hsync --help${RESET}"
    else
        log_error "Build failed"
        exit 1
    fi
}

main "$@"
