"""Plain-language wording shared by the window and the transcript file export.

No Qt here, so the exported file and the saved view say exactly the same thing about
how a meeting ended and what is missing.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from meeting_minutes.storage.store import TimelineRow

SOURCE_NAMES = {"mic": "Mic", "system": "System"}
GAP_TEXT = {
    "stt_backlog": "recognition fell behind",
    "stt_failed": "recognition failed",
    "processing_failed": "processing failed",
    "stopped_undrained": "not finished when Stop's time ran out",
    "device_lost": "device lost",
    "device_switch": "device switched",
    "no_data_delivered": "no audio delivered",
    "queue_overflow": "capture queue full",
    "pcm_budget": "audio memory limit",
    "input_overflow": "driver dropped audio",
    "text_not_saved": "text could not be saved",
    "app_ended_during_meeting": "app ended during the meeting",
}
STATE_TEXT = {
    "capturing": "Recording", "degraded": "Recording — a source is missing", "paused": "Paused",
    "finalizing": "Finalizing — saving the last text (up to 30 s)…", "stopped": "Stopped",
    "interrupted": "Ended incomplete", "ready": "Ready",
    "stopping": "Ended incomplete — saving did not finish",  # stored status of a save that never returned
}


# Recognizer quality marks saved with a line (speech/qwen.py). They describe how sure
# the recognizer was, never whether audio was processed or saved.
QUALITY_TEXT = {
    "lang_retry": "language re-checked (experimental R)",
    "lang_uncertain": "language uncertain",
    "filler_suspect": "possible filler",
    "low_confidence": "uncertain recognition",
}


def quality_note(flags: Sequence[str]) -> str:
    """' · language uncertain' etc. for a line's quality marks, or ''."""
    notes = [QUALITY_TEXT[f] for f in flags if f in QUALITY_TEXT]
    return f" · {', '.join(notes)}" if notes else ""


def model_name(engine_id: str | None) -> str:
    """"Qwen3-ASR 0.6B" / "Qwen3-ASR 1.7B" from an engine identity such as
    "llama.cpp-b11960/qwen3-asr-1.7b-q8_0-gguf;language=auto;policy=A"."""
    match = re.search(r"qwen3-asr-(\d+(?:\.\d+)?)b", engine_id or "", re.IGNORECASE)
    return f"Qwen3-ASR {match.group(1)}B" if match else "Qwen3-ASR"


def engine_text(engine_id: str | None) -> str:
    """The speech model a meeting was recognized with, from its saved identity."""
    if not engine_id:
        return "not recorded"
    base, _, rest = engine_id.partition(";")
    options = dict(part.split("=", 1) for part in rest.split(";") if "=" in part)
    if "qwen3-asr" not in base:
        return base
    language = {"auto": "automatic language", "en": "English only", "ar": "Arabic only"}.get(
        options.get("language", ""), options.get("language", "language not recorded"))
    policy = ", experimental retry (R)" if options.get("policy") == "R" else ""
    return f"{model_name(base)} on this PC ({language}{policy})"


def clock_text(ms: int) -> str:
    seconds = max(0, ms) // 1000
    hours, rem = divmod(seconds, 3600)
    return f"{hours}:{rem // 60:02d}:{rem % 60:02d}" if hours else f"{rem // 60:02d}:{rem % 60:02d}"


def local_time(iso_utc: str) -> str:
    """A stored UTC timestamp in this computer's local time, to the minute."""
    from datetime import datetime

    try:
        return datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso_utc[:16]


def is_rtl(text: str) -> bool:
    """Direction of the first strong character (Arabic/Hebrew scripts are RTL)."""
    for ch in text:
        if "֐" <= ch <= "ࣿ" or "יִ" <= ch <= "ﻼ":
            return True
        if ch.isalpha():
            return False
    return False


def coverage_summary(status: str, timeline: Sequence[TimelineRow]) -> str:
    """Plain-language coverage: how the meeting ended, losses, intentional exclusions."""
    finalized = next((t for t in reversed(timeline) if t.code == "finalized"), None)
    losses = [t for t in timeline if t.metadata.get("loss") or (t.kind == "error" and t.code != "event_log_overflow")]
    lost_ms = sum((t.end_ms or t.start_ms) - t.start_ms for t in losses if t.kind == "gap")
    muted = sum(1 for t in timeline if t.code == "mic_excluded")
    pauses = sum(1 for t in timeline if t.kind == "pause")
    parts = []
    if status == "stopped" and finalized and finalized.metadata.get("complete"):
        parts.append("Complete: everything captured was processed and saved, with no known loss. "
                     "(This does not mean every word was recognized correctly.)")
    elif status == "stopped":
        parts.append("Stopped normally, but some intervals have no text — see the notes below.")
    elif status == "interrupted":
        parts.append("Ended incomplete: some text may be missing — see the notes below.")
    else:
        parts.append(f"Status: {status}.")
    if losses:
        parts.append(f"{len(losses)} interval(s) without text ({lost_ms / 1000:.1f} s known).")
    if muted:
        parts.append(f"Mic excluded {muted} time(s) on purpose (not transcribed).")
    if pauses:
        parts.append(f"Paused {pauses} time(s).")
    return " ".join(parts)


def event_line(event: TimelineRow) -> str:
    when = clock_text(event.start_ms) + (f"–{clock_text(event.end_ms)}" if event.end_ms is not None else "")
    who = f" {SOURCE_NAMES[event.source]}" if event.source else ""
    if event.code == "finalized":
        m = event.metadata
        return (f"{when} Finalized: complete={m.get('complete')}, drained={m.get('drain_complete')}, "
                f"speech gaps={m.get('speech_gaps')}, capture losses={m.get('capture_loss_gaps')}, "
                f"unsaved={m.get('unsaved_finals')}, text not kept={m.get('text_not_saved', 0)}")
    label = {"mic_excluded": "mic excluded (muted in this app)", "mic_included": "mic included again",
             "paused_by_user": "paused", "resumed_by_user": "resumed",
             "segment_redacted": "text redacted"}.get(event.code, GAP_TEXT.get(event.code, event.code))
    if event.kind == "gap" and not event.metadata.get("loss", True):
        label += " (not a loss)"
    return f"{when}{who}: {label}"
