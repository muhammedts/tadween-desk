"""Speech pipeline contracts with synthetic adapters: lineage, fairness, bounds, partials, gaps,
fault accounting and the finish lifecycle."""

from __future__ import annotations

import threading
import time
from uuid import UUID

import numpy as np
import pytest
from speech_fakes import EnergyVad, LinearResampler, ToneRecognizer, linear_factory, silence, tone, utterance

from meeting_minutes.capture.gate import PrivacyBoundary
from meeting_minutes.domain.contracts import NS_PER_SECOND, AudioFrame, Source, TimingQuality
from meeting_minutes.domain.events import GapReason
from meeting_minutes.domain.transcript import SpeechGap, TextKind, TranscriptText
from meeting_minutes.speech.collector import TranscriptCollector
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.pipeline import FAULT_LOG, SpeechPipeline, SpeechWorker

MEETING = UUID(int=3003)
MS = 1_000_000


class Harness:
    """Feeds 10 ms native frames straight into the pipeline's sink side on a fake clock."""

    def __init__(self, config: SpeechConfig = SpeechConfig(partials=False), *, rates=None,
                 vad_class: type[EnergyVad] = EnergyVad, resampler_factory=linear_factory,
                 recognizer=None, consumer=None) -> None:
        self.now = 0
        self.boundary = PrivacyBoundary(mic_included=True)
        self.recognizer = recognizer or ToneRecognizer(self.boundary)
        self.text = TranscriptCollector()
        self.vads: list[EnergyVad] = []
        self.vad_class = vad_class
        self.pipeline = SpeechPipeline(
            self.recognizer, self._vad, on_text=consumer or self.text, resampler_factory=resampler_factory,
            config=config, clock=lambda: self.now,
        )
        self.pipeline.bind(self.boundary, lambda: 0)
        self.rates = rates or {Source.MIC: 16_000, Source.SYSTEM: 16_000}
        self.index = {Source.MIC: 0, Source.SYSTEM: 0}
        self.epoch = {Source.MIC: 1, Source.SYSTEM: 1}

    def _vad(self) -> EnergyVad:
        vad = self.vad_class()
        self.vads.append(vad)
        return vad

    def frames(self, source: Source, samples: np.ndarray, *, channels: int = 1) -> list[AudioFrame]:
        rate = self.rates[source]
        step = rate // 100
        out = []
        for i in range(0, samples.shape[0], step):
            part = samples[i:i + step]
            first = self.index[source]
            self.index[source] += part.shape[0]
            out.append(AudioFrame(
                meeting_id=MEETING, source=source, epoch=self.epoch[source],
                privacy_generation=self.boundary.generation(), sequence=first // step, first_sample_index=first,
                sample_rate=rate, channels=channels, monotonic_ns=first * NS_PER_SECOND // rate,
                timing_quality=TimingQuality.SYNTHETIC, data=np.repeat(part[:, None], channels, axis=1),
            ))
        return out

    def play(self, *, step: bool = True, **audio: np.ndarray) -> None:
        """Deliver both sources' audio interleaved in time, as the capture worker would,
        letting the speech worker run every 100 ms of audio unless `step` is False."""
        streams = {Source(k): self.frames(Source(k), v) for k, v in audio.items()}
        merged = sorted((f for fs in streams.values() for f in fs), key=lambda f: (f.monotonic_ns, f.source.value))
        for i, frame in enumerate(merged):
            self.now = max(self.now, frame.monotonic_ns + frame.duration_ns)
            self.pipeline.accept(frame)
            if step and i % (10 * len(streams)) == 0:
                self.settle()

    def settle(self, steps: int = 200) -> None:
        for _ in range(steps):
            if not self.pipeline.step():
                break

    def finals(self, source: Source) -> list[str]:
        return [t.text for t in self.text.finals if t.source is source]


def test_two_sources_are_segmented_independently_and_keep_original_script() -> None:
    h = Harness()
    h.play(mic=np.concatenate((silence(500), utterance(["hello", "budget"]), silence(1000))),
           system=np.concatenate((silence(300), utterance(["مرحبا", "الميزانية"]), silence(1200))))
    h.settle()
    assert h.finals(Source.MIC) == ["hello budget"]
    assert h.finals(Source.SYSTEM) == ["مرحبا الميزانية"]
    assert len(h.vads) == 2 and all(v.frames > 0 for v in h.vads)
    mic = next(t for t in h.text.finals if t.source is Source.MIC)
    # Lineage: chunk starts at speech onset minus the pre-roll, in meeting time.
    assert 500 - 320 - 32 <= mic.start_ms <= 500 - 320 + 32
    assert (mic.epoch, mic.privacy_generation, mic.hypothesis_id, mic.timing_quality) == (1, 0, "mic:0", "chunk")


