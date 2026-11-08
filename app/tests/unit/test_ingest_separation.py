"""A slow or stuck decode must not stop admitted audio from being
ingested. SpeechWorker runs ingest (2 s inbox -> resample/VAD -> byte-bounded pending
chunks) on its own thread, so the inbox keeps draining while the decode thread waits on
the recognizer; only a backlog beyond the pending cap becomes an explicit gap.
Synthetic tone speech, fake capture clock, no model or network."""

from __future__ import annotations

import time

import numpy as np
from speech_fakes import ToneRecognizer, silence, utterance
from test_speech_pipeline import Harness, gaps

from meeting_minutes.domain.contracts import Source
from meeting_minutes.domain.events import GapReason
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.pipeline import SpeechWorker


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def talk(n: int) -> np.ndarray:
    return np.concatenate([np.concatenate((utterance(["hello"]), silence(800))) for _ in range(n)])


def feed(h: Harness, source: Source, samples: np.ndarray, *, batch: int = 50, pause_s: float = 0.0,
         drained: bool = True) -> None:
    """Deliver 10 ms frames in batches (0.5 s of audio each), as the capture worker would;
    with `drained`, wait after each batch until the ingest thread has emptied the inbox."""
    frames = h.frames(source, samples)
    for i in range(0, len(frames), batch):
        for frame in frames[i:i + batch]:
            h.now = max(h.now, frame.monotonic_ns + frame.duration_ns)
            h.pipeline.accept(frame)
        if drained:
            assert wait_until(lambda: h.pipeline.status()["sources"][source.value]["inbox_ms"] == 0, 2.0), \
                "the inbox stopped draining while a decode was waiting"
        if pause_s:
            time.sleep(pause_s)


def test_a_stuck_decode_does_not_stop_ingest_and_nothing_is_dropped() -> None:
    """The first A twenty-minute run: one request hung for 20 s and both inboxes
    overflowed. Now 11 s of speech keeps flowing into pending chunks meanwhile."""
    h = Harness()
    h.recognizer.block = True  # the first decode waits until released
    worker = SpeechWorker(h.pipeline, interval_s=0.005)
    worker.start()
    try:
        feed(h, Source.SYSTEM, talk(10))
        assert h.recognizer.entered.is_set()  # a decode was hung during all of it
        status = h.pipeline.status()["sources"]["system"]
        assert status["pending_chunks"] >= 8 and gaps(h, GapReason.STT_BACKLOG) == []
        h.recognizer.release.set()
        assert wait_until(lambda: len(h.finals(Source.SYSTEM)) == 10)
    finally:
        h.recognizer.release.set()
        assert worker.stop(timeout_s=5)
    assert h.finals(Source.SYSTEM) == ["hello"] * 10 and h.text.gaps == []


class SlowRecognizer(ToneRecognizer):
    """Every decode takes 0.15 s of wall time (a loaded CPU), while audio arrives at
    about 25x real time: one decode spans ~3.75 s of audio, more than the 2 s inbox."""

    def transcribe(self, chunk, **kwargs):
        time.sleep(0.15)
        return super().transcribe(chunk)


def test_slow_decodes_under_contention_delay_text_instead_of_dropping_audio() -> None:
    h = Harness(recognizer=SlowRecognizer())
    worker = SpeechWorker(h.pipeline, interval_s=0.005)
    worker.start()
    try:
        feed(h, Source.SYSTEM, talk(12), pause_s=0.02, drained=False)  # no waiting for the decoder
        assert gaps(h, GapReason.STT_BACKLOG) == []
        assert wait_until(lambda: len(h.finals(Source.SYSTEM)) == 12, 10.0)
    finally:
        assert worker.stop(timeout_s=5)
    assert h.text.gaps == []


def test_a_backlog_beyond_the_pending_cap_is_still_an_explicit_gap() -> None:
    """The bound stays: whole oldest chunks beyond `max_pending_ms_per_source` become
    `stt_backlog` gaps; every utterance is either decoded or reported."""
    h = Harness(SpeechConfig(partials=False, max_pending_ms_per_source=3_000))
    h.recognizer.block = True
    worker = SpeechWorker(h.pipeline, interval_s=0.005)
    worker.start()
    try:
        feed(h, Source.SYSTEM, talk(10))
        dropped = gaps(h, GapReason.STT_BACKLOG)
        assert dropped and h.pipeline.status()["sources"]["system"]["pending_ms"] <= 3_000
        h.recognizer.release.set()
        assert wait_until(lambda: len(h.finals(Source.SYSTEM)) + len(gaps(h, GapReason.STT_BACKLOG)) == 10)
    finally:
        h.recognizer.release.set()
        assert worker.stop(timeout_s=5)


def test_mic_exclusion_while_a_decode_is_stuck_discards_mic_and_system_continues() -> None:
    h = Harness()
    h.recognizer.block = True
    worker = SpeechWorker(h.pipeline, interval_s=0.005)
    worker.start()
    try:
        feed(h, Source.SYSTEM, talk(1))
        assert wait_until(h.recognizer.entered.is_set)  # the system chunk is being decoded
        feed(h, Source.MIC, talk(3))  # ingested into mic pending chunks meanwhile
        assert h.pipeline.status()["sources"]["mic"]["pending_chunks"] == 3
        h.boundary.change(False, expected_generation=h.boundary.generation())
        h.pipeline.reset(Source.MIC)  # as the capture controller does inside the exclusion
        feed(h, Source.SYSTEM, talk(2))
        h.recognizer.release.set()
        assert wait_until(lambda: len(h.finals(Source.SYSTEM)) == 3)
    finally:
        h.recognizer.release.set()
        assert worker.stop(timeout_s=5)
    assert h.finals(Source.MIC) == [] and h.recognizer.words_seen(Source.MIC) == []
    assert h.pipeline.status()["sources"]["mic"]["pending_chunks"] == 0


def test_stop_with_a_stuck_decode_ends_ingest_and_reports_the_decode_as_still_running() -> None:
    h = Harness()
    h.recognizer.block = True
    worker = SpeechWorker(h.pipeline, interval_s=0.005)
    worker.start()
    try:
        feed(h, Source.SYSTEM, talk(2))
        assert wait_until(h.recognizer.entered.is_set)
        started = time.monotonic()
        assert not worker.stop(timeout_s=0.2)  # bounded: the decode cannot be interrupted
        assert time.monotonic() - started < 1.0
        assert not worker._ingest_thread.is_alive()
    finally:
        h.recognizer.release.set()
        assert worker.stop(timeout_s=5)
