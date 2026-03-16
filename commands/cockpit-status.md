---
name: cockpit-status
description: Check if Claude Cockpit TUI dashboard is running and show auto-memory status
---

Check the status of Claude Cockpit:

1. Run `ps aux | grep -v grep | grep 'cockpit'` to see if the Cockpit TUI process is running.
2. Read `~/.claude/cockpit-settings.json` to check the auto_memory toggle state.
3. If the file doesn't exist, report that auto-memory defaults to enabled (plugin mode).
4. Check if `~/.claude/cockpit-hooks/.last_run` exists and report when auto-memory last ran.
5. Report a summary: Cockpit process (running/not running), auto-memory (on/off), last auto-memory run time.