def test_native_rates_are_resampled_per_source_with_lineage() -> None:
    h = Harness(rates={Source.MIC: 48_000, Source.SYSTEM: 44_100})
    h.play(mic=np.concatenate((silence(1000, 48_000), utterance(["deadline"], rate=48_000), silence(900, 48_000))),
           system=np.concatenate((silence(2000, 44_100), utterance(["system", "again"], rate=44_100),
                                  silence(900, 44_100))))
    h.settle()
    assert h.finals(Source.MIC) == ["deadline"] and h.finals(Source.SYSTEM) == ["system again"]
    starts = {t.source: t.start_ms for t in h.text.finals}
    assert abs(starts[Source.MIC] - (1000 - 320)) <= 40 and abs(starts[Source.SYSTEM] - (2000 - 320)) <= 40


def test_silence_never_reaches_the_recognizer() -> None:
    h = Harness()
    h.play(mic=silence(5000), system=silence(5000))
    h.settle()
    assert h.recognizer.seen == [] and h.text.finals == []


def test_finals_are_scheduled_round_robin_across_sources() -> None:
    h = Harness()
    three = np.concatenate([np.concatenate((utterance(["hello"]), silence(800))) for _ in range(3)])
    h.play(mic=three, system=three)
    h.settle()
    order = [s.source for s in h.recognizer.seen]
    assert len(order) == 6
    assert all(a is not b for a, b in zip(order, order[1:])), order


def test_backlog_is_bounded_and_releases_oldest_whole_chunks_as_gaps() -> None:
    config = SpeechConfig(partials=False, inbox_ms_per_source=120_000, max_pending_ms_per_source=3_000)
    h = Harness(config)
    talk = np.concatenate([np.concatenate((utterance(["hello", "budget"]), silence(800))) for _ in range(8)])
    h.play(system=talk, step=False)
    h.pipeline.step()  # one ingest: 8 chunks cut, backlog trimmed, one decoded
    status = h.pipeline.status()["sources"]["system"]
    assert status["pending_ms"] <= 3_000
    gaps = [g for g in h.text.gaps if g.reason is GapReason.STT_BACKLOG]
    assert gaps and all(g.source is Source.SYSTEM and g.end_ms > g.start_ms for g in gaps)
    h.settle()
    # Every chunk is accounted for: decoded or released as a gap, oldest first.
    assert len(h.recognizer.seen) + len(gaps) == 8
    assert max(g.end_ms for g in gaps) <= min(s.start_ms for s in h.recognizer.seen)


def test_inbox_overflow_releases_oldest_audio_as_a_gap() -> None:
    h = Harness()
    audio = np.concatenate((utterance(["hello"] * 8), silence(2000)))
    h.play(system=audio, step=False)  # the worker never runs
    status = h.pipeline.status()["sources"]["system"]
    assert status["inbox_ms"] <= 2_000
    lost = sum(g.end_ms - g.start_ms for g in h.text.gaps if g.reason is GapReason.STT_BACKLOG)
    assert lost >= audio.shape[0] // 16 - 2_000 - 20


def test_forced_cut_overlap_is_deduplicated_at_commit() -> None:
    h = Harness()
    h.recognizer.script[(Source.SYSTEM, 0)] = "one two three four"
    h.recognizer.script[(Source.SYSTEM, 1)] = "three four five"
    h.play(system=np.concatenate((tone("hello", 13_000), silence(1000))))
    h.settle()
    first, second = [t for t in h.text.finals if t.source is Source.SYSTEM]
    assert first.forced_cut and first.text == "one two three four"
    assert (second.text, second.overlap_trimmed_words, second.boundary_ambiguous) == ("five", 2, False)


def test_unalignable_overlap_is_kept_and_flagged() -> None:
    h = Harness()
    h.recognizer.script[(Source.SYSTEM, 0)] = "one two"
    h.recognizer.script[(Source.SYSTEM, 1)] = "seven eight"
    h.play(system=np.concatenate((tone("hello", 13_000), silence(1000))))
    h.settle()
    second = h.text.finals[-1]
    assert (second.text, second.boundary_ambiguous) == ("seven eight", True)


