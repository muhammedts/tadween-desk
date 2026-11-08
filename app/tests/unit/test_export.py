"""Save As export: one saved revision to a UTF-8 .txt or .md file. Real SQLite
snapshots and real files in a temp folder; the disk-full case is injected at fsync."""

from __future__ import annotations

import errno
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from meeting_minutes import export
from meeting_minutes.export import ExportError, UnsavedLine, render, suggest_filename, write_file
from meeting_minutes.storage.store import TranscriptStore

EXPORTED_AT = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ARABIC = "مرحبا، الميزانية جاهزة"
MIXED = "الموعد النهائي is Friday"


def saved_meeting(tmp_path: Path, *, complete: bool = True, title: str = "Weekly sync") -> TranscriptStore:
    store = TranscriptStore(tmp_path / "meetings.db")
    store.create_meeting("m1", title=title, timezone_name="Africa/Cairo", started_at="2026-10-30T09:05:00.000+00:00")
    for source in ("mic", "system"):
        store.save_epoch("m1", source=source, epoch=1, device_id=source, device_label=source, native_rate=48_000,
                         native_channels=1, start_ms=0, end_ms=None)
    for source, start, text, language in (("system", 1_000, "hello everyone", "en"), ("mic", 65_000, ARABIC, "ar"),
                                          ("system", 3_725_000, "budget <b>[link](x)</b> # *now*", "en")):
        store.add_final("m1", source=source, epoch=1, start_ms=start, end_ms=start + 900, text=text,
                        language=language, engine_id="fake", model_hash="0" * 64)
    store.add_timeline("m1", "notice", "mic_excluded", 10_000, None, "mic", {"discarded_pending_ms": 0})
    if not complete:
        store.add_timeline("m1", "gap", "stt_backlog", 20_000, 24_000, "system", {"stage": "speech", "loss": True})
    store.add_timeline("m1", "notice", "finalized", 3_800_000, None, None,
                       {"complete": complete, "drain_complete": True, "text_not_saved": 0})
    store.set_status("m1", "stopped", ended=True)
    return store


def test_suggested_names_are_windows_safe_and_keep_the_title_script() -> None:
    started = "2026-10-30T09:05:00.000+00:00"
    date = export.local_time(started)[:10]
    assert suggest_filename("Budget: Q4 / plan?", started, "txt") == f"Budget- Q4 - plan {date}.txt"
    assert suggest_filename("اجتماع الميزانية", started, "md") == f"اجتماع الميزانية {date}.md"
    assert suggest_filename(f"Meeting {date} 11:05", started, "txt") == f"Meeting {date} 11-05.txt"  # no 2nd date
    assert suggest_filename("  ...  ", started, "txt") == f"Meeting transcript {date}.txt"
    assert suggest_filename("CON", started, "txt") == f"CON {date}.txt"
    assert suggest_filename("aux.notes", started, "txt").startswith("_aux.notes")
    assert len(suggest_filename("x" * 300, started, "txt")) <= export.NAME_MAX + len(f" {date}.txt")
    assert suggest_filename("tab\there\x00", started, "txt") == f"tab-here {date}.txt"


def test_text_export_is_the_saved_revision_with_labels_original_script_and_coverage(tmp_path: Path) -> None:
    store = saved_meeting(tmp_path)
    [_, mic, _] = store.segments("m1")
    store.correct_segment(mic.id, MIXED)
    snapshot = store.snapshot("m1")
    text = render(snapshot, "txt", exported_at=EXPORTED_AT)
    path = tmp_path / "out.txt"
    write_file(path, text)
    raw = path.read_bytes()
    assert raw.decode("utf-8") == text and MIXED.encode("utf-8") in raw  # UTF-8, no BOM, exact script
    assert not raw.startswith(b"\xef\xbb\xbf")
    lines = text.splitlines()
    assert lines[0] == "Weekly sync" and "INCOMPLETE" not in text
    body = lines[lines.index("TRANSCRIPT") + 3:]  # after the underline and a blank line
    assert body[:9] == ["[00:01] System", "hello everyone", "",
                        "[01:05] Mic · corrected", MIXED, "",
                        "[1:02:05] System", "budget <b>[link](x)</b> # *now*", ""]  # text is kept as said
    assert ARABIC not in text  # only the current revision, not the replaced one
    assert f"Saved revision: {snapshot.meeting.transcript_revision} (exported" in text
    assert "Status: Stopped" in text and "Coverage: Complete" in text
    assert "not speaker names" in text
    assert "00:10 Mic: mic excluded (muted in this app)" in text


def test_markdown_export_escapes_markup_and_marks_an_incomplete_meeting(tmp_path: Path) -> None:
    store = saved_meeting(tmp_path, complete=False, title="# Plan *Q4*")
    snapshot = store.snapshot("m1")
    md = render(snapshot, "md", exported_at=EXPORTED_AT)
    lines = md.splitlines()
    assert lines[0] == r"# \# Plan \*Q4\*"
    assert lines[2].startswith("> **INCOMPLETE TRANSCRIPT")
    assert r"budget \<b\>\[link\](x)\</b\> \# \*now\*" in lines  # no HTML, link or emphasis
    assert "**\\[01:05\\] Mic**  " in lines and ARABIC in lines
    assert "- **Coverage:** Stopped normally, but some intervals have no text" in md
    assert any(line.startswith("- 00:20–00:24 System: recognition fell behind") for line in lines)


def test_unsaved_text_is_listed_apart_and_makes_the_file_incomplete(tmp_path: Path) -> None:
    snapshot = saved_meeting(tmp_path).snapshot("m1")
    text = render(snapshot, "txt", unsaved=[UnsavedLine("system", 4_000_000, "deadline moved", "disk_full"),
                                            UnsavedLine("mic", 4_001_000, None, "disk_full")])
    head, _, tail = text.partition("NOT SAVED IN HISTORY")
    assert text.startswith("Weekly sync") and "INCOMPLETE TRANSCRIPT" in head and "deadline moved" not in head
    assert "[1:06:40] System — NOT SAVED (disk_full)\ndeadline moved" in tail
    assert "[1:06:41] Mic — NOT SAVED (disk_full)\n(text not kept)" in tail


def test_overwrite_replaces_the_file_and_failed_writes_leave_it_unchanged(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "notes.txt"
    target.write_text("old", encoding="utf-8")
    write_file(target, "new " + ARABIC)
    assert target.read_text(encoding="utf-8") == "new " + ARABIC

    os.chmod(target, stat.S_IREAD)  # a read-only existing file
    try:
        with pytest.raises(ExportError) as denied:
            write_file(target, "replacement")
        assert denied.value.code == "access_denied" and str(tmp_path) not in str(denied.value)
        assert target.read_text(encoding="utf-8") == "new " + ARABIC
    finally:
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)

    def full(_fd):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(export.os, "fsync", full)
    with pytest.raises(ExportError) as no_space:
        write_file(target, "x" * 10_000)
    assert no_space.value.code == "disk_full"
    monkeypatch.undo()
    assert target.read_text(encoding="utf-8") == "new " + ARABIC
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]  # no temporary file left behind

    for path, code in ((tmp_path / "missing" / "a.txt", "folder_missing"), (tmp_path / "a<b.txt", "invalid_name"),
                       (tmp_path, "is_folder")):
        with pytest.raises(ExportError) as failed:
            write_file(path, "text")
        assert failed.value.code == code
    assert [p.name for p in tmp_path.iterdir()] == ["notes.txt"]
