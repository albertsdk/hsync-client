#!/bin/bash
# Regenerate docs/assets/demo-p2p.gif (the P2P mode demo embedded in README.md).
#
# Single entry point for the demo asset; run it from any directory:
#   docs/assets/demo-p2p.sh
#
# It records docs/assets/demo-p2p.tape with vhs. The tape drives the hidden
# tmux setup in docs/assets/demo-p2p-setup.sh and types both `hsync p2p`
# commands on camera, so this wrapper only renders and never touches tmux.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
REPO_ROOT="$( realpath "$SCRIPT_DIR/../.." )"

if ! command -v vhs >/dev/null 2>&1; then
  echo "error: vhs not found on PATH (see https://github.com/charmbracelet/vhs)" >&2
  exit 1
fi

# vhs resolves the tape's Output path and the setup command it types relative to
# its working directory, so record from the repository root.
cd "$REPO_ROOT"
exec vhs docs/assets/demo-p2p.tape