def test_partials_replace_by_hypothesis_and_yield_to_finals() -> None:
    h = Harness(SpeechConfig(partials=True))
    h.play(system=tone("budget", 3_000))  # utterance still open
    h.settle()
    assert [p.hypothesis_id for p in h.text.partials.values()] == ["system:0"]
    assert h.text.finals == []
    h.play(system=silence(1_000))
    h.settle()
    assert [t.hypothesis_id for t in h.text.finals] == ["system:0"]
    assert h.text.partials == {}  # replaced by the final


def test_no_partial_is_decoded_while_a_final_waits() -> None:
    h = Harness(SpeechConfig(partials=True))
    h.play(mic=np.concatenate((utterance(["hello"]), silence(800))), system=tone("budget", 3_000), step=False)
    h.pipeline.step()
    assert [s.source for s in h.recognizer.seen] == [Source.MIC]  # the final first


def test_decode_failure_becomes_a_gap_and_processing_continues() -> None:
    h = Harness()
    h.recognizer.fail = True
    h.play(system=np.concatenate((utterance(["hello"]), silence(800))))
    h.settle()
    h.recognizer.fail = False
    h.play(system=np.concatenate((utterance(["again"]), silence(800))))
    h.settle()
    assert [g.reason for g in h.text.gaps] == [GapReason.STT_FAILED]
    assert h.finals(Source.SYSTEM) == ["again"]


def test_idle_source_ends_its_utterance() -> None:
    """WASAPI loopback stops delivering when playback stops; pause closes streams."""
    h = Harness()
    h.play(system=utterance(["hello"]))  # no trailing silence delivered at all
    h.settle()
    assert h.text.finals == []
    h.now += 700 * MS
    h.settle()
    assert h.finals(Source.SYSTEM) == ["hello"]


def test_new_epoch_ends_the_old_utterance_and_resets_vad_state() -> None:
    h = Harness()
    h.play(system=utterance(["hello"]))
    h.epoch[Source.SYSTEM] = 2
    h.index[Source.SYSTEM] = 0
    h.play(system=np.concatenate((utterance(["again"]), silence(900))))
    h.settle()
    assert h.finals(Source.SYSTEM) == ["hello", "again"]
    assert [t.epoch for t in h.text.finals] == [1, 2]
    assert h.vads[0].resets == 1  # same source VAD, reset for the new run


def test_sample_discontinuity_starts_a_new_run() -> None:
    h = Harness()
    h.play(system=utterance(["hello"]))
    h.index[Source.SYSTEM] += 16_000  # one second missing (a capture gap)
    h.play(system=np.concatenate((utterance(["again"]), silence(900))))
    h.settle()
    assert h.finals(Source.SYSTEM) == ["hello", "again"]
    hello, again = h.text.finals
    assert again.start_ms >= hello.end_ms + 900  # the missing second is not squeezed out


def test_finish_closes_open_utterances_and_marks_undrained_work() -> None:
    h = Harness()
    h.play(mic=utterance(["hello"]), system=utterance(["budget"]))
    assert h.pipeline.finish(deadline_ns=h.now + 10 * NS_PER_SECOND)
    assert h.finals(Source.MIC) == ["hello"] and h.finals(Source.SYSTEM) == ["budget"]

    h2 = Harness()
    h2.play(mic=utterance(["hello"]), system=utterance(["budget"]), step=False)
    assert not h2.pipeline.finish(deadline_ns=h2.now)  # no time left: nothing decoded
    assert h2.text.finals == []
    assert sorted(g.source.value for g in h2.text.gaps if g.reason is GapReason.STOPPED_UNDRAINED) == ["mic", "system"]


def test_owned_pcm_stays_bounded_under_continuous_two_source_speech() -> None:
    h = Harness(rates={Source.MIC: 48_000, Source.SYSTEM: 16_000})
    peak = 0
    for _ in range(30):  # 30 s of continuous speech on both sources, decoding kept up
        h.play(mic=tone("hello", 1_000, rate=48_000), system=tone("budget", 1_000))
        h.settle()
        peak = max(peak, h.pipeline.status()["owned_pcm_bytes"])
    # inbox (2 s at 48 kHz) + open chunk (<= 12.4 s at 16 kHz) per source, with slack
    assert peak < 2 * (2 * 48_000 * 4 + 13 * 16_000 * 4)


