"""Tests for cockpit.data."""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from cockpit import data


# ── _tail_read_lines ──────────────────────────────────────────────────────────


class TestTailReadLines:
    """The critical tail-reader for history.jsonl."""

    def _write_file(self, content: str) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8"
        )
        f.write(content)
        f.close()
        return Path(f.name)

    def _write_bytes(self, content: bytes) -> Path:
        f = tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False)
        f.write(content)
        f.close()
        return Path(f.name)

    def test_basic_lines_with_trailing_newline(self):
        path = self._write_file("aaa\nbbb\nccc\n")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["aaa", "bbb", "ccc"]
        os.unlink(path)

    def test_basic_lines_without_trailing_newline(self):
        path = self._write_file("aaa\nbbb\nccc")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["aaa", "bbb", "ccc"]
        os.unlink(path)

    def test_tail_last_n(self):
        path = self._write_file("L1\nL2\nL3\nL4\nL5\n")
        assert data._tail_read_lines(path, 2, chunk_size=4) == ["L4", "L5"]
        os.unlink(path)

    def test_empty_file(self):
        path = self._write_file("")
        assert data._tail_read_lines(path, 10) == []
        os.unlink(path)

    def test_single_line(self):
        path = self._write_file("only line")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["only line"]
        os.unlink(path)

    def test_single_line_with_newline(self):
        path = self._write_file("only line\n")
        assert data._tail_read_lines(path, 10, chunk_size=4) == ["only line"]
        os.unlink(path)

    def test_multibyte_utf8_emoji(self):
        # Each emoji is 4 bytes; small chunk_size forces splits mid-character
        path = self._write_bytes("hello\n🎉🎊\nworld\n".encode("utf-8"))
        result = data._tail_read_lines(path, 10, chunk_size=5)
        assert result[-1] == "world"
        # Middle line should contain the emojis (decoded as one unit)
        assert "🎉" in result[1] or "🎊" in result[1]
        os.unlink(path)

    def test_chunk_boundary_at_newline(self):
        """The bug that splitlines() caused: chunks split exactly at \\n."""
        path = self._write_file("aaa\nbbb\n")
        result = data._tail_read_lines(path, 10, chunk_size=4)
        assert result == ["aaa", "bbb"], f"Got: {result}"
        os.unlink(path)

    def test_nonexistent_file(self):
        assert data._tail_read_lines(Path("/nonexistent/file.txt"), 10) == []

    def test_large_chunk_size(self):
        path = self._write_file("a\nb\nc\n")
        assert data._tail_read_lines(path, 10, chunk_size=65536) == ["a", "b", "c"]
        os.unlink(path)

    def test_jsonl_lines_are_valid(self):
        """Each line should be independently parseable JSON."""
        lines = [json.dumps({"i": i}) for i in range(20)]
        path = self._write_file("\n".join(lines) + "\n")
        result = data._tail_read_lines(path, 10, chunk_size=32)
        for line in result:
            json.loads(line)  # Should not raise
        assert len(result) == 10
        os.unlink(path)


# ── _decode_project_name ──────────────────────────────────────────────────────


class TestDecodeProjectName:
    def test_org_with_trailing_path(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-go-ios"
            )
            == "go-ios"
        )

    def test_org_as_last_segment(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-iSweep17"
            )
            == "iSweep17"
        )

    def test_multi_word_project(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-mobile-automation"
            )
            == "mobile-automation"
        )

    def test_documents_poc(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-Documents-poc-xcresult"
            )
            == "xcresult"
        )

    def test_poc_in_project_name(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-Documents-poc-patrol-segregation-poc"
            )
            == "patrol-segregation-poc"
        )

    def test_bare_username(self):
        assert data._decode_project_name("-Users-amankansal") == "amankansal"

    def test_empty_string(self):
        assert data._decode_project_name("") == "unknown"

    def test_rpi_manager(self):
        assert (
            data._decode_project_name(
                "-Users-amankansal-go-src-github-com-LambdatestIncPrivate-rpi-manager"
            )
            == "rpi-manager"
        )


# ── MemoryFile lazy loading ───────────────────────────────────────────────────


class TestMemoryFileLazy:
    def test_content_lazy_loaded(self, tmp_path):
        md = tmp_path / "test.md"
        md.write_text("# Hello\nWorld\n")
        mf = data.MemoryFile(
            project="test",
            name="test.md",
            path=md,
            size=md.stat().st_size,
        )
        # _content should be None initially
        assert mf._content is None
        # Accessing .content should load it
        assert "Hello" in mf.content
        assert mf._content is not None

    def test_content_missing_file(self, tmp_path):
        mf = data.MemoryFile(
            project="test",
            name="gone.md",
            path=tmp_path / "gone.md",
            size=0,
        )
        assert mf.content == ""


# ── search_memory ─────────────────────────────────────────────────────────────


class TestSearchMemory:
    def test_basic_search(self, tmp_path):
        md = tmp_path / "mem.md"
        md.write_text("line one\nfoo bar baz\nline three\n")
        mf = data.MemoryFile(
            project="proj", name="mem.md", path=md, size=md.stat().st_size
        )
        results = data.search_memory("bar", [mf])
        assert len(results) == 1
        assert results[0].line_num == 2
        assert "bar" in results[0].line

    def test_empty_query(self, tmp_path):
        md = tmp_path / "mem.md"
        md.write_text("content\n")
        mf = data.MemoryFile(
            project="proj", name="mem.md", path=md, size=md.stat().st_size
        )
        assert data.search_memory("", [mf]) == []
        assert data.search_memory("   ", [mf]) == []

    def test_case_insensitive(self, tmp_path):
        md = tmp_path / "mem.md"
        md.write_text("Hello World\n")
        mf = data.MemoryFile(
            project="proj", name="mem.md", path=md, size=md.stat().st_size
        )
        results = data.search_memory("hello", [mf])
        assert len(results) == 1


# ── Helpers ───────────────────────────────────────────────────────────────────


class TestHelpers:
    def test_format_size(self):
        assert data.format_size(500) == "500B"
        assert data.format_size(1024) == "1.0K"
        assert data.format_size(1024 * 1024) == "1.0M"

    def test_format_number(self):
        assert data.format_number(42) == "42"
        assert data.format_number(1500) == "1.5K"
        assert data.format_number(2_500_000) == "2.5M"

    def test_time_ago(self):
        import time

        now = time.time()
        assert data.time_ago(now) == "just now"
        assert data.time_ago(now - 300) == "5m ago"
        assert data.time_ago(now - 7200) == "2h ago"
        assert data.time_ago(now - 172800) == "2d ago"
        # Future timestamps should not crash
        assert data.time_ago(now + 100) == "just now"


# ── Task loading ──────────────────────────────────────────────────────────────


