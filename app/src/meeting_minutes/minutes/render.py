"""Minutes as UTF-8 plain text or Markdown.

The header states the meeting, Draft/In review/Approved, version, the transcript
revision used (and whether it is stale), provider/model/token usage and the coverage
notice. Unknown owners/dates read "Not specified". Evidence is rendered as
[mm:ss System] references. Markdown escapes inserted text so it cannot become markup.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from meeting_minutes.storage.store import MeetingRow, MinutesItemRow, MinutesVersionRow
from meeting_minutes.ui.wording import SOURCE_NAMES, clock_text, local_time

NOT_SPECIFIED = "Not specified"
STATE_LABEL = {"draft": "Draft — not reviewed", "review": "In review", "approved": "Approved"}
DECISION_LABEL = {"agreed": "Agreed", "superseded": "Superseded", "uncertain": "Uncertain"}
COMMITMENT_LABEL = {"explicit": "Committed", "proposed": "Suggested — not agreed", "uncertain": "Unclear"}
SECTIONS = (("summary", "Meeting overview"), ("topic", "Discussion points"), ("decision", "Decisions"),
            ("action", "Action items"), ("question", "Open questions"))
CAUTION = ("Generated draft. Check names, owners, dates and decisions against the transcript before sharing. "
           "Mic and System are audio sources, not people.")
_MD_INLINE = re.compile(r"([\\`*_\[\]<>#|~&!])")
_MD_LINE_START = re.compile(r"^(\s*)([-+=>]|\d+[.)])")


def md_escape(text: str) -> str:
    return _MD_LINE_START.sub(r"\1\\\2", _MD_INLINE.sub(r"\\\1", text))


def evidence_label(ev: dict) -> str:
    start = ev.get("start_ms")
    source = SOURCE_NAMES.get(ev.get("source") or "", ev.get("source") or "")
    when = clock_text(start) if isinstance(start, int) else "--:--"
    return f"{when} {source}".strip() + (" (uncertain)" if ev.get("uncertain") else "")


def version_label(version: MinutesVersionRow, latest: bool = False) -> str:
    label = f"Version {version.version}"
    if version.user_edited:
        label += " (your edits)"
    if latest:
        label += " — latest"
    return label


def due_text(item: MinutesItemRow) -> str:
    if item.due_date:
        text = item.due_date
        if item.date_status == "resolved_relative" and item.due_date_text:
            text += f" (from “{item.due_date_text}” — check the date)"
        return text
    if item.due_date_text:
        return f"{NOT_SPECIFIED} (said “{item.due_date_text}”)"
    return NOT_SPECIFIED


def usage_text(version: MinutesVersionRow) -> str:
    if version.input_tokens is None and version.output_tokens is None:
        return "token usage not reported"
    return f"{version.input_tokens or 0:,} input / {version.output_tokens or 0:,} output tokens"


def render_minutes(meeting: MeetingRow, version: MinutesVersionRow, items: Sequence[MinutesItemRow], fmt: str, *,
                   coverage: str, gaps: int, exported_at: datetime | None = None) -> str:
    if fmt not in ("txt", "md"):
        raise ValueError(fmt)
    md = fmt == "md"
    esc = md_escape if md else (lambda s: s)
    stale = meeting.transcript_revision > version.transcript_revision
    when = (exported_at or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    revision = f"{version.transcript_revision}"
    if stale:
        revision += f" (the saved transcript has changed since: now revision {meeting.transcript_revision})"
    facts = [
        ("Meeting", f"{meeting.title} — {local_time(meeting.started_at)}"),
        ("Minutes", f"{STATE_LABEL.get(version.state, version.state)} · {version_label(version)}"),
        ("Transcript revision", revision),
        ("Generated", f"{local_time(version.created_at)} by {version.provider} ({version.model_id}), "
                      f"prompt {version.prompt_version} · {usage_text(version)}"),
        ("Coverage", coverage),
        ("Exported", when),
    ]
    out: list[str] = []
    title = f"{meeting.title} — minutes"
    warning = None if version.state == "approved" else "DRAFT — check before sharing. " + CAUTION
    gap_note = (f"The transcript has {gaps} interval(s) without text. Minutes can't include what was said then."
                if gaps or version.incomplete else None)
    if md:
        out += [f"# {esc(title)}", ""]
        if warning:
            out += [f"> **{esc(warning)}**", ""]
        out += [f"- **{k}:** {esc(v)}" for k, v in facts] + [""]
        if gap_note:
            out += [f"> {esc(gap_note)}", ""]
    else:
        out += [title, "=" * max(8, min(len(title), 60)), ""]
        if warning:
            out += [warning, ""]
        out += [f"{k}: {v}" for k, v in facts] + [""]
        if gap_note:
            out += [gap_note, ""]
    by_kind: dict[str, list[MinutesItemRow]] = {}
    for item in items:
        by_kind.setdefault(item.kind, []).append(item)
    for kind, heading in SECTIONS:
        out += ([f"## {heading}", ""] if md else [heading.upper(), "-" * len(heading), ""])
        rows = by_kind.get(kind, [])
        if not rows:
            out += ["None recorded.", ""]
            continue
        for item in rows:
            out.append(("- " if md else "- ") + _item_line(item, esc, md))
        out.append("")
    if version.warnings:
        out += (["## Warnings from generation", ""] if md else ["WARNINGS FROM GENERATION", "-" * 24, ""])
        out += [f"- {esc(w)}" for w in version.warnings] + [""]
    note = ("Owners and dates are only filled in when the transcript states them. “Suggested” items were proposed "
            "but not agreed. Mic and System are audio sources, not people.")
    out += [f"*{esc(note)}*" if md else note, ""]
    return "\n".join(out).rstrip("\n") + "\n"


def _refs(item: MinutesItemRow, esc) -> str:
    if item.origin == "user" and not item.evidence:
        return " " + esc("(added by you)")
    return "".join(" " + esc(f"[{evidence_label(ev)}]") for ev in item.evidence)


def _item_line(item: MinutesItemRow, esc, md: bool) -> str:
    text = " ".join(item.text.split())
    if item.kind == "topic":
        body = esc(text)
        body = f"**{body}**" if md else body
        if item.detail:
            body += " — " + esc(" ".join(item.detail.split()))
    elif item.kind == "decision":
        body = esc(f"[{DECISION_LABEL.get(item.status or '', 'Uncertain')}] {text}")
    elif item.kind == "question":
        body = esc(text) + (esc(f" (owner: {item.owner})") if item.owner else "")
    elif item.kind == "action":
        parts = [text, f"Owner: {item.owner or NOT_SPECIFIED}", f"Due: {due_text(item)}",
                 COMMITMENT_LABEL.get(item.commitment or "", "Unclear"),
                 "Confirmed" if item.reviewed else "Needs review"]
        body = esc(" · ".join(parts))
    else:
        body = esc(text)
    return body + _refs(item, esc)
