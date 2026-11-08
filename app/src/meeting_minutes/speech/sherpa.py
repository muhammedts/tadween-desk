"""Local model adapters on sherpa-onnx 1.13.8: multilingual Whisper small int8 and Silero VAD v4.

The app's recognizer is Qwen3-ASR (speech/qwen.py); Silero VAD here is the app's VAD.
Whisper (manifest role "stt-legacy") is used only by the `transcribe-test` console
command and as a reference. The app never falls back to Whisper.

Both run in-process on the CPU from the inspected files listed in models/manifest.json;
nothing is downloaded and no audio leaves memory. `language_mode` "mixed" passes an
empty language so Whisper detects it per chunk (confirmed on 1.13.8: the result
reports the detected language); "en"/"ar" force one language. The task is always
"transcribe", never "translate", so Arabic stays in Arabic script.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from meeting_minutes.domain.contracts import TARGET_SAMPLE_RATE, AudioChunk
from meeting_minutes.models.assets import LEGACY_WHISPER_ROLES, check_speech_assets, file_path
from meeting_minutes.models.manifest import ModelManifest
from meeting_minutes.speech.pipeline import RecognizedText

LANGUAGE_MODES = {"mixed": "", "en": "en", "ar": "ar"}


class SpeechAssetsMissing(RuntimeError):
    """Required STT/VAD files are absent or do not match the manifest (fixed codes only)."""

    def __init__(self, readiness: dict) -> None:
        problems = [f"{r['role']}:{p}" for r in readiness["roles"] for p in r["problems"]]
        super().__init__("speech assets not ready: " + ", ".join(problems))
        self.readiness = readiness


class SileroVad:
    """Per-window speech decision. sherpa's own hysteresis is set to one window so the
    segmenter's documented min-speech/end-silence rules decide utterance boundaries."""

    frame_samples = 512

    def __init__(self, model: Path, *, threshold: float = 0.5) -> None:
        import sherpa_onnx

        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model)
        config.silero_vad.threshold = threshold
        config.silero_vad.window_size = self.frame_samples
        config.silero_vad.min_silence_duration = self.frame_samples / TARGET_SAMPLE_RATE
        config.silero_vad.min_speech_duration = self.frame_samples / TARGET_SAMPLE_RATE
        config.sample_rate = TARGET_SAMPLE_RATE
        config.num_threads = 1
        config.provider = "cpu"
        if not config.validate():
            raise ValueError("invalid Silero VAD configuration")
        self._model = sherpa_onnx.VadModel.create(config)

    def is_speech(self, frame: np.ndarray) -> bool:
        return bool(self._model.is_speech(frame))

    def reset(self) -> None:
        self._model.reset()


class WhisperRecognizer:
    def __init__(self, encoder: Path, decoder: Path, tokens: Path, *, language_mode: str = "mixed",
                 num_threads: int = 4) -> None:
        import sherpa_onnx

        self.language_mode = language_mode
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=str(encoder), decoder=str(decoder), tokens=str(tokens),
            language=LANGUAGE_MODES[language_mode], task="transcribe",
            num_threads=num_threads, decoding_method="greedy_search",
        )

    def transcribe(self, chunk: AudioChunk) -> RecognizedText:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(TARGET_SAMPLE_RATE, chunk.data)
        self._recognizer.decode_stream(stream)
        result = stream.result
        return RecognizedText(result.text, getattr(result, "lang", None) or None)


@dataclass(frozen=True)
class SpeechModels:
    recognizer: WhisperRecognizer
    vad_factory: Callable[[], SileroVad]
    readiness: dict


def load_speech_models(manifest: ModelManifest, models_dir: Path, *, language_mode: str = "mixed",
                       num_threads: int = 4, verify_hashes: bool = True) -> SpeechModels:
    """Check the manifest files (by default including SHA-256), then load the models."""
    if language_mode not in LANGUAGE_MODES:
        raise ValueError(f"language_mode must be one of {sorted(LANGUAGE_MODES)}")
    readiness = check_speech_assets(manifest, models_dir, verify_hashes=verify_hashes, required=LEGACY_WHISPER_ROLES)
    if not readiness["ready"]:
        raise SpeechAssetsMissing(readiness)
    stt = next(m for m in manifest.models if m.role == "stt-legacy")
    vad = next(m for m in manifest.models if m.role == "vad")
    recognizer = WhisperRecognizer(
        file_path(models_dir, stt, "encoder"), file_path(models_dir, stt, "decoder"),
        file_path(models_dir, stt, "tokens"), language_mode=language_mode, num_threads=num_threads,
    )
    vad_model = file_path(models_dir, vad, "model")
    return SpeechModels(recognizer, lambda: SileroVad(vad_model), readiness)
