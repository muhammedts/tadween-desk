"""Deterministic capture rig for controller and privacy tests (fake devices, fake clock)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from meeting_minutes.capture.controller import CaptureConfig, CaptureController
from meeting_minutes.capture.fake import FakeAudioBackend, FakeDeviceSpec, pump_until
from meeting_minutes.domain.contracts import AudioFormat, AudioFrame, Direction, Source

MEETING = UUID(int=2002)
MIC = "fake:mic-48k-mono"
SYSTEM = "fake:system-44k1-stereo"
MIC2 = "fake:mic2-16k-mono"
SECOND = 1_000_000_000
# Canary: a constant no generator produces, marking audio that must never pass the gate.
CANARY = np.float32(0.6180339)

DEVICES = (
    FakeDeviceSpec(MIC, "Fake microphone (48 kHz mono)", Direction.CAPTURE,
                   AudioFormat(sample_rate=48_000, channels=1), 440.0, 480),
    FakeDeviceSpec(SYSTEM, "Fake playback loopback (44.1 kHz stereo)", Direction.RENDER,
                   AudioFormat(sample_rate=44_100, channels=2), 660.0, 441),
    FakeDeviceSpec(MIC2, "Fake headset microphone (16 kHz mono)", Direction.CAPTURE,
                   AudioFormat(sample_rate=16_000, channels=1), 330.0, 160),
)


def canary_signal(first_index: int, count: int, channels: int) -> np.ndarray:
    return np.full((count, channels), CANARY, dtype=np.float32)


def contains_canary(data: np.ndarray) -> bool:
    return bool(np.any(data == CANARY))


@dataclass
class Seen:
    source: Source
    epoch: int
    generation: int
    first_sample_index: int
    monotonic_ns: int
    canary: bool
    admitted_at_accept: bool


class SpySink:
    """Records metadata of every frame reaching downstream input, and whether the
    privacy boundary still admitted it at that moment (accept runs under its lock)."""

    def __init__(self) -> None:
        self.seen: list[Seen] = []
        self.resets: list[Source] = []
        self.controller: CaptureController | None = None

    def accept(self, frame: AudioFrame) -> None:
        state = self.controller.boundary.state if self.controller else None
        self.seen.append(
            Seen(frame.source, frame.epoch, frame.privacy_generation, frame.first_sample_index,
                 frame.monotonic_ns, contains_canary(frame.data),
                 state is None or state.admits(frame.source, frame.privacy_generation))
        )

    def reset(self, source: Source) -> int:
        self.resets.append(source)
        return 0

    def of(self, source: Source) -> list[Seen]:
        return [s for s in self.seen if s.source is source]


@dataclass
class Job:
    source: Source
    generation: int
    canary: bool
    duration_ns: int


class FakeDecoder:
    """A controllable recogniser stand-in: `accept` queues bounded jobs; decoding runs
    outside the gate and its results go through the boundary's commit check."""

    def __init__(self, max_jobs: int = 64) -> None:
        self.pending: list[Job] = []
        self.max_jobs = max_jobs
        self.committed: list[Job] = []
        self.rejected: list[Job] = []
        self.controller: CaptureController | None = None
        self._lock = threading.Lock()

    def accept(self, frame: AudioFrame) -> None:
        with self._lock:
            if len(self.pending) < self.max_jobs:
                self.pending.append(Job(frame.source, frame.privacy_generation, contains_canary(frame.data),
                                        frame.duration_ns))

    def reset(self, source: Source) -> int:
        with self._lock:
            dropped = [j for j in self.pending if j.source is source]
            self.pending = [j for j in self.pending if j.source is not source]
            return sum(j.duration_ns for j in dropped)

    def take(self, source: Source) -> Job | None:
        with self._lock:
            for i, job in enumerate(self.pending):
                if job.source is source:
                    return self.pending.pop(i)
        return None

    def commit(self, job: Job) -> bool:
        ok, _ = self.controller.boundary.commit(job.source, job.generation, lambda: self.committed.append(job))
        if not ok:
            self.rejected.append(job)
        return ok


class Rig:
    def __init__(self, *, mic_included: bool = True, sink=None, config: CaptureConfig | None = None) -> None:
        self.now = 0
        self.backend = FakeAudioBackend(meeting_id=MEETING, devices=DEVICES)
        self.sink = sink if sink is not None else SpySink()
        self.controller = CaptureController(
            self.backend, mic_included=mic_included, sink=self.sink, clock=lambda: self.now,
            config=config or CaptureConfig(mic_stall_timeout_ns=None), meeting_id=MEETING,
        )
        self.sink.controller = self.controller

    def handle(self, source: Source):
        live = [h for h in self.backend.opened if h.source is source and not h.closed]
        return live[-1] if live else None

    def run(self, until_ns: int, *, process: bool = True) -> None:
        """Advance fake time, delivering every running source's buffers up to `until_ns`."""
        while self.now < until_ns:
            step = min(until_ns, self.now + 100_000_000)
            live = [h for h in self.backend.opened if h.running and not h.closed]
            pump_until(live, step)
            self.now = step
            if process:
                self.controller.process_pending()

    def start(self, *, mic: str | None = MIC, system: str | None = SYSTEM, **kwargs):
        return self.controller.start(mic=mic, system=system, **kwargs)

    def exclude(self):
        return self.controller.set_mic_included(False, expected_generation=self.controller.privacy_generation)

    def include(self):
        return self.controller.set_mic_included(True, expected_generation=self.controller.privacy_generation)
