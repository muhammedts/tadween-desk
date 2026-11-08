"""Deterministic meeting-session rig: fake devices on a fake clock, the real
capture controller, speech pipeline (soxr) and SQLite store, a tone-word recognizer
and energy VAD. Nothing here touches audio hardware or speech models."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from capture_rig import DEVICES, MIC, SECOND, SYSTEM
from speech_fakes import EnergyVad, ToneRecognizer, scripted

from meeting_minutes.capture.controller import CaptureConfig
from meeting_minutes.capture.fake import FakeAudioBackend, pump_until
from meeting_minutes.domain.contracts import Source
from meeting_minutes.session.meeting import EngineInfo, MeetingSession
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.storage.store import TranscriptStore

RATES = {Source.MIC: 48_000, Source.SYSTEM: 44_100}
ENGINE = EngineInfo("fake-tone-engine", "0" * 64)


class SessionRig:
    def __init__(self, tmp_path: Path, *, recognizer: Any = None, stop_budget_s: float = 30.0,
                 config: SpeechConfig = SpeechConfig(partials=False)) -> None:
        self.now = 0
        self.db = tmp_path / "meetings.db"
        self.store = TranscriptStore(self.db)
        self.backend = FakeAudioBackend(meeting_id=UUID(int=3003_002), devices=DEVICES)
        self.recognizer = recognizer if recognizer is not None else ToneRecognizer()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.session = MeetingSession(
            self.store, self.backend, self.recognizer, EnergyVad, engine=ENGINE, speech_config=config,
            clock=lambda: self.now, threaded=False, listener=lambda kind, data: self.events.append((kind, data)),
            stop_budget_s=stop_budget_s, capture_config=CaptureConfig(mic_stall_timeout_ns=None),
        )
        self.recognizer.boundary = self.session.controller.boundary

    def start(self, *, mic: str | None = MIC, system: str | None = SYSTEM, **kwargs: Any):
        outcome = self.session.start(mic=mic, system=system, **kwargs)
        for source in Source:  # silence unless a test scripts words
            if self.handle(source) is not None:
                self.say(source, [])
        return outcome

    def handle(self, source: Source):
        live = [h for h in self.backend.opened if h.source is source and not h.closed]
        return live[-1] if live else None

    def say(self, source: Source, words: list[tuple[float, str]]) -> None:
        """Script the open stream of `source`; times are seconds since it opened."""
        self.handle(source).signal = scripted(RATES[source], words)

    def run(self, until_s: float) -> None:
        target = int(until_s * SECOND)
        while self.now < target:
            step = min(target, self.now + SECOND // 10)
            pump_until([h for h in self.backend.opened if h.running and not h.closed], step)
            self.now = step
            self.session.pump()

    def kinds(self, kind: str) -> list[dict[str, Any]]:
        return [data for k, data in self.events if k == kind]

    def saved(self, source: str | None = None) -> list[str]:
        return [s.text for s in self.store.segments(self.session.meeting_id) if source is None or s.source == source]

    def timeline(self) -> list[tuple[str, str, str | None]]:
        return [(t.kind, t.code, t.source) for t in self.store.timeline(self.session.meeting_id)]
