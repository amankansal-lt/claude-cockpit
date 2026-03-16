#!/usr/bin/env bash
#
# Claude Cockpit — Installer
#
# Sets up:
#   1. Python venv with dependencies
#   2. Shell alias 'cockpit' + toggle function in shell rc
#   3. iTerm2 AutoLaunch script (optional, needs Python Runtime)
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
ITERM2_SCRIPTS_DIR="$HOME/Library/Application Support/iTerm2/Scripts/AutoLaunch"
MIN_PYTHON="3.10"

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${CYAN}▸${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
err()   { echo -e "${RED}✗${NC} $1" >&2; }

echo -e "\n${BOLD}Claude Cockpit — Installer${NC}\n"

# ---------- 0. Check prerequisites ----------
# Check Python version
PYTHON_CMD=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>/dev/null || true)
        if [ -n "$ver" ]; then
            major="${ver%%.*}"
            minor="${ver#*.}"
            if [ "$major" -ge 3 ] && [ "$minor" -ge 10 ]; then
                PYTHON_CMD="$cmd"
                break
            fi
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    err "Python $MIN_PYTHON+ is required but not found."
    echo "    Install with: brew install python@3.12"
    exit 1
fi
ok "Found $PYTHON_CMD ($ver)"

# Check Claude Code data exists
if [ ! -d "$HOME/.claude" ]; then
    warn "~/.claude/ not found — install Claude Code first"
    echo "    https://docs.anthropic.com/en/docs/claude-code"
    echo "    Cockpit will work once you've run at least one Claude session."
fi

# ---------- 1. Python venv ----------
if [ -d "$VENV_DIR" ] && [ -f "$VENV_DIR/bin/python" ]; then
    ok "Venv exists at .venv/"
else
    info "Creating Python venv..."
    "$PYTHON_CMD" -m venv "$VENV_DIR"
    ok "Venv created"
fi

info "Installing dependencies..."
"$VENV_DIR/bin/pip" install -q --upgrade pip 2>/dev/null
"$VENV_DIR/bin/pip" install -q textual rich iterm2 watchfiles 2>/dev/null
ok "Dependencies installed (textual, rich, iterm2, watchfiles)"

info "Installing cockpit package..."
"$VENV_DIR/bin/pip" install -q -e "$SCRIPT_DIR" 2>/dev/null
ok "Package installed in editable mode"

# ---------- 2. Shell integration ----------
# Detect shell rc file
if [ -n "${ZSH_VERSION:-}" ] || [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
elif [ -f "$HOME/.bash_profile" ]; then
    SHELL_RC="$HOME/.bash_profile"
else
    SHELL_RC="$HOME/.zshrc"
fi

MARKER="# Claude Cockpit"
ALIAS_LINE="alias cockpit='\"$VENV_DIR/bin/python\" -m cockpit'"

# The toggle function uses AppleScript for iTerm2, falls back to direct launch
read -r -d '' TOGGLE_FUNC << 'TOGGLE_EOF' || true

# Claude Cockpit — open in iTerm2 split pane
cockpit-toggle() {
    local script_dir
    script_dir="$(cat "$HOME/.claude-cockpit-path" 2>/dev/null || echo "$HOME/claude-cockpit")"
    local cockpit_python="$script_dir/.venv/bin/python"
    if [ ! -f "$cockpit_python" ]; then
        echo "Cockpit not installed. Run: cd $script_dir && ./install.sh"
        return 1
    fi
    local cockpit_cmd="$cockpit_python -m cockpit"
    if [ "${TERM_PROGRAM:-}" = "iTerm.app" ]; then
        osascript <<APPLEOF
            tell application "iTerm2"
                tell current session of current tab of current window
                    split vertically with default profile command "$cockpit_cmd"
                end tell
            end tell
APPLEOF
        if [ $? -ne 0 ]; then
            eval "$cockpit_cmd"
        fi
    else
        eval "$cockpit_cmd"
    fi
}
TOGGLE_EOF

if grep -q "$MARKER" "$SHELL_RC" 2>/dev/null; then
    # Update existing block
    info "Updating shell integration in $SHELL_RC..."
    # Remove old block and re-add
    sed -i.bak "/$MARKER/,/^$/d" "$SHELL_RC" 2>/dev/null || true
fi

info "Adding shell integration to $SHELL_RC..."
{
    echo ""
    echo "$MARKER"
    echo "$ALIAS_LINE"
    echo "$TOGGLE_FUNC"
} >> "$SHELL_RC"
ok "Added 'cockpit' alias and 'cockpit-toggle' function"

# Store the install path so cockpit-toggle can find it from any machine
echo "$SCRIPT_DIR" > "$HOME/.claude-cockpit-path"

# ---------- 3. iTerm2 AutoLaunch (optional) ----------
echo ""
ITERM2_API_ENABLED=$(defaults read com.googlecode.iterm2 EnableAPIServer 2>/dev/null || echo "0")

if [ "$ITERM2_API_ENABLED" = "1" ]; then
    ok "iTerm2 API Server is enabled"
    if [ -d "$ITERM2_SCRIPTS_DIR" ]; then
        info "Installing iTerm2 status bar script..."
        cp "$SCRIPT_DIR/iterm2_plugin/status_bar.py" \
           "$ITERM2_SCRIPTS_DIR/claude_cockpit_statusbar.py"
        ok "Status bar script installed to AutoLaunch"
        echo ""
        echo "    To see status bar components:"
        echo "    1. iTerm2 > Settings > Profiles > Session > Status bar enabled"
        echo "    2. Click 'Configure Status Bar'"
        echo "    3. Drag 'Claude Memory', 'Claude Tasks', 'Claude Context'"
        echo "    4. Restart iTerm2"
    else
        warn "iTerm2 AutoLaunch directory not found"
        echo "    Install Python Runtime: iTerm2 > Scripts > Manage > Install Python Runtime"
        echo "    Then re-run this installer."
    fi
else
    warn "iTerm2 API not enabled (status bar components won't work)"
    echo "    Enable: iTerm2 > Settings > General > Magic > Enable Python API"
    echo "    The TUI ('cockpit' command) works without this."
fi

# ---------- Done ----------
echo ""
echo -e "${BOLD}${GREEN}Installation complete!${NC}"
echo ""
echo "  Commands:"
echo "    cockpit              Launch the full TUI dashboard"
echo "    cockpit-toggle       Open in iTerm2 split pane (or launch directly)"
echo ""
echo "  Keybindings (inside cockpit):"
echo "    /    Search memory        ?    Help screen"
echo "    m    Memory tab           t    Tasks tab"
echo "    p    Plans tab            s    Stats tab"
echo "    h    History tab          r    Refresh all"
echo "    Esc  Unfocus input        q    Quit"
echo ""
echo -e "  Run ${CYAN}source $SHELL_RC${NC} or open a new terminal to start."
echo ""
