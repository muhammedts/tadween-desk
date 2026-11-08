"""The end-to-end workflow self-test (meeting_minutes.selftest), run here with the fake
tone recognizer instead of the speech model (no device, model or network)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
from speech_fakes import EnergyVad, ToneRecognizer, silence, utterance

import meeting_minutes.minutes.service  # noqa: F401 - loads urllib/ssl before the network guard patches socket
from meeting_minutes.selftest import run_workflow
from meeting_minutes.session.meeting import EngineInfo
from meeting_minutes.ui.desktop import Engine


def test_workflow_selftest_records_mutes_reopens_corrects_exports_and_refuses_minutes_without_key(tmp_path: Path) -> None:
    audio = np.concatenate([utterance(["hello", "budget"]), silence(900)] * 2)
    wav = tmp_path / "tones.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16_000)
        w.writeframes((audio * 32767).astype("<i2").tobytes())
    engine = Engine(ToneRecognizer(), EnergyVad, EngineInfo("fake-tone-engine", "0" * 64), "mixed")
    report = run_workflow(wav, tmp_path / "data", seconds=6.0, engine=engine)
    assert report["ok"], report
    assert report["sources"]["system"] > 0 and report["mic_lines_after_mute"] == 0
    assert report["export_md_has_correction"] and report["minutes_without_key"]["error"] == "no_key"
    assert report["minutes_without_key"]["request_sent"] is False
