"""Downmix and stateful resampling to 16 kHz mono.

One resampler per source and contiguous run of audio: its filter history must never
mix two sources, two epochs or the two sides of a discontinuity. Output sample `m` of
a run corresponds to time `run start + m / 16 kHz`; the resampler's filter delay is
absorbed by holding samples back until more input (or `flush`) arrives.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import numpy as np

from meeting_minutes.domain.contracts import TARGET_SAMPLE_RATE


class Resampler(Protocol):
    def process(self, mono: np.ndarray) -> np.ndarray: ...

    def flush(self) -> np.ndarray:
        """Return the held-back tail at the end of a run; the resampler is then spent."""
        ...


ResamplerFactory = Callable[[int], Resampler]


def downmix(data: np.ndarray) -> np.ndarray:
    """(frames, channels) float32 -> new 1-D float32 array (channel mean)."""
    if data.shape[1] == 1:
        return np.array(data[:, 0], dtype=np.float32, copy=True)
    return data.mean(axis=1, dtype=np.float32)


class Passthrough:
    """Input already at 16 kHz."""

    def process(self, mono: np.ndarray) -> np.ndarray:
        return mono

    def flush(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)


class SoxrResampler:
    """libsoxr streaming resampler (python-soxr `ResampleStream`), high quality."""

    def __init__(self, in_rate: int) -> None:
        import soxr  # imported on first use: a Windows-only dependency (pyproject.toml)

        self._stream = soxr.ResampleStream(in_rate, TARGET_SAMPLE_RATE, 1, dtype="float32", quality="HQ")

    def process(self, mono: np.ndarray) -> np.ndarray:
        return np.asarray(self._stream.resample_chunk(mono), dtype=np.float32)

    def flush(self) -> np.ndarray:
        return np.asarray(self._stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True), dtype=np.float32)


def soxr_factory(in_rate: int) -> Resampler:
    return Passthrough() if in_rate == TARGET_SAMPLE_RATE else SoxrResampler(in_rate)
