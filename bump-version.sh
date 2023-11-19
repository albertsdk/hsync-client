#!/bin/bash
# HSYNC Client Version Bump Script
# Updates version in all required files
#
# Usage:
#   ./bump-version.sh <new_version>
#   ./bump-version.sh 0.2.0
#
# This script updates:
#   - pyproject.toml
#   - hsync_client/constants.py

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Validate arguments
if [ $# -ne 1 ]; then
    echo -e "${RED}Error: Version argument required${NC}"
    echo "Usage: $0 <new_version>"
    echo "Example: $0 0.2.0"
    exit 1
fi

NEW_VERSION="$1"

# Validate version format (semver: X.Y.Z)
if ! [[ "$NEW_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo -e "${RED}Error: Invalid version format${NC}"
    echo "Version must be in semver format: X.Y.Z (e.g., 0.2.0, 1.0.0)"
    exit 1
fi

echo -e "${YELLOW}Bumping hsync-client version to ${NEW_VERSION}...${NC}"
echo ""

# Function to update version in a file
update_version() {
    local file="$1"
    local pattern="$2"
    local replacement="$3"
    
    if [ ! -f "$file" ]; then
        echo -e "${RED}Error: File not found: $file${NC}"
        exit 1
    fi
    
    # Use sed to replace the version
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' "s|$pattern|$replacement|g" "$file"
    else
        sed -i "s|$pattern|$replacement|g" "$file"
    fi
    
    echo -e "${GREEN}✓${NC} Updated: $file"
}

# Update files
update_version \
    "$SCRIPT_DIR/pyproject.toml" \
    'version = "[0-9]*\.[0-9]*\.[0-9]*"' \
    "version = \"$NEW_VERSION\""

update_version \
    "$SCRIPT_DIR/hsync_client/constants.py" \
    'HSYNC_VERSION = "[0-9]*\.[0-9]*\.[0-9]*"' \
    "HSYNC_VERSION = \"$NEW_VERSION\""

echo ""
echo -e "${GREEN}Version bumped to ${NEW_VERSION} successfully!${NC}"
echo ""
echo "Files updated:"
echo "  - pyproject.toml"
echo "  - hsync_client/constants.py"
echo ""
echo "Next steps:"
echo "  1. Review changes: git diff"
echo "  2. Commit: git commit -am \"Bump version to ${NEW_VERSION}\""
echo "  3. Tag: git tag v${NEW_VERSION}"

