"""Data layer for ~/.claude/ — reads structured data, with limited write support for memory/plans."""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TASKS_DIR = CLAUDE_DIR / "tasks"
PLANS_DIR = CLAUDE_DIR / "plans"
DEBUG_DIR = CLAUDE_DIR / "debug"
STATS_FILE = CLAUDE_DIR / "stats-cache.json"
HISTORY_FILE = CLAUDE_DIR / "history.jsonl"
PINNED_FILE = CLAUDE_DIR / "cockpit-pinned.json"
PINNED_PLANS_FILE = CLAUDE_DIR / "cockpit-pinned-plans.json"
SETTINGS_FILE = CLAUDE_DIR / "cockpit-settings.json"
EXPORT_DIR = Path.home() / "Desktop"

# Directories the file watcher should monitor
WATCH_PATHS = [TASKS_DIR, PLANS_DIR, DEBUG_DIR, STATS_FILE.parent, PROJECTS_DIR]

# --- Tuning constants ---
TAIL_CHUNK_SIZE = 8192
NEW_SESSION_MATCH_DELTA_SECS = 120
RESUMED_SESSION_MATCH_DELTA_SECS = 300
MATCH_SESSIONS_LIMIT = 50
DASHBOARD_MAX_SESSIONS = 8
SESSION_ACTIVE_THRESHOLD_MINS = 10
DASHBOARD_SCAN_LIMIT = 50
AUTOCOMPACT_TAIL_BYTES = 204_800
DEFAULT_CONTEXT_WINDOW = 200_000
CONTEXT_CHARS_ESTIMATE = 800_000
CHARS_PER_TOKEN = 4
SESSION_LIST_LIMIT = 100
MAX_PROJECTS_SCAN = 200
MAX_FILES_PER_DIR = 500


def _log_warn(msg: str) -> None:
    """Log warning to stderr for cockpit diagnostics."""
    print(f"cockpit-warn: {msg}", file=sys.stderr)


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically via temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.rename(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_safe_child(child: Path, parent: Path) -> bool:
    """Check child is strictly inside parent (no prefix tricks)."""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


_XML_TAG_RE = re.compile(r"<[^>]+>")


def strip_xml_tags(text: str) -> str:
    """Remove XML/HTML-like tags and collapse whitespace (e.g. Claude Code command markup)."""
    cleaned = _XML_TAG_RE.sub("", text)
    # Collapse newlines and multiple spaces into single space
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _decode_project_name(encoded: str) -> str:
    """Convert URL-encoded project dir name to readable path.

    e.g. '-Users-amankansal-go-src-github-com-LambdatestIncPrivate-go-ios'
    becomes 'go-ios'

    Strategy: find the last org-like segment (capitalized, not a common dir name),
    take everything after it. Fallback to last non-trivial segments.
    """
    if not encoded:
        return "unknown"
    parts = encoded.strip("-").split("-")
    # Common directory names that look like orgs but aren't
    not_orgs = {"Users", "Documents", "Library", "Applications", "Desktop", "Downloads"}
    # Find the last segment that looks like an org name (has uppercase, not a common dir)
    last_org_idx = -1
    for i, p in enumerate(parts):
        if any(c.isupper() for c in p) and p not in not_orgs:
            last_org_idx = i
    if last_org_idx >= 0:
        after = parts[last_org_idx + 1:]
        if after:
            return "-".join(after)
        return parts[last_org_idx]
    # Fallback: strip contiguous leading path segments (left-to-right only)
    skip = {"users", "go", "src", "github", "com", "documents", "poc"}
    # Drop "Users/username" prefix if present
    tail = parts
    if len(parts) >= 2 and parts[0].lower() == "users":
        tail = parts[2:]
    # Strip leading path-like segments, keep everything from first meaningful one
    start = 0
    for i, p in enumerate(tail):
        if p.lower() not in skip:
            start = i
            break
    else:
        start = len(tail)
    remaining = tail[start:]
    if not remaining:
        return tail[-1] if tail else (parts[-1] if parts else encoded)
    return "-".join(remaining)


MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB — skip files larger than this


@dataclass
class MemoryFile:
    project: str
    name: str
    path: Path
    size: int
    lines: int = 0
    _content: str | None = field(default=None, repr=False)

    @property
    def display_name(self) -> str:
        return f"{self.project}/{self.name}"

    @property
    def content(self) -> str:
        """Lazy-load file content on first access."""
        if self._content is None:
            try:
                self._content = self.path.read_text(errors="replace")
            except OSError as e:
                _log_warn(f"read {self.path}: {e}")
                self._content = ""
        return self._content

    def load_content(self) -> None:
        """Force-load content (e.g. before search)."""
        _ = self.content


@dataclass
class Task:
    id: str
    subject: str
    description: str
    status: str  # pending, in_progress, completed
    active_form: str = ""
    blocks: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)
    session_dir: str = ""


@dataclass
class Plan:
    name: str
    path: Path
    content: str
    lines: int
    size: int
    mtime: float


@dataclass
class HistoryEntry:
    display: str
    timestamp: float
    project: str
    session_id: str


@dataclass
class DayStats:
    date: str
    messages: int = 0
    sessions: int = 0
    tool_calls: int = 0


@dataclass
class SessionInfo:
    session_id: str
    path: Path
    size: int
    mtime: float


@dataclass
class SearchResult:
    file: MemoryFile
    line_num: int
    line: str
    context_before: str = ""
    context_after: str = ""


# ---------- Memory ----------

