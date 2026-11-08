"""Mic mute/unmute through the actual speech pipeline and recognizer adapter.

The capture controller (fake devices, fake clock) feeds the speech pipeline with the
production soxr resampler; a spy recognizer records every chunk at the adapter input
and the collector records every text the consumer accepted. Canary words spoken
while the mic is excluded must be absent from both; system speech must continue.
"""

from __future__ import annotations

import threading

import numpy as np
from capture_rig import MEETING, MIC, MIC2, SECOND, SYSTEM, Rig
from speech_fakes import CANARY_WORD, EnergyVad, ToneRecognizer, scripted, tone

from meeting_minutes.domain.contracts import AudioFrame, Source, TimingQuality
from meeting_minutes.domain.events import MicPrivacyStateEvent
from meeting_minutes.speech.collector import TranscriptCollector
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.pipeline import SpeechPipeline

RATES = {Source.MIC: 48_000, Source.SYSTEM: 44_100}
MIC2_RATE = 16_000


class SpeechRig:
    def __init__(self, *, mic_included: bool = True, config: SpeechConfig = SpeechConfig(partials=False)) -> None:
        self.text = TranscriptCollector()
        self.recognizer = ToneRecognizer()
        self.rig: Rig | None = None
        self.pipeline = SpeechPipeline(self.recognizer, EnergyVad, on_text=self.text, config=config,
                                       clock=lambda: self.rig.now)
        self.rig = Rig(mic_included=mic_included, sink=self.pipeline)
        self.pipeline.attach(self.rig.controller)
        self.recognizer.boundary = self.rig.controller.boundary

    @property
    def controller(self):
        return self.rig.controller

    def say(self, source: Source, events: list[tuple[float, str]], rate: int | None = None) -> None:
        """Script the currently open stream of `source`; times are seconds since it opened."""
        self.rig.handle(source).signal = scripted(rate or RATES[source], events)

    def run(self, until_s: float, *, speech: bool = True) -> None:
        target = int(until_s * SECOND)
        while self.rig.now < target:
            self.rig.run(min(target, self.rig.now + SECOND // 10))
            while speech and self.pipeline.step():
                pass

    def finals(self, source: Source) -> list[str]:
        return [t.text for t in self.text.finals if t.source is source]

    def all_text(self) -> str:
        finals, partials, _ = self.text.snapshot()
        return " ".join(t.text for t in finals + partials)


def racing_mic_frame(rig: SpeechRig, generation: int, index: int) -> AudioFrame:
    """A native mic callback stamped before the exclusion but delivered after it."""
    data = tone(CANARY_WORD, 400, rate=48_000)[:, None]
    return AudioFrame(meeting_id=MEETING, source=Source.MIC, epoch=rig.controller.source_epoch(Source.MIC),
                      privacy_generation=generation, sequence=10_000, first_sample_index=index, sample_rate=48_000,
                      channels=1, monotonic_ns=rig.rig.now, timing_quality=TimingQuality.SYNTHETIC, data=data)


def test_excluded_mic_canary_never_reaches_the_recognizer_while_system_continues() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5, "hello"), (1.0, "budget")])
    r.say(Source.SYSTEM, [(0.5, "system"), (3.0, "again"), (5.0, "deadline")])
    r.run(2.5)
    generation = r.controller.privacy_generation
    r.rig.exclude()
    r.controller._on_frame(racing_mic_frame(r, generation, index=200_000))  # rejected at admission
    r.run(6.0)
    r.rig.include()  # new mic epoch at t = 6 s
    r.say(Source.MIC, [(0.5, "again"), (1.0, "deadline")])
    r.run(9.0)

    assert r.finals(Source.SYSTEM) == ["system", "again", "deadline"]  # never interrupted
    assert r.finals(Source.MIC) == ["hello budget", "again deadline"]
    assert CANARY_WORD not in r.recognizer.words_seen() and CANARY_WORD not in r.all_text()
    assert all(seen.admitted for seen in r.recognizer.seen)  # adapter input only ever admitted audio
    after = next(t for t in r.text.finals if t.source is Source.MIC and t.text.startswith("again"))
    assert after.epoch == 2 and after.privacy_generation == 2
    assert after.start_ms >= 6_000  # no pre-roll from before the unmute


def test_exclusion_discards_the_open_mic_utterance() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5 + 0.45 * i, "budget") for i in range(8)])  # one long utterance
    r.run(2.0)
    r.rig.exclude()  # mid-utterance: nothing of it was committed
    r.run(5.0)
    assert r.finals(Source.MIC) == []
    assert [s for s in r.recognizer.seen if s.source is Source.MIC] == []
    [event] = [e for e in r.controller.events() if isinstance(e, MicPrivacyStateEvent)]
    assert event.payload.discarded_pending_ms >= 1_400  # open utterance incl. pre-roll


def test_exclusion_discards_cut_but_undecoded_mic_chunks() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5, "hello")])
    r.run(2.0, speech=False)
    r.pipeline.ingest()  # the chunk is cut and waiting for the recognizer
    assert r.pipeline.status()["sources"]["mic"]["pending_chunks"] == 1
    r.rig.exclude()
    r.run(3.0)
    assert [s for s in r.recognizer.seen if s.source is Source.MIC] == [] and r.finals(Source.MIC) == []
    assert r.pipeline.status()["sources"]["mic"]["pending_chunks"] == 0


