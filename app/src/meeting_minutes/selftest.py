"""End-to-end self-test of the normal workflow.

`python -m meeting_minutes.selftest <file.wav> <empty-data-folder>` (or `run_workflow(...)`
from Python) runs, in the app's own code:

1. a meeting with the synthetic capture devices (no real device is opened) whose mic
   and system streams both play the WAV in a loop, recognized by the app's own engine
   (the local Qwen3-ASR server unless an engine is given);
2. mic Mute half-way (system audio continues), then Stop and save, waiting for the
   final save to settle;
3. History reopen from a new store on the same folder, a correction of the first line,
   and TXT and Markdown export of the saved revision through the same code Save As uses;
4. Generate MoM with no OpenAI key: it must stop before sending anything.

Data go only to `<data-folder>` (never the user's History). The WAV is read into
memory; no audio is written. Prints one JSON report.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np

MIC, SYSTEM = "fake:mic-48k-mono", "fake:system-44k1-stereo"


def _wav_16k(path: Path) -> np.ndarray:
    import wave

    with wave.open(str(path), "rb") as handle:
        rate, channels, width = handle.getframerate(), handle.getnchannels(), handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2:
        raise SystemExit("selftest needs a 16-bit PCM WAV")
    samples = np.frombuffer(raw, "<i2").astype(np.float32).reshape(-1, channels).mean(axis=1) / 32768.0
    if rate != 16_000:
        import soxr

        samples = soxr.resample(samples, rate, 16_000).astype(np.float32)
    return np.ascontiguousarray(samples, dtype=np.float32)


def _signal(loop: np.ndarray, start_s: float):
    """A fake-device `signal`: native-rate samples of the 16 kHz loop from `start_s`."""
    fp = np.concatenate([loop, loop[:1]])
    n = loop.shape[0]

    def signal(first: int, count: int, channels: int, rate: int) -> np.ndarray:
        x = np.mod((start_s + (first + np.arange(count)) / rate) * 16_000, n)
        i = x.astype(np.int64)
        frac = (x - i).astype(np.float32)
        return np.repeat((fp[i] * (1 - frac) + fp[i + 1] * frac)[:, None], channels, axis=1)

    return signal


def run_workflow(wav: Path, data_dir: Path, *, seconds: float = 24.0, engine: Any = None) -> dict[str, Any]:
    from meeting_minutes.capture.fake import RealtimeFakeBackend
    from meeting_minutes.export import render, write_file
    from meeting_minutes.minutes.credentials import MemoryCredentialStore
    from meeting_minutes.minutes.provider import FakeMinutesGenerator
    from meeting_minutes.minutes.service import MinutesService
    from meeting_minutes.session.meeting import MeetingSession
    from meeting_minutes.speech.config import SpeechConfig
    from meeting_minutes.storage.store import TranscriptStore
    from meeting_minutes.ui.desktop import close_local_engines, load_local_engine

    data_dir.mkdir(parents=True, exist_ok=True)
    loop = _wav_16k(wav)
    report: dict[str, Any] = {"data_dir": str(data_dir), "wav_s": round(loop.shape[0] / 16_000, 1)}
    started = time.monotonic()
    try:
        engine = engine or load_local_engine("mixed")
        report["engine"] = engine.info.engine_id
        rec = engine.recognizer  # the "uncertain recognition" profile this build applies
        report["confidence_profile"] = {"floor": getattr(rec, "confidence_floor", None),
                                        "mark_other_language": getattr(rec, "mark_other_language", None)}
        report["engine_ready_s"] = round(time.monotonic() - started, 1)
        store = TranscriptStore(data_dir / "meetings.db")
        backend = RealtimeFakeBackend()
        session = MeetingSession(store, backend, engine.recognizer, engine.vad_factory, engine=engine.info,
                                 speech_config=SpeechConfig(partials=False))
        t0 = time.monotonic()
        assigned: set[int] = set()

        def feed() -> None:
            for handle in list(backend.opened):
                if id(handle) not in assigned and not handle.closed:
                    rate = handle._spec.fmt.sample_rate
                    sig = _signal(loop, time.monotonic() - t0)
                    handle.signal = lambda first, count, channels, _s=sig, _r=rate: _s(first, count, channels, _r)
                    assigned.add(id(handle))

        outcome = session.start(mic=MIC, system=SYSTEM, mic_included=True, title="Self-test")
        report["started"] = outcome.started
        feed()
        muted_at_ms = None
        while time.monotonic() - t0 < seconds:
            if muted_at_ms is None and time.monotonic() - t0 >= seconds / 2:
                session.set_mic_included(False)
                muted_at_ms = session.controller.status()["elapsed_ms"]
            feed()
            time.sleep(0.05)
        session.stop()
        report["stopped_in_s"] = round(time.monotonic() - t0 - seconds, 2)
        session.finished.wait(60)
        report["save_settled"] = session.save_settled.wait(60)
        report["finish_report"] = {k: session.finish_report.get(k) for k in ("complete", "drain_complete",
                                                                              "speech_gaps", "speech_faults")}
        meeting_id = session.meeting_id
        backend.close()
        store.close(wait_s=5.0)

        history = TranscriptStore(data_dir / "meetings.db")  # reopen, as the next app start would
        segments = history.segments(meeting_id)
        report["reopened_segments"] = len(segments)
        report["sources"] = {s: sum(x.source == s for x in segments) for s in ("mic", "system")}
        report["mic_lines_after_mute"] = sum(1 for x in segments if x.source == "mic" and muted_at_ms is not None
                                             and x.start_ms >= muted_at_ms + 1000)
        report["languages"] = sorted({x.language for x in segments})
        report["flags"] = {f: sum(f in x.flags for x in segments) for f in sorted({f for x in segments for f in x.flags})}
        report["sample_text"] = [x.text for x in segments[:3]]
        if segments:
            history.correct_segment(segments[0].id, segments[0].text + " (corrected)")
        snapshot = history.snapshot(meeting_id)
        report["revision_after_correction"] = snapshot.meeting.transcript_revision
        for fmt in ("txt", "md"):
            target = data_dir / f"selftest export.{fmt}"
            write_file(target, render(snapshot, fmt))
            report[f"export_{fmt}_bytes"] = target.stat().st_size
            report[f"export_{fmt}_has_correction"] = "(corrected)" in target.read_text(encoding="utf-8")

        generator = FakeMinutesGenerator()  # must never be reached without a key
        service = MinutesService(history, MemoryCredentialStore(), lambda creds: generator)
        job = service.start(meeting_id, snapshot.meeting.transcript_revision)
        job.wait(30)
        report["minutes_without_key"] = {"state": job.state, "error": getattr(job.error, "code", None),
                                         "request_sent": getattr(job.error, "request_sent", None),
                                         "provider_called": bool(generator.envelopes)}
        history.close(wait_s=5.0)
    finally:
        close_local_engines()
    report["seconds_total"] = round(time.monotonic() - started, 1)
    ok = (report.get("started") and report.get("save_settled") and report.get("reopened_segments", 0) > 0
          and report.get("mic_lines_after_mute") == 0 and report.get("export_txt_has_correction")
          and report.get("minutes_without_key", {}).get("provider_called") is False)
    report["ok"] = bool(ok)
    return report


def main(argv: list[str]) -> int:
    report = run_workflow(Path(argv[0]), Path(argv[1]))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