def get_memory_files() -> list[MemoryFile]:
    """Find all memory files across all projects.

    Content is lazy-loaded — only metadata (path, size, line count estimate)
    is read eagerly. Call .content or .load_content() to read file contents.
    Files larger than MAX_FILE_SIZE (10MB) are skipped.
    """
    files = []
    if not PROJECTS_DIR.exists():
        return files
    for memory_dir in sorted(PROJECTS_DIR.glob("*/memory"))[:MAX_PROJECTS_SCAN]:
        project_name = _decode_project_name(memory_dir.parent.name)
        # Scan direct .md files and auto/ subdirectory
        for md_file in sorted(memory_dir.glob("*.md"))[:MAX_FILES_PER_DIR]:
            try:
                stat = md_file.stat()
                if stat.st_size > MAX_FILE_SIZE:
                    continue
                est_lines = max(1, stat.st_size // 40)
                files.append(MemoryFile(
                    project=project_name,
                    name=md_file.name,
                    path=md_file,
                    size=stat.st_size,
                    lines=est_lines,
                ))
            except OSError as e:
                _log_warn(f"stat {md_file}: {e}")
                continue
        # Auto-generated memory files
        auto_dir = memory_dir / "auto"
        if auto_dir.exists():
            for md_file in sorted(auto_dir.glob("*.md"))[:MAX_FILES_PER_DIR]:
                try:
                    stat = md_file.stat()
                    if stat.st_size > MAX_FILE_SIZE:
                        continue
                    est_lines = max(1, stat.st_size // 40)
                    files.append(MemoryFile(
                        project=project_name,
                        name=md_file.name,
                        path=md_file,
                        size=stat.st_size,
                        lines=est_lines,
                    ))
                except OSError as e:
                    _log_warn(f"stat auto {md_file}: {e}")
                    continue
    return files


def search_memory(query: str, files: list[MemoryFile], context: int = 1) -> list[SearchResult]:
    """Full-text search across all memory files. Case-insensitive."""
    if not query.strip():
        return []
    results = []
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    for mf in files:
        lines = mf.content.splitlines()
        for i, line in enumerate(lines):
            if pattern.search(line):
                before = "\n".join(lines[max(0, i - context):i]) if context > 0 else ""
                after = "\n".join(lines[i + 1:i + 1 + context]) if context > 0 else ""
                results.append(SearchResult(
                    file=mf,
                    line_num=i + 1,
                    line=line,
                    context_before=before,
                    context_after=after,
                ))
    return results


def memory_summary(files: list[MemoryFile]) -> dict:
    """Quick summary stats for memory."""
    total_lines = sum(f.lines for f in files)
    total_size = sum(f.size for f in files)
    projects = len({f.project for f in files})
    return {
        "files": len(files),
        "lines": total_lines,
        "size": total_size,
        "projects": projects,
    }


# ---------- Tasks ----------

def _get_task_dirs_sorted() -> list[tuple[Path, float]]:
    """Get task directories sorted by most recent modification time."""
    if not TASKS_DIR.exists():
        return []
    task_dirs = []
    try:
        for d in list(TASKS_DIR.iterdir())[:MAX_PROJECTS_SCAN]:
            if d.is_dir():
                json_files = list(d.glob("*.json"))[:MAX_FILES_PER_DIR]
                if json_files:
                    latest_mtime = max(f.stat().st_mtime for f in json_files)
                    task_dirs.append((d, latest_mtime))
    except OSError as e:
        _log_warn(f"scan task dirs: {e}")
        return []
    task_dirs.sort(key=lambda x: x[1], reverse=True)
    return task_dirs


def get_tasks() -> list[Task]:
    """Get tasks from the most recent active task session."""
    task_dirs = _get_task_dirs_sorted()
    if not task_dirs:
        return []
    return _load_tasks_from_dir(task_dirs[0][0])


def get_all_recent_tasks(limit: int = 3, max_age_hours: float = 24) -> list[Task]:
    """Get tasks from recent task sessions that have unfinished work.

    Skips sessions where all tasks are completed (stale finished sessions)
    and sessions older than max_age_hours.
    """
    task_dirs = _get_task_dirs_sorted()
    all_tasks = []
    cutoff = time.time() - max_age_hours * 3600
    found = 0
    for task_dir, mtime in task_dirs:
        if mtime < cutoff:
            break  # Sorted by mtime desc, so all remaining are older
        tasks = _load_tasks_from_dir(task_dir)
        # Skip sessions where everything is done
        if tasks and all(t.status == "completed" for t in tasks):
            continue
        all_tasks.extend(tasks)
        found += 1
        if found >= limit:
            break
    return all_tasks


def _load_tasks_from_dir(directory: Path) -> list[Task]:
    tasks = []
    for json_file in sorted(directory.glob("*.json"))[:MAX_FILES_PER_DIR]:
        if json_file.name.startswith("."):
            continue
        try:
            raw = json.loads(json_file.read_text())
            tasks.append(Task(
                id=raw.get("id", json_file.stem),
                subject=raw.get("subject", "Untitled"),
                description=raw.get("description", ""),
                status=raw.get("status", "pending"),
                active_form=raw.get("activeForm", ""),
                blocks=raw.get("blocks", []),
                blocked_by=raw.get("blockedBy", []),
                session_dir=directory.name,
            ))
        except (json.JSONDecodeError, OSError) as e:
            _log_warn(f"skip task {json_file.name}: {e}")
            continue
    return tasks


def _find_task_file(task: Task) -> Path | None:
    """Find the JSON file backing a Task."""
    task_dir = TASKS_DIR / task.session_dir
    candidate = task_dir / f"{task.id}.json"
    if candidate.exists():
        return candidate
    return None


def update_task_status(task: Task, new_status: str) -> tuple[bool, str]:
    """Update a task's status (e.g. 'completed', 'deleted'). Returns (ok, error)."""
    path = _find_task_file(task)
    if path is None:
        return False, "Task file not found"
    try:
        raw = json.loads(path.read_text())
        raw["status"] = new_status
        _atomic_write(path, json.dumps(raw, indent=2) + "\n")
        return True, ""
    except (json.JSONDecodeError, OSError) as e:
        return False, str(e)


def delete_task(task: Task) -> tuple[bool, str]:
    """Delete a task's JSON file. Returns (ok, error)."""
    path = _find_task_file(task)
    if path is None:
        return False, "Task file not found"
    try:
        path.unlink()
        return True, ""
    except OSError as e:
        return False, str(e)


def task_summary(tasks: list[Task]) -> dict:
    pending = sum(1 for t in tasks if t.status == "pending")
    active = sum(1 for t in tasks if t.status == "in_progress")
    done = sum(1 for t in tasks if t.status == "completed")
    return {"pending": pending, "active": active, "done": done, "total": len(tasks)}


def build_session_lookup(sessions: list["SessionEntry"]) -> dict[str, "SessionEntry"]:
    """Build a dict mapping session_id -> SessionEntry for fast lookup."""
    return {s.session_id: s for s in sessions}


@dataclass
class LiveProcess:
    """Rich info about a running Claude process."""
    pid: int
    tty: str  # /dev/ttys014
    cpu_percent: float
    uptime: str  # "1d 23h", "23m"
    tab_name: str  # iTerm tab name
    children: list[str]  # child process names ["logtail-mcp", "agent"]
    start_epoch: float = 0.0  # process start time (unix epoch)


def _get_live_processes() -> list[LiveProcess]:
    """Get detailed info about all running Claude processes."""
    import subprocess

    # Get Claude processes with CPU, PID, TTY, elapsed time, start time
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,ppid,tty,%cpu,etime,lstart,command"],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    claude_procs: dict[int, dict] = {}  # pid -> info
    all_procs: list[dict] = []  # for child lookup

    for line in out.stdout.splitlines()[1:]:  # skip header
        parts = line.split()
        if len(parts) < 11:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            tty = parts[2]
            cpu = float(parts[3])
            etime = parts[4]
            # lstart is 5 tokens: "Mon Mar 16 13:46:08 2026"
            lstart_str = " ".join(parts[5:10])
            command = " ".join(parts[10:])
        except (ValueError, IndexError):
            continue

        # Parse lstart to epoch
        start_epoch = 0.0
        try:
            dt = datetime.strptime(lstart_str.strip(), "%a %b %d %H:%M:%S %Y")
            start_epoch = dt.timestamp()
        except (ValueError, OSError) as e:
            _log_warn(f"parse lstart '{lstart_str}': {e}")

        proc = {"pid": pid, "ppid": ppid, "tty": tty, "cpu": cpu,
                "etime": etime, "command": command, "start_epoch": start_epoch}
        all_procs.append(proc)

        if command.strip() == "claude" and tty != "??":
            norm_tty = tty if tty.startswith("/dev/") else f"/dev/{tty}"
            claude_procs[pid] = {**proc, "tty": norm_tty}

    if not claude_procs:
        return []

    # Get iTerm tab names
    iterm_map = _get_iterm_tty_names()

    # Find children of each Claude process
    result = []
    for pid, info in claude_procs.items():
        tab_name = iterm_map.get(info["tty"], "")

        children = []
        for p in all_procs:
            if p["ppid"] == pid and p["command"] != "claude":
                # Extract short name from command
                cmd = p["command"]
                if "logtail-mcp" in cmd:
                    children.append("logtail")
                elif "slack-mcp" in cmd:
                    children.append("slack")
                elif "claude" in cmd:
                    children.append("agent")
                elif "node" in cmd:
                    name = cmd.split("/")[-1].split(".")[0]
                    children.append(name[:15])
                else:
                    children.append(cmd.split()[0].split("/")[-1][:15])

        # Format uptime: "01-23:21:15" -> "1d 23h" or "23:06" -> "23m"
        etime = info["etime"].strip()
        uptime = etime
        if "-" in etime:
            days, rest = etime.split("-", 1)
            hours = rest.split(":")[0] if ":" in rest else "0"
            uptime = f"{days}d {hours}h"
        elif etime.count(":") == 2:
            h, m, _ = etime.split(":")
            if int(h) > 0:
                uptime = f"{int(h)}h {int(m)}m"
            else:
                uptime = f"{int(m)}m"
        elif etime.count(":") == 1:
            m, s = etime.split(":")
            uptime = f"{int(m)}m"

        result.append(LiveProcess(
            pid=pid,
            tty=info["tty"],
            cpu_percent=info["cpu"],
            uptime=uptime,
            tab_name=tab_name,
            children=children,
            start_epoch=info.get("start_epoch", 0.0),
        ))

    return result


def _get_claude_tty_set() -> set[str]:
    """Get TTY devices of running Claude processes (not cockpit)."""
    return {p.tty for p in _get_live_processes()}


def _get_iterm_tty_names() -> dict[str, str]:
    """Get a mapping of TTY -> tab name for all iTerm sessions."""
    import subprocess
    script = (
        'tell application "iTerm"\n'
        '    set allPairs to {}\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        '                copy ((tty of s) & "|" & (name of s)) to end of allPairs\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        '    return allPairs\n'
        'end tell\n'
    )
    try:
        out = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode != 0:
            return {}
        # osascript returns "tty1|name1, tty2|name2, ..."
        result = {}
        for pair in out.stdout.strip().split(", "):
            if "|" in pair:
                tty, name = pair.split("|", 1)
                result[tty.strip()] = name.strip()
        return result
    except (OSError, subprocess.TimeoutExpired):
        return {}


def _parse_jsonl_timestamp(ts: str) -> float:
    """Parse an ISO 8601 timestamp string to epoch seconds."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt.timestamp()


def _get_jsonl_creation_time(path: Path) -> float:
    """Get session creation time from the first JSONL line's timestamp.

    Portable (no macOS-specific stat), no subprocess.
    Falls back to file mtime if parsing fails.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            first_line = f.readline(TAIL_CHUNK_SIZE)
            if first_line:
                obj = json.loads(first_line)
                ts = obj.get("timestamp", "")
                if ts:
                    return _parse_jsonl_timestamp(ts)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
        _log_warn(f"jsonl creation time {path.name}: {e}")
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _get_jsonl_last_activity(path: Path) -> float:
    """Get the timestamp of the last JSONL line (most recent activity).

    For resumed sessions, this is close to the current process start time.
    Falls back to file mtime.
    """
    try:
        # Read last non-empty line efficiently
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return 0.0
            # Read last chunk — enough for one JSONL line
            read_size = min(size, TAIL_CHUNK_SIZE)
            f.seek(size - read_size)
            chunk = f.read(read_size).decode("utf-8", errors="replace")
        lines = chunk.strip().split("\n")
        for line in reversed(lines):
            line = line.strip()
            if line:
                obj = json.loads(line)
                ts = obj.get("timestamp", "")
                if ts:
                    return _parse_jsonl_timestamp(ts)
    except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
        _log_warn(f"jsonl last activity {path.name}: {e}")
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


PASS3_MATCH_WINDOW_SECS = 3600  # 1 hour max for pass 3 matching


def _get_first_entry_after(path: Path, target_epoch: float) -> float:
    """Find the first user/system JSONL entry timestamp AFTER target_epoch.

    Returns the delta (seconds after target), or -1 if none found within window.
    Used to match resumed sessions: when a process resumes a session,
    the first user message in that session's JSONL will be shortly after
    the process start time.
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if obj.get("type") not in ("user", "system"):
                        continue
                    ts = obj.get("timestamp", "")
                    if not ts:
                        continue
                    epoch = _parse_jsonl_timestamp(ts)
                    if epoch >= target_epoch:
                        delta = epoch - target_epoch
                        if delta <= PASS3_MATCH_WINDOW_SECS:
                            return delta
                        return -1  # first entry too far away
                except (json.JSONDecodeError, ValueError, KeyError):
                    continue
    except OSError:
        pass
    return -1


def _optimal_match(
    pairs: list[tuple[float, LiveProcess, "SessionEntry"]],
    used_procs: set[int],
    used_sessions: set[str],
) -> dict[str, LiveProcess]:
    """Given (delta, proc, session) triples sorted by delta, assign optimally."""
    matched: dict[str, LiveProcess] = {}
    for delta, proc, session in pairs:
        if proc.pid in used_procs or session.session_id in used_sessions:
            continue
        matched[session.session_id] = proc
        used_procs.add(proc.pid)
        used_sessions.add(session.session_id)
    return matched


_SESSION_ID_IN_TAB_RE = re.compile(r"§([0-9a-f]{8})")


def _match_procs_to_sessions(
    procs: list[LiveProcess],
    sessions: list["SessionEntry"],
) -> dict[str, LiveProcess]:
    """Match Claude processes to sessions. Returns session_id -> LiveProcess.

    Pass 0: Direct match — extract session ID stamped in iTerm tab name (§abcdef12).
    Pass 1: Match by JSONL first-line timestamp (new sessions).
    Pass 2: Match remaining by JSONL last-activity (resumed sessions).
    Pass 3: Heuristic — mtime filtered by post-start activity.

    After matching, stamps session IDs into iTerm tab names for future
    deterministic matching.
    """
    if not procs:
        return {}

    valid_procs = [p for p in procs if p.start_epoch > 0]
    if not valid_procs:
        return {}

    # Build session lookup
    session_by_id: dict[str, "SessionEntry"] = {
        s.session_id: s for s in sessions[:MATCH_SESSIONS_LIMIT]
    }

    # --- Pass 0: direct match from stamped iTerm tab names ---
    matched: dict[str, LiveProcess] = {}
    used_procs: set[int] = set()
    used_sessions: set[str] = set()
    for proc in valid_procs:
        m = _SESSION_ID_IN_TAB_RE.search(proc.tab_name)
        if m:
            sid_prefix = m.group(1)
            for full_sid, s in session_by_id.items():
                if full_sid.startswith(sid_prefix):
                    matched[full_sid] = proc
                    used_procs.add(proc.pid)
                    used_sessions.add(full_sid)
                    break

    # Precompute creation times
    session_info: list[tuple["SessionEntry", float]] = []
    for s in sessions[:MATCH_SESSIONS_LIMIT]:
        ctime = _get_jsonl_creation_time(s.full_path)
        if ctime > 0:
            session_info.append((s, ctime))

    if not session_info:
        return matched

    # --- Pass 1: match by creation time (new sessions) ---
    pairs: list[tuple[float, LiveProcess, "SessionEntry"]] = []
    for proc in valid_procs:
        if proc.pid in used_procs:
            continue
        for s, ctime in session_info:
            if s.session_id in used_sessions:
                continue
            # JSONL created after process starts (5s clock-skew tolerance)
            if ctime >= proc.start_epoch - 5:
                delta = abs(ctime - proc.start_epoch)
                if delta < NEW_SESSION_MATCH_DELTA_SECS:
                    pairs.append((delta, proc, s))
    pairs.sort(key=lambda x: x[0])
    matched.update(_optimal_match(pairs, used_procs, used_sessions))

    # --- Pass 2: match remaining by last-activity time (resumed sessions) ---
    remaining_procs = [p for p in valid_procs if p.pid not in used_procs]
    if remaining_procs:
        pairs2: list[tuple[float, LiveProcess, "SessionEntry"]] = []
        for proc in remaining_procs:
            for s, _ in session_info:
                if s.session_id in used_sessions:
                    continue
                last_act = _get_jsonl_last_activity(s.full_path)
                if last_act <= 0:
                    continue
                # For resumed sessions, last activity should be near process start
                # or after it (Claude resumes and writes messages)
                delta = abs(last_act - proc.start_epoch)
                if delta < RESUMED_SESSION_MATCH_DELTA_SECS:
                    pairs2.append((delta, proc, s))
        pairs2.sort(key=lambda x: x[0])
        matched.update(_optimal_match(pairs2, used_procs, used_sessions))

    # --- Pass 3: match remaining by mtime, filtered to post-start activity ---
    # Only match sessions that were modified AFTER the process started
    # (the process must have written to the session). Among valid candidates,
    # pair most-recently-modified session → most-recently-started process.
    remaining_procs = [p for p in valid_procs if p.pid not in used_procs]
    if remaining_procs:
        remaining_procs.sort(key=lambda p: p.start_epoch, reverse=True)
        for proc in remaining_procs:
            candidates: list[tuple[float, "SessionEntry"]] = []
            for s, _ in session_info:
                if s.session_id in used_sessions:
                    continue
                try:
                    mt = s.full_path.stat().st_mtime
                except OSError:
                    continue
                if mt > proc.start_epoch:
                    candidates.append((mt, s))
            if candidates:
                candidates.sort(reverse=True)  # most recent first
                _, best = candidates[0]
                matched[best.session_id] = proc
                used_procs.add(proc.pid)
                used_sessions.add(best.session_id)

    # Stamp session IDs into iTerm tab names for future Pass 0 matching
    _stamp_iterm_session_ids(matched)

    return matched


def _stamp_iterm_session_ids(matched: dict[str, "LiveProcess"]) -> None:
    """Append §<session_id[:8]> to iTerm tab names for deterministic matching.

    Only stamps tabs that don't already have a § marker.
    Uses a single AppleScript call to stamp all tabs at once.
    """
    import subprocess

    to_stamp: list[tuple[str, str]] = []  # (tty, session_id_prefix)
    for session_id, proc in matched.items():
        if _SESSION_ID_IN_TAB_RE.search(proc.tab_name):
            continue  # already stamped
        to_stamp.append((proc.tty, session_id[:8]))

    if not to_stamp:
        return

    # Build a single AppleScript that stamps all tabs
    clauses = []
    for tty, sid_prefix in to_stamp:
        clauses.append(
            f'if tty of s is "{tty}" then\n'
            f'    set name of s to (name of s) & " §{sid_prefix}"\n'
            f'end if'
        )
    inner = "\n".join(clauses)
    script = (
        'tell application "iTerm"\n'
        '    repeat with w in windows\n'
        '        repeat with t in tabs of w\n'
        '            repeat with s in sessions of t\n'
        f'                {inner}\n'
        '            end repeat\n'
        '        end repeat\n'
        '    end repeat\n'
        'end tell\n'
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _log_warn(f"stamp iterm session ids: {e}")


def get_dashboard_sessions(
    sessions: list["SessionEntry"],
    max_recent: int = DASHBOARD_MAX_SESSIONS,
    active_threshold_minutes: int = SESSION_ACTIVE_THRESHOLD_MINS,
) -> list[dict]:
    """Get LIVE sessions for the Tasks dashboard.

    Returns dicts with keys: session, age_label, process (LiveProcess|None), tty.
    Uses two-pass matching (creation time + last-activity time) to handle
    both new and resumed sessions.
    """
    live_procs = _get_live_processes()
    live_count = len(live_procs)

    proc_map = _match_procs_to_sessions(live_procs, sessions)

    now = time.time()

    def _make_entry(s: "SessionEntry") -> dict | None:
        try:
            mtime = s.full_path.stat().st_mtime
        except OSError:
            return None
        age_seconds = now - mtime
        age_minutes = int(age_seconds / 60)
        if age_minutes < 1:
            age_label = "just now"
        elif age_minutes < 60:
            age_label = f"{age_minutes}m ago"
        else:
            age_label = f"{age_minutes // 60}h {age_minutes % 60}m ago"
        proc = proc_map.get(s.session_id)
        return {
            "session": s, "age_label": age_label,
            "process": proc, "tty": proc.tty if proc else "",
        }

    # Collect recent sessions (by mtime order)
    result = []
    seen: set[str] = set()
    cap = max(live_count, max_recent) if live_count > 0 else max_recent

    for s in sessions[:DASHBOARD_SCAN_LIMIT]:
        entry = _make_entry(s)
        if entry is None:
            continue
        # Only show sessions that have a matched process or were recently active
        has_proc = s.session_id in proc_map
        if not has_proc:
            try:
                age = now - s.full_path.stat().st_mtime
            except OSError:
                continue
            if age >= active_threshold_minutes * 60:
                continue
        result.append(entry)
        seen.add(s.session_id)
        if len(result) >= cap:
            break

    # Ensure sessions with matched processes are always visible
    for sid in proc_map:
        if sid not in seen:
            s = next((s for s in sessions if s.session_id == sid), None)
            if s:
                entry = _make_entry(s)
                if entry:
                    result.append(entry)
                    seen.add(sid)

    return result


def rename_session(session: "SessionEntry", new_summary: str) -> tuple[bool, str]:
    """Rename a session by updating its summary in sessions-index.json."""
    new_summary = new_summary.strip()
    if not new_summary:
        return False, "Name cannot be empty"
    # Find the sessions-index.json that contains this session
    if not PROJECTS_DIR.exists():
        return False, "Projects directory not found"
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        idx_file = proj_dir / "sessions-index.json"
        if not idx_file.exists():
            continue
        try:
            idx_mtime = idx_file.stat().st_mtime
            raw = json.loads(idx_file.read_text())
            entries = raw.get("entries", [])
            found = False
            for entry in entries:
                if entry.get("sessionId") == session.session_id:
                    entry["summary"] = new_summary
                    found = True
                    break
            if not found:
                continue
            # Write back with mtime check
            current_mtime = idx_file.stat().st_mtime
            if abs(current_mtime - idx_mtime) > 0.01:
                return False, "Index changed externally"
            _atomic_write(idx_file, json.dumps(raw, indent=2) + "\n")
            return True, ""
        except (json.JSONDecodeError, OSError) as e:
            return False, str(e)
    return False, "Session not found in any index"


# ---------- Plans ----------

def get_plans() -> list[Plan]:
    """Get all plan files, sorted by modification time (newest first)."""
    if not PLANS_DIR.exists():
        return []
    plans = []
    for md_file in list(PLANS_DIR.glob("*.md"))[:MAX_FILES_PER_DIR]:
        try:
            stat = md_file.stat()
            content = md_file.read_text(errors="replace")
            plans.append(Plan(
                name=md_file.stem,
                path=md_file,
                content=content,
                lines=content.count("\n") + 1,
                size=stat.st_size,
                mtime=stat.st_mtime,
            ))
        except OSError as e:
            _log_warn(f"read plan {md_file.name}: {e}")
            continue
    plans.sort(key=lambda p: p.mtime, reverse=True)
    return plans


# ---------- Stats ----------

def get_stats() -> list[DayStats]:
    """Parse stats-cache.json for daily usage metrics."""
    if not STATS_FILE.exists():
        return []
    try:
        raw = json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"read stats: {e}")
        return []
    stats = []
    # v2 format uses "dailyActivity" as a list of dicts
    daily = raw.get("dailyActivity", [])
    if isinstance(daily, list):
        for day_data in daily:
            stats.append(DayStats(
                date=day_data.get("date", ""),
                messages=day_data.get("messageCount", 0),
                sessions=day_data.get("sessionCount", 0),
                tool_calls=day_data.get("toolCallCount", 0),
            ))
    elif isinstance(daily, dict):
        for date_str, day_data in sorted(daily.items()):
            stats.append(DayStats(
                date=date_str,
                messages=day_data.get("messageCount", 0),
                sessions=day_data.get("sessionCount", 0),
                tool_calls=day_data.get("toolCallCount", 0),
            ))
    return stats


def get_stats_overview() -> dict:
    """Get the top-level summary from stats-cache.json."""
    if not STATS_FILE.exists():
        return {}
    try:
        raw = json.loads(STATS_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"read stats overview: {e}")
        return {}
    return {
        "total_sessions": raw.get("totalSessions", 0),
        "total_messages": raw.get("totalMessages", 0),
        "first_session": raw.get("firstSessionDate", ""),
        "models": raw.get("modelUsage", []),
        "longest_session": raw.get("longestSession", {}),
    }


def stats_summary(stats: list[DayStats]) -> dict:
    """Aggregate stats summary."""
    if not stats:
        return {"total_messages": 0, "total_sessions": 0, "total_tools": 0, "days": 0,
                "avg_daily_messages": 0, "last_7": []}
    total_messages = sum(s.messages for s in stats)
    total_sessions = sum(s.sessions for s in stats)
    total_tools = sum(s.tool_calls for s in stats)
    recent = stats[-7:] if len(stats) >= 7 else stats
    avg_messages = sum(s.messages for s in recent) // max(len(recent), 1)
    return {
        "total_messages": total_messages,
        "total_sessions": total_sessions,
        "total_tools": total_tools,
        "days": len(stats),
        "avg_daily_messages": avg_messages,
        "last_7": recent,
    }


# ---------- History ----------

def _tail_read_lines(filepath: Path, max_lines: int, chunk_size: int = TAIL_CHUNK_SIZE) -> list[str]:
    """Read the last max_lines lines from a file efficiently without loading it all.

    Reads raw bytes backwards in chunks, concatenates, then decodes once to
    avoid splitting multi-byte UTF-8 characters across chunk boundaries.
    Uses split("\\n") instead of splitlines() to preserve line boundaries.
    """
    try:
        file_size = filepath.stat().st_size
    except OSError:
        return []
    if file_size == 0:
        return []
    with open(filepath, "rb") as f:
        # Read backwards in chunks, accumulating raw bytes
        raw_chunks: list[bytes] = []
        offset = 0
        # Estimate: we need enough bytes for max_lines lines.
        # Typical JSONL line is ~200 bytes. Read conservatively.
        while offset < file_size:
            read_size = min(chunk_size, file_size - offset)
            offset += read_size
            f.seek(file_size - offset)
            chunk = f.read(read_size)
            raw_chunks.insert(0, chunk)
            # Check if we have enough newlines (quick byte scan)
            newline_count = sum(c.count(b"\n") for c in raw_chunks)
            if newline_count >= max_lines + 1:
                break
        # Concatenate bytes and decode once (safe for multi-byte UTF-8)
        raw = b"".join(raw_chunks)
        text = raw.decode("utf-8", errors="replace")
    # Use split("\n") to preserve empty strings at boundaries (not splitlines)
    lines = text.split("\n")
    # If we didn't read the whole file, first "line" is partial — drop it
    if offset < file_size and lines:
        lines = lines[1:]
    # Remove trailing empty string from final \n
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return lines[-max_lines:]


def get_history(limit: int = 200) -> list[HistoryEntry]:
    """Parse the last N entries from history.jsonl using efficient tail reading."""
    if not HISTORY_FILE.exists():
        return []
    entries = []
    lines = _tail_read_lines(HISTORY_FILE, limit)
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
            display = raw.get("display", raw.get("message", ""))
            if not display:
                continue
            entries.append(HistoryEntry(
                display=display[:200],
                timestamp=raw.get("timestamp", 0),
                project=_decode_project_name(raw.get("project", "")),
                session_id=raw.get("sessionId", ""),
            ))
        except json.JSONDecodeError:
            continue
    entries.reverse()  # Most recent first
    return entries


# ---------- Sessions ----------

def get_recent_sessions(limit: int = 20) -> list[SessionInfo]:
    """Get the most recent debug session files."""
    if not DEBUG_DIR.exists():
        return []
    sessions = []
    for txt_file in DEBUG_DIR.glob("*.txt"):
        try:
            stat = txt_file.stat()
            sessions.append(SessionInfo(
                session_id=txt_file.stem,
                path=txt_file,
                size=stat.st_size,
                mtime=stat.st_mtime,
            ))
        except OSError:
            continue
    sessions.sort(key=lambda s: s.mtime, reverse=True)
    return sessions[:limit]


def _parse_autocompact(path: Path) -> tuple[int, int] | None:
    """Extract actual token count from the debug transcript's autocompact lines.

    Claude Code logs: 'autocompact: tokens=151143 threshold=167000 effectiveWindow=180000'
    Returns (tokens, effective_window) or None if not found.
    """
    try:
        file_size = path.stat().st_size
    except OSError:
        return None
    # Read last chunk — autocompact lines appear frequently
    read_size = min(file_size, AUTOCOMPACT_TAIL_BYTES)
    try:
        with open(path, "rb") as f:
            if file_size > read_size:
                f.seek(file_size - read_size)
                f.readline()  # skip partial line
            tail = f.read()
    except OSError:
        return None

    # Find the last autocompact line
    last_tokens = None
    last_window = None
    for line in tail.split(b"\n"):
        if b"autocompact: tokens=" not in line:
            continue
        try:
            text = line.decode("utf-8", errors="replace")
            # Parse: autocompact: tokens=151143 threshold=167000 effectiveWindow=180000
            parts = text.split("autocompact: ", 1)[1].split()
            for part in parts:
                if part.startswith("tokens="):
                    last_tokens = int(part.split("=")[1])
                elif part.startswith("effectiveWindow="):
                    last_window = int(part.split("=")[1])
        except (IndexError, ValueError):
            continue

    if last_tokens is not None:
        return (last_tokens, last_window or DEFAULT_CONTEXT_WINDOW)
    return None


def estimate_context_usage(session: SessionInfo | None = None) -> dict:
    """Estimate context window usage from the debug transcript.

    Reads the actual token count from Claude Code's autocompact debug lines,
    which report real context window usage after each API call.
    """
    if session is None:
        sessions = get_recent_sessions(1)
        if not sessions:
            return {"percent": 0, "tokens_est": 0, "cost_est": 0.0, "active": False}
        session = sessions[0]

    age_hours = (time.time() - session.mtime) / 3600
    if age_hours > 1:
        return {"percent": 0, "tokens_est": 0, "cost_est": 0.0, "active": False}

    # Try to get actual token count from debug log
    autocompact = _parse_autocompact(session.path)
    if autocompact:
        tokens_est, context_limit = autocompact
    else:
        # Fallback: estimate from file size (capped to context window)
        chars = min(session.size, CONTEXT_CHARS_ESTIMATE)
        tokens_est = chars // CHARS_PER_TOKEN
        context_limit = DEFAULT_CONTEXT_WINDOW

    percent = min(100, int(tokens_est / context_limit * 100))
    # Cost estimate: Opus 4 pricing (~$15/M input, $75/M output, 70/30 split)
    input_tokens = int(tokens_est * 0.7)
    output_tokens = int(tokens_est * 0.3)
    cost_est = (input_tokens * 15 + output_tokens * 75) / 1_000_000

    return {
        "percent": percent,
        "tokens_est": tokens_est,
        "cost_est": cost_est,
        "active": True,
        "age_minutes": int(age_hours * 60),
    }


# ---------- Conversations ----------

@dataclass
class SessionEntry:
    """Metadata from sessions-index.json — lightweight, no content loaded."""
    session_id: str
    project: str
    full_path: Path
    summary: str
    first_prompt: str
    message_count: int
    created: str
    modified: str
    git_branch: str
    is_sidechain: bool
    file_size: int
    custom_title: str = ""


@dataclass
class ConversationMessage:
    """A single displayable message from a JSONL conversation."""
    uuid: str
    role: str
    text: str
    timestamp: str
    has_thinking: bool
    tool_names: list[str]
    is_sidechain: bool


def _read_custom_title(jsonl_path: Path) -> str:
    """Read the last custom-title entry from a JSONL file (tail-read for speed)."""
    try:
        size = jsonl_path.stat().st_size
        read_bytes = min(size, TAIL_CHUNK_SIZE * 4)  # 32KB should cover recent renames
        with open(jsonl_path, "rb") as f:
            if size > read_bytes:
                f.seek(size - read_bytes)
            chunk = f.read().decode("utf-8", errors="replace")
        title = ""
        for line in chunk.splitlines():
            line = line.strip()
            if not line or '"custom-title"' not in line:
                continue
            try:
                obj = json.loads(line)
                if obj.get("type") == "custom-title":
                    title = obj.get("customTitle", "")
            except (json.JSONDecodeError, KeyError):
                continue
        return title
    except OSError:
        return ""


def _extract_message(obj: dict) -> ConversationMessage | None:
    """Extract a displayable message from a JSONL line object."""
    msg_type = obj.get("type")
    if msg_type not in ("user", "assistant"):
        return None

    message = obj.get("message", {})
    content = message.get("content", "")
    timestamp = obj.get("timestamp", "")
    uuid = obj.get("uuid", "")
    is_sidechain = obj.get("isSidechain", False)

    text_parts: list[str] = []
    has_thinking = False
    tool_names: list[str] = []

    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "thinking":
                has_thinking = True
            elif btype == "tool_use":
                tool_names.append(block.get("name", "unknown"))

    text = "\n".join(text_parts).strip()
    if not text and not tool_names and not has_thinking:
        return None

    return ConversationMessage(
        uuid=uuid,
        role=msg_type,
        text=text,
        timestamp=timestamp,
        has_thinking=has_thinking,
        tool_names=tool_names,
        is_sidechain=is_sidechain,
    )


def _load_sessions_from_index(
    proj_dir: Path, project_name: str,
) -> tuple[list[SessionEntry], set[str]]:
    """Load sessions from sessions-index.json. Returns (sessions, seen_paths)."""
    sessions: list[SessionEntry] = []
    seen_paths: set[str] = set()
    idx_file = proj_dir / "sessions-index.json"
    if not idx_file.exists():
        return sessions, seen_paths
    try:
        raw = json.loads(idx_file.read_text())
        for entry in raw.get("entries", []):
            if entry.get("isSidechain", False):
                continue
            full_path = Path(entry.get("fullPath", ""))
            if not full_path.exists():
                continue
            try:
                file_size = full_path.stat().st_size
            except OSError as e:
                _log_warn(f"stat session {full_path.name}: {e}")
                continue
            seen_paths.add(str(full_path))
            custom_title = _read_custom_title(full_path)
            sessions.append(SessionEntry(
                session_id=entry.get("sessionId", ""),
                project=project_name,
                full_path=full_path,
                summary=entry.get("summary", ""),
                first_prompt=entry.get("firstPrompt", ""),
                message_count=entry.get("messageCount", 0),
                created=entry.get("created", ""),
                custom_title=custom_title,
                modified=entry.get("modified", ""),
                git_branch=entry.get("gitBranch", ""),
                is_sidechain=False,
                file_size=file_size,
            ))
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"read sessions index {idx_file}: {e}")
    return sessions, seen_paths


def _discover_sessions_from_jsonl(
    proj_dir: Path, project_name: str, seen_paths: set[str],
) -> list[SessionEntry]:
    """Discover sessions from JSONL files not in the index."""
    sessions: list[SessionEntry] = []
    for jsonl_file in list(proj_dir.glob("*.jsonl"))[:MAX_FILES_PER_DIR]:
        if str(jsonl_file) in seen_paths:
            continue
        if jsonl_file.name == "history.jsonl":
            continue
        try:
            stat = jsonl_file.stat()
            if stat.st_size < 10:
                continue
        except OSError as e:
            _log_warn(f"stat jsonl {jsonl_file.name}: {e}")
            continue
        first_prompt = ""
        timestamp = ""
        try:
            with open(jsonl_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "user":
                        timestamp = obj.get("timestamp", "")
                        msg = obj.get("message", {})
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            first_prompt = content[:100]
                        elif isinstance(content, list):
                            for b in content:
                                if b.get("type") == "text":
                                    first_prompt = b.get("text", "")[:100]
                                    break
                        break
        except OSError as e:
            _log_warn(f"read jsonl {jsonl_file.name}: {e}")
        if not first_prompt:
            continue
        try:
            mtime_dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
            modified = mtime_dt.isoformat()
        except (OSError, ValueError):
            modified = ""
        custom_title = _read_custom_title(jsonl_file)
        sessions.append(SessionEntry(
            session_id=jsonl_file.stem,
            project=project_name,
            full_path=jsonl_file,
            summary="",
            first_prompt=first_prompt,
            message_count=0,
            created=timestamp or modified,
            modified=modified,
            git_branch="",
            is_sidechain=False,
            file_size=stat.st_size,
            custom_title=custom_title,
        ))
    return sessions


def get_all_sessions() -> list[SessionEntry]:
    """Load session metadata from index files and JSONL discovery."""
    if not PROJECTS_DIR.exists():
        return []
    sessions: list[SessionEntry] = []
    seen_paths: set[str] = set()

    for proj_dir in sorted(PROJECTS_DIR.iterdir())[:MAX_PROJECTS_SCAN]:
        if not proj_dir.is_dir():
            continue
        project_name = _decode_project_name(proj_dir.name)
        idx_sessions, idx_seen = _load_sessions_from_index(proj_dir, project_name)
        sessions.extend(idx_sessions)
        seen_paths.update(idx_seen)
        sessions.extend(
            _discover_sessions_from_jsonl(proj_dir, project_name, seen_paths)
        )

    sessions.sort(key=lambda s: s.modified, reverse=True)
    return sessions


def get_session_messages(
    path: Path, offset: int = 0, limit: int = 50
) -> tuple[list[ConversationMessage], bool, int]:
    """Load displayable messages from a session JSONL with pagination.

    Streams line-by-line to handle very large files (100MB+).
    Returns (messages, has_more, total_count).
    """
    if not path.exists():
        return [], False, 0
    messages: list[ConversationMessage] = []
    displayable_count = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = _extract_message(obj)
                if msg is None:
                    continue
                displayable_count += 1
                if displayable_count <= offset:
                    continue
                if len(messages) < limit:
                    messages.append(msg)
                # Keep counting for total_count
    except OSError:
        return [], False, 0
    has_more = displayable_count > offset + limit
    return messages, has_more, displayable_count


def get_last_messages(
    path: Path, limit: int = 50
) -> tuple[list[ConversationMessage], int]:
    """Load the last N displayable messages from a session JSONL.

    Single pass — keeps a sliding window of the last `limit` messages.
    Returns (messages, total_count). No double-scan needed.
    """
    if not path.exists():
        return [], 0
    window: deque[ConversationMessage] = deque(maxlen=limit)
    total = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # Quick check before full JSON parse
                if '"user"' not in line and '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = _extract_message(obj)
                if msg is None:
                    continue
                total += 1
                window.append(msg)
    except OSError:
        return [], 0
    return list(window), total


def search_session(
    path: Path, query: str, limit: int = 30
) -> list[ConversationMessage]:
    """Search within a session's messages for a query string."""
    if not query.strip() or not path.exists():
        return []
    query_lower = query.lower()
    results: list[ConversationMessage] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = _extract_message(obj)
                if msg is None:
                    continue
                if query_lower in msg.text.lower():
                    results.append(msg)
                    if len(results) >= limit:
                        break
    except OSError:
        pass
    return results


# ---------- Tool Stats ----------

def get_tool_stats(messages: list[ConversationMessage]) -> dict[str, int]:
    """Count tool usage across messages, sorted by frequency descending."""
    counts: dict[str, int] = {}
    for msg in messages:
        for name in msg.tool_names:
            counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


def format_tool_stats(stats: dict[str, int], top_n: int = 4) -> str:
    """Format tool stats as compact string, e.g. 'Read:12 Edit:8 +2 more'."""
    if not stats:
        return ""
    items = list(stats.items())
    parts = [f"{name}:{count}" for name, count in items[:top_n]]
    remaining = len(items) - top_n
    if remaining > 0:
        parts.append(f"+{remaining} more")
    return " ".join(parts)


# ---------- Pinned Sessions ----------

def get_pinned() -> set[str]:
    """Read pinned session IDs from cockpit-pinned.json."""
    if not PINNED_FILE.exists():
        return set()
    try:
        raw = json.loads(PINNED_FILE.read_text())
        if isinstance(raw, list):
            return {s for s in raw if isinstance(s, str)}
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"read pinned: {e}")
    return set()


def toggle_pin(session_id: str) -> tuple[bool, str]:
    """Toggle pin state for a session. Returns (new_pin_state, error_msg)."""
    pinned = get_pinned()
    if session_id in pinned:
        pinned.discard(session_id)
        new_state = False
    else:
        pinned.add(session_id)
        new_state = True
    try:
        _atomic_write(PINNED_FILE, json.dumps(sorted(pinned), indent=2) + "\n")
    except OSError as e:
        _log_warn(f"write pinned: {e}")
        return not new_state, f"Failed to save pin: {e}"
    return new_state, ""


# ---------- Pinned Plans ----------

def get_pinned_plans() -> set[str]:
    """Read pinned plan names from cockpit-pinned-plans.json."""
    if not PINNED_PLANS_FILE.exists():
        return set()
    try:
        raw = json.loads(PINNED_PLANS_FILE.read_text())
        if isinstance(raw, list):
            return {s for s in raw if isinstance(s, str)}
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"read pinned plans: {e}")
    return set()


