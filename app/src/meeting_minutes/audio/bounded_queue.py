"""Bounded per-source PCM frame queue.

`put` never blocks and never spools to disk. When a byte or duration cap would be
exceeded, the oldest whole frames are dropped and returned as exact sample spans
so the caller can record a `capture.gap`.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass

from meeting_minutes.domain.contracts import AudioFrame, Source


@dataclass(frozen=True, slots=True)
class DroppedSpan:
    source: Source
    epoch: int
    first_sample_index: int
    end_sample_index: int
    sample_rate: int
    monotonic_ns: int


def _span(frame: AudioFrame) -> DroppedSpan:
    return DroppedSpan(
        source=frame.source,
        epoch=frame.epoch,
        first_sample_index=frame.first_sample_index,
        end_sample_index=frame.end_sample_index,
        sample_rate=frame.sample_rate,
        monotonic_ns=frame.monotonic_ns,
    )


class BoundedFrameQueue:
    def __init__(self, source: Source, *, max_bytes: int, max_duration_ns: int) -> None:
        if max_bytes <= 0 or max_duration_ns <= 0:
            raise ValueError("queue caps must be positive")
        self._source = source
        self._max_bytes = max_bytes
        self._max_duration_ns = max_duration_ns
        self._frames: deque[AudioFrame] = deque()
        self._bytes = 0
        self._duration_ns = 0
        self._dropped_frames = 0
        self._dropped_samples = 0
        self._lock = threading.Lock()

    @property
    def max_bytes(self) -> int:
        return self._max_bytes

    @property
    def max_duration_ns(self) -> int:
        return self._max_duration_ns

    def put(self, frame: AudioFrame) -> list[DroppedSpan]:
        if frame.source is not self._source:
            raise ValueError("frame source does not match queue source")
        if frame.nbytes > self._max_bytes or frame.duration_ns > self._max_duration_ns:
            raise ValueError("single frame exceeds queue capacity")
        dropped: list[DroppedSpan] = []
        with self._lock:
            while self._frames and (
                self._bytes + frame.nbytes > self._max_bytes
                or self._duration_ns + frame.duration_ns > self._max_duration_ns
            ):
                old = self._remove_oldest()
                self._dropped_frames += 1
                self._dropped_samples += old.frame_count
                dropped.append(_span(old))
            self._frames.append(frame)
            self._bytes += frame.nbytes
            self._duration_ns += frame.duration_ns
        return dropped

    def drop_oldest(self) -> DroppedSpan | None:
        """Release the oldest frame as loss (global budget enforcement); None if empty."""
        with self._lock:
            if not self._frames:
                return None
            old = self._remove_oldest()
            self._dropped_frames += 1
            self._dropped_samples += old.frame_count
            return _span(old)

    def get(self) -> AudioFrame | None:
        with self._lock:
            return self._remove_oldest() if self._frames else None

    def clear(self) -> int:
        """Release every queued frame (e.g. on mic exclusion); returns frames released."""
        with self._lock:
            released = len(self._frames)
            self._frames.clear()
            self._bytes = 0
            self._duration_ns = 0
            return released

    def _remove_oldest(self) -> AudioFrame:
        old = self._frames.popleft()
        self._bytes -= old.nbytes
        self._duration_ns -= old.duration_ns
        return old

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def oldest_monotonic_ns(self) -> int | None:
        with self._lock:
            return self._frames[0].monotonic_ns if self._frames else None

    @property
    def queued_duration_ns(self) -> int:
        with self._lock:
            return self._duration_ns

    @property
    def dropped_frames(self) -> int:
        with self._lock:
            return self._dropped_frames

    @property
    def dropped_samples(self) -> int:
        with self._lock:
            return self._dropped_samples
