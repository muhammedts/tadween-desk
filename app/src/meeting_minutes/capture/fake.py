"""Deterministic in-memory fake capture backend.

Generates synthetic tones only; it never touches an audio device, file or network.
Frames are produced synchronously by `pump`, so sample counters and timestamps are
fully reproducible: timestamp = origin_ns + first_sample_index * 1e9 // rate.
`skip` advances the sample position without delivering data (an idle loopback or a
dropout), `lose` makes a handle behave like an unplugged device, and device ids in
`fail_open` refuse to open, so controller paths can be exercised without hardware.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from uuid import UUID

import numpy as np

from meeting_minutes.capture.base import Clock, DeviceUnavailable, FrameCallback
from meeting_minutes.domain.contracts import (
    FIRST_EPOCH,
    NS_PER_SECOND,
    AudioFormat,
    AudioFrame,
    DeviceDescriptor,
    DeviceSelection,
    Direction,
    IdentityStability,
    Source,
    TimingQuality,
)

FAKE_BACKEND = "fake"


@dataclass(frozen=True, slots=True)
class FakeDeviceSpec:
    device_id: str
    display_name: str
    direction: Direction
    fmt: AudioFormat
    tone_hz: float
    frames_per_buffer: int
    amplitude: float = 0.1


DEFAULT_FAKE_DEVICES: tuple[FakeDeviceSpec, ...] = (
    FakeDeviceSpec(
        device_id="fake:mic-48k-mono",
        display_name="Fake microphone (48 kHz mono)",
        direction=Direction.CAPTURE,
        fmt=AudioFormat(sample_rate=48_000, channels=1),
        tone_hz=440.0,
        frames_per_buffer=480,
    ),
    FakeDeviceSpec(
        device_id="fake:system-44k1-stereo",
        display_name="Fake playback endpoint loopback (44.1 kHz stereo)",
        direction=Direction.RENDER,
        fmt=AudioFormat(sample_rate=44_100, channels=2),
        tone_hz=660.0,
        frames_per_buffer=441,
    ),
)

_REQUIRED_DIRECTION = {Source.MIC: Direction.CAPTURE, Source.SYSTEM: Direction.RENDER}


class FakeCaptureHandle:
    def __init__(
        self,
        *,
        meeting_id: UUID,
        source: Source,
        spec: FakeDeviceSpec,
        callback: FrameCallback,
        epoch: int,
        origin_ns: int,
        privacy_generation: Callable[[], int],
    ) -> None:
        self._meeting_id = meeting_id
        self._source = source
        self._spec = spec
        self._callback = callback
        self._epoch = epoch
        self._origin_ns = origin_ns
        self._privacy_generation = privacy_generation
        self._next_index = 0
        self._sequence = 0
        self._running = True
        self._active = True
        self.closed = False
        # Optional override of generated samples: f(first_index, count, channels) -> array.
        self.signal: Callable[[int, int, int], np.ndarray] | None = None

    @property
    def source(self) -> Source:
        return self._source

    @property
    def running(self) -> bool:
        return self._running

    @property
    def epoch(self) -> int:
        return self._epoch

    def is_active(self) -> bool:
        return self._active and not self.closed

    @property
    def next_monotonic_ns(self) -> int:
        return self._origin_ns + self._next_index * NS_PER_SECOND // self._spec.fmt.sample_rate

    def _samples(self, start: int, count: int) -> np.ndarray:
        rate = self._spec.fmt.sample_rate
        n = np.arange(start, start + count, dtype=np.float64)
        mono = self._spec.amplitude * np.sin(2.0 * np.pi * self._spec.tone_hz * n / rate)
        # Distinct per-channel gains keep stereo channels distinguishable.
        gains = 1.0 - 0.25 * np.arange(self._spec.fmt.channels, dtype=np.float64)
        return (mono[:, None] * gains[None, :]).astype(np.float32)

    def pump(self, buffers: int = 1) -> int:
        """Deliver up to `buffers` frames to the callback; returns how many were sent."""
        sent = 0
        while sent < buffers and self._running:
            count = self._spec.frames_per_buffer
            data = (
                self._samples(self._next_index, count)
                if self.signal is None
                else self.signal(self._next_index, count, self._spec.fmt.channels)
            )
            frame = AudioFrame(
                meeting_id=self._meeting_id,
                source=self._source,
                epoch=self._epoch,
                privacy_generation=self._privacy_generation(),
                sequence=self._sequence,
                first_sample_index=self._next_index,
                sample_rate=self._spec.fmt.sample_rate,
                channels=self._spec.fmt.channels,
                monotonic_ns=self.next_monotonic_ns,
                timing_quality=TimingQuality.SYNTHETIC,
                data=data,
            )
            self._next_index += count
            self._sequence += 1
            self._callback(frame)
            sent += 1
        return sent

    def skip(self, samples: int) -> None:
        """Advance the sample position without delivering data."""
        self._next_index += samples

    def lose(self) -> None:
        """Simulate an unplugged endpoint: no more data and the stream reports inactive."""
        self._running = False
        self._active = False

    def stop(self) -> None:
        self._running = False

    def close(self) -> None:
        self.stop()
        self.closed = True


class FakeAudioBackend:
    """Implements the CaptureBackend protocol with synthetic devices."""

    def __init__(
        self,
        *,
        meeting_id: UUID,
        devices: Sequence[FakeDeviceSpec] = DEFAULT_FAKE_DEVICES,
        origin_ns: int = 0,
        privacy_generation: Callable[[Source], int] = lambda _source: 0,
    ) -> None:
        self._meeting_id = meeting_id
        self._devices = {d.device_id: d for d in devices}
        self._origin_ns = origin_ns
        self._privacy_generation = privacy_generation
        self.fail_open: set[str] = set()
        self.opened: list[FakeCaptureHandle] = []

    def enumerate(self) -> list[DeviceDescriptor]:
        return [
            DeviceDescriptor(
                device_id=d.device_id,
                display_name=d.display_name,
                backend=FAKE_BACKEND,
                direction=d.direction,
                identity_stability=IdentityStability.STABLE_ENDPOINT_ID,
                supports_loopback=d.direction is Direction.RENDER,
                probed_formats=(d.fmt,),
            )
            for d in self._devices.values()
        ]

    def open(
        self,
        selection: DeviceSelection,
        callback: FrameCallback,
        *,
        epoch: int = FIRST_EPOCH,
        meeting_id: UUID | None = None,
        privacy_generation: Callable[[], int] | None = None,
        clock: Clock | None = None,
    ) -> FakeCaptureHandle:
        spec = self._devices.get(selection.device_id)
        if spec is None or selection.device_id in self.fail_open:
            raise DeviceUnavailable("fake device unavailable")  # no silent fallback
        if spec.direction is not _REQUIRED_DIRECTION[selection.source]:
            raise ValueError(
                f"{selection.source} source needs a {_REQUIRED_DIRECTION[selection.source]} device"
            )
        source = selection.source
        handle = FakeCaptureHandle(
            meeting_id=meeting_id or self._meeting_id,
            source=source,
            spec=spec,
            callback=callback,
            epoch=epoch,
            origin_ns=self._origin_ns if clock is None else clock(),
            privacy_generation=privacy_generation or (lambda: self._privacy_generation(source)),
        )
        self.opened.append(handle)
        return handle


def pump_until(handles: Sequence[FakeCaptureHandle], until_ns: int) -> int:
    """Pump running handles in capture-time order until each reaches `until_ns`."""
    sent = 0
    while True:
        due = [h for h in handles if h.running and h.next_monotonic_ns < until_ns]
        if not due:
            return sent
        sent += min(due, key=lambda h: (h.next_monotonic_ns, h.source.value)).pump(1)


class RealtimeFakeBackend(FakeAudioBackend):
    """Fake backend driven by a background thread in (monotonic) real time.

    For running the CLI and control panel without audio hardware. Each opened handle
    delivers its synthetic buffers when the clock passes their capture time, from one
    pump thread, the way native callbacks arrive from PortAudio's threads.
    """

    def __init__(self, *, clock: Clock | None = None, tick_s: float = 0.01, **kwargs) -> None:
        import threading
        import time

        super().__init__(meeting_id=kwargs.pop("meeting_id", UUID(int=0)), **kwargs)
        self._clock_fn = clock or time.monotonic_ns
        self._tick_s = tick_s
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._threading = threading

    def open(self, selection: DeviceSelection, callback: FrameCallback, **kwargs) -> FakeCaptureHandle:
        kwargs.setdefault("clock", self._clock_fn)
        handle = super().open(selection, callback, **kwargs)
        if self._thread is None:
            self._thread = self._threading.Thread(target=self._run, name="fake-capture", daemon=True)
            self._thread.start()
        return handle

    def _run(self) -> None:
        while not self._stop_event.wait(self._tick_s):
            live = [h for h in self.opened if h.running and not h.closed]
            pump_until(live, self._clock_fn())

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(2.0)
        for handle in self.opened:
            handle.close()