def toggle_pin_plan(plan_name: str) -> tuple[bool, str]:
    """Toggle pin state for a plan. Returns (new_pin_state, error_msg)."""
    pinned = get_pinned_plans()
    if plan_name in pinned:
        pinned.discard(plan_name)
        new_state = False
    else:
        pinned.add(plan_name)
        new_state = True
    try:
        _atomic_write(PINNED_PLANS_FILE, json.dumps(sorted(pinned), indent=2) + "\n")
    except OSError as e:
        _log_warn(f"write pinned plans: {e}")
        return not new_state, f"Failed to save pin: {e}"
    return new_state, ""


# ---------- Timeline ----------

@dataclass
class TimelineEntry:
    """A chronological entry: either a session or an auto-memory write."""
    date: str  # ISO date
    entry_type: str  # "session" or "memory"
    summary: str
    session_id: str = ""
    path: Path | None = None
    project: str = ""


def get_session_timeline(project: str = "") -> list[TimelineEntry]:
    """Get sessions + auto-memory writes for a project, sorted chronologically."""
    entries: list[TimelineEntry] = []

    # Sessions
    all_sessions = get_all_sessions()
    for s in all_sessions:
        if project and s.project != project:
            continue
        date = s.modified[:10] if s.modified else s.created[:10] if s.created else ""
        if not date:
            continue
        summary_text = s.custom_title or s.summary or s.first_prompt or "Untitled"
        msg_info = f" ({s.message_count} msgs)" if s.message_count > 0 else ""
        entries.append(TimelineEntry(
            date=date,
            entry_type="session",
            summary=f"{summary_text[:60]}{msg_info}",
            session_id=s.session_id,
            path=s.full_path,
            project=s.project,
        ))

    # Auto-memory writes
    if PROJECTS_DIR.exists():
        for auto_dir in list(PROJECTS_DIR.glob("*/memory/auto"))[:MAX_PROJECTS_SCAN]:
            proj_name = _decode_project_name(auto_dir.parent.parent.name)
            if project and proj_name != project:
                continue
            for md_file in list(auto_dir.glob("*.md"))[:MAX_FILES_PER_DIR]:
                date = md_file.stem  # e.g. "2026-03-14" or "session-summary-abc12345"
                if not date or date.startswith("session-summary"):
                    # Session summary — use mtime for date
                    try:
                        mtime = md_file.stat().st_mtime
                        date = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d")
                    except OSError:
                        continue
                    summary_text = f"Session summary ({md_file.stem})"
                else:
                    summary_text = f"Auto-memory ({md_file.name})"
                entries.append(TimelineEntry(
                    date=date,
                    entry_type="memory",
                    summary=summary_text,
                    path=md_file,
                    project=proj_name,
                ))

    entries.sort(key=lambda e: e.date, reverse=True)
    return entries


