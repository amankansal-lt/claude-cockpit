"""Claude Cockpit — Textual TUI application."""

from __future__ import annotations

import re
import sys
import threading
from datetime import datetime
from pathlib import Path

from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    ListItem,
    ListView,
    Markdown,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
    Tree,
)

from cockpit import data


SPARKLINE_CHARS = " ▁▂▃▄▅▆▇█"


def _log_warn(msg: str) -> None:
    """Log warning to stderr for cockpit diagnostics."""
    print(f"cockpit-warn: {msg}", file=sys.stderr)


def _sanitize_applescript_str(s: str) -> str:
    """Remove unsafe characters for AppleScript string interpolation."""
    return re.sub(r'[^a-zA-Z0-9_\-/. ]', '', s)


def sparkline(values: list[int], width: int = 30) -> str:
    """Render a sparkline string from integer values."""
    if not values:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn if mx != mn else 1
    step = max(1, len(values) // width)
    sampled = values[::step][:width]
    return "".join(
        SPARKLINE_CHARS[int((v - mn) / rng * (len(SPARKLINE_CHARS) - 1))]
        for v in sampled
    )


def gauge_bar(percent: int, width: int = 20) -> str:
    """Render a gauge bar like ██████░░░░."""
    filled = int(width * percent / 100)
    empty = width - filled
    if percent >= 80:
        color = "red"
    elif percent >= 50:
        color = "yellow"
    else:
        color = "green"
    return f"[{color}]{'█' * filled}{'░' * empty}[/{color}] {percent}%"


# ============================================================
# Help Screen
# ============================================================

HELP_TEXT = """\
[bold]Claude Cockpit[/bold] — X-ray vision for your Claude Code brain

[bold cyan]Navigation[/bold cyan]
  [bold]m[/bold]  Memory tab        [bold]t[/bold]  Tasks tab
  [bold]p[/bold]  Plans tab         [bold]s[/bold]  Stats tab
  [bold]h[/bold]  History tab       [bold]c[/bold]  Conversations tab
  [bold]\u2190 \u2192[/bold]  Previous / Next tab
  [bold]/[/bold]  Focus search

[bold cyan]Actions[/bold cyan]
  [bold]r[/bold]  Refresh all data from disk
  [bold]a[/bold]  Toggle auto-memory (real-time context capture)
  [bold]Esc[/bold]  Unfocus search input / cancel edit
  [bold]q[/bold]  Quit cockpit (Claude keeps running)
  [bold]?[/bold]  Toggle this help screen

[bold cyan]Memory Tab[/bold cyan]
  [bold]e[/bold]  Edit selected memory file
  [bold]Ctrl+S[/bold]  Save edited file
  [bold]Esc[/bold]  Cancel editing

[bold cyan]Plans Tab[/bold cyan]
  [bold]e[/bold]  Edit selected plan
  [bold]Ctrl+S[/bold]  Save edited plan
  [bold]F2[/bold]  Rename selected plan
  [bold]f[/bold]  Toggle favorite (pin plan)
  [bold]Esc[/bold]  Cancel editing

[bold cyan]Tasks Tab[/bold cyan]
  [bold]/[/bold]  Filter tasks by subject/description
  [bold]x[/bold]  Mark selected task as completed
  [bold]d[/bold]  Delete selected task
  Click session header to navigate to that conversation

[bold cyan]Conversations Tab[/bold cyan]
  [bold]/[/bold]  Search in conversation
  [bold]f[/bold]  Toggle favorite (pin session)
  [bold]x[/bold]  Export conversation as markdown to ~/Desktop
  [bold]F2[/bold]  Rename selected session
  [bold]t[/bold]  Toggle timeline view (sessions + auto-memory)

[bold cyan]What this shows[/bold cyan]
  [bold]Memory[/bold]   All memory files (editable with [bold]e[/bold])
  [bold]Tasks[/bold]    Active/pending/done from recent sessions
  [bold]Plans[/bold]    All plan files (renamable with [bold]F2[/bold])
  [bold]Conversations[/bold]  Full transcripts (even after compaction)
  [bold]Stats[/bold]    Usage metrics, model breakdown, sparklines
  [bold]History[/bold]  Searchable command history

[dim]Press Esc or ? to close this screen[/dim]
"""


class HelpScreen(ModalScreen):
    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Center():
            with Vertical(id="help-modal"):
                yield Static(HELP_TEXT, id="help-content")

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-modal {
        width: 60;
        height: auto;
        max-height: 80%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #help-content {
        height: auto;
    }
    """


# ============================================================
# Memory Tab
# ============================================================

class MemoryTab(TabPane):
    """Memory explorer with full-text search, debounce, and inline editing."""

    BINDINGS = [
        Binding("e", "toggle_edit", "Edit", show=True),
        Binding("ctrl+s", "save_memory", "Save", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("Memory", id="tab-memory")
        self._memory_files: list[data.MemoryFile] = []
        self._selected_file: data.MemoryFile | None = None
        self._search_timer: Timer | None = None
        self._editing: bool = False
        self._edit_mtime: float = 0.0

    def compose(self) -> ComposeResult:
        with Horizontal(id="memory-container"):
            with Vertical(id="memory-sidebar"):
                with Vertical(id="memory-search"):
                    yield Input(
                        placeholder="Search memory... (/ to focus, Esc to unfocus)",
                        id="memory-search-input",
                    )
                with VerticalScroll(id="memory-tree-container"):
                    yield Tree("Memory", id="memory-tree")
            with Vertical(id="memory-preview"):
                yield Static("Select a file or search to preview", id="memory-preview-title")
                yield VerticalScroll(Markdown("", id="memory-preview-content"))
                yield VerticalScroll(id="search-results-container", classes="hidden")

    def on_mount(self) -> None:
        self._load_memory()

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()

    def _load_memory(self) -> None:
        self._memory_files = data.get_memory_files()
        tree: Tree = self.query_one("#memory-tree", Tree)
        tree.clear()
        tree.root.expand()
        by_project: dict[str, list[data.MemoryFile]] = {}
        for mf in self._memory_files:
            by_project.setdefault(mf.project, []).append(mf)
        summary = data.memory_summary(self._memory_files)
        tree.root.set_label(
            f"Memory ({summary['files']} files, {data.format_size(summary['size'])})"
        )
        for proj, files in sorted(by_project.items()):
            # Separate auto-generated from manual files
            manual = [f for f in files if "/auto/" not in str(f.path)]
            auto = [f for f in files if "/auto/" in str(f.path)]

            proj_node = tree.root.add(
                f"📁 {escape(proj)} ({len(files)})", expand=True
            )
            for mf in manual:
                icon = "📄" if mf.name == "MEMORY.md" else "📝"
                proj_node.add_leaf(
                    f"{icon} {escape(mf.name)} ({data.format_size(mf.size)})",
                    data=mf,
                )
            if auto:
                auto_node = proj_node.add(
                    f"🤖 Auto-Generated ({len(auto)})", expand=False
                )
                for mf in auto:
                    auto_node.add_leaf(
                        f"🤖 {escape(mf.name)} ({data.format_size(mf.size)})",
                        data=mf,
                    )

    def on_tree_node_selected(self, event: Tree.NodeSelected) -> None:
        node = event.node
        if node.data and isinstance(node.data, data.MemoryFile):
            self._selected_file = node.data
            title = self.query_one("#memory-preview-title", Static)
            title.update(f" {node.data.display_name} ({node.data.lines} lines)")
            md = self.query_one("#memory-preview-content", Markdown)
            md.update(node.data.content)
            self.query_one("#memory-preview-content").parent.remove_class("hidden")
            self.query_one("#search-results-container").add_class("hidden")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "memory-search-input":
            return
        # Debounce: cancel previous timer, start new 200ms timer
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value
        self._search_timer = self.set_timer(
            0.2, lambda: self._do_search(query)
        )

    def _do_search(self, query: str) -> None:
        results_container = self.query_one("#search-results-container")
        preview_scroll = self.query_one("#memory-preview-content").parent

        if not query.strip():
            results_container.add_class("hidden")
            preview_scroll.remove_class("hidden")
            return

        results = data.search_memory(query, self._memory_files, context=1)
        results_container.remove_class("hidden")
        preview_scroll.add_class("hidden")

        title = self.query_one("#memory-preview-title", Static)
        title.update(f" Search: '{query}' — {len(results)} results")

        results_container.remove_children()
        if not results:
            results_container.mount(Static("[dim]No results found.[/dim]"))
            return
        for r in results[:50]:
            parts = [
                f"[bold cyan]{escape(r.file.display_name)}[/bold cyan]"
                f":[yellow]{r.line_num}[/yellow]"
            ]
            if r.context_before:
                parts.append(f"[dim]{escape(r.context_before)}[/dim]")
            parts.append(f"  {escape(r.line)}")
            if r.context_after:
                parts.append(f"[dim]{escape(r.context_after)}[/dim]")
            results_container.mount(
                Static("\n".join(parts), classes="search-result")
            )

    def action_toggle_edit(self) -> None:
        if self._editing:
            self._cancel_edit()
            return
        if self._selected_file is None:
            self.app.notify("Select a file first", severity="warning", timeout=2)
            return
        try:
            self._edit_mtime = self._selected_file.path.stat().st_mtime
        except OSError:
            self.app.notify("Cannot read file", severity="error", timeout=2)
            return
        self._editing = True
        title = self.query_one("#memory-preview-title", Static)
        title.update(
            f" Editing: {self._selected_file.name}  "
            "[dim](Ctrl+S save, Esc cancel)[/dim]"
        )
        md = self.query_one("#memory-preview-content", Markdown)
        md.display = False
        preview = self.query_one("#memory-preview")
        self.query_one("#search-results-container").add_class("hidden")
        ta = TextArea(
            self._selected_file.content,
            language="markdown",
            id="memory-edit-area",
        )
        preview.mount(ta)
        ta.focus()

    def action_save_memory(self) -> None:
        if not self._editing or self._selected_file is None:
            return
        ta = self.query_one("#memory-edit-area", TextArea)
        content = ta.text
        ok, err = data.save_memory_file(
            self._selected_file.path, content, self._edit_mtime
        )
        if ok:
            self._selected_file._content = content
            self._exit_edit_mode(content)
            self.app.notify(f"Saved {self._selected_file.name}", timeout=2)
        else:
            self.app.notify(f"Save failed: {err}", severity="error", timeout=4)

    def _cancel_edit(self) -> None:
        if not self._editing:
            return
        content = self._selected_file.content if self._selected_file else ""
        self._exit_edit_mode(content)

    def _exit_edit_mode(self, preview_content: str) -> None:
        self._editing = False
        for ta in self.query("#memory-edit-area"):
            ta.remove()
        md = self.query_one("#memory-preview-content", Markdown)
        md.display = True
        md.update(preview_content)
        if self._selected_file:
            title = self.query_one("#memory-preview-title", Static)
            title.update(
                f" {self._selected_file.display_name} ({self._selected_file.lines} lines)"
            )

    def on_key(self, event) -> None:
        if event.key == "escape" and self._editing:
            self._cancel_edit()
            event.stop()


# ============================================================
# Tasks Tab
# ============================================================

def _progress_bar(done: int, total: int, width: int = 20) -> str:
    """Render a task progress bar: ▓▓▓▓▓▓▓▓░░░░ 3/5."""
    if total == 0:
        return ""
    filled = int(width * done / total)
    empty = width - filled
    return f"[green]{'▓' * filled}[/green][dim]{'░' * empty}[/dim] {done}/{total}"


class TasksTab(TabPane):
    """Live task board — agentic workflow view with session context."""

    BINDINGS = [
        Binding("down", "cursor_down", "Down", show=False, priority=True),
        Binding("up", "cursor_up", "Up", show=False, priority=True),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
        Binding("enter", "select_item", "Open", show=False),
        Binding("x", "complete_task", "Complete", show=False),
        Binding("d", "delete_task", "Delete", show=False),
        Binding("slash", "focus_task_search", "Search", show=False),
        Binding("escape", "unfocus_search", "Unfocus", show=False),
    ]

    def __init__(self) -> None:
        super().__init__("Tasks", id="tab-tasks")
        self._all_tasks: list[data.Task] = []
        self._filtered_tasks: list[data.Task] = []
        self._search_timer: Timer | None = None
        self._session_lookup: dict[str, data.SessionEntry] = {}
        self._dashboard_sessions: list[dict] = []
        self._all_dashboard_sessions: list[dict] = []
        self._session_by_name: dict[str, data.SessionEntry] = {}
        self._session_tty: dict[str, str] = {}  # session_id -> TTY device path
        self._task_by_widget: dict[str, data.Task] = {}  # widget name -> Task
        self._navigable: list[Static] = []  # navigable items in order
        self._selected_idx: int = -1

    def compose(self) -> ComposeResult:
        with Vertical(id="tasks-outer"):
            with Vertical(id="task-search"):
                yield Input(
                    placeholder="Filter tasks... (/ to search)",
                    id="task-search-input",
                )
            yield VerticalScroll(id="tasks-container")

    def on_mount(self) -> None:
        self._load_tasks()
        # Focus the container so arrow keys work immediately (not the Input)
        self.set_timer(0.1, self._focus_container)

    def _focus_container(self) -> None:
        try:
            container = self.query_one("#tasks-container")
            container.can_focus = True
            container.focus()
        except Exception as e:
            _log_warn(f"focus tasks container: {e}")

    def action_focus_task_search(self) -> None:
        try:
            self.query_one("#task-search-input", Input).focus()
        except Exception as e:
            _log_warn(f"focus task search: {e}")

    def action_unfocus_search(self) -> None:
        self._focus_container()

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()

    def _load_tasks(self) -> None:
        try:
            self._all_tasks = data.get_all_recent_tasks(limit=10, max_age_hours=720)
            all_sessions = data.get_all_sessions()
            self._session_lookup = data.build_session_lookup(all_sessions)
            self._dashboard_sessions = data.get_dashboard_sessions(all_sessions)
            self._all_dashboard_sessions = list(self._dashboard_sessions)
            self._filtered_tasks = self._all_tasks
            self._render_tasks()
        except Exception as exc:
            _log_warn(f"task load error: {exc}")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "task-search-input":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value.strip().lower()
        self._search_timer = self.set_timer(
            0.2, lambda: self._filter_tasks(query)
        )

    def _filter_tasks(self, query: str) -> None:
        if query:
            self._filtered_tasks = [
                t for t in self._all_tasks
                if query in t.subject.lower() or query in t.description.lower()
            ]
            self._dashboard_sessions = [
                e for e in self._all_dashboard_sessions
                if query in (e["session"].summary or "").lower()
                or query in (e["session"].first_prompt or "").lower()
                or query in (e["session"].project or "").lower()
            ]
        else:
            self._filtered_tasks = self._all_tasks
            self._dashboard_sessions = list(self._all_dashboard_sessions)
        self._render_tasks()

    def _render_tasks(self) -> None:
        container = self.query_one("#tasks-container")
        container.remove_children()
        self._session_by_name.clear()
        self._session_tty.clear()
        self._proc_by_session: dict[str, data.LiveProcess] = {}
        self._task_by_widget.clear()
        self._navigable.clear()
        self._selected_idx = -1

        self._render_sessions_section(container)
        self._render_tasks_section(container)

        # --- Empty state ---
        if not self._filtered_tasks and not self._dashboard_sessions:
            container.mount(Static(
                "[dim]No sessions or tasks.\n\n"
                "Sessions appear here when Claude Code is running.\n"
                "Tasks appear when Claude creates task lists.[/dim]"
            ))

        self._render_deferred_section(container)
        self._selected_idx = -1

    def _render_sessions_section(self, container) -> None:
        """Render LIVE SESSIONS section into the tasks container."""
        if not self._dashboard_sessions:
            return
        container.mount(Static(
            f"[bold]SESSIONS[/bold]  [dim]{len(self._dashboard_sessions)} live[/dim]",
            classes="task-group-title",
        ))

        for entry in self._dashboard_sessions:
            s: data.SessionEntry = entry["session"]
            age_label: str = entry["age_label"]
            proc: data.LiveProcess | None = entry.get("process")
            tty: str = entry.get("tty", "")

            sid_short = s.session_id[:8]
            # Priority: iTerm tab name → custom_title → summary → first_prompt → project
            tab_label = ""
            if proc and proc.tab_name:
                # Strip §session_id stamp and clean up
                tab_label = data._SESSION_ID_IN_TAB_RE.sub("", proc.tab_name).strip()
                # Ignore generic iTerm default names
                if tab_label.lower() in ("bash", "zsh", "login", ""):
                    tab_label = ""
            base_name = tab_label or s.custom_title or data.strip_xml_tags(s.summary) or ""
            if not base_name or base_name.lower() in ("claude code", "claude"):
                prompt = data.strip_xml_tags(s.first_prompt or "").replace("\n", " ").strip()
                base_name = prompt[:45] if prompt else s.project
            title = f"{base_name[:42]}  [dim cyan]#{sid_short}[/dim cyan]"

            if proc and proc.cpu_percent > 5:
                status = "[bold green]● ACTIVE[/bold green]"
                cpu_info = f"  [yellow]{proc.cpu_percent:.0f}% CPU[/yellow]"
            elif proc:
                status = "[dim green]○ idle[/dim green]"
                cpu_info = ""
            else:
                status = "[green]● LIVE[/green]"
                cpu_info = ""

            details = []
            if proc:
                details.append(proc.uptime)
                if proc.children:
                    details.append(" + ".join(proc.children))
            details.append(escape(s.project))
            details.append(escape(age_label))

            summary_line = ""
            prompt = data.strip_xml_tags(s.first_prompt or "")
            prompt_clean = prompt[:70].replace("\n", " ").strip()
            if prompt_clean and prompt_clean not in (base_name, base_name[:45]):
                summary_line = f"\n       [italic dim]\"{escape(prompt_clean)}\"[/italic dim]"

            self._session_by_name[s.session_id] = s
            if proc:
                self._proc_by_session[s.session_id] = proc
            if tty:
                self._session_tty[s.session_id] = tty

            card = Static(
                f"  {status}  [bold]{title}[/bold]{cpu_info}\n"
                f"       [dim]{' · '.join(details)}[/dim]{summary_line}",
                classes="session-card nav-item",
                name=s.session_id,
            )
            container.mount(card)
            self._navigable.append(card)

    def _render_tasks_section(self, container) -> None:
        """Render TASKS section grouped by session."""
        tasks = self._filtered_tasks
        if not tasks:
            return

        active_count = sum(1 for t in tasks if t.status == "in_progress")
        pending_count = sum(1 for t in tasks if t.status == "pending")
        done_count = sum(1 for t in tasks if t.status == "completed")
        counts = []
        if active_count:
            counts.append(f"[yellow]{active_count} active[/yellow]")
        if pending_count:
            counts.append(f"{pending_count} pending")
        if done_count:
            counts.append(f"[green]{done_count} done[/green]")
        count_str = f"  [dim]{' · '.join(counts)}[/dim]" if counts else ""

        container.mount(Static(""))
        container.mount(Static(
            f"[bold]TASKS[/bold]{count_str}",
            classes="task-group-title",
        ))

        groups: dict[str, list[data.Task]] = {}
        for t in tasks:
            groups.setdefault(t.session_dir, []).append(t)

        for session_dir, group_tasks in groups.items():
            session = self._session_lookup.get(session_dir)
            total = len(group_tasks)
            g_done = sum(1 for t in group_tasks if t.status == "completed")

            if session:
                # Priority: iTerm tab name → custom_title → summary → first_prompt
                tab_label = ""
                proc = self._proc_by_session.get(session.session_id)
                if proc and proc.tab_name:
                    tab_label = data._SESSION_ID_IN_TAB_RE.sub("", proc.tab_name).strip()
                    if tab_label.lower() in ("bash", "zsh", "login", ""):
                        tab_label = ""
                summary_text = tab_label or session.custom_title or data.strip_xml_tags(session.summary) or data.strip_xml_tags(session.first_prompt) or "Untitled"
                summary_text = summary_text[:45]
                progress = _progress_bar(g_done, total, width=15)
                self._session_by_name[session.session_id] = session
                header = Static(
                    f"  [cyan]{escape(summary_text)}[/cyan]  {progress}",
                    classes="task-session-header nav-item",
                    name=session.session_id,
                )
                container.mount(header)
                self._navigable.append(header)
            elif session_dir:
                progress = _progress_bar(g_done, total, width=15)
                header = Static(
                    f"  [dim]{escape(session_dir[:16])}...[/dim]  {progress}",
                    classes="task-session-header",
                )
                container.mount(header)

            for t in sorted(group_tasks, key=lambda x: {"in_progress": 0, "pending": 1}.get(x.status, 2)):
                wname = f"task-{t.session_dir}-{t.id}"
                if t.status == "in_progress":
                    w = Static(
                        f"    [bold yellow]▶ #{t.id}[/bold yellow] [yellow]{escape(t.subject)}[/yellow]"
                        + (f"\n      [italic dim]{escape(t.active_form)}[/italic dim]" if t.active_form else ""),
                        classes="task-card task-active nav-item",
                        name=wname,
                    )
                elif t.status == "completed":
                    w = Static(
                        f"    [green dim]✓ #{t.id} {escape(t.subject)}[/green dim]",
                        classes="task-card nav-item",
                        name=wname,
                    )
                else:
                    blocked = f"  [red]⊘ #{', #'.join(t.blocked_by)}[/red]" if t.blocked_by else ""
                    w = Static(
                        f"    [dim]○ #{t.id}[/dim] {escape(t.subject)}{blocked}",
                        classes="task-card nav-item",
                        name=wname,
                    )
                self._task_by_widget[wname] = t
                container.mount(w)
                self._navigable.append(w)

    def _render_deferred_section(self, container) -> None:
        """Render DEFERRED items section."""
        deferred = data.get_deferred_items()
        if not deferred:
            return
        container.mount(Static(""))
        container.mount(Static(
            f"[bold]DEFERRED[/bold]  [dim]{len(deferred)} items[/dim]",
            classes="task-group-title",
        ))
        for item in deferred[:10]:
            reason = f" [dim]— {escape(item.reason)}[/dim]" if item.reason else ""
            date = f" [dim]({item.date})[/dim]" if item.date else ""
            w = Static(
                f"    [magenta]⏳ {escape(item.task)}[/magenta]{reason}{date}",
                classes="task-card nav-item",
            )
            container.mount(w)
            self._navigable.append(w)

    def _highlight_selected(self) -> None:
        """Apply highlight to the currently selected navigable item."""
        for i, w in enumerate(self._navigable):
            if i == self._selected_idx:
                w.add_class("nav-selected")
            else:
                w.remove_class("nav-selected")
        # Scroll selected item into view
        if 0 <= self._selected_idx < len(self._navigable):
            self._navigable[self._selected_idx].scroll_visible()

    def _is_search_focused(self) -> bool:
        """Check if the search input currently has focus."""
        try:
            inp = self.query_one("#task-search-input", Input)
            return inp.has_focus
        except Exception as e:
            _log_warn(f"check search focus: {e}")
            return False

    def action_cursor_down(self) -> None:
        if self._is_search_focused():
            self._focus_container()
        if not self._navigable:
            return
        if self._selected_idx < 0:
            self._selected_idx = 0
        else:
            self._selected_idx = min(self._selected_idx + 1, len(self._navigable) - 1)
        self._highlight_selected()

    def action_cursor_up(self) -> None:
        if self._is_search_focused():
            self._focus_container()
        if not self._navigable:
            return
        if self._selected_idx < 0:
            self._selected_idx = 0
        else:
            self._selected_idx = max(self._selected_idx - 1, 0)
        self._highlight_selected()

    def _get_selected_task(self) -> data.Task | None:
        """Get the Task for the currently selected widget, if any."""
        if not (0 <= self._selected_idx < len(self._navigable)):
            return None
        name = self._navigable[self._selected_idx].name or ""
        return self._task_by_widget.get(name)

    def _navigate_task_to_iterm(self, name: str) -> None:
        """Navigate to the iTerm session for a task widget."""
        task = self._task_by_widget.get(name)
        if task:
            session = self._session_lookup.get(task.session_dir)
            if session:
                self._open_session_in_iterm(session)

    def action_select_item(self) -> None:
        """Open the selected item — navigate to iTerm tab if it's a session."""
        if not (0 <= self._selected_idx < len(self._navigable)):
            return
        widget = self._navigable[self._selected_idx]
        name = widget.name or ""
        if name in self._session_by_name:
            self._open_session_in_iterm(self._session_by_name[name])
        elif name.startswith("task-"):
            self._navigate_task_to_iterm(name)

    def action_complete_task(self) -> None:
        """Mark the selected task as completed."""
        task = self._get_selected_task()
        if task is None:
            self.app.notify("No task selected", severity="warning")
            return
        if task.status == "completed":
            self.app.notify("Already completed", severity="information")
            return
        ok, err = data.update_task_status(task, "completed")
        if ok:
            self.app.notify(f"Task #{task.id} completed", severity="information")
            self._load_tasks()
        else:
            self.app.notify(f"Failed: {err}", severity="error")

    def action_delete_task(self) -> None:
        """Delete the selected task."""
        task = self._get_selected_task()
        if task is None:
            self.app.notify("No task selected", severity="warning")
            return
        ok, err = data.delete_task(task)
        if ok:
            self.app.notify(f"Task #{task.id} deleted", severity="information")
            self._load_tasks()
        else:
            self.app.notify(f"Failed: {err}", severity="error")

    def on_click(self, event) -> None:
        """Focus the iTerm tab for this session when clicking. Also update selection."""
        widget = event.widget
        while widget is not None:
            if isinstance(widget, Static) and widget in self._navigable:
                idx = self._navigable.index(widget)
                self._selected_idx = idx
                self._highlight_selected()
                name = widget.name or ""
                if name in self._session_by_name:
                    self._open_session_in_iterm(self._session_by_name[name])
                elif name.startswith("task-"):
                    self._navigate_task_to_iterm(name)
                return
            widget = widget.parent

    def _open_session_in_iterm(self, session: data.SessionEntry) -> None:
        """Focus the iTerm tab/pane running this Claude session using TTY matching."""
        import subprocess
        tty = self._session_tty.get(session.session_id, "")
        sid_short = session.session_id[:8]

        if tty:
            # Strategy 1: Match by TTY (deterministic, works with split panes)
            script = f'''
            tell application "iTerm"
                activate
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if tty of s is "{tty}" then
                                tell w
                                    select t
                                end tell
                                select s
                                set index of w to 1
                                return "ok"
                            end if
                        end repeat
                    end repeat
                end repeat
                return "not_found"
            end tell
            '''
        else:
            # Fallback: match by §session_id stamp in tab name, then project name
            stamp = f"§{sid_short}"
            project = _sanitize_applescript_str(session.project)
            script = f'''
            tell application "iTerm"
                activate
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if name of s contains "{stamp}" then
                                tell w
                                    select t
                                end tell
                                select s
                                set index of w to 1
                                return "ok"
                            end if
                        end repeat
                    end repeat
                end repeat
                -- second pass: project name fallback
                repeat with w in windows
                    repeat with t in tabs of w
                        repeat with s in sessions of t
                            if name of s contains "{project}" then
                                tell w
                                    select t
                                end tell
                                select s
                                set index of w to 1
                                return "ok"
                            end if
                        end repeat
                    end repeat
                end repeat
                return "not_found"
            end tell
            '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=5,
            )
            output = result.stdout.strip()
            if output == "not_found":
                self.app.notify(
                    f"Session #{sid_short} not found in iTerm",
                    severity="warning", timeout=3,
                )
            elif result.returncode != 0:
                self.app.notify(
                    f"iTerm error: {result.stderr.strip()[:50]}",
                    severity="error", timeout=3,
                )
        except subprocess.TimeoutExpired:
            self.app.notify("iTerm timed out", severity="error", timeout=3)
        except OSError:
            self.app.notify("Failed to run osascript", severity="error", timeout=3)


# ============================================================
# Plans Tab
# ============================================================

class PlansTab(TabPane):
    """Browse plan files with rename and edit support."""

    BINDINGS = [
        Binding("f2", "rename_plan", "Rename", show=True),
        Binding("e", "toggle_edit", "Edit", show=True),
        Binding("ctrl+s", "save_plan", "Save", show=False),
        Binding("f", "toggle_favorite_plan", "Favorite", show=True),
    ]

    def __init__(self) -> None:
        super().__init__("Plans", id="tab-plans")
        self._plans: list[data.Plan] = []
        self._selected_plan: data.Plan | None = None
        self._editing: bool = False
        self._edit_mtime: float = 0.0
        self._renaming: bool = False
        self._rename_plan: data.Plan | None = None
        self._pinned_plans: set[str] = set()

    def compose(self) -> ComposeResult:
        with Horizontal(id="plans-container"):
            yield ListView(id="plans-list")
            with Vertical(id="plans-preview-container"):
                yield Static("", id="plans-preview-title")
                yield VerticalScroll(Markdown("Select a plan to preview", id="plans-preview"))

    def on_mount(self) -> None:
        self._load_plans()

    def _load_plans(self) -> None:
        self._pinned_plans = data.get_pinned_plans()
        all_plans = data.get_plans()
        # Sort pinned first, then by mtime (already sorted by mtime from get_plans)
        pinned = [p for p in all_plans if p.name in self._pinned_plans]
        regular = [p for p in all_plans if p.name not in self._pinned_plans]
        self._plans = pinned + regular
        plan_list = self.query_one("#plans-list", ListView)
        plan_list.clear()
        for p in self._plans:
            is_pinned = p.name in self._pinned_plans
            pin_prefix = "[yellow]★[/yellow] " if is_pinned else ""
            plan_list.append(
                ListItem(
                    Static(
                        f"{pin_prefix}[bold]{escape(p.name)}[/bold]\n"
                        f"[dim]{p.lines} lines · {data.format_size(p.size)} · "
                        f"{data.time_ago(p.mtime)}[/dim]"
                    ),
                    name=p.name,
                )
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if self._editing:
            return  # Don't switch files while editing
        idx = event.list_view.index
        if idx is not None and idx < len(self._plans):
            self._selected_plan = self._plans[idx]
            md = self.query_one("#plans-preview", Markdown)
            md.update(self._selected_plan.content)
            title = self.query_one("#plans-preview-title", Static)
            title.update(
                f" {escape(self._selected_plan.name)} · "
                f"{self._selected_plan.lines} lines"
            )

    def action_toggle_edit(self) -> None:
        if self._editing:
            self._cancel_edit()
            return
        if self._selected_plan is None:
            self.app.notify("Select a plan first", severity="warning", timeout=2)
            return
        try:
            self._edit_mtime = self._selected_plan.path.stat().st_mtime
        except OSError:
            self.app.notify("Cannot read file", severity="error", timeout=2)
            return
        self._editing = True
        title = self.query_one("#plans-preview-title", Static)
        title.update(
            f" Editing: {self._selected_plan.name}  "
            "[dim](Ctrl+S save, Esc cancel)[/dim]"
        )
        md = self.query_one("#plans-preview", Markdown)
        md.display = False
        container = self.query_one("#plans-preview-container")
        ta = TextArea(
            self._selected_plan.content,
            language="markdown",
            id="plans-edit-area",
        )
        container.mount(ta)
        ta.focus()

    def action_save_plan(self) -> None:
        if not self._editing or self._selected_plan is None:
            return
        ta = self.query_one("#plans-edit-area", TextArea)
        content = ta.text
        ok, err = data.save_plan_file(
            self._selected_plan.path, content, self._edit_mtime
        )
        if ok:
            self._exit_edit_mode(content)
            self._load_plans()
            self.app.notify(f"Saved {self._selected_plan.name}", timeout=2)
        else:
            self.app.notify(f"Save failed: {err}", severity="error", timeout=4)

    def _cancel_edit(self) -> None:
        if not self._editing:
            return
        content = self._selected_plan.content if self._selected_plan else ""
        self._exit_edit_mode(content)

    def _exit_edit_mode(self, preview_content: str) -> None:
        self._editing = False
        for ta in self.query("#plans-edit-area"):
            ta.remove()
        md = self.query_one("#plans-preview", Markdown)
        md.display = True
        md.update(preview_content)
        if self._selected_plan:
            title = self.query_one("#plans-preview-title", Static)
            title.update(
                f" {escape(self._selected_plan.name)} · "
                f"{self._selected_plan.lines} lines"
            )

    def on_key(self, event) -> None:
        if event.key == "escape":
            if self._renaming:
                self._cancel_rename()
                event.stop()
            elif self._editing:
                self._cancel_edit()
                event.stop()

    def action_rename_plan(self) -> None:
        if self._editing or self._renaming:
            return
        plan_list = self.query_one("#plans-list", ListView)
        idx = plan_list.index
        if idx is None or idx >= len(self._plans):
            self.app.notify("Select a plan first", severity="warning", timeout=2)
            return
        plan = self._plans[idx]
        self._rename_plan = plan
        self._renaming = True
        # Replace the title Static with an Input for inline rename
        title = self.query_one("#plans-preview-title", Static)
        title.display = False
        container = self.query_one("#plans-preview-container")
        rename_input = Input(
            value=plan.name,
            placeholder="New name...",
            id="plans-rename-input",
        )
        container.mount(rename_input, before=title)
        rename_input.focus()
        # Select all text so typing replaces
        rename_input.action_select_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "plans-rename-input":
            self._do_rename(event.value)

    def _do_rename(self, new_name: str) -> None:
        if not self._rename_plan:
            return
        ok, err = data.rename_plan(self._rename_plan.path, new_name)
        self._exit_rename_mode()
        if ok:
            self._load_plans()
            self.app.notify(f"Renamed to {new_name}", timeout=2)
        else:
            self.app.notify(f"Rename failed: {err}", severity="error", timeout=4)

    def _cancel_rename(self) -> None:
        self._exit_rename_mode()

    def _exit_rename_mode(self) -> None:
        self._renaming = False
        self._rename_plan = None
        for inp in self.query("#plans-rename-input"):
            inp.remove()
        title = self.query_one("#plans-preview-title", Static)
        title.display = True

    def action_toggle_favorite_plan(self) -> None:
        if self._editing or self._renaming:
            return
        plan_list = self.query_one("#plans-list", ListView)
        idx = plan_list.index
        if idx is None or idx >= len(self._plans):
            self.app.notify("Select a plan first", severity="warning", timeout=2)
            return
        plan = self._plans[idx]
        new_state, err = data.toggle_pin_plan(plan.name)
        if err:
            self.app.notify(f"Pin failed: {err}", severity="error", timeout=3)
            return
        self._pinned_plans = data.get_pinned_plans()
        icon = "★" if new_state else "☆"
        self.app.notify(f"{icon} {'Pinned' if new_state else 'Unpinned'} {plan.name}", timeout=2)
        self._load_plans()


# ============================================================
# Conversations Tab
# ============================================================

class ConversationsTab(TabPane):
    """Browse full conversation transcripts."""

    BINDINGS = [
        Binding("slash", "focus_conv_search", "Search"),
        Binding("f", "toggle_favorite", "Favorite", show=True),
        Binding("x", "export_conversation", "Export", show=True),
        Binding("f2", "rename_session", "Rename", show=True),
        Binding("t", "toggle_timeline", "Timeline", show=True),
    ]

    def __init__(self) -> None:
        super().__init__("Conversations", id="tab-conversations")
        self._sessions: list[data.SessionEntry] = []
        self._all_sessions: list[data.SessionEntry] = []
        self._selected: data.SessionEntry | None = None
        self._messages: list[data.ConversationMessage] = []
        self._offset: int = 0
        self._limit: int = 50
        self._has_more: bool = False
        self._search_timer: Timer | None = None
        self._pinned: set[str] = set()
        self._renaming: bool = False
        self._timeline_mode: bool = False
        self._timeline_project: str = ""
        self._session_by_name: dict[str, data.SessionEntry] = {}

    def compose(self) -> ComposeResult:
        with Horizontal(id="conv-container"):
            with Vertical(id="conv-sidebar"):
                with Vertical(id="conv-search"):
                    yield Input(
                        placeholder="Filter sessions...",
                        id="conv-search-input",
                    )
                yield VerticalScroll(id="conv-session-list")
            with Vertical(id="conv-preview"):
                yield Static(
                    "Select a session to view conversation",
                    id="conv-title",
                )
                with Vertical(id="conv-msg-search", classes="hidden"):
                    yield Input(
                        placeholder="Search in conversation... (Esc to close)",
                        id="conv-msg-search-input",
                    )
                yield VerticalScroll(id="conv-messages")

    def on_mount(self) -> None:
        self._load_sessions()

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()

    def _load_sessions(self) -> None:
        self._pinned = data.get_pinned()
        all_sessions = data.get_all_sessions()
        # Filter out metadata-only sessions (no summary AND no first prompt)
        self._all_sessions = [
            s for s in all_sessions
            if s.custom_title or s.summary or s.first_prompt
        ]
        self._sessions = self._all_sessions
        self._render_session_list()

    def _render_session_list(self) -> None:
        container = self.query_one("#conv-session-list")
        container.remove_children()
        self._session_by_name.clear()
        if not self._sessions:
            container.mount(Static("[dim]No sessions found.[/dim]"))
            return
        # Partition: pinned first, then regular
        pinned = [s for s in self._sessions if s.session_id in self._pinned]
        regular = [s for s in self._sessions if s.session_id not in self._pinned]
        ordered = pinned + regular
        for s in ordered[:100]:
            summary = s.custom_title or data.strip_xml_tags(s.summary) or data.strip_xml_tags(s.first_prompt) or "Untitled"
            summary = summary[:60]
            is_pinned = s.session_id in self._pinned
            pin_prefix = "[yellow]★[/yellow] " if is_pinned else ""
            ts = ""
            if s.modified:
                try:
                    dt = datetime.fromisoformat(s.modified.replace("Z", "+00:00"))
                    ts = dt.strftime("%b %d %H:%M")
                except (ValueError, TypeError):
                    ts = s.modified[:10]
            msg_info = f"{s.message_count} msgs · " if s.message_count > 0 else ""
            duration = data.format_duration(s.created, s.modified)
            dur_info = f"{duration} · " if duration else ""
            self._session_by_name[s.session_id] = s
            widget = Static(
                f"{pin_prefix}[bold]{escape(summary)}[/bold]\n"
                f"[dim]{escape(s.project)} · {msg_info}"
                f"{data.format_size(s.file_size)} · {dur_info}{ts}[/dim]",
                classes="session-item",
                name=s.session_id,
            )
            container.mount(widget)

    def on_click(self, event) -> None:
        widget = event.widget
        while widget is not None:
            if isinstance(widget, Static) and widget.name and widget.name in self._session_by_name:
                session = self._session_by_name[widget.name]
                self._select_session(session)
                return
            widget = widget.parent

    def _select_session(self, session: data.SessionEntry) -> None:
        self._selected = session
        self._offset = 0
        title = self.query_one("#conv-title", Static)
        summary = session.custom_title or data.strip_xml_tags(session.summary) or data.strip_xml_tags(session.first_prompt) or "Untitled"
        branch = f" [{escape(session.git_branch)}]" if session.git_branch else ""
        # Single-pass: get last N messages + total count
        self._messages, total = data.get_last_messages(
            session.full_path, limit=self._limit
        )
        self._has_more = total > self._limit
        self._offset = max(0, total - self._limit)
        # Tool stats from loaded messages
        tool_stats = data.get_tool_stats(self._messages)
        tools_info = f" · {data.format_tool_stats(tool_stats)}" if tool_stats else ""
        title.update(
            f" {escape(summary[:50])}{branch} — "
            f"[dim]{session.project} · {total} messages · "
            f"{data.format_size(session.file_size)}{tools_info}[/dim]"
        )
        if total == 0:
            container = self.query_one("#conv-messages")
            container.remove_children()
            container.mount(Static(
                "[dim]No conversation messages in this session.\n"
                "It may contain only metadata events.[/dim]"
            ))
            return
        self._render_messages()

    def _render_messages(self) -> None:
        container = self.query_one("#conv-messages")
        container.remove_children()
        if self._has_more:
            container.mount(
                Button("Load 50 more...", variant="default", classes="conv-load-more")
            )
        for msg in self._messages:
            self._mount_message(container, msg)

    def _mount_message(
        self, container, msg: data.ConversationMessage, highlight: str = ""
    ) -> None:
        parts: list[str] = []
        # Timestamp
        ts = ""
        if msg.timestamp:
            try:
                dt = datetime.fromisoformat(msg.timestamp.replace("Z", "+00:00"))
                ts = dt.strftime("%H:%M:%S")
            except (ValueError, TypeError):
                ts = ""
        if msg.role == "user":
            parts.append(f"[bold cyan]You[/bold cyan] [dim]{ts}[/dim]")
        else:
            parts.append(f"[bold green]Claude[/bold green] [dim]{ts}[/dim]")
        if msg.has_thinking:
            parts.append("[dim italic]Thinking...[/dim italic]")
        if msg.text:
            # Truncate very long messages for display
            text = msg.text if len(msg.text) <= 2000 else msg.text[:2000] + "\n[dim]... (truncated)[/dim]"
            escaped = escape(text)
            if highlight:
                pattern = re.compile(re.escape(escape(highlight)), re.IGNORECASE)
                escaped = pattern.sub(
                    lambda m: f"[bold reverse yellow]{m.group()}[/bold reverse yellow]",
                    escaped,
                )
            parts.append(escaped)
        if msg.tool_names:
            tools = ", ".join(msg.tool_names[:10])
            parts.append(f"[dim]Tools: {escape(tools)}[/dim]")
        css_class = "msg-user" if msg.role == "user" else "msg-assistant"
        container.mount(Static("\n".join(parts), classes=css_class))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("conv-load-more") and self._selected:
            self._offset = max(0, self._offset - self._limit)
            older_msgs, _, _ = data.get_session_messages(
                self._selected.full_path,
                offset=self._offset,
                limit=self._limit,
            )
            self._messages = older_msgs + self._messages
            self._has_more = self._offset > 0
            self._render_messages()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "conv-search-input":
            if self._search_timer is not None:
                self._search_timer.stop()
            query = event.value.strip().lower()
            self._search_timer = self.set_timer(
                0.2, lambda: self._filter_sessions(query)
            )
        elif event.input.id == "conv-msg-search-input":
            if self._search_timer is not None:
                self._search_timer.stop()
            query = event.value.strip()
            self._search_timer = self.set_timer(
                0.3, lambda: self._search_in_conversation(query)
            )

    def _filter_sessions(self, query: str) -> None:
        if query:
            self._sessions = [
                s for s in self._all_sessions
                if query in (s.summary or "").lower()
                or query in (s.first_prompt or "").lower()
                or query in s.project.lower()
            ]
        else:
            self._sessions = self._all_sessions
        self._render_session_list()

    def _search_in_conversation(self, query: str) -> None:
        if not self._selected or not query:
            if not query and self._selected:
                # Clear search — restore full message view
                self._select_session(self._selected)
            return
        results = data.search_session(self._selected.full_path, query, limit=50)
        container = self.query_one("#conv-messages")
        container.remove_children()
        title = self.query_one("#conv-title", Static)
        title.update(
            f" Search: '{escape(query)}' — [dim]{len(results)} matches[/dim]"
        )
        if not results:
            container.mount(Static("[dim]No matches found.[/dim]"))
            return
        for msg in results:
            self._mount_message(container, msg, highlight=query)

    def action_focus_conv_search(self) -> None:
        if self._selected:
            # If a session is selected, show and focus the message search
            search_bar = self.query_one("#conv-msg-search")
            search_bar.remove_class("hidden")
            self.query_one("#conv-msg-search-input", Input).focus()
        else:
            self.query_one("#conv-search-input", Input).focus()

    def action_toggle_favorite(self) -> None:
        if self._selected is None:
            self.app.notify("Select a session first", severity="warning", timeout=2)
            return
        new_state, err = data.toggle_pin(self._selected.session_id)
        if err:
            self.app.notify(f"Pin failed: {err}", severity="error", timeout=3)
            return
        self._pinned = data.get_pinned()
        icon = "★" if new_state else "☆"
        self.app.notify(f"{icon} {'Pinned' if new_state else 'Unpinned'}", timeout=2)
        self._render_session_list()

    def action_export_conversation(self) -> None:
        if self._selected is None:
            self.app.notify("Select a session first", severity="warning", timeout=2)
            return
        out_path, err = data.export_conversation(
            self._selected.full_path, self._selected
        )
        if out_path:
            self.app.notify(f"Exported to {out_path.name}", timeout=3)
        else:
            self.app.notify(f"Export failed: {err}", severity="error", timeout=4)

    def action_toggle_timeline(self) -> None:
        """Toggle between session list and timeline view."""
        self._timeline_mode = not self._timeline_mode
        if self._timeline_mode:
            self._render_timeline()
        else:
            self._render_session_list()
            title = self.query_one("#conv-title", Static)
            title.update("Select a session to view conversation")

    def _render_timeline(self) -> None:
        """Render chronological timeline of sessions + auto-memory."""
        container = self.query_one("#conv-session-list")
        container.remove_children()
        title = self.query_one("#conv-title", Static)

        entries = data.get_session_timeline(self._timeline_project)
        filter_label = f" ({self._timeline_project})" if self._timeline_project else " (all)"
        title.update(f" Timeline{filter_label} — {len(entries)} entries  [dim]t to exit[/dim]")

        # Message area shows project filter options
        msg_container = self.query_one("#conv-messages")
        msg_container.remove_children()
        projects = data.get_timeline_projects()
        if projects:
            filter_text = "[bold]Filter by project:[/bold] [dim](click or type in search)[/dim]\n"
            for p in projects[:20]:
                selected = " [yellow]★[/yellow]" if p == self._timeline_project else ""
                filter_text += f"  {escape(p)}{selected}\n"
            msg_container.mount(Static(filter_text))

        if not entries:
            container.mount(Static("[dim]No timeline entries found.[/dim]"))
            return

        current_date = ""
        for entry in entries[:100]:
            if entry.date != current_date:
                current_date = entry.date
                container.mount(Static(
                    f"\n[bold]{current_date}[/bold]",
                    classes="session-item",
                ))
            if entry.entry_type == "session":
                icon = "💬"
                color = "cyan"
            else:
                icon = "🤖"
                color = "magenta"
            widget = Static(
                f"  {icon} [{color}]{escape(entry.summary)}[/{color}]\n"
                f"     [dim]{escape(entry.project)}[/dim]",
                classes="session-item",
            )
            container.mount(widget)

    def action_rename_session(self) -> None:
        if self._renaming:
            return
        if self._selected is None:
            self.app.notify("Select a session first", severity="warning", timeout=2)
            return
        self._renaming = True
        title = self.query_one("#conv-title", Static)
        title.display = False
        preview = self.query_one("#conv-preview")
        current_name = self._selected.summary or self._selected.first_prompt or ""
        rename_input = Input(
            value=current_name,
            placeholder="New session name...",
            id="conv-rename-input",
        )
        preview.mount(rename_input, before=0)
        rename_input.focus()
        rename_input.action_select_all()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "conv-rename-input":
            self._do_rename_session(event.value)

    def _do_rename_session(self, new_name: str) -> None:
        if not self._selected:
            return
        ok, err = data.rename_session(self._selected, new_name.strip())
        self._exit_rename_mode()
        if ok:
            self._selected.summary = new_name.strip()
            self._load_sessions()
            self.app.notify(f"Renamed to: {new_name.strip()[:40]}", timeout=2)
        else:
            self.app.notify(f"Rename failed: {err}", severity="error", timeout=4)

    def _cancel_rename(self) -> None:
        self._exit_rename_mode()

    def _exit_rename_mode(self) -> None:
        self._renaming = False
        for inp in self.query("#conv-rename-input"):
            inp.remove()
        title = self.query_one("#conv-title", Static)
        title.display = True

    def on_key(self, event) -> None:
        if event.key == "escape":
            if self._renaming:
                self._cancel_rename()
                event.stop()
                return
            search_bar = self.query_one("#conv-msg-search")
            if not search_bar.has_class("hidden"):
                search_bar.add_class("hidden")
                inp = self.query_one("#conv-msg-search-input", Input)
                inp.value = ""
                if self._selected:
                    self._select_session(self._selected)
                event.stop()


# ============================================================
# Stats Tab
# ============================================================

class StatsTab(TabPane):
    """Usage statistics dashboard."""

    def __init__(self) -> None:
        super().__init__("Stats", id="tab-stats")

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="stats-container")

    def on_mount(self) -> None:
        self._load_stats()

    def _load_stats(self) -> None:
        container = self.query_one("#stats-container")
        container.remove_children()

        all_stats = data.get_stats()
        overview = data.get_stats_overview()

        if not all_stats and not overview:
            container.mount(Static(
                "[dim]No stats data found.\n\n"
                "Stats are collected automatically by Claude Code\n"
                "and appear here after your first session.[/dim]"
            ))
            return

        summary = data.stats_summary(all_stats)

        # Overview
        container.mount(Static("[bold]Usage Overview[/bold]\n"))
        total_msgs = overview.get("total_messages", summary["total_messages"])
        total_sess = overview.get("total_sessions", summary["total_sessions"])
        container.mount(Static(
            f"  [bold]{data.format_number(total_msgs)}[/bold] messages  |  "
            f"[bold]{data.format_number(summary['total_tools'])}[/bold] tool calls  |  "
            f"[bold]{total_sess}[/bold] sessions  |  "
            f"[bold]{summary['days']}[/bold] days tracked"
        ))
        container.mount(Static(
            f"  Avg: [bold]{data.format_number(summary['avg_daily_messages'])}[/bold] msgs/day  |  "
            f"First session: {overview.get('first_session', 'N/A')[:10]}\n"
        ))

        # Model usage
        models = overview.get("models", {})
        if models and isinstance(models, dict):
            container.mount(Static("[bold]Model Usage[/bold]"))
            for model_name, usage in models.items():
                if isinstance(usage, dict):
                    cache_read = usage.get("cacheReadInputTokens", 0)
                    cache_write = usage.get("cacheCreationInputTokens", 0)
                    inp = usage.get("inputTokens", 0)
                    out = usage.get("outputTokens", 0)
                    total_tokens = cache_read + cache_write + inp + out
                    short = model_name.replace("claude-", "").split("-2025")[0]
                    container.mount(Static(
                        f"  [cyan]{short}[/cyan]  "
                        f"in:{data.format_number(inp)}  out:{data.format_number(out)}  "
                        f"cache:{data.format_number(cache_read)}  "
                        f"total:{data.format_number(total_tokens)}"
                    ))
            container.mount(Static(""))

        # Context gauge
        ctx = data.estimate_context_usage()
        if ctx.get("active"):
            container.mount(Static(
                f"  [bold]Active Session[/bold]  "
                f"{gauge_bar(ctx['percent'])}  "
                f"~{data.format_number(ctx['tokens_est'])} tokens  "
                f"~${ctx['cost_est']:.2f}  "
                f"{ctx.get('age_minutes', 0)}m ago\n"
            ))
        else:
            container.mount(Static("  [dim]No active session detected[/dim]\n"))

        # Sparklines
        recent_30 = all_stats[-30:] if len(all_stats) >= 30 else all_stats
        msg_values = [s.messages for s in recent_30]
        tool_values = [s.tool_calls for s in recent_30]

        if msg_values:
            spark = sparkline(msg_values, width=40)
            container.mount(Static(f"  [bold]Messages (last {len(recent_30)}d)[/bold]"))
            container.mount(Static(f"  [cyan]{spark}[/cyan]"))
            container.mount(Static(
                f"  [dim]{recent_30[0].date}  {'·' * 20}  {recent_30[-1].date}[/dim]\n"
            ))

        if tool_values:
            spark = sparkline(tool_values, width=40)
            container.mount(Static(f"  [bold]Tool calls (last {len(recent_30)}d)[/bold]"))
            container.mount(Static(f"  [magenta]{spark}[/magenta]"))
            container.mount(Static(""))

        # Top 5 days
        sorted_days = sorted(all_stats, key=lambda s: s.messages, reverse=True)[:5]
        if sorted_days:
            peak = max(d.messages for d in sorted_days)
            container.mount(Static("  [bold]Top 5 Most Active Days[/bold]"))
            for s in sorted_days:
                bar_len = int(s.messages / peak * 25) if peak > 0 else 0
                container.mount(Static(
                    f"  {s.date}  [cyan]{'█' * bar_len}[/cyan] "
                    f"{data.format_number(s.messages)} msgs, "
                    f"{s.sessions} sess, {data.format_number(s.tool_calls)} tools"
                ))

        # Longest session
        longest = overview.get("longest_session", {})
        if isinstance(longest, dict) and longest.get("messageCount"):
            dur_h = longest.get("duration", 0) / 3_600_000
            container.mount(Static(
                f"\n  [bold]Longest Session[/bold]  "
                f"{longest['messageCount']} messages · {dur_h:.1f}h · "
                f"{longest.get('timestamp', 'N/A')[:10]}"
            ))


# ============================================================
# History Tab
# ============================================================

class HistoryTab(TabPane):
    """Searchable command history with debounce."""

    def __init__(self) -> None:
        super().__init__("History", id="tab-history")
        self._entries: list[data.HistoryEntry] = []
        self._filtered: list[data.HistoryEntry] = []
        self._search_timer: Timer | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="history-container"):
            with Vertical(id="history-search"):
                yield Input(
                    placeholder="Filter history... (Esc to unfocus)",
                    id="history-search-input",
                )
            yield VerticalScroll(id="history-list")

    def on_mount(self) -> None:
        self._load_history()

    def on_unmount(self) -> None:
        if self._search_timer is not None:
            self._search_timer.stop()

    def _load_history(self) -> None:
        self._entries = data.get_history(limit=500)
        self._filtered = self._entries
        self._render_list()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "history-search-input":
            return
        if self._search_timer is not None:
            self._search_timer.stop()
        query = event.value.strip().lower()
        self._search_timer = self.set_timer(
            0.15, lambda: self._filter_and_render(query)
        )

    def _filter_and_render(self, query: str) -> None:
        if query:
            self._filtered = [
                e for e in self._entries
                if query in e.display.lower() or query in e.project.lower()
            ]
        else:
            self._filtered = self._entries
        self._render_list()

    def _render_list(self) -> None:
        container = self.query_one("#history-list")
        container.remove_children()
        for entry in self._filtered[:100]:
            ts = (data.time_ago(entry.timestamp / 1000)
                  if entry.timestamp > 1e12
                  else data.time_ago(entry.timestamp))
            display = entry.display.replace("\n", " ")[:100]
            container.mount(Static(
                f"[bold]{escape(display)}[/bold]\n"
                f"[dim]{escape(entry.project)} · {ts}[/dim]",
                classes="history-entry",
            ))
        if not self._filtered:
            container.mount(Static("[dim]No matching entries.[/dim]"))


# ============================================================
# Main App
# ============================================================

class CockpitApp(App):
    """Claude Cockpit — X-ray vision for your Claude Code brain."""

    CSS_PATH = "app.tcss"
    TITLE = "Claude Cockpit"
    SUB_TITLE = "~/.claude/"

    TAB_ORDER = [
        "tab-memory", "tab-tasks", "tab-plans",
        "tab-conversations", "tab-stats", "tab-history",
    ]

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("slash", "focus_search", "Search", show=True, key_display="/"),
        Binding("question_mark", "toggle_help", "Help", show=True, key_display="?"),
        Binding("m", "switch_tab('tab-memory')", "Memory", show=True),
        Binding("t", "switch_tab('tab-tasks')", "Tasks", show=True),
        Binding("p", "switch_tab('tab-plans')", "Plans", show=True),
        Binding("c", "switch_tab('tab-conversations')", "Conversations", show=True),
        Binding("s", "switch_tab('tab-stats')", "Stats", show=True),
        Binding("h", "switch_tab('tab-history')", "History", show=True),
        Binding("r", "refresh_all", "Refresh", show=True),
        Binding("a", "toggle_auto_memory", "Auto-Memory", show=True),
        Binding("left", "prev_tab", "Prev Tab", show=False),
        Binding("right", "next_tab", "Next Tab", show=False),
        Binding("escape", "unfocus", "Unfocus", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._gauge_cache: str = ""
        self._gauge_cache_tick: int = 0
        self._watcher_thread: threading.Thread | None = None
        self._watcher_stop = threading.Event()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="context-gauge")
        with TabbedContent():
            yield MemoryTab()
            yield TasksTab()
            yield PlansTab()
            yield ConversationsTab()
            yield StatsTab()
            yield HistoryTab()
        yield Footer()

    def on_mount(self) -> None:
        self._update_context_gauge()
        self.set_interval(5.0, self._update_context_gauge)
        self._start_file_watcher()

    def on_unmount(self) -> None:
        self._watcher_stop.set()

    def _check_watcher_health(self) -> None:
        """Warn user if the file watcher thread died."""
        if self._watcher_thread and not self._watcher_thread.is_alive():
            self.notify("File watcher stopped — auto-refresh disabled", severity="warning")

    def _start_file_watcher(self) -> None:
        """Watch ~/.claude/ for changes and auto-refresh affected tabs."""
        try:
            from watchfiles import watch, Change
        except ImportError:
            _log_warn("watchfiles not installed, auto-refresh disabled")
            return

        def _watcher():
            watch_dirs = [str(p) for p in data.WATCH_PATHS if p.exists()]
            # Also watch memory dirs
            if data.PROJECTS_DIR.exists():
                for mem_dir in data.PROJECTS_DIR.glob("*/memory"):
                    watch_dirs.append(str(mem_dir))
            if not watch_dirs:
                return
            try:
                for changes in watch(
                    *watch_dirs,
                    stop_event=self._watcher_stop,
                    debounce=1500,
                    step=500,
                    rust_timeout=5000,
                ):
                    changed_paths = {Path(p) for _, p in changes}
                    # Determine which tabs need refreshing
                    refresh_memory = any(
                        "memory" in str(p) for p in changed_paths
                    )
                    refresh_tasks = any(
                        "tasks" in str(p)
                        or p.name == "sessions-index.json"
                        for p in changed_paths
                    )
                    refresh_plans = any(
                        "plans" in str(p)
                        or p.name == "cockpit-pinned-plans.json"
                        for p in changed_paths
                    )
                    refresh_stats = any(
                        "stats" in str(p) for p in changed_paths
                    )
                    refresh_convos = any(
                        p.suffix == ".jsonl"
                        or p.name == "sessions-index.json"
                        or p.name == "cockpit-pinned.json"
                        for p in changed_paths
                    )
                    refresh_settings = any(
                        p.name == "cockpit-settings.json"
                        for p in changed_paths
                    )
                    # Schedule refreshes on the main thread
                    if refresh_convos:
                        self.call_from_thread(self._refresh_tab, "conversations")
                    if refresh_tasks:
                        self.call_from_thread(self._refresh_tab, "tasks")
                    if refresh_memory:
                        self.call_from_thread(self._refresh_tab, "memory")
                    if refresh_plans:
                        self.call_from_thread(self._refresh_tab, "plans")
                    if refresh_stats:
                        self.call_from_thread(self._refresh_tab, "stats")
                    if refresh_settings:
                        self.call_from_thread(self._invalidate_gauge_cache)
                    self.call_from_thread(self._invalidate_gauge_cache)
            except Exception as exc:
                _log_warn(f"file watcher stopped: {exc}")

        self._watcher_thread = threading.Thread(target=_watcher, daemon=True)
        self._watcher_thread.start()
        self.set_timer(2.0, self._check_watcher_health)

    def _refresh_tab(self, tab_name: str) -> None:
        """Refresh a specific tab's data."""
        method_map = {
            "memory": "_load_memory",
            "tasks": "_load_tasks",
            "plans": "_load_plans",
            "conversations": "_load_sessions",
            "stats": "_load_stats",
            "history": "_load_history",
        }
        method = method_map.get(tab_name)
        if not method:
            return
        for tab in self.query(TabPane):
            if hasattr(tab, method):
                getattr(tab, method)()
                break

    def _invalidate_gauge_cache(self) -> None:
        self._gauge_cache = ""

    def _update_context_gauge(self) -> None:
        ctx = data.estimate_context_usage()
        gauge = self.query_one("#context-gauge", Static)
        am_status = "[green]AM:ON[/green]" if data.is_auto_memory_enabled() else "[dim]AM:OFF[/dim]"
        if ctx.get("active"):
            bar = gauge_bar(ctx["percent"], width=15)
            gauge.update(
                f" Session: {bar}  "
                f"~{data.format_number(ctx['tokens_est'])} tokens  "
                f"~${ctx['cost_est']:.2f}  "
                f"{ctx.get('age_minutes', 0)}m ago  |  {am_status}"
            )
            self._gauge_cache = ""
        else:
            self._gauge_cache_tick += 1
            if not self._gauge_cache or self._gauge_cache_tick % 12 == 0:
                # Use lightweight stat-only calls (lazy content not loaded)
                files = data.get_memory_files()
                summary = data.memory_summary(files)
                tasks = data.get_tasks()
                ts = data.task_summary(tasks)
                self._gauge_cache = (
                    f" {summary['files']} memory files · "
                    f"{data.format_number(summary['size'])} "
                    f"|  {ts['active']} active · {ts['pending']} pending · "
                    f"{ts['done']} done  |  No active session  |  {am_status}"
                )
            gauge.update(self._gauge_cache)

    def action_focus_search(self) -> None:
        self.query_one(TabbedContent).active = "tab-memory"
        search_input = self.query_one("#memory-search-input", Input)
        search_input.focus()

    def action_unfocus(self) -> None:
        """Unfocus any focused input so keyboard shortcuts work again."""
        self.set_focus(None)

    def action_toggle_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_switch_tab(self, tab_id: str) -> None:
        self.query_one(TabbedContent).active = tab_id

    def action_prev_tab(self) -> None:
        """Switch to the previous tab (left arrow). No-op if editing."""
        focused = self.focused
        if isinstance(focused, (TextArea, Input)):
            return  # Don't steal arrow keys from text editing
        tc = self.query_one(TabbedContent)
        current = tc.active
        try:
            idx = self.TAB_ORDER.index(current)
        except ValueError:
            idx = 0
        new_idx = (idx - 1) % len(self.TAB_ORDER)
        tc.active = self.TAB_ORDER[new_idx]

    def action_next_tab(self) -> None:
        """Switch to the next tab (right arrow). No-op if editing."""
        focused = self.focused
        if isinstance(focused, (TextArea, Input)):
            return  # Don't steal arrow keys from text editing
        tc = self.query_one(TabbedContent)
        current = tc.active
        try:
            idx = self.TAB_ORDER.index(current)
        except ValueError:
            idx = 0
        new_idx = (idx + 1) % len(self.TAB_ORDER)
        tc.active = self.TAB_ORDER[new_idx]

    def action_toggle_auto_memory(self) -> None:
        """Toggle auto-memory (real-time context capture)."""
        new_state, err = data.toggle_auto_memory()
        if err:
            self.notify(f"Auto-Memory toggle failed: {err}", severity="error", timeout=4)
            return
        icon = "ON" if new_state else "OFF"
        self.notify(f"Auto-Memory: {icon}", timeout=2)
        self._gauge_cache = ""  # Force rebuild to reflect new AM status
        self._update_context_gauge()

    def action_refresh_all(self) -> None:
        """Reload all data from disk."""
        self._gauge_cache = ""
        for tab in self.query(TabPane):
            for method_name in ("_load_memory", "_load_tasks", "_load_plans",
                                "_load_sessions", "_load_stats", "_load_history"):
                if hasattr(tab, method_name):
                    getattr(tab, method_name)()
                    break
        self._update_context_gauge()
        self.notify("Refreshed all data", timeout=2)


def main():
    import sys
    if "--version" in sys.argv:
        from cockpit import __version__
        print(f"Claude Cockpit {__version__}")
        sys.exit(0)
    app = CockpitApp()
    app.run()


if __name__ == "__main__":
    main()