def test_in_flight_mic_decode_is_rejected_at_commit_after_exclusion() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5, "hello")])
    r.say(Source.SYSTEM, [(3.0, "system")])
    r.run(2.0, speech=False)
    r.pipeline.ingest()
    r.recognizer.block = True
    worker = threading.Thread(target=r.pipeline.decode_next)
    worker.start()
    assert r.recognizer.entered.wait(5)  # the mic chunk is inside the recognizer now
    r.rig.exclude()
    r.rig.include()  # even re-including cannot revive the older generation
    r.recognizer.release.set()
    worker.join(5)
    r.run(5.0)
    assert r.finals(Source.MIC) == []
    assert r.controller.boundary.rejected_commits == 1
    assert r.pipeline.status()["sources"]["mic"]["rejected_at_commit"] == 1
    assert r.finals(Source.SYSTEM) == ["system"]


def test_late_partial_for_a_reset_mic_is_never_shown() -> None:
    r = SpeechRig(config=SpeechConfig(partials=True))
    r.rig.start()
    r.say(Source.MIC, [(0.5 + 0.45 * i, "budget") for i in range(8)])
    r.run(2.0, speech=False)
    r.pipeline.ingest()
    r.recognizer.block = True
    worker = threading.Thread(target=r.pipeline.decode_next)  # a partial of the open utterance
    worker.start()
    assert r.recognizer.entered.wait(5)
    r.rig.exclude()
    r.recognizer.release.set()
    worker.join(5)
    r.run(4.0)
    assert r.text.partials == {} and r.text.partials_shown == 0 and r.finals(Source.MIC) == []


def test_device_switch_ends_the_old_epoch_utterance_and_exclusion_persists() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5, "hello")])
    r.run(0.9)  # still inside the utterance's trailing silence
    switch = r.controller.switch_source(Source.MIC, MIC2, expected_epoch=1)
    assert switch.outcome.value == "applied"
    r.say(Source.MIC, [(0.5, "budget")], rate=MIC2_RATE)
    r.run(3.0)
    assert [(t.text, t.epoch) for t in r.text.finals if t.source is Source.MIC] == [("hello", 1), ("budget", 2)]

    r.rig.exclude()
    recorded = r.controller.switch_source(Source.MIC, MIC, expected_epoch=r.controller.source_epoch(Source.MIC))
    assert recorded.outcome.value == "recorded" and r.rig.handle(Source.MIC) is None  # still excluded
    r.run(5.0)
    assert r.finals(Source.MIC) == ["hello", "budget"]


def test_pause_ends_the_utterance_and_resume_continues_in_a_new_epoch() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.SYSTEM, [(0.5, "hello")])
    r.run(0.9)
    r.controller.pause()
    r.run(2.0)  # no audio arrives; the idle source's utterance is finalized
    assert r.finals(Source.SYSTEM) == ["hello"]
    r.controller.resume()
    r.say(Source.SYSTEM, [(0.5, "again")])
    r.run(4.0)
    assert [(t.text, t.epoch) for t in r.text.finals] == [("hello", 1), ("again", 2)]


def test_start_excluded_transcribes_system_only_until_unmute() -> None:
    r = SpeechRig(mic_included=False)
    r.rig.start()
    r.say(Source.SYSTEM, [(0.5, "system"), (2.5, "again")])
    r.run(4.0)
    assert r.rig.handle(Source.MIC) is None and r.finals(Source.SYSTEM) == ["system", "again"]
    r.rig.include()
    r.say(Source.MIC, [(0.5, "hello")])
    r.run(6.0)
    assert r.finals(Source.MIC) == ["hello"]


def test_speech_pipeline_holds_no_audio_after_stop() -> None:
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5, "hello")])
    r.say(Source.SYSTEM, [(0.5, "system")])
    r.run(1.0)
    r.controller.stop()
    assert r.pipeline.finish(deadline_ns=r.rig.now + 10 * SECOND)
    status = r.pipeline.status()
    assert status["owned_pcm_bytes"] == 0
    assert r.finals(Source.MIC) == ["hello"] and r.finals(Source.SYSTEM) == ["system"]
    assert np.all([s.admitted for s in r.recognizer.seen])


def test_mic_job_picked_before_exclusion_never_enters_the_recognizer() -> None:
    """The worker has taken the mic chunk off the queue but not yet claimed it when
    exclusion completes. The claim, serialized with exclusion, refuses it; the discard
    is intentional, so it is not a coverage loss."""
    r = SpeechRig()
    r.rig.start()
    r.say(Source.MIC, [(0.5, CANARY_WORD)])
    r.say(Source.SYSTEM, [(3.0, "system")])
    r.run(2.0, speech=False)
    r.pipeline.ingest()
    picked, go = threading.Event(), threading.Event()
    claim = r.pipeline._claim

    def late_claim(job):
        picked.set()
        assert go.wait(5)
        return claim(job)

    r.pipeline._claim = late_claim
    worker = threading.Thread(target=r.pipeline.decode_next)
    worker.start()
    try:
        assert picked.wait(5)
        assert r.pipeline.status()["in_flight"]["source"] == "mic"  # owned and visible while picked
        r.rig.exclude()
    finally:
        go.set()
        worker.join(5)
        del r.pipeline._claim
    r.run(5.0)
    assert [s for s in r.recognizer.seen if s.source is Source.MIC] == []
    assert CANARY_WORD.lower() not in r.all_text().lower()
    assert r.finals(Source.SYSTEM) == ["system"]
    status = r.pipeline.status()
    assert status["sources"]["mic"]["rejected_at_dispatch"] == 1 and r.controller.boundary.rejected_at_dispatch == 1
    assert status["in_flight"] is None and r.text.gaps == []
    r.controller.stop()
    assert r.pipeline.finish(deadline_ns=r.rig.now + 10 * SECOND)