def get_timeline_projects() -> list[str]:
    """Get unique project names that have sessions or auto-memory."""
    projects = set()
    if PROJECTS_DIR.exists():
        for proj_dir in list(PROJECTS_DIR.iterdir())[:MAX_PROJECTS_SCAN]:
            if proj_dir.is_dir():
                projects.add(_decode_project_name(proj_dir.name))
    return sorted(projects)


# ---------- Deferred Items ----------

@dataclass
class DeferredItem:
    """A deferred task extracted from auto-memory."""
    task: str
    reason: str
    context: str
    source_file: Path
    date: str
    session_id: str = ""


def get_deferred_items() -> list[DeferredItem]:
    """Scan auto-memory files for deferred items."""
    items: list[DeferredItem] = []
    if not PROJECTS_DIR.exists():
        return items
    for auto_dir in list(PROJECTS_DIR.glob("*/memory/auto"))[:MAX_PROJECTS_SCAN]:
        for md_file in sorted(auto_dir.glob("*.md"), reverse=True)[:MAX_FILES_PER_DIR]:
            try:
                content = md_file.read_text(errors="replace")
            except OSError:
                continue
            # Parse deferred items: lines starting with "- **Deferred:**"
            for line in content.splitlines():
                line = line.strip()
                if not line.startswith("- **Deferred:**"):
                    continue
                # Format: - **Deferred:** task — reason (context)
                text = line[len("- **Deferred:**"):].strip()
                parts = text.split(" — ", 1)
                task_text = parts[0].strip()
                reason = ""
                ctx = ""
                if len(parts) > 1:
                    rest = parts[1]
                    # Extract (context) from end
                    if rest.endswith(")") and "(" in rest:
                        paren_idx = rest.rfind("(")
                        reason = rest[:paren_idx].strip()
                        ctx = rest[paren_idx + 1:-1].strip()
                    else:
                        reason = rest.strip()
                # Extract session_id from section header above
                session_id = ""
                date = md_file.stem  # e.g. "2026-03-14"
                items.append(DeferredItem(
                    task=task_text,
                    reason=reason,
                    context=ctx,
                    source_file=md_file,
                    date=date,
                    session_id=session_id,
                ))
    return items


