"""Actual-model checks: Whisper small int8 + Silero VAD v4 on sherpa-onnx 1.13.8.

Inputs are English Windows SAPI voices synthesized in memory (tests/speech_tts.py).
These confirm the adapters, the transcribe task and the privacy boundary with the real
recognizer; they are not a quality evaluation (no Arabic, dialect or code-switch
speech is available locally). Without the installed model files or voices the tests
skip, which is a NOT RUN, never a pass.
"""

from __future__ import annotations

import re
import threading

import numpy as np
import pytest
import soxr
from capture_rig import MEETING, SECOND, Rig
from speech_tts import available_voices, speak

from meeting_minutes.domain.contracts import AudioFrame, Source, TimingQuality
from meeting_minutes.models.assets import LEGACY_WHISPER_ROLES, check_speech_assets
from meeting_minutes.models.manifest import default_manifest_path, load_manifest
from meeting_minutes.speech.collector import TranscriptCollector
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.pipeline import SpeechPipeline
from meeting_minutes.speech.sherpa import load_speech_models

MANIFEST = load_manifest(default_manifest_path())
MODELS_DIR = default_manifest_path().parent
# These tests run the legacy Whisper recognizer, which "Install Models.cmd" does not install.
READY = check_speech_assets(MANIFEST, MODELS_DIR, required=LEGACY_WHISPER_ROLES)["ready"]

pytestmark = [
    pytest.mark.skipif(not READY, reason="NOT RUN: the legacy Whisper model files are not installed in app/models"),
    pytest.mark.skipif(not available_voices(), reason="NOT RUN: no Windows SAPI English voice available"),
]

SYSTEM_SENTENCE = "We will review the budget on Sunday and the deadline is the fifth of November"
CANARY_SENTENCE = "The secret canary phrase is purple elephant"


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


@pytest.fixture(scope="module")
def models():
    return load_speech_models(MANIFEST, MODELS_DIR, verify_hashes=True)


class SpyRecognizer:
    def __init__(self, inner, boundary) -> None:
        self.inner, self.boundary = inner, boundary
        self.inputs: list[tuple[Source, int, bool]] = []
        self.outputs: list[str] = []
        self._lock = threading.Lock()

    def transcribe(self, chunk):
        admitted = self.boundary.state.admits(chunk.source, chunk.privacy_generation)
        result = self.inner.transcribe(chunk)
        with self._lock:
            self.inputs.append((chunk.source, chunk.privacy_generation, admitted))
            self.outputs.append(result.text)
        return result


def test_hashes_verified_and_models_load(models) -> None:
    assert models.readiness["ready"] and all(r["hashes_verified"] for r in models.readiness["roles"])


def test_silero_marks_speech_and_not_silence(models) -> None:
    vad = models.vad_factory()
    audio = np.concatenate((np.zeros(16_000, np.float32), speak(SYSTEM_SENTENCE), np.zeros(16_000, np.float32)))
    flags = [vad.is_speech(audio[i:i + 512]) for i in range(0, audio.shape[0] - 511, 512)]
    lead = 16_000 // 512
    assert not any(flags[:lead - 1]) and sum(flags[lead:-lead]) > 0.5 * len(flags[lead:-lead])


def test_whisper_transcribes_english_and_reports_the_language(models) -> None:
    from meeting_minutes.domain.contracts import AudioChunk

    audio = speak(SYSTEM_SENTENCE)
    chunk = AudioChunk(meeting_id=MEETING, source=Source.SYSTEM, epoch=1, privacy_generation=0, chunk_index=0,
                       start_ms=0, end_ms=audio.shape[0] // 16, data=audio)
    result = models.recognizer.transcribe(chunk)
    assert result.language == "en"
    heard = set(words(result.text))
    assert {"review", "budget", "sunday", "deadline", "november"} <= heard


def test_real_pipeline_excludes_mic_canary_while_system_is_transcribed(models) -> None:
    """With the actual recognizer, canary audio queued, racing or spoken during
    exclusion never reaches the adapter or the text consumer."""
    text = TranscriptCollector()
    rig: Rig | None = None
    spy = SpyRecognizer(models.recognizer, None)
    pipeline = SpeechPipeline(spy, models.vad_factory, on_text=text, config=SpeechConfig(partials=False),
                              clock=lambda: rig.now)
    rig = Rig(sink=pipeline)
    pipeline.attach(rig.controller)
    spy.boundary = rig.controller.boundary

    system_audio = soxr.resample(np.concatenate((np.zeros(8_000, np.float32), speak(SYSTEM_SENTENCE, "Microsoft David Desktop"),
                                                 np.zeros(24_000, np.float32))), 16_000, 44_100).astype(np.float32)
    canary = soxr.resample(speak(CANARY_SENTENCE), 16_000, 48_000).astype(np.float32)

    def play(audio):
        def signal(first, count, channels):
            out = np.zeros(count, np.float32)
            part = audio[first:first + count]
            out[:part.shape[0]] = part
            return np.repeat(out[:, None], channels, axis=1)
        return signal

    def run(until_s, process=True):
        while rig.now < int(until_s * SECOND):
            rig.run(min(int(until_s * SECOND), rig.now + SECOND // 10), process=process)
            while process and pipeline.step():
                pass

    rig.start()
    rig.handle(Source.SYSTEM).signal = play(system_audio)
    rig.handle(Source.MIC).signal = play(canary)  # canary queued in capture, never forwarded
    run(1.0, process=False)
    generation = rig.controller.privacy_generation
    rig.exclude()
    late = canary[48_000:48_000 + 19_200, None]  # a racing callback with the old generation
    rig.controller._on_frame(AudioFrame(meeting_id=MEETING, source=Source.MIC, epoch=1, privacy_generation=generation,
                                        sequence=99_999, first_sample_index=48_000, sample_rate=48_000, channels=1,
                                        monotonic_ns=rig.now, timing_quality=TimingQuality.SYNTHETIC, data=late))
    run(1.0 + system_audio.shape[0] / 44_100)
    assert pipeline.finish(deadline_ns=rig.now + 60 * SECOND)

    assert all(source is Source.SYSTEM for source, _, _ in spy.inputs)
    assert all(admitted for _, _, admitted in spy.inputs)
    finals = " ".join(t.text for t in text.finals)
    assert {"purple", "elephant", "canary", "secret"}.isdisjoint(words(finals + " ".join(spy.outputs)))
    assert {"budget", "sunday", "deadline"} <= set(words(finals))
    assert all(t.source is Source.SYSTEM and t.language == "en" for t in text.finals)


def test_transcribe_test_command_runs_with_mic_excluded(capsys) -> None:
    from meeting_minutes import cli

    code = cli.main(["transcribe-test", "--backend", "fake", "--mic", "fake:mic-48k-mono", "--start-excluded",
                     "--system", "fake:system-44k1-stereo", "--seconds", "1.5", "--json"])
    assert code == 0
    import json

    report = json.loads(capsys.readouterr().out)
    mic = report["capture_status"]["sources"]["mic"]
    assert report["started"] and report["complete"]
    assert report["drain_complete"] and report["speech_worker_stopped"] and report["capture_loss_gaps"] == 0
    assert (report["speech_finish"]["speech_gaps"], report["speech_finish"]["faults"]) == (0, 0)
    assert mic["epoch"] == 0 and mic["frames_admitted"] == 0  # the excluded mic was never opened
    assert all(f["source"] == "system" for f in report["finals"])
    assert report["speech_status"]["owned_pcm_bytes"] == 0
