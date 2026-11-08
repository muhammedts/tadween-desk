"""VAD segmentation: pre/post-roll, end of speech, soft/hard cuts, overlap, bounds."""

from __future__ import annotations

import numpy as np
import pytest
from speech_fakes import EnergyVad, tone

from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.segmenter import Segmenter

F = 512
CFG = SpeechConfig()


def frames(kind: str, n: int) -> np.ndarray:
    return tone("hello", n * F * 1000 // 16000) if kind == "speech" else np.zeros(n * F, dtype=np.float32)


def signal(*parts: tuple[str, int]) -> np.ndarray:
    return np.concatenate([frames(kind, n) for kind, n in parts])


def run(samples: np.ndarray, *, piece: int | None = None, flush: bool = True, config: SpeechConfig = CFG):
    seg = Segmenter(EnergyVad(), config)
    cuts = []
    step = piece or samples.shape[0]
    for i in range(0, samples.shape[0], step):
        cuts += seg.feed(samples[i:i + step])
    if flush:
        cuts += seg.flush()
    return seg, cuts


def test_frame_counts_follow_the_documented_milliseconds() -> None:
    assert (CFG.frames(CFG.pre_roll_ms), CFG.frames(CFG.post_roll_ms), CFG.frames(CFG.end_silence_ms)) == (10, 7, 19)
    assert (CFG.frames(CFG.min_speech_ms), CFG.frames(CFG.forced_overlap_ms)) == (7, 13)


def test_silence_yields_nothing_and_keeps_only_pre_roll() -> None:
    seg, cuts = run(signal(("silence", 2000)), piece=480)
    assert cuts == []
    assert seg.owned_bytes <= 10 * F * 4  # pre-roll only, no growing buffer (flushed state)


def test_one_utterance_has_pre_roll_and_post_roll() -> None:
    _, cuts = run(signal(("silence", 32), ("speech", 32), ("silence", 40)), flush=False)
    assert len(cuts) == 1
    cut = cuts[0]
    assert cut.reason == "end_of_speech" and not cut.forced and cut.overlap == 0
    assert cut.start == (32 - 10) * F
    assert cut.samples.shape[0] == (10 + 32 + 7) * F
    assert np.all(cut.samples[:10 * F] == 0) and np.any(cut.samples[10 * F:11 * F] != 0)


def test_short_blip_is_discarded_and_counted_short_speech_is_kept() -> None:
    seg, cuts = run(signal(("silence", 20), ("speech", 3), ("silence", 40)))
    assert cuts == [] and seg.short_discarded == 1
    _, cuts = run(signal(("silence", 20), ("speech", 8), ("silence", 40)))  # 256 ms "yes"
    assert len(cuts) == 1


def test_hard_cut_repeats_the_overlap_into_the_next_chunk() -> None:
    _, cuts = run(signal(("silence", 32), ("speech", 500), ("silence", 40)))
    assert [c.reason for c in cuts] == ["hard_limit", "end_of_speech"]
    first, second = cuts
    assert first.forced and first.samples.shape[0] == 375 * F
    assert second.overlap == 13 * F and not second.forced
    assert second.start == first.start + (375 - 13) * F
    np.testing.assert_array_equal(second.samples[:13 * F], first.samples[-13 * F:])
    assert all(c.samples.shape[0] <= 375 * F for c in cuts)


def test_soft_cut_prefers_a_pause_after_the_soft_limit() -> None:
    _, cuts = run(signal(("silence", 32), ("speech", 260), ("silence", 1), ("speech", 30), ("silence", 40)))
    assert [c.reason for c in cuts] == ["soft_limit", "end_of_speech"]
    first, second = cuts
    assert not first.forced and second.overlap == 0
    assert second.start == first.start + first.samples.shape[0]  # contiguous, nothing repeated


@pytest.mark.parametrize("piece", [1, 100, 441, 512, 4000])
def test_arbitrary_callback_sizes_give_identical_cuts(piece: int) -> None:
    samples = signal(("silence", 30), ("speech", 40), ("silence", 25), ("speech", 400), ("silence", 30))
    _, reference = run(samples)
    _, cuts = run(samples, piece=piece)
    assert [(c.start, c.samples.shape[0], c.overlap, c.reason) for c in cuts] == \
        [(c.start, c.samples.shape[0], c.overlap, c.reason) for c in reference]


def test_flush_keeps_speech_that_runs_to_the_end_including_the_remainder() -> None:
    samples = np.concatenate((signal(("silence", 12), ("speech", 20)), tone("hello", 6)))  # + 96 samples
    _, cuts = run(samples)
    assert len(cuts) == 1 and cuts[0].reason == "flush"
    assert cuts[0].samples.shape[0] == (10 + 20) * F + 96


def test_snapshot_is_a_copy_of_the_open_chunk() -> None:
    seg, _ = run(signal(("silence", 12), ("speech", 40)), flush=False)
    start, overlap, samples = seg.snapshot()
    assert (start, overlap, samples.shape[0]) == (2 * F, 0, 50 * F)
    samples[:] = 1.0
    assert seg.snapshot()[2][0] == 0.0


def test_retained_audio_never_exceeds_the_hard_limit_plus_bounded_extras() -> None:
    seg = Segmenter(EnergyVad(), CFG)
    peak = 0
    for _ in range(60):  # 60 s of continuous speech, fed in 1 s pieces
        seg.feed(tone("hello", 1000))
        peak = max(peak, seg.owned_bytes)
    assert peak <= (375 + 10 + 1) * F * 4