# ---------- Settings ----------

def get_settings() -> dict:
    """Read cockpit settings with defaults."""
    defaults = {"auto_memory": False}
    if not SETTINGS_FILE.exists():
        return defaults
    try:
        raw = json.loads(SETTINGS_FILE.read_text())
        if isinstance(raw, dict):
            defaults.update(raw)
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"read settings: {e}")
    return defaults


_STOP_AGENT_PROMPT = (
    "You are an auto-memory agent. Extract and persist valuable context from this Claude Code session.\n\n"
    "Session info from ARGUMENTS: $ARGUMENTS\n"
    "Use the transcript_path field to locate the JSONL transcript. "
    "The project memory directory is at the same level as the transcript (sibling directory called 'memory/auto/').\n\n"
    "Steps:\n"
    "1. Use Bash to count user messages: grep -c '\"type\":\"user\"' <transcript_path>. If fewer than 3, skip.\n"
    "2. Use Bash to extract recent exchanges: grep '\"type\":\"user\"\\|\"type\":\"assistant\"' <transcript_path> | tail -30\n"
    "3. From those messages, identify ANY of:\n"
    "   - Decisions: architecture choices, tool/library selections, config changes\n"
    "   - Bugs fixed: root cause + what fixed it\n"
    "   - Environment discoveries: working commands, paths, port numbers, API patterns\n"
    "   - Deferred work: anything postponed ('do X later', 'revisit', 'TODO')\n"
    "   - Patterns: techniques that worked, gotchas, things to avoid\n"
    "4. If items found, determine auto dir from transcript_path "
    "(~/.claude/projects/<slug>/<session>.jsonl → ~/.claude/projects/<slug>/memory/auto/). Create with mkdir -p.\n"
    "5. Write to <auto-dir>/<YYYY-MM-DD>.md (append if exists). Format:\n"
    "   ### HH:MM\n"
    "   - **Decision:** what — why\n"
    "   - **Finding:** what — evidence\n"
    "   - **Deferred:** what — context\n"
    "6. For deferred items, ALSO append to <project-dir>/memory/MEMORY.md under '## Deferred'.\n\n"
    "IMPORTANT: Err on the side of capturing MORE. A 2-line entry costs nothing; losing context costs hours. "
    "Only skip if the conversation is truly trivial (greetings, simple yes/no)."
)

