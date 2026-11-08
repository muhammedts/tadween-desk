"""VAD-driven cutting of one source's 16 kHz audio into bounded recognizer chunks.

Accumulate audio into exact VAD frames; on speech, start a chunk with pre-roll; end it
after `end_silence_ms` keeping `post_roll_ms` of the silence; after `soft_chunk_ms` cut
at the next non-speech frame; at `hard_chunk_ms` force a cut and repeat the last
`forced_overlap_ms` at the start of the next chunk (recorded as its overlap). Chunks
with less than `min_speech_ms` of speech are discarded and counted, never decoded.

One segmenter (and one VAD state) serves one contiguous run of one source; the
pipeline creates a fresh one for every new run, epoch or privacy generation. All
retained audio is bounded: pre-roll, the active chunk (at most the hard limit) and
less than one frame of remainder.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from meeting_minutes.speech.config import SAMPLES_PER_MS, SpeechConfig


class FrameVad(Protocol):
    def is_speech(self, frame: np.ndarray) -> bool:
        """Classify one `vad_frame_samples` window of 16 kHz mono float32."""
        ...

    def reset(self) -> None: ...


@dataclass(frozen=True, slots=True, eq=False)
class Cut:
    start: int  # 16 kHz sample index within the run
    samples: np.ndarray = field(repr=False)
    overlap: int  # leading samples repeated from the previous cut of this run
    forced: bool  # ended at the hard limit, so the next cut repeats its tail
    reason: str  # end_of_speech | soft_limit | hard_limit | flush


class Segmenter:
    def __init__(self, vad: FrameVad, config: SpeechConfig) -> None:
        self._vad = vad
        self._frame = config.vad_frame_samples
        self._pre_frames = config.frames(config.pre_roll_ms)
        self._post_frames = config.frames(config.post_roll_ms)
        self._end_frames = config.frames(config.end_silence_ms)
        self._min_voiced = config.frames(config.min_speech_ms)
        self._soft = config.soft_chunk_ms * SAMPLES_PER_MS
        self._hard = config.hard_chunk_ms * SAMPLES_PER_MS
        self._overlap_frames = config.frames(config.forced_overlap_ms)
        self._remainder = np.zeros(0, dtype=np.float32)
        self._pos = 0  # run index of the next whole frame
        self._pre: deque[np.ndarray] = deque(maxlen=self._pre_frames)
        self._active: list[tuple[np.ndarray, bool]] = []
        self._start = 0
        self._overlap = 0
        self._trailing = 0
        self.short_discarded = 0

    @property
    def active_samples(self) -> int:
        return len(self._active) * self._frame

    @property
    def open_overlap_samples(self) -> int:
        """Leading samples of the open chunk repeated from the previous cut."""
        return self._overlap if self._active else 0

    @property
    def uncommitted_start(self) -> int:
        """First run sample not yet handed out in a cut: the open chunk's start, or the
        start of the pre-roll that the next chunk would begin with."""
        return self._start if self._active else self._pos - len(self._pre) * self._frame

    @property
    def owned_bytes(self) -> int:
        frames = len(self._active) + len(self._pre)
        return frames * self._frame * 4 + int(self._remainder.nbytes)

    def feed(self, samples: np.ndarray) -> list[Cut]:
        cuts: list[Cut] = []
        buf = np.concatenate((self._remainder, samples)) if self._remainder.size else samples
        whole = buf.shape[0] // self._frame
        for k in range(whole):
            # Copied, so a retained frame never pins the caller's larger buffer.
            self._step(np.array(buf[k * self._frame:(k + 1) * self._frame], dtype=np.float32), cuts)
        self._remainder = np.array(buf[whole * self._frame:], dtype=np.float32)
        return cuts

    def flush(self) -> list[Cut]:
        """End of the run: emit the open chunk (if it holds enough speech)."""
        cuts: list[Cut] = []
        if self._active:
            keep = self._trailing_kept()
            del self._active[keep:]
            if self._trailing < self._post_frames and self._remainder.size:
                tail = self._remainder  # speech ran to the very end of the run
            else:
                tail = None
            self._close("flush", cuts, forced=False, tail=tail)
        self._remainder = np.zeros(0, dtype=np.float32)
        self._pre.clear()
        return cuts

    def snapshot(self) -> tuple[int, int, np.ndarray] | None:
        """(start, overlap, copy of samples) of the open chunk, for a partial decode."""
        if not self._active:
            return None
        return self._start, self._overlap, np.concatenate([f for f, _ in self._active])

    # ----------------------------------------------------------------- internals

    def _step(self, frame: np.ndarray, cuts: list[Cut]) -> None:
        speech = bool(self._vad.is_speech(frame))
        index = self._pos
        self._pos += self._frame
        if not self._active:
            if speech:
                self._start = index - len(self._pre) * self._frame
                self._active = [(f, False) for f in self._pre] + [(frame, True)]
                self._pre.clear()
                self._overlap = 0
                self._trailing = 0
            else:
                self._pre.append(frame)
            return
        self._active.append((frame, speech))
        self._trailing = 0 if speech else self._trailing + 1
        length = self.active_samples
        if self._trailing >= self._end_frames:
            keep = self._trailing_kept()
            silence = [f for f, _ in self._active[keep:]]
            del self._active[keep:]
            self._close("end_of_speech", cuts, forced=False)
            self._pre.extend(silence)  # the silence before the next utterance is its pre-roll
        elif not speech and length >= self._soft:
            self._close("soft_limit", cuts, forced=False)
        elif length >= self._hard:
            carried = self._active[-self._overlap_frames:]
            self._close("hard_limit", cuts, forced=True)
            self._active = list(carried)
            self._start = self._pos - len(carried) * self._frame
            self._overlap = len(carried) * self._frame
            self._trailing = 0

    def _trailing_kept(self) -> int:
        """Number of active frames to keep: drop trailing silence beyond the post-roll."""
        return len(self._active) - max(0, self._trailing - self._post_frames)

    def _close(self, reason: str, cuts: list[Cut], *, forced: bool, tail: np.ndarray | None = None) -> None:
        voiced = sum(1 for _, s in self._active if s)
        if voiced >= self._min_voiced:
            parts = [f for f, _ in self._active]
            if tail is not None:
                parts.append(tail)
            cuts.append(Cut(self._start, np.concatenate(parts), self._overlap, forced, reason))
        else:
            self.short_discarded += 1
        self._active = []
        self._overlap = 0
        self._trailing = 0
