"""Display text and control availability for the capture panel.

Pure functions over `CaptureController.status()` and events, so wording and button
rules are testable without a window. Nothing here sees audio or transcript text.
"""

from __future__ import annotations

from typing import Any

from meeting_minutes.domain.events import (
    CaptureGapEvent,
    CaptureStateEvent,
    MicPrivacyStateEvent,
    SourceChangedEvent,
)

MIC_INCLUDED_TEXT = "Mic included in transcription"
MIC_EXCLUDED_TEXT = "Mic excluded from transcription — meeting audio continues"
ROUTING_NOTE = (
    "This mute controls only this app's transcription; it does not mute Teams, Zoom or Meet. "
    "Your voice can still reach System audio through sidetone, mic monitoring ('Listen to this "
    "device') or echo; use Pause all for a fully private moment. Headphones are recommended."
)

SOURCE_LABELS = {"mic": "Microphone", "system": "System audio"}


def mic_banner(status: dict[str, Any]) -> str:
    return MIC_INCLUDED_TEXT if status["mic_included"] else MIC_EXCLUDED_TEXT


def mic_button_text(status: dict[str, Any]) -> str:
    return "Mute mic" if status["mic_included"] else "Unmute mic"


def controls(status: dict[str, Any], *, mic_selected: bool, system_selected: bool) -> dict[str, bool]:
    """Which buttons are usable. Mute/unmute is always available, even before Start."""
    state = status["meeting_state"]
    live = state in ("capturing", "degraded")
    return {
        "start": state in ("idle", "preflight") and (mic_selected or system_selected),
        "pause": live,
        "resume": state == "paused",
        "stop": state in ("capturing", "degraded", "paused"),
        "mic": state != "stopped",
        "devices": state in ("idle", "preflight"),
        "apply_devices": state in ("capturing", "degraded", "paused"),
    }


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f} s"


def source_line(name: str, status: dict[str, Any], *, picked: bool | None = None) -> str:
    """`picked` is the device picker's state, used before capture has started."""
    s = status["sources"][name]
    label = SOURCE_LABELS[name]
    if status["meeting_state"] in ("idle", "preflight") and picked is not None:
        if not picked:
            return f"{label}: not selected"
        if name == "mic" and not status["mic_included"]:
            return f"{label}: selected, will stay excluded at Start"
        return f"{label}: selected — press Start to capture"
    if s["device_id"] is None:
        return f"{label}: not selected"
    if name == "mic" and not status["mic_included"]:
        # A closed, excluded mic is not "silent"; it is not captured at all.
        return f"{label}: not captured (excluded)"
    state = s["state"]
    detail = {
        "running": "capturing",
        "closed": "paused" if status["meeting_state"] == "paused" else "closed",
        "failed": f"FAILED ({s['failure']}) — choose a device or retry",
        "lost": "LOST — reconnect or choose another device",
        "opening": "opening",
        "switching": "switching",
    }.get(state, state)
    idle = s["lost_ms"].get("no_data_delivered", 0)
    lost = sum(ms for reason, ms in s["lost_ms"].items() if reason != "no_data_delivered")
    parts = [f"{label}: {detail}", f"epoch {s['epoch']}"]
    if state == "running":
        parts.append(f"level {s['level_dbfs']:.0f} dBFS")
    parts.append(f"admitted {s['frames_admitted']} buffers")
    if s["queued_ms"]:
        parts.append(f"queued {s['queued_ms']} ms")
    if idle:
        # Normal for system audio while nothing plays; a dropout for a microphone.
        parts.append(f"no audio delivered {_seconds(idle)}")
    if lost:
        parts.append(f"lost {_seconds(lost)}")
    if s["input_overflows"]:
        parts.append(f"INPUT OVERFLOW x{s['input_overflows']} (audio dropped by the driver)")
    if s["frames_failed_downstream"]:
        parts.append(f"PROCESSING FAILED x{s['frames_failed_downstream']}")
    return ", ".join(parts)


