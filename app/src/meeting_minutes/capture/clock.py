"""Sample-count clock for one source epoch.

A native callback reports `frame_count` samples and is observed at a monotonic time
shortly after its last sample was captured. The clock keeps a linear mapping

    capture time of sample i = origin_ns + i * 1e9 / sample_rate

so timestamps never come from callback arrival order. When the observed time runs
ahead of the mapping by more than callback jitter explains, the device delivered no
samples for a while (WASAPI loopback sends nothing while nothing plays; a mic can drop
out): the clock skips the missing sample positions and reports them, so the caller can
record an explicit gap instead of silently compressing time. When the device runs
ahead of the monotonic clock the mapping is never moved backwards (already placed
samples keep their times); the lead is measured and flagged once it exceeds 50 ms.
"""

from __future__ import annotations

from dataclasses import dataclass

from meeting_minutes.domain.contracts import NS_PER_SECOND

DRIFT_FLAG_NS = 50_000_000
MIN_DISCONTINUITY_NS = 250_000_000


@dataclass(frozen=True, slots=True)
class Placement:
    first_sample_index: int
    monotonic_ns: int
    # Sample positions skipped immediately before this buffer (no data delivered).
    skipped_samples: int
    # Device time ahead of the monotonic clock beyond DRIFT_FLAG_NS (0 if within).
    device_lead_ns: int


class SampleClock:
    def __init__(self, sample_rate: int, *, frames_per_buffer: int) -> None:
        if sample_rate <= 0 or frames_per_buffer <= 0:
            raise ValueError("sample_rate and frames_per_buffer must be positive")
        self._rate = sample_rate
        buffer_ns = frames_per_buffer * NS_PER_SECOND // sample_rate
        # Jitter allowance: a few buffers of late scheduling are normal on Windows.
        self._tolerance_ns = max(MIN_DISCONTINUITY_NS, 4 * buffer_ns)
        self._origin_ns: int | None = None
        self._next_index = 0
        self._max_lead_ns = 0

    @property
    def origin_ns(self) -> int | None:
        return self._origin_ns

    @property
    def next_index(self) -> int:
        return self._next_index

    @property
    def max_device_lead_ns(self) -> int:
        return self._max_lead_ns

    @property
    def tolerance_ns(self) -> int:
        return self._tolerance_ns

    def time_of(self, sample_index: int) -> int:
        if self._origin_ns is None:
            raise RuntimeError("clock has no origin yet")
        return self._origin_ns + sample_index * NS_PER_SECOND // self._rate

    def place(self, frame_count: int, observed_ns: int) -> Placement:
        """Assign a sample index and capture time to a buffer observed at `observed_ns`."""
        if frame_count <= 0:
            raise ValueError("frame_count must be positive")
        duration_ns = frame_count * NS_PER_SECOND // self._rate
        if self._origin_ns is None:
            # The first buffer ended when it was observed.
            self._origin_ns = max(0, observed_ns - duration_ns)
        skipped = 0
        lag_ns = observed_ns - self.time_of(self._next_index + frame_count)
        if lag_ns > self._tolerance_ns:
            skipped = lag_ns * self._rate // NS_PER_SECOND
            self._next_index += skipped
        elif -lag_ns > DRIFT_FLAG_NS:
            self._max_lead_ns = max(self._max_lead_ns, -lag_ns)
        first = self._next_index
        self._next_index += frame_count
        lead = -lag_ns if -lag_ns > DRIFT_FLAG_NS else 0
        return Placement(first, self.time_of(first), skipped, lead)
