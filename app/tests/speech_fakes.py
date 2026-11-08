"""Synthetic stand-ins for the speech pipeline's model adapters (no model, no file).

Each "word" is a tone at its own frequency; `ToneRecognizer` turns a chunk back into
words by finding tone peaks, so tests can follow specific words - including privacy
canaries - through the actual adapter input and the text output. These test the
pipeline's contracts only; they say nothing about recognition quality.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import numpy as np

from meeting_minutes.domain.contracts import TARGET_SAMPLE_RATE, AudioChunk, Source
from meeting_minutes.speech.pipeline import RecognizedText

WINDOW = 1024  # 64 ms at 16 kHz; bin width 15.625 Hz
WORDS = {
    "hello": 500.0,
    "budget": 625.0,
    "deadline": 750.0,
    "مرحبا": 875.0,
    "الميزانية": 1000.0,
    "system": 1125.0,
    "again": 1250.0,
    "CANARY": 1500.0,
}
CANARY_WORD = "CANARY"
_BY_BIN = {round(hz * WINDOW / TARGET_SAMPLE_RATE): word for word, hz in WORDS.items()}


def tone(word: str, ms: int, rate: int = TARGET_SAMPLE_RATE, amplitude: float = 0.3, phase0: int = 0) -> np.ndarray:
    n = np.arange(phase0, phase0 + ms * rate // 1000, dtype=np.float64)
    return (amplitude * np.sin(2 * np.pi * WORDS[word] * n / rate)).astype(np.float32)


def silence(ms: int, rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    return np.zeros(ms * rate // 1000, dtype=np.float32)


def utterance(words: list[str], *, word_ms: int = 320, gap_ms: int = 128, rate: int = TARGET_SAMPLE_RATE) -> np.ndarray:
    parts: list[np.ndarray] = []
    for i, word in enumerate(words):
        if i:
            parts.append(silence(gap_ms, rate))
        parts.append(tone(word, word_ms, rate))
    return np.concatenate(parts)


def decode_words(samples: np.ndarray) -> list[str]:
    words: list[str] = []
    previous: str | None = None
    for k in range(samples.shape[0] // WINDOW):
        window = samples[k * WINDOW:(k + 1) * WINDOW].astype(np.float64)
        if np.sqrt(np.mean(window**2)) < 0.05:
            previous = None
            continue
        spectrum = np.abs(np.fft.rfft(window * np.hanning(WINDOW)))
        word = _BY_BIN.get(int(np.argmax(spectrum)))
        if word is not None and word != previous:
            words.append(word)
        previous = word
    return words


class EnergyVad:
    """Speech = RMS above a threshold. Counts resets so tests can see per-source state."""

    def __init__(self, threshold: float = 0.02) -> None:
        self.threshold = threshold
        self.frames = 0
        self.resets = 0

    def is_speech(self, frame: np.ndarray) -> bool:
        assert frame.shape == (512,) and frame.dtype == np.float32
        self.frames += 1
        return float(np.sqrt(np.mean(np.square(frame, dtype=np.float64)))) > self.threshold

    def reset(self) -> None:
        self.resets += 1


@dataclass
class Seen:
    source: Source
    epoch: int
    generation: int
    chunk_index: int
    start_ms: int
    end_ms: int
    overlap_ms: int
    words: list[str]
    admitted: bool | None  # privacy boundary verdict at adapter input


class ToneRecognizer:
    """Spy recognizer: records every chunk it is given (metadata and decoded words).

    `block` makes the next decode wait until `release` is set (for in-flight races);
    `fail` makes decodes raise; `script` overrides the text per chunk index.
    """

    def __init__(self, boundary=None) -> None:
        self.seen: list[Seen] = []
        self.boundary = boundary
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.fail = False
        self.script: dict[tuple[Source, int], str] = {}
        self.language: str | None = None

    def transcribe(self, chunk: AudioChunk) -> RecognizedText:
        words = decode_words(chunk.data)
        state = self.boundary.state if self.boundary is not None else None
        self.seen.append(Seen(chunk.source, chunk.epoch, chunk.privacy_generation, chunk.chunk_index,
                              chunk.start_ms, chunk.end_ms, chunk.overlap_ms, words,
                              None if state is None else state.admits(chunk.source, chunk.privacy_generation)))
        if self.block:
            self.block = False
            self.entered.set()
            assert self.release.wait(5), "test never released the blocked decode"
        if self.fail:
            raise RuntimeError("synthetic decode failure")
        text = self.script.get((chunk.source, chunk.chunk_index), " ".join(words))
        return RecognizedText(text, self.language)

    def words_seen(self, source: Source | None = None) -> list[str]:
        return [w for s in self.seen if source is None or s.source is source for w in s.words]


class LinearResampler:
    """Test-only stateful linear-interpolation resampler (production uses soxr)."""

    def __init__(self, rate: int) -> None:
        self.step = rate / TARGET_SAMPLE_RATE
        self.pos = 0.0  # next output position, in input samples since the run began
        self.base = 0  # input index of tail[0]
        self.tail = np.zeros(0, dtype=np.float32)

    def process(self, mono: np.ndarray) -> np.ndarray:
        buf = np.concatenate((self.tail, mono))
        end = self.base + buf.shape[0] - 1  # last position with a right neighbour
        count = max(0, int(np.floor((end - self.pos) / self.step)) + 1) if end >= self.pos else 0
        positions = self.pos + self.step * np.arange(count)
        out = np.interp(positions - self.base, np.arange(buf.shape[0]), buf).astype(np.float32)
        self.pos += self.step * count
        keep = buf.shape[0] - 1
        self.base += keep
        self.tail = buf[keep:].copy()
        return out

    def flush(self) -> np.ndarray:
        return np.zeros(0, dtype=np.float32)


def linear_factory(rate: int):
    from meeting_minutes.speech.resample import Passthrough

    return Passthrough() if rate == TARGET_SAMPLE_RATE else LinearResampler(rate)


def scripted(rate: int, events: list[tuple[float, str]], *, word_ms: int = 320, amplitude: float = 0.3):
    """A FakeCaptureHandle `signal`: each (seconds since stream open, word) is a tone."""
    spans = [(int(t * rate), int(t * rate) + word_ms * rate // 1000, WORDS[w]) for t, w in events]

    def signal(first: int, count: int, channels: int) -> np.ndarray:
        n = np.arange(first, first + count)
        mono = np.zeros(count, dtype=np.float64)
        for start, end, hz in spans:
            inside = (n >= start) & (n < end)
            if inside.any():
                mono[inside] = amplitude * np.sin(2 * np.pi * hz * n[inside] / rate)
        return np.repeat(mono.astype(np.float32)[:, None], channels, axis=1)

    return signal
