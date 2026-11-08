"""Save As: one saved transcript revision as a UTF-8 text or Markdown file.

The file is made from a `MeetingSnapshot` (one consistent read of the store), never
from what the window shows, so it is exactly one saved revision: current corrected
text, original script, source/time labels and the same coverage wording as the
saved view. Text the store refused and that is still held in memory is added in a
separate section labelled NOT SAVED, never mixed into the saved transcript.

Writing goes to a temporary file in the chosen folder that replaces the target
only once fully written, so a failed write (disk full, read-only, file open in
another program) leaves an existing file as it was. Errors carry fixed codes only.
Nothing here logs content or paths, or touches audio or the network.
"""

from __future__ import annotations

import errno
import os
import re
import tempfile
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from meeting_minutes.storage.store import MeetingSnapshot
from meeting_minutes.ui.wording import (
    SOURCE_NAMES,
    STATE_TEXT,
    clock_text,
    coverage_summary,
    engine_text,
    event_line,
    local_time,
    quality_note,
)

FORMATS = ("txt", "md")
NAME_MAX = 80  # characters of the suggested name before the date
_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
# Markdown characters that could turn transcript text into markup (links, HTML, emphasis).
_MD_INLINE = re.compile(r"([\\`*_\[\]<>#|~&!])")
_MD_LINE_START = re.compile(r"^(\s*)([-+=>]|\d+[.)])")


class ExportError(RuntimeError):
    """A failed file write. `code` is fixed text with no content or path."""

    def __init__(self, code: str) -> None:
        super().__init__(f"transcript file not written: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class UnsavedLine:
    """A final the store refused, still in memory (text None: not kept, cap reached)."""

    source: str
    start_ms: int
    text: str | None
    code: str


def suggest_filename(title: str, started_at: str, fmt: str) -> str:
    """A Windows-safe name from the title and the meeting's local date."""
    name = _UNSAFE.sub("-", unicodedata.normalize("NFC", title))
    name = " ".join(name.split())[:NAME_MAX].strip(" .-")
    date = local_time(started_at)[:10]
    if not name:
        name = "Meeting transcript"
    if date not in name:
        name = f"{name} {date}"
    if name.split(".")[0].upper() in _RESERVED:
        name = "_" + name
    return f"{name}.{fmt}"


def is_complete(snapshot: MeetingSnapshot) -> bool:
    finalized = next((t for t in reversed(snapshot.timeline) if t.code == "finalized"), None)
    return snapshot.meeting.status == "stopped" and finalized is not None and bool(finalized.metadata.get("complete"))


def render(snapshot: MeetingSnapshot, fmt: str, *, unsaved: Sequence[UnsavedLine] = (),
           exported_at: datetime | None = None) -> str:
    if fmt not in FORMATS:
        raise ValueError(fmt)
    md = fmt == "md"
    m = snapshot.meeting
    complete = is_complete(snapshot) and not unsaved
    when = (exported_at or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    facts = [
        ("Started", local_time(m.started_at) + " (this computer's time)"),
        ("Ended", local_time(m.ended_at) if m.ended_at else "not recorded"),
        ("Status", STATE_TEXT.get(m.status, m.status)),
        ("Coverage", coverage_summary(m.status, snapshot.timeline)),
        ("Saved revision", f"{m.transcript_revision} (exported {when})"),
        ("Speech model", engine_text(_finalized_engine(snapshot))),
    ]
    about = ("Source labels say which input the speech came from (Mic = this computer's microphone, System = what "
             "it played); they are not speaker names. Times are from the start of the meeting. Text only: no audio "
             "is saved.")
    warning = None
    if not complete:
        warning = ("INCOMPLETE TRANSCRIPT: some speech may be missing or was not saved. "
                   "See Coverage and the notes at the end.")
    out: list[str] = []
    if md:
        out += [f"# {_md(m.title)}", ""]
        if warning:
            out += [f"> **{_md(warning)}**", ""]
        out += [f"- **{k}:** {_md(v)}" for k, v in facts] + ["", f"*{_md(about)}*", "", "## Transcript", ""]
    else:
        out += [m.title, "=" * max(8, min(len(m.title), 60)), ""]
        if warning:
            out += [warning, ""]
        out += [f"{k}: {v}" for k, v in facts] + ["", about, "", "TRANSCRIPT", "----------", ""]
    if not snapshot.segments:
        out += ["(No saved text.)", ""]
    for seg in snapshot.segments:
        label = f"[{clock_text(seg.start_ms)}] {SOURCE_NAMES.get(seg.source, seg.source)}"
        if seg.origin == "user":
            label += " · corrected"
        label += quality_note(seg.flags)
        out += _entry(label, seg.text, md)
    if unsaved:
        heading = "NOT SAVED in History — kept only in this app's memory when this file was made"
        out += ([f"## {_md(heading)}", ""] if md else [heading.upper(), "-" * 20, ""])
        for line in unsaved:
            label = f"[{clock_text(line.start_ms)}] {SOURCE_NAMES.get(line.source, line.source)} — NOT SAVED ({line.code})"
            out += _entry(label, line.text if line.text is not None else "(text not kept)", md)
    events = [event_line(t) for t in snapshot.timeline]
    if events:
        out += (["## Coverage and events", ""] if md else ["COVERAGE AND EVENTS", "-------------------", ""])
        out += [f"- {_md(e)}" for e in events] if md else events
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def _finalized_engine(snapshot: MeetingSnapshot) -> str | None:
    finalized = next((t for t in reversed(snapshot.timeline) if t.code == "finalized"), None)
    return None if finalized is None else finalized.metadata.get("speech_engine")


def _entry(label: str, text: str, md: bool) -> list[str]:
    if not md:
        return [label, text, ""]
    body = "  \n".join(_md(line) for line in text.splitlines() or [""])
    return [f"**{_md(label)}**  ", body, ""]


def _md(text: str) -> str:
    return _MD_LINE_START.sub(r"\1\\\2", _MD_INLINE.sub(r"\\\1", text))


def write_file(path: Path, text: str) -> None:
    """Write `text` as UTF-8 to `path`, replacing it only once fully written."""
    data = text.encode("utf-8")
    tmp: str | None = None
    try:
        if path.is_dir():
            raise ExportError("is_folder")
        fd, tmp = tempfile.mkstemp(prefix=".meeting-minutes-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as exc:
        raise ExportError(_os_code(exc)) from None
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _os_code(exc: OSError) -> str:
    winerror = getattr(exc, "winerror", None)
    if exc.errno == errno.ENOSPC or winerror in (39, 112):
        return "disk_full"
    if exc.errno == errno.EROFS or winerror == 19:
        return "read_only"
    if isinstance(exc, PermissionError) or winerror in (5, 32, 33):
        return "access_denied"  # protected folder, read-only file, or open in another program
    if isinstance(exc, FileNotFoundError) or winerror in (2, 3):
        return "folder_missing"
    if exc.errno == errno.ENAMETOOLONG or winerror == 206:
        return "name_too_long"
    if exc.errno == errno.EINVAL or winerror == 123:
        return "invalid_name"
    return "write_failed"