def test_unbound_pipeline_refuses_to_decode() -> None:
    pipeline = SpeechPipeline(ToneRecognizer(), EnergyVad, on_text=TranscriptCollector())
    with pytest.raises(RuntimeError):
        pipeline.step()


def test_text_events_never_show_text_in_repr() -> None:
    h = Harness()
    h.play(system=np.concatenate((utterance(["hello"]), silence(900))))
    h.settle()
    assert "hello" not in repr(h.text.finals[0]) and h.text.finals[0].kind is TextKind.FINAL


def test_dispatch_rejects_an_excluded_generation_even_without_a_reset() -> None:
    """Defence in depth: the recognizer input check alone stops stale mic chunks."""
    h = Harness()
    h.play(mic=np.concatenate((utterance(["hello"]), silence(800))), step=False)
    h.pipeline.ingest()
    h.boundary.change(False, expected_generation=0)  # no pipeline.reset on purpose
    h.settle()
    assert h.recognizer.seen == [] and h.text.finals == []
    assert h.pipeline.status()["sources"]["mic"]["rejected_at_input"] == 1


def test_reset_alone_discards_waiting_chunks_and_the_open_utterance() -> None:
    """Defence in depth: reset clears mic work even if the generation did not change."""
    h = Harness()
    h.play(mic=np.concatenate((utterance(["hello"]), silence(800), utterance(["budget"]))), step=False)
    h.pipeline.ingest()  # "hello" waiting, "budget" still open
    discarded_ns = h.pipeline.reset(Source.MIC)
    assert discarded_ns >= 2 * 320 * MS
    status = h.pipeline.status()  # released at once, not left for the claim to refuse
    assert status["sources"]["mic"]["pending_chunks"] == 0 and status["owned_pcm_bytes"] == 0
    h.now += 5 * NS_PER_SECOND  # long enough for an idle utterance to be finalized
    h.settle()
    assert h.recognizer.seen == [] and h.text.finals == []
    assert h.pipeline.status()["owned_pcm_bytes"] == 0


def test_reset_between_pick_and_claim_keeps_the_job_out_of_the_recognizer() -> None:
    """Defence in depth: the claim also refuses a job whose source was reset after it
    was picked, even when the privacy generation did not change."""
    h = Harness()
    h.play(mic=np.concatenate((utterance(["hello"]), silence(800))), step=False)
    h.pipeline.ingest()
    decode = h.pipeline._decode

    def reset_first(job):
        h.pipeline.reset(Source.MIC)
        decode(job)

    h.pipeline._decode = reset_first
    assert h.pipeline.decode_next()
    assert h.recognizer.seen == [] and h.text.finals == []
    assert h.pipeline.status()["sources"]["mic"]["rejected_at_dispatch"] == 1


# ------------------------------------------------- faults and the finish lifecycle


class Switch:
    def __init__(self) -> None:
        self.on = False


class FlakyResampler:
    """Wraps the test resampler; raises while `switch` is on."""

    def __init__(self, inner, switch: Switch, *, on_flush: bool = False) -> None:
        self.inner, self.switch, self.on_flush = inner, switch, on_flush

    def process(self, mono):
        if self.switch.on and not self.on_flush:
            raise RuntimeError("injected resampler failure")
        return self.inner.process(mono)

    def flush(self):
        if self.switch.on and self.on_flush:
            raise RuntimeError("injected resampler flush failure")
        return self.inner.flush()


def flaky_factory(fail_rate: int, switch: Switch, *, on_flush: bool = False):
    def make(rate: int):
        inner = linear_factory(rate)
        return FlakyResampler(inner, switch, on_flush=on_flush) if rate == fail_rate else inner
    return make


class FailingVad(EnergyVad):
    failing = False

    def is_speech(self, frame):
        if FailingVad.failing:
            raise RuntimeError("injected VAD failure")
        return super().is_speech(frame)


def gaps(h: Harness, reason: GapReason) -> list[SpeechGap]:
    return [g for g in h.text.gaps if g.reason is reason]


