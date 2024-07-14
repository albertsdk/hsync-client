#!/usr/bin/env bash
# Backward-compatible wrapper for the centralized Python version bump.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "${SCRIPT_DIR}/../hsync-common/scripts/dev/bump-python-version.sh" "$@"