_PRECOMPACT_AGENT_PROMPT = (
    "You are an auto-memory agent running BEFORE context compaction. "
    "After compaction, earlier messages are lost forever. Save a comprehensive session summary.\n\n"
    "Session info: $ARGUMENTS\nUse the transcript_path field directly.\n\n"
    "Steps:\n"
    "1. Use Bash: grep '\"type\":\"user\"\\|\"type\":\"assistant\"' <transcript_path> | tail -80\n"
    "2. Write a comprehensive summary (<50 lines) covering:\n"
    "   - What was accomplished\n"
    "   - Key decisions and why\n"
    "   - Bugs and root causes\n"
    "   - Environment setup, commands, paths\n"
    "   - Unresolved issues or blockers\n"
    "   - Deferred work with context\n"
    "3. Write to <auto-dir>/session-summary-<first-8-chars-of-session-id>.md\n"
    "4. If deferred items, ALSO append to <project-dir>/memory/MEMORY.md under '## Deferred'.\n\n"
    "Be SPECIFIC — include file paths, function names, error messages, exact commands."
)

_AUTO_MEMORY_HOOKS = {
    "Stop": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 ~/.claude/cockpit-hooks/check_enabled.py",
                    "timeout": 3,
                },
                {
                    "type": "agent",
                    "prompt": _STOP_AGENT_PROMPT,
                    "timeout": 60,
                },
            ]
        }
    ],
    "PreCompact": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": "python3 ~/.claude/cockpit-hooks/check_enabled.py",
                    "timeout": 3,
                },
                {
                    "type": "agent",
                    "prompt": _PRECOMPACT_AGENT_PROMPT,
                    "timeout": 120,
                },
            ]
        }
    ],
}