def switch_notice(result: Any) -> str:
    """Explain a device switch that did not simply apply (a `SwitchResult`).

    Fixed wording plus fixed error codes only; the picker itself shows which device
    is in force. A selection stored while paused or excluded is not claimed as tested.
    """
    label = SOURCE_LABELS[result.source.value]
    outcome = result.outcome.value
    if outcome == "applied":
        return ""
    if outcome == "recorded":
        when = {"mic_included": "when the mic is unmuted", "start": "on Start"}.get(result.opens_on, "on Resume")
        return f"{label}: new device selected; it has not been opened yet and will be tried {when}."
    if outcome == "rolled_back":
        return (f"{label}: the selected device could not be opened ({result.error}); "
                f"still capturing from the previous device.")
    if result.previous is None:
        return f"{label}: the selected device could not be opened ({result.error}); this source was not added."
    if result.requested == result.previous:
        return (f"{label}: the device could not be reopened ({result.error}); "
                f"this source is not capturing. Choose a device or try again.")
    if result.rollback_error is not None:
        return (f"{label}: the selected device could not be opened ({result.error}) and the previous "
                f"device could not be reopened ({result.rollback_error}); this source is not capturing. "
                f"Choose a device or try again.")
    return (f"{label}: the selected device could not be opened ({result.error}); the previous device "
            f"stays selected but is not capturing. Choose a device or try again.")


def reopen_notice(status: dict[str, Any], trigger: str) -> str:
    """After Resume/unmute: name each selected source that failed to open (fixed codes)."""
    failed = [
        f"{SOURCE_LABELS[name]} could not be opened on {trigger} ({s['failure']})"
        for name, s in status["sources"].items()
        if s["state"] == "failed" and s["device_id"] is not None
    ]
    return "; ".join(failed) + "; choose a device or try again." if failed else ""


def meter_fraction(level_dbfs: float, *, floor_dbfs: float = -60.0) -> float:
    if level_dbfs <= floor_dbfs:
        return 0.0
    return min(1.0, (level_dbfs - floor_dbfs) / -floor_dbfs)


def event_line(event: Any) -> str:
    """One metadata-only line per event; never device content or audio."""
    p = event.payload
    if isinstance(event, MicPrivacyStateEvent):
        text = f"{_seconds(p.at_ms)}  mic {'included (fresh start)' if p.code == 'mic_included' else 'excluded'}"
        if p.discarded_pending_ms:
            text += f", discarded {_seconds(p.discarded_pending_ms)} of pending mic audio"
        return text
    if isinstance(event, CaptureGapEvent):
        length = _seconds(p.end_ms - p.start_ms) if p.duration_known else "of unknown length"
        return (f"{_seconds(p.start_ms)}  {SOURCE_LABELS[p.source.value]} gap "
                f"{length} ({p.reason.value.replace('_', ' ')})")
    if isinstance(event, CaptureStateEvent):
        reason = p.reason.value.replace("_", " ") if p.reason else ""
        return f"{_seconds(p.at_ms or 0)}  {SOURCE_LABELS[p.source.value]} {p.state.value} ({reason}), epoch {p.epoch}"
    if isinstance(event, SourceChangedEvent):
        return (f"{_seconds(p.at_ms)}  {SOURCE_LABELS[p.source.value]} epoch {p.epoch} "
                f"at {p.sample_rate} Hz x{p.channels}")
    return type(event).__name__


def summary_line(status: dict[str, Any]) -> str:
    line = (f"State: {status['meeting_state']}  |  elapsed {_seconds(status['elapsed_ms'])}  |  "
            f"gaps {status['gaps']}  |  owned PCM {status['pcm_bytes'] // 1024} KiB "
            f"(peak {status['peak_pcm_bytes'] // 1024} KiB of {status['pcm_budget_bytes'] // 2**20} MiB)")
    if status["sink_errors"]:
        line += f"  |  WARNING: processing failed for {status['sink_errors']} buffers (recorded as gaps)"
    return line