def test_resampler_failure_reports_the_lost_utterance_and_recovers_with_true_times() -> None:
    switch = Switch()
    h = Harness(rates={Source.MIC: 48_000, Source.SYSTEM: 16_000},
                resampler_factory=flaky_factory(16_000, switch))
    mic = np.concatenate((silence(300, 48_000), tone("budget", 2_000, rate=48_000), silence(900, 48_000)))
    h.play(mic=mic[:1_800 * 48], system=np.concatenate((silence(500), tone("hello", 1_000))))
    switch.on = True
    h.play(system=tone("hello", 10))  # the system frame at 1500-1510 ms fails
    switch.on = False
    h.play(mic=mic[1_800 * 48:], system=np.concatenate((silence(1_000), utterance(["again"]), silence(900))))
    h.settle()

    [gap] = gaps(h, GapReason.PROCESSING_FAILED)
    assert gap.source is Source.SYSTEM and gap.start_ms <= 500 - 320 + 32 and gap.end_ms == 1_510
    assert h.finals(Source.SYSTEM) == ["again"]  # the open "hello" was lost, not compressed in
    again = [t for t in h.text.finals if t.source is Source.SYSTEM][0]
    assert abs(again.start_ms - (2_510 - 320)) <= 40  # the new run is anchored at its own time
    assert h.finals(Source.MIC) == ["budget"]  # the other source's open utterance survived
    status = h.pipeline.status()
    assert status["sources"]["system"]["processing_failures"] == 1 and status["faults"] == 1
    assert status["fault_log"] == [{"stage": "front_end", "source": "system", "error": "RuntimeError"}]
    assert not h.pipeline.finish(deadline_ns=h.now + 10 * NS_PER_SECOND)
    assert h.pipeline.finish_report["drain_complete"]


def test_persistent_vad_failure_is_one_bounded_run_of_gaps_with_a_bounded_fault_log() -> None:
    h = Harness(vad_class=FailingVad)
    FailingVad.failing = True
    try:
        h.play(system=tone("hello", 7_000))
        h.settle()
    finally:
        FailingVad.failing = False
    h.play(system=np.concatenate((silence(1_000), utterance(["again"]), silence(900))))
    h.settle()
    lost = gaps(h, GapReason.PROCESSING_FAILED)
    # Every fourth 10 ms buffer completes a 512-sample VAD window, which fails; the
    # failures merge into gaps of at most 5 s that tile the failing interval.
    assert [(g.start_ms, g.end_ms) for g in lost] == [(0, 5_000), (5_000, 7_000)]
    status = h.pipeline.status()
    assert status["sources"]["system"]["processing_failures"] == status["faults"] == 175
    assert len(status["fault_log"]) == FAULT_LOG
    assert h.finals(Source.SYSTEM) == ["again"]


def test_resampler_flush_failure_at_idle_end_is_a_gap() -> None:
    switch = Switch()
    h = Harness(rates={Source.MIC: 16_000, Source.SYSTEM: 48_000},
                resampler_factory=flaky_factory(48_000, switch, on_flush=True))
    h.play(system=np.concatenate((silence(400, 48_000), utterance(["hello"], rate=48_000))))
    h.settle()
    switch.on = True
    h.now += 700 * MS  # loopback went quiet: the idle utterance is finalized, and flush fails
    h.settle()
    [gap] = gaps(h, GapReason.PROCESSING_FAILED)
    assert gap.start_ms <= 400 - 320 + 32 and gap.end_ms == 400 + 320
    assert h.text.finals == [] and h.recognizer.seen == []


def test_refused_final_is_a_gap_and_is_not_counted_or_used_for_overlap_trimming() -> None:
    refused: list[TranscriptText] = []

    def consumer(event):
        if isinstance(event, TranscriptText) and not refused:
            refused.append(event)
            raise RuntimeError("consumer refused the final")
        h.text(event)

    h = Harness(consumer=consumer)
    h.recognizer.script[(Source.SYSTEM, 0)] = "one two three four"
    h.recognizer.script[(Source.SYSTEM, 1)] = "three four five"
    h.play(system=np.concatenate((tone("hello", 13_000), silence(1000))))
    h.settle()
    [gap] = gaps(h, GapReason.PROCESSING_FAILED)
    assert (gap.start_ms, gap.end_ms) == (refused[0].start_ms, refused[0].end_ms)
    [second] = h.text.finals
    # The refused words were never shown, so nothing is trimmed against them.
    assert (second.text, second.overlap_trimmed_words) == ("three four five", 0)
    counts = h.pipeline.status()["sources"]["system"]
    assert (counts["finals"], counts["delivery_failures"]) == (1, 1)
    assert h.pipeline.status()["fault_log"] == [{"stage": "delivery", "source": "system", "error": "RuntimeError"}]
    assert not h.pipeline.finish(deadline_ns=h.now + NS_PER_SECOND)