class TestTasks:
    def test_load_tasks_from_dir(self, tmp_path):
        task = {
            "id": "1",
            "subject": "Test task",
            "description": "Do something",
            "status": "pending",
        }
        (tmp_path / "1.json").write_text(json.dumps(task))
        tasks = data._load_tasks_from_dir(tmp_path)
        assert len(tasks) == 1
        assert tasks[0].subject == "Test task"

    def test_corrupted_json_skipped(self, tmp_path):
        (tmp_path / "1.json").write_text("{bad json")
        (tmp_path / "2.json").write_text(json.dumps({"id": "2", "subject": "OK"}))
        tasks = data._load_tasks_from_dir(tmp_path)
        assert len(tasks) == 1
        assert tasks[0].id == "2"

    def test_hidden_files_skipped(self, tmp_path):
        (tmp_path / ".hidden.json").write_text(json.dumps({"id": "h"}))
        (tmp_path / "1.json").write_text(json.dumps({"id": "1", "subject": "Vis"}))
        tasks = data._load_tasks_from_dir(tmp_path)
        assert len(tasks) == 1

    def test_task_summary(self):
        tasks = [
            data.Task("1", "A", "", "pending"),
            data.Task("2", "B", "", "in_progress"),
            data.Task("3", "C", "", "completed"),
            data.Task("4", "D", "", "completed"),
        ]
        s = data.task_summary(tasks)
        assert s == {"pending": 1, "active": 1, "done": 2, "total": 4}

    def test_all_recent_tasks_skips_all_completed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TASKS_DIR", tmp_path)
        # Session with all completed tasks — should be skipped
        done_dir = tmp_path / "done-session"
        done_dir.mkdir()
        (done_dir / "1.json").write_text(json.dumps({
            "id": "1", "subject": "Done task", "status": "completed",
        }))
        # Session with pending task — should be included
        active_dir = tmp_path / "active-session"
        active_dir.mkdir()
        (active_dir / "2.json").write_text(json.dumps({
            "id": "2", "subject": "Active task", "status": "pending",
        }))
        tasks = data.get_all_recent_tasks(limit=3, max_age_hours=9999)
        assert len(tasks) == 1
        assert tasks[0].subject == "Active task"

    def test_all_recent_tasks_skips_old(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TASKS_DIR", tmp_path)
        old_dir = tmp_path / "old-session"
        old_dir.mkdir()
        task_file = old_dir / "1.json"
        task_file.write_text(json.dumps({
            "id": "1", "subject": "Old task", "status": "pending",
        }))
        # Set mtime to 48 hours ago
        old_time = time.time() - 48 * 3600
        os.utime(task_file, (old_time, old_time))
        tasks = data.get_all_recent_tasks(limit=3, max_age_hours=24)
        assert len(tasks) == 0


# ── Conversations ────────────────────────────────────────────────────────────


def _make_jsonl_line(msg_type, content, uuid="u1", timestamp="2026-01-01T00:00:00Z", **extra):
    obj = {"type": msg_type, "uuid": uuid, "timestamp": timestamp,
           "message": {"role": msg_type, "content": content}, **extra}
    return json.dumps(obj)


class TestGetAllSessions:
    def test_reads_index(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-test"
        proj.mkdir(parents=True)
        jsonl = proj / "abc123.jsonl"
        jsonl.write_text("")
        idx = {"version": 1, "entries": [{
            "sessionId": "abc123", "fullPath": str(jsonl),
            "summary": "Test session", "firstPrompt": "hello",
            "messageCount": 5, "created": "2026-01-01T00:00:00Z",
            "modified": "2026-01-02T00:00:00Z", "gitBranch": "main",
            "projectPath": "/Users/test", "isSidechain": False,
        }]}
        (proj / "sessions-index.json").write_text(json.dumps(idx))
        orig = data.PROJECTS_DIR
        data.PROJECTS_DIR = tmp_path / "projects"
        try:
            sessions = data.get_all_sessions()
            assert len(sessions) == 1
            assert sessions[0].session_id == "abc123"
            assert sessions[0].summary == "Test session"
            assert sessions[0].message_count == 5
        finally:
            data.PROJECTS_DIR = orig

    def test_skips_sidechains(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-test"
        proj.mkdir(parents=True)
        idx = {"version": 1, "entries": [{
            "sessionId": "s1", "fullPath": "/fake", "summary": "side",
            "firstPrompt": "", "messageCount": 1, "created": "",
            "modified": "2026-01-01T00:00:00Z", "gitBranch": "",
            "projectPath": "", "isSidechain": True,
        }]}
        (proj / "sessions-index.json").write_text(json.dumps(idx))
        orig = data.PROJECTS_DIR
        data.PROJECTS_DIR = tmp_path / "projects"
        try:
            assert data.get_all_sessions() == []
        finally:
            data.PROJECTS_DIR = orig

    def test_sorts_by_modified(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-test"
        proj.mkdir(parents=True)
        # Create actual JSONL files so they pass the exists() check
        (proj / "old.jsonl").write_text('{"type":"user","message":{"content":"hi"}}\n')
        (proj / "new.jsonl").write_text('{"type":"user","message":{"content":"hi"}}\n')
        entries = [
            {"sessionId": "old", "fullPath": str(proj / "old.jsonl"), "summary": "old",
             "firstPrompt": "", "messageCount": 1, "created": "",
             "modified": "2026-01-01T00:00:00Z", "gitBranch": "",
             "projectPath": "", "isSidechain": False},
            {"sessionId": "new", "fullPath": str(proj / "new.jsonl"), "summary": "new",
             "firstPrompt": "", "messageCount": 1, "created": "",
             "modified": "2026-03-01T00:00:00Z", "gitBranch": "",
             "projectPath": "", "isSidechain": False},
        ]
        (proj / "sessions-index.json").write_text(json.dumps({"version": 1, "entries": entries}))
        orig = data.PROJECTS_DIR
        data.PROJECTS_DIR = tmp_path / "projects"
        try:
            sessions = data.get_all_sessions()
            # Filter to only indexed sessions (exclude JSONL discovery duplicates)
            indexed = [s for s in sessions if s.summary]
            assert indexed[0].session_id == "new"
            assert indexed[1].session_id == "old"
        finally:
            data.PROJECTS_DIR = orig

    def test_missing_dir(self, tmp_path):
        orig = data.PROJECTS_DIR
        data.PROJECTS_DIR = tmp_path / "nonexistent"
        try:
            assert data.get_all_sessions() == []
        finally:
            data.PROJECTS_DIR = orig

    def test_skips_snapshot_only_sessions(self, tmp_path):
        """JSONL files with no user/assistant messages should be excluded."""
        proj = tmp_path / "projects" / "-Users-test"
        proj.mkdir(parents=True)
        # Snapshot-only file (no user messages)
        snapshot = proj / "snap-only.jsonl"
        snapshot.write_text(
            json.dumps({"type": "file-history-snapshot", "data": {}}) + "\n"
            + json.dumps({"type": "file-history-snapshot", "data": {}}) + "\n"
        )
        # File with a real user message
        real = proj / "real-session.jsonl"
        real.write_text(
            _make_jsonl_line("user", "hello world") + "\n"
            + _make_jsonl_line("assistant", [{"type": "text", "text": "hi"}]) + "\n"
        )
        orig = data.PROJECTS_DIR
        data.PROJECTS_DIR = tmp_path / "projects"
        try:
            sessions = data.get_all_sessions()
            # Only the real session should appear (snapshot-only skipped)
            assert len(sessions) == 1
            assert sessions[0].first_prompt == "hello world"
        finally:
            data.PROJECTS_DIR = orig


class TestGetSessionMessages:
    def test_user_string_content(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(_make_jsonl_line("user", "hello world") + "\n")
        msgs, has_more, total = data.get_session_messages(f)
        assert len(msgs) == 1
        assert msgs[0].role == "user"
        assert msgs[0].text == "hello world"
        assert not has_more
        assert total == 1

    def test_assistant_text_blocks(self, tmp_path):
        f = tmp_path / "session.jsonl"
        content = [{"type": "text", "text": "I'll help you."}]
        f.write_text(_make_jsonl_line("assistant", content) + "\n")
        msgs, _, total = data.get_session_messages(f)
        assert len(msgs) == 1
        assert msgs[0].text == "I'll help you."
        assert total == 1

    def test_tool_use_extracted(self, tmp_path):
        f = tmp_path / "session.jsonl"
        content = [
            {"type": "text", "text": "Let me read that."},
            {"type": "tool_use", "name": "Read", "id": "t1", "input": {}},
        ]
        f.write_text(_make_jsonl_line("assistant", content) + "\n")
        msgs, _, _ = data.get_session_messages(f)
        assert msgs[0].tool_names == ["Read"]

    def test_thinking_detected(self, tmp_path):
        f = tmp_path / "session.jsonl"
        content = [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Here's my answer."},
        ]
        f.write_text(_make_jsonl_line("assistant", content) + "\n")
        msgs, _, _ = data.get_session_messages(f)
        assert msgs[0].has_thinking is True

    def test_skips_progress_lines(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = [
            _make_jsonl_line("user", "hi"),
            json.dumps({"type": "progress", "data": {}}),
            json.dumps({"type": "file-history-snapshot", "data": {}}),
            _make_jsonl_line("assistant", [{"type": "text", "text": "hey"}]),
        ]
        f.write_text("\n".join(lines) + "\n")
        msgs, _, total = data.get_session_messages(f)
        assert len(msgs) == 2
        assert total == 2

    def test_pagination(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = [_make_jsonl_line("user", f"msg {i}", uuid=f"u{i}") for i in range(10)]
        f.write_text("\n".join(lines) + "\n")
        msgs, has_more, total = data.get_session_messages(f, offset=0, limit=3)
        assert len(msgs) == 3
        assert has_more is True
        assert total == 10
        assert msgs[0].text == "msg 0"
        msgs2, has_more2, total2 = data.get_session_messages(f, offset=8, limit=3)
        assert len(msgs2) == 2
        assert has_more2 is False
        assert total2 == 10

    def test_corrupt_lines_skipped(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = ["{bad json", _make_jsonl_line("user", "valid")]
        f.write_text("\n".join(lines) + "\n")
        msgs, _, _ = data.get_session_messages(f)
        assert len(msgs) == 1

    def test_nonexistent_file(self):
        msgs, has_more, total = data.get_session_messages(Path("/no/such/file.jsonl"))
        assert msgs == []
        assert not has_more
        assert total == 0


class TestSearchSession:
    def test_basic_search(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = [
            _make_jsonl_line("user", "hello world"),
            _make_jsonl_line("assistant", [{"type": "text", "text": "goodbye"}]),
            _make_jsonl_line("user", "hello again"),
        ]
        f.write_text("\n".join(lines) + "\n")
        results = data.search_session(f, "hello")
        assert len(results) == 2

    def test_empty_query(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text(_make_jsonl_line("user", "test") + "\n")
        assert data.search_session(f, "") == []
        assert data.search_session(f, "   ") == []


# ── Memory/Plans Write ───────────────────────────────────────────────────────


class TestSaveMemoryFile:
    def test_save_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        f = tmp_path / "mem.md"
        f.write_text("original")
        mtime = f.stat().st_mtime
        ok, err = data.save_memory_file(f, "updated", mtime)
        assert ok is True
        assert err == ""
        assert f.read_text() == "updated"

    def test_save_conflict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        f = tmp_path / "mem.md"
        f.write_text("v1")
        old_mtime = f.stat().st_mtime
        time.sleep(0.05)
        f.write_text("v2")  # External change
        ok, err = data.save_memory_file(f, "v3", old_mtime)
        assert ok is False
        assert "externally" in err
        assert f.read_text() == "v2"  # Original preserved

    def test_save_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        ok, err = data.save_memory_file(tmp_path / "gone.md", "x", 0.0)
        assert ok is False
        assert "not found" in err.lower()

    def test_rejects_path_outside_projects(self, tmp_path):
        f = tmp_path / "mem.md"
        f.write_text("content")
        mtime = f.stat().st_mtime
        ok, err = data.save_memory_file(f, "hacked", mtime)
        assert ok is False
        assert "not in allowed directory" in err.lower()


class TestSavePlanFile:
    def test_save_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PLANS_DIR", tmp_path)
        f = tmp_path / "plan.md"
        f.write_text("original plan")
        mtime = f.stat().st_mtime
        ok, err = data.save_plan_file(f, "updated plan", mtime)
        assert ok is True
        assert err == ""
        assert f.read_text() == "updated plan"

    def test_save_conflict(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PLANS_DIR", tmp_path)
        f = tmp_path / "plan.md"
        f.write_text("v1")
        old_mtime = f.stat().st_mtime
        time.sleep(0.05)
        f.write_text("v2")  # External change
        ok, err = data.save_plan_file(f, "v3", old_mtime)
        assert ok is False
        assert "externally" in err
        assert f.read_text() == "v2"

    def test_save_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PLANS_DIR", tmp_path)
        ok, err = data.save_plan_file(tmp_path / "gone.md", "x", 0.0)
        assert ok is False
        assert "not found" in err.lower()

    def test_rejects_path_outside_plans(self, tmp_path):
        f = tmp_path / "plan.md"
        f.write_text("content")
        mtime = f.stat().st_mtime
        ok, err = data.save_plan_file(f, "hacked", mtime)
        assert ok is False
        assert "not in allowed directory" in err.lower()


class TestRenamePlan:
    def test_rename_success(self, tmp_path):
        f = tmp_path / "old-name.md"
        f.write_text("content")
        ok, err = data.rename_plan(f, "new-name")
        assert ok is True
        assert not f.exists()
        assert (tmp_path / "new-name.md").exists()
        assert (tmp_path / "new-name.md").read_text() == "content"

    def test_adds_md_extension(self, tmp_path):
        f = tmp_path / "plan.md"
        f.write_text("x")
        ok, _ = data.rename_plan(f, "renamed")
        assert (tmp_path / "renamed.md").exists()

    def test_rejects_conflict(self, tmp_path):
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        ok, err = data.rename_plan(tmp_path / "a.md", "b.md")
        assert ok is False
        assert "already exists" in err

    def test_rejects_empty_name(self, tmp_path):
        f = tmp_path / "x.md"
        f.write_text("x")
        ok, err = data.rename_plan(f, "")
        assert ok is False

    def test_rejects_path_separators(self, tmp_path):
        f = tmp_path / "x.md"
        f.write_text("x")
        ok, err = data.rename_plan(f, "foo/bar")
        assert ok is False
        assert "separator" in err.lower()

    def test_same_name_noop(self, tmp_path):
        f = tmp_path / "plan.md"
        f.write_text("x")
        ok, _ = data.rename_plan(f, "plan.md")
        assert ok is True
        assert f.exists()


# ── format_duration ──────────────────────────────────────────────────────────


class TestFormatDuration:
    def test_minutes_only(self):
        assert data.format_duration(
            "2026-01-01T10:00:00Z", "2026-01-01T10:45:00Z"
        ) == "45m"

    def test_hours_and_minutes(self):
        assert data.format_duration(
            "2026-01-01T10:00:00Z", "2026-01-01T12:15:00Z"
        ) == "2h 15m"

    def test_hours_exact(self):
        assert data.format_duration(
            "2026-01-01T10:00:00Z", "2026-01-01T13:00:00Z"
        ) == "3h"

    def test_days_and_hours(self):
        assert data.format_duration(
            "2026-01-01T10:00:00Z", "2026-01-04T12:00:00Z"
        ) == "3d 2h"

    def test_days_exact(self):
        assert data.format_duration(
            "2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z"
        ) == "2d"

    def test_less_than_one_minute(self):
        assert data.format_duration(
            "2026-01-01T10:00:00Z", "2026-01-01T10:00:30Z"
        ) == "<1m"

    def test_empty_created(self):
        assert data.format_duration("", "2026-01-01T10:00:00Z") == ""

    def test_empty_modified(self):
        assert data.format_duration("2026-01-01T10:00:00Z", "") == ""

    def test_both_empty(self):
        assert data.format_duration("", "") == ""

    def test_negative_duration(self):
        assert data.format_duration(
            "2026-01-02T10:00:00Z", "2026-01-01T10:00:00Z"
        ) == ""

    def test_invalid_timestamp(self):
        assert data.format_duration("not-a-date", "2026-01-01T10:00:00Z") == ""


# ── Tool Stats ───────────────────────────────────────────────────────────────


class TestToolStats:
    def _msg(self, tools: list[str]) -> data.ConversationMessage:
        return data.ConversationMessage(
            uuid="u1", role="assistant", text="", timestamp="",
            has_thinking=False, tool_names=tools, is_sidechain=False,
        )

    def test_basic_counts(self):
        msgs = [self._msg(["Read", "Read", "Edit"]), self._msg(["Read"])]
        stats = data.get_tool_stats(msgs)
        assert stats["Read"] == 3
        assert stats["Edit"] == 1

    def test_sorted_by_frequency(self):
        msgs = [self._msg(["Edit"]), self._msg(["Read", "Read", "Read"])]
        stats = data.get_tool_stats(msgs)
        keys = list(stats.keys())
        assert keys[0] == "Read"
        assert keys[1] == "Edit"

    def test_empty_messages(self):
        assert data.get_tool_stats([]) == {}

    def test_no_tools(self):
        msgs = [self._msg([])]
        assert data.get_tool_stats(msgs) == {}

    def test_format_basic(self):
        stats = {"Read": 12, "Edit": 8, "Bash": 5, "Grep": 3}
        result = data.format_tool_stats(stats)
        assert "Read:12" in result
        assert "Edit:8" in result

    def test_format_with_more(self):
        stats = {"Read": 12, "Edit": 8, "Bash": 5, "Grep": 3, "Write": 2, "Glob": 1}
        result = data.format_tool_stats(stats, top_n=4)
        assert "+2 more" in result

    def test_format_empty(self):
        assert data.format_tool_stats({}) == ""

    def test_format_fewer_than_top_n(self):
        stats = {"Read": 5, "Edit": 2}
        result = data.format_tool_stats(stats, top_n=4)
        assert "more" not in result
        assert "Read:5" in result
        assert "Edit:2" in result


# ── get_all_messages ─────────────────────────────────────────────────────────


class TestGetAllMessages:
    def test_basic(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = [
            _make_jsonl_line("user", "hello", uuid="u1"),
            _make_jsonl_line("assistant", [{"type": "text", "text": "hi"}], uuid="u2"),
        ]
        f.write_text("\n".join(lines) + "\n")
        msgs = data.get_all_messages(f)
        assert len(msgs) == 2
        assert msgs[0].role == "user"
        assert msgs[1].role == "assistant"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "session.jsonl"
        f.write_text("")
        assert data.get_all_messages(f) == []

    def test_nonexistent_file(self):
        assert data.get_all_messages(Path("/no/such/file.jsonl")) == []

    def test_filters_non_displayable(self, tmp_path):
        f = tmp_path / "session.jsonl"
        lines = [
            _make_jsonl_line("user", "hello"),
            json.dumps({"type": "progress", "data": {}}),
            _make_jsonl_line("assistant", [{"type": "text", "text": "hi"}]),
        ]
        f.write_text("\n".join(lines) + "\n")
        msgs = data.get_all_messages(f)
        assert len(msgs) == 2


# ── export_conversation ──────────────────────────────────────────────────────


class TestExportConversation:
    def _session(self, **overrides) -> data.SessionEntry:
        defaults = {
            "session_id": "abc12345-dead-beef",
            "project": "test-project",
            "full_path": Path("/tmp/test.jsonl"),
            "summary": "Test Session",
            "first_prompt": "hello",
            "message_count": 2,
            "created": "2026-01-01T10:00:00Z",
            "modified": "2026-01-01T11:30:00Z",
            "git_branch": "main",
            "is_sidechain": False,
            "file_size": 1024,
        }
        defaults.update(overrides)
        return data.SessionEntry(**defaults)

    def test_export_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "EXPORT_DIR", tmp_path)
        jsonl = tmp_path / "session.jsonl"
        lines = [
            _make_jsonl_line("user", "hello world"),
            _make_jsonl_line("assistant", [{"type": "text", "text": "hi there"}]),
        ]
        jsonl.write_text("\n".join(lines) + "\n")
        session = self._session(full_path=jsonl)
        out_path, err = data.export_conversation(jsonl, session)
        assert out_path is not None
        assert err == ""
        assert out_path.exists()
        content = out_path.read_text()
        assert "# Test Session" in content
        assert "hello world" in content
        assert "hi there" in content

    def test_export_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "EXPORT_DIR", tmp_path)
        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text("")
        session = self._session(full_path=jsonl)
        out_path, err = data.export_conversation(jsonl, session)
        assert out_path is None
        assert "No messages" in err

    def test_export_metadata(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "EXPORT_DIR", tmp_path)
        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text(_make_jsonl_line("user", "hi") + "\n")
        session = self._session(full_path=jsonl, git_branch="feature")
        out_path, err = data.export_conversation(jsonl, session)
        assert out_path is not None
        content = out_path.read_text()
        assert "**Branch:** feature" in content
        assert "**Project:** test-project" in content
        assert "**Duration:** 1h 30m" in content

    def test_export_with_tools(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "EXPORT_DIR", tmp_path)
        jsonl = tmp_path / "session.jsonl"
        content_blocks = [
            {"type": "text", "text": "Let me read that."},
            {"type": "tool_use", "name": "Read", "id": "t1", "input": {}},
        ]
        jsonl.write_text(_make_jsonl_line("assistant", content_blocks) + "\n")
        session = self._session(full_path=jsonl)
        out_path, _ = data.export_conversation(jsonl, session)
        content = out_path.read_text()
        assert "*Tools: Read*" in content

    def test_export_with_thinking(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "EXPORT_DIR", tmp_path)
        jsonl = tmp_path / "session.jsonl"
        content_blocks = [
            {"type": "thinking", "thinking": "hmm"},
            {"type": "text", "text": "Here's my answer."},
        ]
        jsonl.write_text(_make_jsonl_line("assistant", content_blocks) + "\n")
        session = self._session(full_path=jsonl)
        out_path, _ = data.export_conversation(jsonl, session)
        content = out_path.read_text()
        assert "*Thinking...*" in content


# ── Pinned Sessions ──────────────────────────────────────────────────────────


class TestPinnedSessions:
    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PINNED_FILE", tmp_path / "no-such.json")
        assert data.get_pinned() == set()

    def test_toggle_pin_on(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned.json"
        monkeypatch.setattr(data, "PINNED_FILE", pinned_file)
        new_state, err = data.toggle_pin("session-1")
        assert new_state is True
        assert err == ""
        assert "session-1" in data.get_pinned()

    def test_toggle_pin_off(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned.json"
        pinned_file.write_text(json.dumps(["session-1"]))
        monkeypatch.setattr(data, "PINNED_FILE", pinned_file)
        new_state, err = data.toggle_pin("session-1")
        assert new_state is False
        assert err == ""
        assert "session-1" not in data.get_pinned()

    def test_read_existing(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned.json"
        pinned_file.write_text(json.dumps(["s1", "s2", "s3"]))
        monkeypatch.setattr(data, "PINNED_FILE", pinned_file)
        assert data.get_pinned() == {"s1", "s2", "s3"}

    def test_corrupt_json(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned.json"
        pinned_file.write_text("{bad json")
        monkeypatch.setattr(data, "PINNED_FILE", pinned_file)
        assert data.get_pinned() == set()

    def test_wrong_type_in_array(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned.json"
        pinned_file.write_text(json.dumps(["valid", 123, None, "also-valid"]))
        monkeypatch.setattr(data, "PINNED_FILE", pinned_file)
        assert data.get_pinned() == {"valid", "also-valid"}

    def test_toggle_idempotent(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned.json"
        monkeypatch.setattr(data, "PINNED_FILE", pinned_file)
        data.toggle_pin("s1")
        data.toggle_pin("s1")
        assert "s1" not in data.get_pinned()
        data.toggle_pin("s1")
        assert "s1" in data.get_pinned()


# ── Pinned Plans ─────────────────────────────────────────────────────────────


class TestPinnedPlans:
    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PINNED_PLANS_FILE", tmp_path / "no-such.json")
        assert data.get_pinned_plans() == set()

    def test_toggle_pin_on(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned-plans.json"
        monkeypatch.setattr(data, "PINNED_PLANS_FILE", pinned_file)
        new_state, err = data.toggle_pin_plan("my-plan")
        assert new_state is True
        assert err == ""
        assert "my-plan" in data.get_pinned_plans()

    def test_toggle_pin_off(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned-plans.json"
        pinned_file.write_text(json.dumps(["my-plan"]))
        monkeypatch.setattr(data, "PINNED_PLANS_FILE", pinned_file)
        new_state, err = data.toggle_pin_plan("my-plan")
        assert new_state is False
        assert err == ""
        assert "my-plan" not in data.get_pinned_plans()

    def test_read_existing(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned-plans.json"
        pinned_file.write_text(json.dumps(["plan-a", "plan-b"]))
        monkeypatch.setattr(data, "PINNED_PLANS_FILE", pinned_file)
        assert data.get_pinned_plans() == {"plan-a", "plan-b"}

    def test_corrupt_json(self, tmp_path, monkeypatch):
        pinned_file = tmp_path / "pinned-plans.json"
        pinned_file.write_text("{bad json")
        monkeypatch.setattr(data, "PINNED_PLANS_FILE", pinned_file)
        assert data.get_pinned_plans() == set()


# ── build_session_lookup ────────────────────────────────────────────────────


class TestBuildSessionLookup:
    def _session(self, session_id: str, **kwargs) -> data.SessionEntry:
        defaults = {
            "session_id": session_id,
            "project": "test",
            "full_path": Path("/tmp/test.jsonl"),
            "summary": "Test",
            "first_prompt": "hello",
            "message_count": 5,
            "created": "",
            "modified": "",
            "git_branch": "",
            "is_sidechain": False,
            "file_size": 100,
        }
        defaults.update(kwargs)
        return data.SessionEntry(**defaults)

    def test_basic_lookup(self):
        sessions = [self._session("abc"), self._session("def")]
        lookup = data.build_session_lookup(sessions)
        assert "abc" in lookup
        assert "def" in lookup
        assert lookup["abc"].session_id == "abc"

    def test_empty_list(self):
        assert data.build_session_lookup([]) == {}

    def test_duplicate_ids_last_wins(self):
        s1 = self._session("abc", summary="first")
        s2 = self._session("abc", summary="second")
        lookup = data.build_session_lookup([s1, s2])
        assert lookup["abc"].summary == "second"


# ── rename_session ──────────────────────────────────────────────────────────


class TestRenameSession:
    def _session(self, session_id: str = "abc123") -> data.SessionEntry:
        return data.SessionEntry(
            session_id=session_id,
            project="test",
            full_path=Path("/tmp/test.jsonl"),
            summary="Old Name",
            first_prompt="hello",
            message_count=5,
            created="",
            modified="",
            git_branch="",
            is_sidechain=False,
            file_size=100,
        )

    def test_rename_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = {"version": 1, "entries": [
            {"sessionId": "abc123", "summary": "Old Name", "fullPath": "/tmp/test.jsonl"},
        ]}
        (proj / "sessions-index.json").write_text(json.dumps(idx))
        session = self._session("abc123")
        ok, err = data.rename_session(session, "New Name")
        assert ok is True
        assert err == ""
        # Verify written
        updated = json.loads((proj / "sessions-index.json").read_text())
        assert updated["entries"][0]["summary"] == "New Name"

    def test_rename_empty_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        session = self._session()
        ok, err = data.rename_session(session, "")
        assert ok is False
        assert "empty" in err.lower()

    def test_rename_whitespace_name(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        session = self._session()
        ok, err = data.rename_session(session, "   ")
        assert ok is False
        assert "empty" in err.lower()

    def test_rename_session_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = {"version": 1, "entries": [
            {"sessionId": "other", "summary": "Other Session"},
        ]}
        (proj / "sessions-index.json").write_text(json.dumps(idx))
        session = self._session("nonexistent")
        ok, err = data.rename_session(session, "New Name")
        assert ok is False
        assert "not found" in err.lower()

    def test_rename_no_projects_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path / "nonexistent")
        session = self._session()
        ok, err = data.rename_session(session, "New Name")
        assert ok is False

    def test_rename_strips_whitespace(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "proj"
        proj.mkdir()
        idx = {"version": 1, "entries": [
            {"sessionId": "abc123", "summary": "Old"},
        ]}
        (proj / "sessions-index.json").write_text(json.dumps(idx))
        session = self._session("abc123")
        ok, err = data.rename_session(session, "  Trimmed Name  ")
        assert ok is True
        updated = json.loads((proj / "sessions-index.json").read_text())
        assert updated["entries"][0]["summary"] == "Trimmed Name"

    def test_rename_multiple_projects(self, tmp_path, monkeypatch):
        """Session found in second project directory."""
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj1 = tmp_path / "proj1"
        proj1.mkdir()
        (proj1 / "sessions-index.json").write_text(json.dumps({
            "version": 1, "entries": [{"sessionId": "other", "summary": "X"}]
        }))
        proj2 = tmp_path / "proj2"
        proj2.mkdir()
        (proj2 / "sessions-index.json").write_text(json.dumps({
            "version": 1, "entries": [{"sessionId": "abc123", "summary": "Old"}]
        }))
        session = self._session("abc123")
        ok, err = data.rename_session(session, "Found It")
        assert ok is True
        updated = json.loads((proj2 / "sessions-index.json").read_text())
        assert updated["entries"][0]["summary"] == "Found It"


# ── Settings (Auto-Memory) ──────────────────────────────────────────────────


class TestSettings:
    def test_defaults_when_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "SETTINGS_FILE", tmp_path / "nonexistent.json")
        settings = data.get_settings()
        assert settings == {"auto_memory": False}

    def test_reads_existing(self, tmp_path, monkeypatch):
        sf = tmp_path / "settings.json"
        sf.write_text(json.dumps({"auto_memory": True, "extra": "val"}))
        monkeypatch.setattr(data, "SETTINGS_FILE", sf)
        settings = data.get_settings()
        assert settings["auto_memory"] is True
        assert settings["extra"] == "val"

    def test_corrupt_json_returns_defaults(self, tmp_path, monkeypatch):
        sf = tmp_path / "settings.json"
        sf.write_text("{bad json")
        monkeypatch.setattr(data, "SETTINGS_FILE", sf)
        settings = data.get_settings()
        assert settings == {"auto_memory": False}

    def test_toggle_on(self, tmp_path, monkeypatch):
        sf = tmp_path / "settings.json"
        monkeypatch.setattr(data, "SETTINGS_FILE", sf)
        new_state, err = data.toggle_auto_memory()
        assert new_state is True
        assert err == ""
        assert data.is_auto_memory_enabled() is True

    def test_toggle_off(self, tmp_path, monkeypatch):
        sf = tmp_path / "settings.json"
        sf.write_text(json.dumps({"auto_memory": True}))
        monkeypatch.setattr(data, "SETTINGS_FILE", sf)
        new_state, err = data.toggle_auto_memory()
        assert new_state is False
        assert err == ""
        assert data.is_auto_memory_enabled() is False

    def test_toggle_preserves_other_settings(self, tmp_path, monkeypatch):
        sf = tmp_path / "settings.json"
        sf.write_text(json.dumps({"auto_memory": False, "theme": "dark"}))
        monkeypatch.setattr(data, "SETTINGS_FILE", sf)
        new_state, err = data.toggle_auto_memory()
        assert err == ""
        settings = json.loads(sf.read_text())
        assert settings["auto_memory"] is True
        assert settings["theme"] == "dark"

    def test_is_auto_memory_enabled_default(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "SETTINGS_FILE", tmp_path / "none.json")
        assert data.is_auto_memory_enabled() is False


# ── Auto-memory file detection ──────────────────────────────────────────────


class TestAutoMemoryFiles:
    def test_finds_auto_subdirectory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "-Users-test"
        mem = proj / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("# Manual memory\n")
        auto = mem / "auto"
        auto.mkdir()
        (auto / "2026-03-14.md").write_text("# Auto\n")
        files = data.get_memory_files()
        assert len(files) == 2
        names = {f.name for f in files}
        assert "MEMORY.md" in names
        assert "2026-03-14.md" in names
        # Auto file path should contain /auto/
        auto_files = [f for f in files if "/auto/" in str(f.path)]
        assert len(auto_files) == 1

    def test_no_auto_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "-Users-test"
        mem = proj / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("# Memory\n")
        files = data.get_memory_files()
        assert len(files) == 1


# ── Deferred Items ──────────────────────────────────────────────────────────


class TestDeferredItems:
    def test_finds_deferred(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        auto = tmp_path / "-Users-test" / "memory" / "auto"
        auto.mkdir(parents=True)
        (auto / "2026-03-14.md").write_text(
            "# Auto-Memory\n\n"
            "### 10:30 (session: abc12345)\n"
            "- **Deferred:** Add retry logic — not urgent (feature branch)\n"
            "- **Finding:** Something else\n"
        )
        items = data.get_deferred_items()
        assert len(items) == 1
        assert items[0].task == "Add retry logic"
        assert items[0].reason == "not urgent"
        assert items[0].context == "feature branch"
        assert items[0].date == "2026-03-14"

    def test_no_deferred(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        auto = tmp_path / "-Users-test" / "memory" / "auto"
        auto.mkdir(parents=True)
        (auto / "2026-03-14.md").write_text(
            "- **Finding:** Some finding\n"
            "- **Decision:** Some decision\n"
        )
        items = data.get_deferred_items()
        assert len(items) == 0

    def test_no_auto_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        assert data.get_deferred_items() == []

    def test_multiple_deferred(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        auto = tmp_path / "-Users-test" / "memory" / "auto"
        auto.mkdir(parents=True)
        (auto / "2026-03-14.md").write_text(
            "- **Deferred:** Task one — reason one\n"
            "- **Deferred:** Task two — reason two (ctx)\n"
        )
        items = data.get_deferred_items()
        assert len(items) == 2
        assert items[0].task == "Task one"
        assert items[1].task == "Task two"
        assert items[1].context == "ctx"

    def test_deferred_no_reason(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        auto = tmp_path / "-Users-test" / "memory" / "auto"
        auto.mkdir(parents=True)
        (auto / "2026-03-14.md").write_text(
            "- **Deferred:** Simple task\n"
        )
        items = data.get_deferred_items()
        assert len(items) == 1
        assert items[0].task == "Simple task"
        assert items[0].reason == ""
        assert items[0].context == ""


# ── Timeline ────────────────────────────────────────────────────────────────


class TestTimeline:
    def test_timeline_with_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "-Users-test"
        proj.mkdir()
        jsonl = proj / "abc.jsonl"
        jsonl.write_text(_make_jsonl_line("user", "hello") + "\n")
        idx = {"version": 1, "entries": [{
            "sessionId": "abc", "fullPath": str(jsonl),
            "summary": "Test session", "firstPrompt": "hello",
            "messageCount": 5, "created": "2026-03-14T10:00:00Z",
            "modified": "2026-03-14T11:00:00Z", "gitBranch": "main",
            "projectPath": "", "isSidechain": False,
        }]}
        (proj / "sessions-index.json").write_text(json.dumps(idx))
        entries = data.get_session_timeline()
        session_entries = [e for e in entries if e.entry_type == "session"]
        assert len(session_entries) >= 1
        assert session_entries[0].date == "2026-03-14"
        assert "Test session" in session_entries[0].summary

    def test_timeline_with_auto_memory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "-Users-test"
        auto = proj / "memory" / "auto"
        auto.mkdir(parents=True)
        (auto / "2026-03-14.md").write_text("# Auto\n")
        entries = data.get_session_timeline()
        mem_entries = [e for e in entries if e.entry_type == "memory"]
        assert len(mem_entries) >= 1

    def test_timeline_project_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj1 = tmp_path / "-Users-test-ProjectA"
        (proj1 / "memory" / "auto").mkdir(parents=True)
        (proj1 / "memory" / "auto" / "2026-03-14.md").write_text("# A\n")
        proj2 = tmp_path / "-Users-test-ProjectB"
        (proj2 / "memory" / "auto").mkdir(parents=True)
        (proj2 / "memory" / "auto" / "2026-03-14.md").write_text("# B\n")
        # Filter by one project
        entries_a = data.get_session_timeline("ProjectA")
        entries_b = data.get_session_timeline("ProjectB")
        assert all(e.project == "ProjectA" for e in entries_a)
        assert all(e.project == "ProjectB" for e in entries_b)

    def test_timeline_sorted_desc(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        proj = tmp_path / "-Users-test"
        auto = proj / "memory" / "auto"
        auto.mkdir(parents=True)
        (auto / "2026-03-12.md").write_text("# Old\n")
        (auto / "2026-03-14.md").write_text("# New\n")
        entries = data.get_session_timeline()
        mem_entries = [e for e in entries if e.entry_type == "memory"]
        assert len(mem_entries) >= 2
        dates = [e.date for e in mem_entries]
        assert dates == sorted(dates, reverse=True)

    def test_get_timeline_projects(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        (tmp_path / "-Users-test-ProjA").mkdir()
        (tmp_path / "-Users-test-ProjB").mkdir()
        projects = data.get_timeline_projects()
        assert len(projects) >= 2


class TestDashboardSessions:
    def _make_session(self, tmp_path, session_id, mtime_offset_seconds=0):
        """Create a SessionEntry with a real file for mtime checking."""
        jsonl = tmp_path / f"{session_id}.jsonl"
        jsonl.write_text('{"type":"user"}\n')
        now = time.time()
        os.utime(jsonl, (now - mtime_offset_seconds, now - mtime_offset_seconds))
        return data.SessionEntry(
            session_id=session_id,
            project="test-proj",
            full_path=jsonl,
            summary=f"Session {session_id}",
            first_prompt="hello",
            message_count=10,
            created="2026-03-14T10:00:00Z",
            modified="2026-03-14T11:00:00Z",
            git_branch="",
            is_sidechain=False,
            file_size=100,
        )

    def test_active_session_detected(self, tmp_path, monkeypatch):
        """A session modified <10 minutes ago should appear as live."""
        monkeypatch.setattr(data, "_get_live_processes", lambda: [])
        s = self._make_session(tmp_path, "active1", mtime_offset_seconds=60)
        result = data.get_dashboard_sessions([s])
        assert len(result) == 1
        assert result[0]["age_label"] == "1m ago"

    def test_inactive_session_excluded(self, tmp_path, monkeypatch):
        """A session >10 min old should NOT appear when no Claude processes."""
        monkeypatch.setattr(data, "_get_live_processes", lambda: [])
        s = self._make_session(tmp_path, "old1", mtime_offset_seconds=1800)
        result = data.get_dashboard_sessions([s])
        assert len(result) == 0

    def test_just_now_label(self, tmp_path, monkeypatch):
        """A session modified <1 min ago should say 'just now'."""
        monkeypatch.setattr(data, "_get_live_processes", lambda: [])
        s = self._make_session(tmp_path, "fresh1", mtime_offset_seconds=10)
        result = data.get_dashboard_sessions([s])
        assert len(result) == 1
        assert result[0]["age_label"] == "just now"

    def test_empty_sessions(self, monkeypatch):
        """No sessions returns empty list."""
        monkeypatch.setattr(data, "_get_live_processes", lambda: [])
        assert data.get_dashboard_sessions([]) == []

    def test_max_recent_limit(self, tmp_path, monkeypatch):
        """Should respect max_recent limit."""
        monkeypatch.setattr(data, "_get_live_processes", lambda: [])
        sessions = [
            self._make_session(tmp_path, f"s{i}", mtime_offset_seconds=i * 10)
            for i in range(15)
        ]
        result = data.get_dashboard_sessions(sessions, max_recent=5)
        assert len(result) <= 5

    def test_process_detection_includes_idle_sessions(self, tmp_path, monkeypatch):
        """Only sessions with matched processes or recently active show up."""
        monkeypatch.setattr(data, "_get_live_processes", lambda: [
            data.LiveProcess(pid=1001, tty="/dev/ttys001", cpu_percent=5.0,
                             uptime="1h 0m", tab_name="✳ Session A (server)", children=[]),
            data.LiveProcess(pid=1002, tty="/dev/ttys002", cpu_percent=0.0,
                             uptime="30m", tab_name="✳ Session B (server)", children=[]),
        ])
        sessions = [
            self._make_session(tmp_path, f"s{i}", mtime_offset_seconds=i * 600)
            for i in range(5)
        ]
        result = data.get_dashboard_sessions(sessions)
        # Only s0 (0s old) and s1 (600s = 10min, at boundary) are recent enough;
        # s2-s4 (20-40min old) have no process match so they're excluded
        assert len(result) <= 2


# ── Task operations ──────────────────────────────────────────────────────────


class TestTaskOperations:
    def _make_task(self, tmp_path, session_dir="sess1", task_id="1",
                   status="in_progress"):
        task_dir = tmp_path / session_dir
        task_dir.mkdir(parents=True, exist_ok=True)
        raw = {
            "id": task_id,
            "subject": f"Test task {task_id}",
            "description": "A test task",
            "status": status,
            "activeForm": "Testing",
            "blocks": [],
            "blockedBy": [],
        }
        (task_dir / f"{task_id}.json").write_text(json.dumps(raw))
        return data.Task(
            id=task_id, subject=f"Test task {task_id}",
            description="A test task", status=status,
            active_form="Testing", session_dir=session_dir,
        )

    def test_complete_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TASKS_DIR", tmp_path)
        task = self._make_task(tmp_path)
        ok, err = data.update_task_status(task, "completed")
        assert ok
        raw = json.loads((tmp_path / "sess1" / "1.json").read_text())
        assert raw["status"] == "completed"

    def test_delete_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TASKS_DIR", tmp_path)
        task = self._make_task(tmp_path)
        ok, err = data.delete_task(task)
        assert ok
        assert not (tmp_path / "sess1" / "1.json").exists()

    def test_complete_missing_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TASKS_DIR", tmp_path)
        task = data.Task(id="99", subject="missing", description="",
                         status="pending", session_dir="nosuch")
        ok, err = data.update_task_status(task, "completed")
        assert not ok
        assert "not found" in err

    def test_delete_missing_task(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "TASKS_DIR", tmp_path)
        task = data.Task(id="99", subject="missing", description="",
                         status="pending", session_dir="nosuch")
        ok, err = data.delete_task(task)
        assert not ok
        assert "not found" in err


# ── _match_procs_to_sessions ────────────────────────────────────────────────


class TestMatchProcsToSessions:
    """Tests for the two-pass process-session matching algorithm."""

    def _make_session(self, tmp_path, session_id, first_ts, last_ts=None):
        """Create a SessionEntry with JSONL that has specific timestamps."""
        jsonl = tmp_path / f"{session_id}.jsonl"
        first_line = json.dumps({"type": "system", "timestamp": first_ts})
        last_line = json.dumps({"type": "user", "timestamp": last_ts or first_ts})
        if last_ts and last_ts != first_ts:
            jsonl.write_text(f"{first_line}\n{last_line}\n")
        else:
            jsonl.write_text(f"{first_line}\n")
        return data.SessionEntry(
            session_id=session_id, project="test", full_path=jsonl,
            summary="", first_prompt="hi", message_count=5,
            created=first_ts, modified=last_ts or first_ts,
            git_branch="", is_sidechain=False, file_size=100,
        )

    def _make_proc(self, pid, tty, start_epoch, tab_name=""):
        return data.LiveProcess(
            pid=pid, tty=tty, cpu_percent=1.0, uptime="5m",
            tab_name=tab_name, children=[], start_epoch=start_epoch,
        )

    def test_empty_procs(self, tmp_path):
        s = self._make_session(tmp_path, "s1", "2026-03-16T10:00:00Z")
        result = data._match_procs_to_sessions([], [s])
        assert result == {}

    def test_empty_sessions(self):
        p = self._make_proc(100, "/dev/ttys001", 1773655200.0)
        result = data._match_procs_to_sessions([p], [])
        assert result == {}

    def test_new_session_matched_by_creation_time(self, tmp_path):
        """Pass 1: new session — first-line timestamp within 120s of process start."""
        start = 1773655200.0  # some epoch
        ts = "2026-03-16T10:00:05Z"  # 5s after start
        p = self._make_proc(100, "/dev/ttys001", start)
        s = self._make_session(tmp_path, "new1", ts)
        result = data._match_procs_to_sessions([p], [s])
        assert "new1" in result
        assert result["new1"].pid == 100

    def test_resumed_session_matched_by_last_activity(self, tmp_path):
        """Pass 2: resumed session — first-line is old but last-line is near process start."""
        start = 1773655200.0
        old_ts = "2026-03-10T10:00:00Z"  # days ago
        recent_ts = "2026-03-16T10:00:03Z"  # 3s after start
        p = self._make_proc(200, "/dev/ttys002", start)
        s = self._make_session(tmp_path, "resumed1", old_ts, last_ts=recent_ts)
        result = data._match_procs_to_sessions([p], [s])
        assert "resumed1" in result
        assert result["resumed1"].pid == 200

    def test_new_preferred_over_resumed(self, tmp_path):
        """New session (pass 1) should be matched before resumed session (pass 2)."""
        start = 1773655200.0
        p = self._make_proc(300, "/dev/ttys003", start)
        s_new = self._make_session(tmp_path, "new1", "2026-03-16T10:00:02Z")
        s_old = self._make_session(tmp_path, "old1", "2026-03-10T10:00:00Z",
                                    last_ts="2026-03-16T10:00:01Z")
        result = data._match_procs_to_sessions([p], [s_new, s_old])
        assert "new1" in result
        assert result["new1"].pid == 300

    def test_two_procs_no_swap(self, tmp_path):
        """Two procs started close together should not swap sessions."""
        p1 = self._make_proc(101, "/dev/ttys001", 1773655200.0)
        p2 = self._make_proc(102, "/dev/ttys002", 1773655203.0)  # 3s later
        s1 = self._make_session(tmp_path, "s1", "2026-03-16T10:00:01Z")  # matches p1
        s2 = self._make_session(tmp_path, "s2", "2026-03-16T10:00:04Z")  # matches p2
        result = data._match_procs_to_sessions([p1, p2], [s1, s2])
        assert result.get("s1", data.LiveProcess(pid=0, tty="", cpu_percent=0, uptime="", tab_name="", children=[])).pid == 101
        assert result.get("s2", data.LiveProcess(pid=0, tty="", cpu_percent=0, uptime="", tab_name="", children=[])).pid == 102

    def test_unmatched_proc_uses_pass3(self, tmp_path):
        """Process matched by pass 3 when JSONL has entry after process start."""
        p = self._make_proc(500, "/dev/ttys005", 1773655200.0)
        # Session has a user entry 30s after process start
        s = self._make_session(tmp_path, "resumed", "2025-01-01T00:00:00Z",
                               last_ts="2026-03-16T10:00:30Z")  # 30s after start
        result = data._match_procs_to_sessions([p], [s])
        assert "resumed" in result
        assert result["resumed"].pid == 500

    def test_ancient_session_not_matched_if_unmodified(self, tmp_path):
        """A session not modified after process start is NOT matched."""
        p = self._make_proc(500, "/dev/ttys005", time.time() + 3600)  # proc starts "in future"
        s = self._make_session(tmp_path, "ancient", "2025-01-01T00:00:00Z")
        result = data._match_procs_to_sessions([p], [s])
        assert "ancient" not in result  # file mtime < proc start

    def test_mixed_new_and_resumed(self, tmp_path):
        """One new session + one resumed session, two procs — both matched correctly."""
        p1 = self._make_proc(101, "/dev/ttys001", 1773655200.0)
        p2 = self._make_proc(102, "/dev/ttys002", 1773655210.0)
        s_new = self._make_session(tmp_path, "new1", "2026-03-16T10:00:12Z")  # p2
        s_resumed = self._make_session(tmp_path, "res1", "2026-03-10T00:00:00Z",
                                        last_ts="2026-03-16T10:00:02Z")  # p1
        result = data._match_procs_to_sessions([p1, p2], [s_new, s_resumed])
        assert result.get("new1") is not None and result["new1"].pid == 102
        assert result.get("res1") is not None and result["res1"].pid == 101

    def test_zero_start_epoch_excluded(self, tmp_path):
        """Procs with start_epoch=0 should be excluded from matching."""
        p = self._make_proc(999, "/dev/ttys001", 0.0)
        s = self._make_session(tmp_path, "s1", "2026-03-16T10:00:00Z")
        result = data._match_procs_to_sessions([p], [s])
        assert result == {}

    @patch("cockpit.data._stamp_iterm_session_ids")
    def test_pass0_stamped_tab_matched(self, mock_stamp, tmp_path):
        """Pass 0: process with §session_id in tab name is matched directly."""
        p = self._make_proc(100, "/dev/ttys001", 1773655200.0,
                            tab_name="My Project §abcdef12")
        s = self._make_session(tmp_path, "abcdef12-3456-7890-abcd-ef1234567890",
                               "2025-01-01T00:00:00Z")
        result = data._match_procs_to_sessions([p], [s])
        sid = "abcdef12-3456-7890-abcd-ef1234567890"
        assert sid in result
        assert result[sid].pid == 100

    @patch("cockpit.data._stamp_iterm_session_ids")
    def test_pass0_prevents_pass1_reuse(self, mock_stamp, tmp_path):
        """Pass 0 match prevents the same proc/session being reused in Pass 1."""
        p1 = self._make_proc(100, "/dev/ttys001", 1773655200.0,
                             tab_name="Project §abcdef12")
        p2 = self._make_proc(200, "/dev/ttys002", 1773655200.0)
        s1 = self._make_session(tmp_path, "abcdef12-0000-0000-0000-000000000000",
                                "2026-03-16T10:00:02Z")
        s2 = self._make_session(tmp_path, "newone00-0000-0000-0000-000000000000",
                                "2026-03-16T10:00:02Z")
        result = data._match_procs_to_sessions([p1, p2], [s1, s2])
        # p1 matched to s1 via Pass 0, p2 matched to s2 via Pass 1
        assert result["abcdef12-0000-0000-0000-000000000000"].pid == 100
        assert result["newone00-0000-0000-0000-000000000000"].pid == 200


class TestGetJsonlCreationTime:
    def test_valid_timestamp(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"timestamp":"2026-03-16T10:00:00Z","type":"system"}\n')
        t = data._get_jsonl_creation_time(f)
        assert abs(t - 1773655200.0) < 2  # ~10am UTC

    def test_missing_timestamp_falls_back_to_mtime(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"type":"system"}\n')
        t = data._get_jsonl_creation_time(f)
        assert t > 0  # fallback to mtime

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "missing.jsonl"
        assert data._get_jsonl_creation_time(f) == 0.0

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        t = data._get_jsonl_creation_time(f)
        assert t > 0  # falls back to mtime


class TestGetJsonlLastActivity:
    def test_last_line_timestamp(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text(
            '{"timestamp":"2026-03-10T10:00:00Z","type":"system"}\n'
            '{"timestamp":"2026-03-16T10:00:00Z","type":"user"}\n'
        )
        t = data._get_jsonl_last_activity(f)
        assert abs(t - 1773655200.0) < 2

    def test_single_line(self, tmp_path):
        f = tmp_path / "test.jsonl"
        f.write_text('{"timestamp":"2026-03-16T10:00:00Z","type":"system"}\n')
        t = data._get_jsonl_last_activity(f)
        assert abs(t - 1773655200.0) < 2

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.jsonl"
        f.write_text("")
        assert data._get_jsonl_last_activity(f) == 0.0

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "missing.jsonl"
        t = data._get_jsonl_last_activity(f)
        assert t == 0.0


# ── Atomic Write ───────────────────────────────────────────────────────────


class TestAtomicWrite:
    def test_happy_path(self, tmp_path):
        target = tmp_path / "test.txt"
        data._atomic_write(target, "hello world")
        assert target.read_text() == "hello world"

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "test.txt"
        target.write_text("old content")
        data._atomic_write(target, "new content")
        assert target.read_text() == "new content"

    def test_creates_parent_dirs(self, tmp_path):
        target = tmp_path / "sub" / "dir" / "test.txt"
        data._atomic_write(target, "nested")
        assert target.read_text() == "nested"

    def test_no_temp_file_left_on_success(self, tmp_path):
        target = tmp_path / "test.txt"
        data._atomic_write(target, "content")
        tmps = list(tmp_path.glob("*.tmp"))
        assert len(tmps) == 0

    def test_cleanup_on_write_error(self, tmp_path):
        target = tmp_path / "test.txt"
        # Make parent read-only after creating the temp file to trigger error
        with patch("os.fdopen", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                data._atomic_write(target, "content")
        # No temp files should be left
        tmps = list(tmp_path.glob("*.tmp"))
        assert len(tmps) == 0

    def test_concurrent_writes_no_corruption(self, tmp_path):
        target = tmp_path / "test.txt"
        errors = []

        def writer(value):
            try:
                for _ in range(20):
                    data._atomic_write(target, f"value={value}\n")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        # File should be valid (not a mix of writes)
        content = target.read_text()
        assert content.startswith("value=")
        assert content.endswith("\n")

    def test_unicode_content(self, tmp_path):
        target = tmp_path / "test.txt"
        data._atomic_write(target, "Hello 世界 🎉")
        assert target.read_text() == "Hello 世界 🎉"

    def test_empty_content(self, tmp_path):
        target = tmp_path / "test.txt"
        data._atomic_write(target, "")
        assert target.read_text() == ""


# ── Path Validation ────────────────────────────────────────────────────────


class TestPathValidation:
    def test_safe_child(self, tmp_path):
        parent = tmp_path / "allowed"
        parent.mkdir()
        child = parent / "file.txt"
        child.write_text("ok")
        assert data._is_safe_child(child, parent) is True

    def test_prefix_attack_blocked(self, tmp_path):
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        shadow = tmp_path / "allowed-shadow"
        shadow.mkdir()
        evil = shadow / "evil.txt"
        evil.write_text("bad")
        # This was the bug: str.startswith would pass this
        assert data._is_safe_child(evil, allowed) is False

    def test_outside_parent(self, tmp_path):
        parent = tmp_path / "allowed"
        parent.mkdir()
        outside = tmp_path / "other" / "file.txt"
        assert data._is_safe_child(outside, parent) is False

    def test_same_dir(self, tmp_path):
        assert data._is_safe_child(tmp_path, tmp_path) is True

    def test_save_file_path_validation(self, tmp_path):
        """_save_file_with_mtime_check should block prefix attacks."""
        allowed = tmp_path / "allowed"
        allowed.mkdir()
        shadow = tmp_path / "allowed-shadow"
        shadow.mkdir()
        target = shadow / "evil.txt"
        target.write_text("original")
        ok, err = data._save_file_with_mtime_check(
            target, "hacked", target.stat().st_mtime,
            allowed_parents=[allowed],
        )
        assert ok is False
        assert "not in allowed" in err.lower()
        assert target.read_text() == "original"


# ── Error Propagation ─────────────────────────────────────────────────────


class TestErrorPropagation:
    def test_toggle_auto_memory_returns_error_on_write_fail(self, tmp_path, monkeypatch):
        sf = tmp_path / "settings.json"
        monkeypatch.setattr(data, "SETTINGS_FILE", sf)
        # Make parent read-only to prevent writes
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)
        monkeypatch.setattr(data, "SETTINGS_FILE", readonly_dir / "settings.json")
        new_state, err = data.toggle_auto_memory()
        assert err != ""
        assert "Failed" in err
        readonly_dir.chmod(0o755)  # Cleanup

    def test_toggle_pin_returns_error_on_write_fail(self, tmp_path, monkeypatch):
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)
        monkeypatch.setattr(data, "PINNED_FILE", readonly_dir / "pinned.json")
        new_state, err = data.toggle_pin("test-session")
        assert err != ""
        assert "Failed" in err
        readonly_dir.chmod(0o755)

    def test_toggle_pin_plan_returns_error_on_write_fail(self, tmp_path, monkeypatch):
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        readonly_dir.chmod(0o444)
        monkeypatch.setattr(data, "PINNED_PLANS_FILE", readonly_dir / "pinned.json")
        new_state, err = data.toggle_pin_plan("test-plan")
        assert err != ""
        assert "Failed" in err
        readonly_dir.chmod(0o755)


# ── Resource Bounds ────────────────────────────────────────────────────────


class TestResourceBounds:
    def test_memory_files_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        monkeypatch.setattr(data, "MAX_PROJECTS_SCAN", 2)
        # Create 5 projects with memory
        for i in range(5):
            mem = tmp_path / f"-proj-{i}" / "memory"
            mem.mkdir(parents=True)
            (mem / "MEMORY.md").write_text(f"# Proj {i}\n")
        files = data.get_memory_files()
        # Should scan at most 2 projects worth of memory dirs
        projects = {f.project for f in files}
        assert len(projects) <= 2

    def test_session_list_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(data, "PROJECTS_DIR", tmp_path)
        monkeypatch.setattr(data, "MAX_PROJECTS_SCAN", 3)
        for i in range(10):
            proj = tmp_path / f"-proj-{i}"
            proj.mkdir()
        projects = data.get_timeline_projects()
        assert len(projects) <= 3

    def test_constants_exist(self):
        """Verify all tuning constants are defined at module level."""
        assert hasattr(data, "TAIL_CHUNK_SIZE")
        assert hasattr(data, "NEW_SESSION_MATCH_DELTA_SECS")
        assert hasattr(data, "RESUMED_SESSION_MATCH_DELTA_SECS")
        assert hasattr(data, "MATCH_SESSIONS_LIMIT")
        assert hasattr(data, "DASHBOARD_MAX_SESSIONS")
        assert hasattr(data, "SESSION_ACTIVE_THRESHOLD_MINS")
        assert hasattr(data, "MAX_PROJECTS_SCAN")
        assert hasattr(data, "MAX_FILES_PER_DIR")
        assert hasattr(data, "CONTEXT_CHARS_ESTIMATE")
        assert hasattr(data, "CHARS_PER_TOKEN")
