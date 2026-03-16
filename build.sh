#!/usr/bin/env bash
#
# Claude Cockpit — Build binary release
#
# Produces: dist/cockpit (single-file binary)
#           dist/cockpit-<VERSION>-macos-arm64.zip
#           dist/cockpit-<VERSION>-macos-arm64.zip.sha256
#
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}▸${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1" >&2; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VERSION=$(python3 -c "from cockpit import __version__; print(__version__)")
echo -e "\n${BOLD}Building Claude Cockpit v${VERSION}${NC}\n"

# Clean previous builds
info "Cleaning previous builds..."
rm -rf build/ dist/
ok "Cleaned"

# Ensure PyInstaller is available
if ! python3 -c "import PyInstaller" 2>/dev/null; then
    info "Installing PyInstaller..."
    pip install pyinstaller
fi
ok "PyInstaller available"

# Build
info "Building binary (this takes 30-60s)..."
python3 -m PyInstaller cockpit.spec --noconfirm 2>&1 | tail -5
ok "Binary built: dist/cockpit"

# Verify
info "Smoke test..."
RESULT=$(./dist/cockpit --version 2>&1)
if echo "$RESULT" | grep -q "Claude Cockpit"; then
    ok "Smoke test passed: $RESULT"
else
    err "Smoke test failed: $RESULT"
    exit 1
fi

# Package
info "Packaging..."
cd dist
chmod +x cockpit
ZIPNAME="cockpit-${VERSION}-macos-arm64.zip"
zip "$ZIPNAME" cockpit
shasum -a 256 "$ZIPNAME" > "${ZIPNAME}.sha256"
cd ..

SIZE=$(du -sh "dist/$ZIPNAME" | cut -f1)
SHA=$(cat "dist/${ZIPNAME}.sha256" | cut -d' ' -f1)

echo ""
echo -e "${BOLD}${GREEN}Build complete!${NC}"
echo ""
echo "  Binary:  dist/cockpit"
echo "  Archive: dist/$ZIPNAME ($SIZE)"
echo "  SHA-256: $SHA"
echo ""
echo "  To release:"
echo "    git tag v${VERSION}"
echo "    git push origin v${VERSION}"
echo ""