def test_consumer_refusing_a_gap_is_a_recorded_fault() -> None:
    def consumer(event):
        if isinstance(event, SpeechGap):
            raise RuntimeError("consumer refused the gap")
        h.text(event)

    h = Harness(consumer=consumer)
    h.recognizer.fail = True
    h.play(system=np.concatenate((utterance(["hello"]), silence(800))))
    h.settle()
    status = h.pipeline.status()
    assert status["speech_gaps"] == 1 and h.text.gaps == []
    assert [f["stage"] for f in status["fault_log"]] == ["recognizer", "consumer"]
    assert not h.pipeline.finish(deadline_ns=h.now + NS_PER_SECOND)


def blocked_decode(h: Harness) -> threading.Thread:
    h.play(system=np.concatenate((utterance(["hello"]), silence(800))), step=False)
    h.pipeline.ingest()
    h.recognizer.block = True
    worker = threading.Thread(target=h.pipeline.decode_next)
    worker.start()
    assert h.recognizer.entered.wait(5)
    return worker


def test_finish_waits_for_a_running_decode_within_its_deadline() -> None:
    h = Harness()
    worker = blocked_decode(h)
    threading.Timer(0.1, h.recognizer.release.set).start()
    started = time.monotonic()
    assert h.pipeline.finish(deadline_ns=h.now + 5 * NS_PER_SECOND)
    assert time.monotonic() - started < 4
    worker.join(5)
    assert h.finals(Source.SYSTEM) == ["hello"]
    report = h.pipeline.finish_report
    assert report["drain_complete"] and not report["abandoned_in_flight"] and report["complete"]


def test_finish_at_the_deadline_voids_the_running_decode_and_reports_it() -> None:
    h = Harness()
    worker = blocked_decode(h)
    try:
        assert not h.pipeline.finish(deadline_ns=h.now)
        status = h.pipeline.status()
        chunk = h.recognizer.seen[0]
        assert status["in_flight"] == {"source": "system", "kind": "final",
                                       "audio_ms": chunk.end_ms - chunk.start_ms, "abandoned": True}
        assert status["owned_pcm_bytes"] > 0  # the recognizer still holds the chunk
        report = h.pipeline.finish_report
        assert (report["abandoned_in_flight"], report["drain_complete"], report["complete"]) == (True, False, False)
        [gap] = gaps(h, GapReason.STOPPED_UNDRAINED)
        assert (gap.source, gap.start_ms, gap.end_ms) == (Source.SYSTEM, chunk.start_ms, chunk.end_ms)
    finally:
        h.recognizer.release.set()
        worker.join(5)
    assert h.text.finals == []  # the late result is never published
    status = h.pipeline.status()
    assert status["sources"]["system"]["rejected_after_finish"] == 1
    assert status["in_flight"] is None and status["owned_pcm_bytes"] == 0
    assert h.pipeline.finish(deadline_ns=h.now + NS_PER_SECOND) is False  # the answer does not change


class CountingRecognizer(ToneRecognizer):
    def __init__(self) -> None:
        super().__init__()
        self.active = self.peak = 0
        self.guard = threading.Lock()

    def transcribe(self, chunk):
        with self.guard:
            self.active += 1
            self.peak = max(self.peak, self.active)
        try:
            time.sleep(0.01)
            return super().transcribe(chunk)
        finally:
            with self.guard:
                self.active -= 1


def test_finish_and_a_running_worker_never_decode_at_the_same_time() -> None:
    h = Harness(SpeechConfig(partials=False, inbox_ms_per_source=120_000), recognizer=CountingRecognizer())
    h.recognizer.boundary = h.boundary
    talk = np.concatenate([np.concatenate((utterance(["hello"]), silence(800))) for _ in range(6)])
    h.play(system=talk, step=False)
    h.pipeline.ingest()
    worker = SpeechWorker(h.pipeline, interval_s=0.001)
    worker.start()
    try:
        assert h.pipeline.finish(deadline_ns=h.now + 10 * NS_PER_SECOND)  # the worker is still running
    finally:
        assert worker.stop(timeout_s=5)
    assert h.recognizer.peak == 1 and h.finals(Source.SYSTEM) == ["hello"] * 6


