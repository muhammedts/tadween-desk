"""Downstream consumers of admitted PCM.

The level meter and the speech pipeline (resampling, VAD and recognition) both sit
behind the `FrameSink` interface. `accept` is called by the
capture worker under the privacy boundary lock (see gate.py), so it must be bounded
and must not block. `reset` drops any per-source state (pending audio, partial
utterances, resampler/VAD history) and returns how much pending audio it discarded.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Sequence
from typing import Protocol

import numpy as np

from meeting_minutes.domain.contracts import AudioFrame, Source

SILENCE_DBFS = -120.0


class FrameSink(Protocol):
    def accept(self, frame: AudioFrame) -> None: ...

    def reset(self, source: Source) -> int:
        """Discard per-source pending state; returns discarded pending nanoseconds."""
        ...


class LevelMeterSink:
    """Per-source signal level of admitted audio. Keeps numbers only, never samples."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rms_dbfs = {Source.MIC: SILENCE_DBFS, Source.SYSTEM: SILENCE_DBFS}
        self._peak_dbfs = {Source.MIC: SILENCE_DBFS, Source.SYSTEM: SILENCE_DBFS}
        self._samples = {Source.MIC: 0, Source.SYSTEM: 0}

    def accept(self, frame: AudioFrame) -> None:
        data = frame.data
        rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))
        peak = float(np.max(np.abs(data)))
        with self._lock:
            self._rms_dbfs[frame.source] = _dbfs(rms)
            self._peak_dbfs[frame.source] = max(self._peak_dbfs[frame.source], _dbfs(peak))
            self._samples[frame.source] += frame.frame_count

    def reset(self, source: Source) -> int:
        with self._lock:
            self._rms_dbfs[source] = SILENCE_DBFS
        return 0

    def level(self, source: Source) -> float:
        with self._lock:
            return self._rms_dbfs[source]

    def peak(self, source: Source) -> float:
        with self._lock:
            return self._peak_dbfs[source]

    def samples(self, source: Source) -> int:
        with self._lock:
            return self._samples[source]


class FanoutSink:
    def __init__(self, sinks: Sequence[FrameSink]) -> None:
        self._sinks = tuple(sinks)

    def accept(self, frame: AudioFrame) -> None:
        for sink in self._sinks:
            sink.accept(frame)

    def reset(self, source: Source) -> int:
        return max((sink.reset(source) for sink in self._sinks), default=0)


def _dbfs(value: float) -> float:
    return SILENCE_DBFS if value <= 0 else max(SILENCE_DBFS, 20.0 * math.log10(value))