CLAUDE_SETTINGS_FILE = CLAUDE_DIR / "settings.json"


def toggle_auto_memory() -> tuple[bool, str]:
    """Toggle auto-memory setting. Returns (new_state, error_msg). Empty error on success."""
    settings = get_settings()
    new_state = not settings.get("auto_memory", False)
    settings["auto_memory"] = new_state
    try:
        _atomic_write(SETTINGS_FILE, json.dumps(settings, indent=2) + "\n")
    except OSError as e:
        _log_warn(f"write cockpit settings: {e}")
        return not new_state, f"Failed to write settings: {e}"

    # Clear throttle file when enabling so first run fires immediately
    if new_state:
        throttle_file = CLAUDE_DIR / "cockpit-hooks" / ".last_run"
        try:
            throttle_file.unlink(missing_ok=True)
        except OSError as e:
            _log_warn(f"clear throttle file: {e}")

    # Add or remove hooks from Claude Code's settings.json
    try:
        claude_settings: dict = {}
        if CLAUDE_SETTINGS_FILE.exists():
            claude_settings = json.loads(CLAUDE_SETTINGS_FILE.read_text())
        if not isinstance(claude_settings, dict):
            claude_settings = {}

        if new_state:
            claude_settings["hooks"] = _AUTO_MEMORY_HOOKS
        else:
            claude_settings.pop("hooks", None)

        _atomic_write(CLAUDE_SETTINGS_FILE, json.dumps(claude_settings, indent=2) + "\n")
    except (json.JSONDecodeError, OSError) as e:
        _log_warn(f"write claude settings: {e}")
        return not new_state, f"Failed to write Claude settings: {e}"

    return new_state, ""