def test_audio_arriving_after_finish_is_a_gap_not_a_silent_drop() -> None:
    h = Harness()
    assert h.pipeline.finish(deadline_ns=h.now + NS_PER_SECOND)  # nothing captured: complete
    h.play(system=tone("hello", 100), step=False)
    late = gaps(h, GapReason.STOPPED_UNDRAINED)
    assert sum(g.end_ms - g.start_ms for g in late) == 100
    status = h.pipeline.status()
    assert status["sources"]["system"]["rejected_after_finish"] == 10 and status["owned_pcm_bytes"] == 0


def test_an_earlier_backlog_gap_keeps_coverage_incomplete_after_a_full_drain() -> None:
    config = SpeechConfig(partials=False, inbox_ms_per_source=120_000, max_pending_ms_per_source=3_000)
    h = Harness(config)
    talk = np.concatenate([np.concatenate((utterance(["hello", "budget"]), silence(800))) for _ in range(8)])
    h.play(system=talk, step=False)
    h.pipeline.step()
    assert not h.pipeline.finish(deadline_ns=h.now + 10 * NS_PER_SECOND)
    report = h.pipeline.finish_report
    assert report["drain_complete"] and report["speech_gaps"] == len(gaps(h, GapReason.STT_BACKLOG)) > 0
    assert (report["faults"], report["undrained_chunks"]) == (0, 0)


def test_transcribe_report_counts_only_capture_gaps_that_lose_audio() -> None:
    from meeting_minutes.cli import _capture_loss
    from meeting_minutes.domain.events import CaptureGapPayload

    def gap(source: Source, reason: GapReason) -> CaptureGapPayload:
        return CaptureGapPayload(source=source, epoch=1, start_ms=0, end_ms=10, reason=reason)

    assert not _capture_loss(gap(Source.MIC, GapReason.MIC_EXCLUDED_PENDING_DISCARDED))
    assert not _capture_loss(gap(Source.SYSTEM, GapReason.NO_DATA_DELIVERED))  # nothing was playing
    assert _capture_loss(gap(Source.MIC, GapReason.NO_DATA_DELIVERED))  # a mic dropout
    for reason in (GapReason.QUEUE_OVERFLOW, GapReason.DEVICE_SWITCH, GapReason.PROCESSING_FAILED,
                   GapReason.STOPPED_UNDRAINED):
        assert _capture_loss(gap(Source.SYSTEM, reason))


def test_a_decode_started_by_the_drain_cannot_hold_finish_past_its_deadline() -> None:
    """The drain's own decode overruns the stop budget."""
    h = Harness()
    h.play(system=np.concatenate((utterance(["hello"]), silence(800))), step=False)
    h.pipeline.ingest()
    h.recognizer.block = True
    started = time.monotonic()
    try:
        assert not h.pipeline.finish(deadline_ns=h.now + 50 * MS)
        assert time.monotonic() - started < 1.0
        assert h.recognizer.entered.is_set()  # the drain did start recognition
        report = h.pipeline.finish_report
        assert (report["abandoned_in_flight"], report["drain_complete"]) == (True, False)
        assert [g.reason for g in h.text.gaps] == [GapReason.STOPPED_UNDRAINED]
        assert h.pipeline.status()["owned_pcm_bytes"] > 0  # still held by the recognizer
    finally:
        h.recognizer.release.set()
    drain = next((t for t in threading.enumerate() if t.name == "speech-drain"), None)
    if drain is not None:
        drain.join(5)
        assert not drain.is_alive()  # the helper ends once its decode returns
    assert h.text.finals == []
    status = h.pipeline.status()
    assert status["sources"]["system"]["rejected_after_finish"] == 1 and status["owned_pcm_bytes"] == 0


def test_concurrent_finish_calls_share_one_drain_and_one_answer() -> None:
    h = Harness(SpeechConfig(partials=False, inbox_ms_per_source=120_000), recognizer=CountingRecognizer())
    h.recognizer.boundary = h.boundary
    talk = np.concatenate([np.concatenate((utterance(["hello"]), silence(800))) for _ in range(3)])
    h.play(system=talk, step=False)
    answers: list[bool] = []
    callers = [threading.Thread(target=lambda: answers.append(h.pipeline.finish(deadline_ns=h.now + 5 * NS_PER_SECOND)))
               for _ in range(3)]
    for t in callers:
        t.start()
    for t in callers:
        t.join(10)
    assert answers == [True, True, True] and h.recognizer.peak == 1
    assert h.finals(Source.SYSTEM) == ["hello"] * 3
