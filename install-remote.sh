#!/usr/bin/env bash
#
# Claude Cockpit — One-line installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/AmanKansal2012/claude-cockpit/master/install-remote.sh | bash
#
set -euo pipefail

REPO="https://github.com/AmanKansal2012/claude-cockpit.git"
INSTALL_DIR="$HOME/claude-cockpit"

BOLD='\033[1m'
GREEN='\033[0;32m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}▸${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1" >&2; }

echo -e "\n${BOLD}Claude Cockpit — One-line Installer${NC}\n"

# Check prerequisites
if ! command -v git &>/dev/null; then
    err "git is required. Install with: brew install git"
    exit 1
fi

if ! command -v python3 &>/dev/null; then
    err "Python 3.10+ is required. Install with: brew install python@3.12"
    exit 1
fi

ver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
major="${ver%%.*}"
minor="${ver#*.}"
if [ "$major" -lt 3 ] || { [ "$major" -eq 3 ] && [ "$minor" -lt 10 ]; }; then
    err "Python 3.10+ required (found $ver). Install with: brew install python@3.12"
    exit 1
fi

# Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    git -C "$INSTALL_DIR" pull --ff-only origin master 2>/dev/null || {
        info "Pull failed, re-cloning..."
        rm -rf "$INSTALL_DIR"
        git clone --depth 1 "$REPO" "$INSTALL_DIR"
    }
    ok "Updated"
else
    if [ -d "$INSTALL_DIR" ]; then
        err "$INSTALL_DIR exists but is not a git repo. Remove it first."
        exit 1
    fi
    info "Downloading Claude Cockpit..."
    git clone --depth 1 "$REPO" "$INSTALL_DIR"
    ok "Downloaded to $INSTALL_DIR"
fi

# Run the full installer
info "Running installer..."
cd "$INSTALL_DIR"
bash ./install.sh

echo ""
echo -e "${BOLD}${GREEN}Done!${NC} Open a new terminal or run: ${CYAN}source ~/.zshrc${NC}"
echo ""