def is_auto_memory_enabled() -> bool:
    """Quick check if auto-memory is on."""
    return get_settings().get("auto_memory", False)


# ---------- Export ----------

def get_all_messages(path: Path) -> list[ConversationMessage]:
    """Load ALL displayable messages from a session JSONL (no sliding window)."""
    if not path.exists():
        return []
    messages: list[ConversationMessage] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = _extract_message(obj)
                if msg is not None:
                    messages.append(msg)
    except OSError:
        pass
    return messages


def export_conversation(
    path: Path, session: SessionEntry,
) -> tuple[Path | None, str]:
    """Export a conversation as markdown to ~/Desktop.

    Returns (output_path, error_message). On success error is empty.
    """
    messages = get_all_messages(path)
    if not messages:
        return None, "No messages to export"
    parts: list[str] = []
    # Header
    title = session.custom_title or session.summary or session.first_prompt or "Untitled"
    parts.append(f"# {title}\n")
    parts.append(f"- **Project:** {session.project}")
    parts.append(f"- **Session:** {session.session_id}")
    if session.git_branch:
        parts.append(f"- **Branch:** {session.git_branch}")
    parts.append(f"- **Messages:** {len(messages)}")
    duration = format_duration(session.created, session.modified)
    if duration:
        parts.append(f"- **Duration:** {duration}")
    # Tool stats
    tool_stats = get_tool_stats(messages)
    if tool_stats:
        parts.append(f"- **Tools:** {format_tool_stats(tool_stats, top_n=6)}")
    parts.append(f"- **Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    parts.append("")
    parts.append("---\n")
    # Messages
    for msg in messages:
        ts = ""
        if msg.timestamp:
            try:
                dt = datetime.fromisoformat(msg.timestamp.replace("Z", "+00:00"))
                ts = dt.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                pass
        role_label = "You" if msg.role == "user" else "Claude"
        parts.append(f"### {role_label} {ts}\n")
        if msg.has_thinking:
            parts.append("*Thinking...*\n")
        if msg.text:
            parts.append(msg.text + "\n")
        if msg.tool_names:
            tools = ", ".join(msg.tool_names)
            parts.append(f"*Tools: {tools}*\n")
    content = "\n".join(parts)
    short_id = session.session_id[:8]
    out_path = EXPORT_DIR / f"cockpit-export-{short_id}.md"
    try:
        _atomic_write(out_path, content)
        return out_path, ""
    except OSError as e:
        return None, str(e)


# ---------- Memory/Plans Write ----------

def _save_file_with_mtime_check(
    path: Path, content: str, expected_mtime: float,
    allowed_parents: list[Path] | None = None,
) -> tuple[bool, str]:
    """Write content to a file with optimistic concurrency.

    Checks mtime before writing to detect external changes.
    Optionally validates path is under one of allowed_parents.
    Returns (success, error_message).
    """
    if allowed_parents:
        resolved = path.resolve()
        if not any(_is_safe_child(resolved, p) for p in allowed_parents):
            return False, "Path not in allowed directory"
    try:
        current_mtime = path.stat().st_mtime
    except OSError:
        return False, "File not found"
    if abs(current_mtime - expected_mtime) > 0.01:
        return False, "File changed externally since last read"
    try:
        _atomic_write(path, content)
        return True, ""
    except OSError as e:
        return False, str(e)


def save_memory_file(
    path: Path, content: str, expected_mtime: float
) -> tuple[bool, str]:
    """Write content to a memory file with optimistic concurrency."""
    return _save_file_with_mtime_check(path, content, expected_mtime,
                                        allowed_parents=[PROJECTS_DIR])


def save_plan_file(
    path: Path, content: str, expected_mtime: float
) -> tuple[bool, str]:
    """Write content to a plan file with optimistic concurrency."""
    return _save_file_with_mtime_check(path, content, expected_mtime,
                                        allowed_parents=[PLANS_DIR])


def rename_plan(old_path: Path, new_name: str) -> tuple[bool, str]:
    """Rename a plan file with sanitization.

    Returns (success, error_message).
    """
    new_name = new_name.strip()
    if not new_name:
        return False, "Name cannot be empty"
    if "/" in new_name or "\\" in new_name:
        return False, "Name cannot contain path separators"
    if not new_name.endswith(".md"):
        new_name += ".md"
    new_path = old_path.parent / new_name
    if new_path == old_path:
        return True, ""
    if new_path.exists():
        return False, f"Plan '{new_name}' already exists"
    try:
        old_path.rename(new_path)
        return True, ""
    except OSError as e:
        return False, str(e)


# ---------- Helpers ----------

def format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}K"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}M"


def format_number(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def time_ago(timestamp: float) -> str:
    delta = time.time() - timestamp
    if delta < 0:
        return "just now"
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    return f"{int(delta / 86400)}d ago"


def format_duration(created: str, modified: str) -> str:
    """Compute human-readable duration between two ISO timestamps.

    Returns e.g. "45m", "2h 15m", "3d 2h". Empty string on bad input.
    """
    if not created or not modified:
        return ""
    try:
        t0 = datetime.fromisoformat(created.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(modified.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    delta = t1 - t0
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        return ""
    if total_seconds < 60:
        return "<1m"
    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24
    if days > 0:
        rem_hours = hours % 24
        return f"{days}d {rem_hours}h" if rem_hours else f"{days}d"
    if hours > 0:
        rem_minutes = minutes % 60
        return f"{hours}h {rem_minutes}m" if rem_minutes else f"{hours}h"
    return f"{minutes}m"
