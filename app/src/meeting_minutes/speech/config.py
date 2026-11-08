"""Speech pipeline settings: VAD cutting, bounded queues, partials and boundary de-duplication.

All durations are milliseconds of 16 kHz mono audio unless named otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from meeting_minutes.domain.contracts import TARGET_SAMPLE_RATE

SAMPLES_PER_MS = TARGET_SAMPLE_RATE // 1000


@dataclass(frozen=True, slots=True)
class SpeechConfig:
    # Silero ONNX takes 512-sample windows at 16 kHz (32 ms).
    vad_frame_samples: int = 512
    min_speech_ms: int = 200
    end_silence_ms: int = 600
    pre_roll_ms: int = 300
    post_roll_ms: int = 200
    soft_chunk_ms: int = 8_000
    hard_chunk_ms: int = 12_000
    forced_overlap_ms: int = 400
    # Admitted native PCM waiting for resampling/VAD, per source.
    inbox_ms_per_source: int = 2_000
    # Cut chunks waiting for the recognizer, per source; the oldest whole chunks go first.
    max_pending_ms_per_source: int = 24_000
    # A source that delivers nothing for this long (pause, idle loopback, stop) ends its
    # utterance: WASAPI loopback sends no buffers at all while nothing plays.
    idle_finalize_ms: int = 600
    # Off by default: measured on the target laptop, partials doubled recognizer duty
    # (0.37 -> 0.79) and delayed finals (p50 1.7 s -> 2.4 s).
    partials: bool = False
    partial_interval_ms: int = 2_000
    partial_min_ms: int = 1_000
    # Words compared when trimming the repeated audio of a forced cut.
    dedup_max_tokens: int = 8

    def frames(self, ms: int) -> int:
        """Whole VAD frames covering `ms` (rounded up)."""
        return -(-ms * SAMPLES_PER_MS // self.vad_frame_samples)
