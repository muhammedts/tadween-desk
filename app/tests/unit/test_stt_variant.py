"""The pinned Qwen3-ASR 1.7B alternative: the manifest lists it as "stt-alt" while the app
uses 0.6B, and `with_stt` makes another listed model the speech model of a copy of the
manifest without changing the repository's. Labels follow the model actually used. No
server is started and nothing is downloaded."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from meeting_minutes.models.manifest import (
    ALT_STT_ROLE,
    default_manifest_path,
    load_manifest,
    stt_label,
    with_stt,
)
from meeting_minutes.speech.qwen import QwenService  # imports ssl: before the network guard
from meeting_minutes.ui.wording import engine_text, model_name

APP = Path(__file__).resolve().parents[2]

SMALL, LARGE = "qwen3-asr-0.6b-q8_0-gguf", "qwen3-asr-1.7b-q8_0-gguf"
LARGE_FILES = {"Qwen3-ASR-1.7B-Q8_0.gguf": "58e22d0532d4eacaf034cfac17a6fed159f37c41390c710186783be439d1fc57",
               "mmproj-Qwen3-ASR-1.7B-Q8_0.gguf": "46c1d533af3f354ceb37ce855dbceff7da7fa7cf1e6a523df3b13440bd164c0d"}


def test_repository_manifest_keeps_0_6b_as_the_speech_model_and_pins_1_7b_as_an_alternative() -> None:
    manifest = load_manifest(default_manifest_path())
    roles = {m.id: m.role for m in manifest.models}
    assert roles[SMALL] == "stt" and roles[LARGE] == ALT_STT_ROLE
    large = next(m for m in manifest.models if m.id == LARGE)
    assert {f.basename: f.sha256 for f in large.files} == LARGE_FILES
    assert {f.role for f in large.files} == {"model", "mmproj"}
    assert all("36a678687ba7d07a74ca70ccb0e36902e005fb80" in f.source_url for f in large.files)
    assert large.engine_version == "b11960" and large.install_subdir == "stt/qwen3-asr-1.7b-q8"
    assert stt_label(large) == "Qwen3-ASR 1.7B"


def test_with_stt_swaps_only_the_two_speech_roles_and_refuses_unknown_models() -> None:
    manifest = load_manifest(default_manifest_path())
    swapped = with_stt(manifest, LARGE)
    roles = {m.id: m.role for m in swapped.models}
    assert roles[LARGE] == "stt" and roles[SMALL] == ALT_STT_ROLE
    assert {m.id: m.role for m in swapped.models if m.id not in (SMALL, LARGE)} == \
        {m.id: m.role for m in manifest.models if m.id not in (SMALL, LARGE)}
    assert {m.id: m.role for m in with_stt(swapped, SMALL).models} == {m.id: m.role for m in manifest.models}
    assert next(m for m in manifest.models if m.id == SMALL).role == "stt"  # the input is unchanged
    for bad in ("whisper-small-int8-sherpa", "silero-vad", "nope"):
        with pytest.raises(ValueError):
            with_stt(manifest, bad)


def test_the_app_service_runs_whichever_entry_is_stt() -> None:
    path = default_manifest_path()
    standard = QwenService(load_manifest(path), path.parent)
    trial = QwenService(with_stt(load_manifest(path), LARGE), path.parent)
    assert standard.label == "Qwen3-ASR 0.6B" and SMALL in standard.base_id
    assert trial.label == "Qwen3-ASR 1.7B" and trial.base_id == f"llama.cpp-b11960/{LARGE}"
    assert trial._paths[1].name == "Qwen3-ASR-1.7B-Q8_0.gguf" and trial._paths[2].name.startswith("mmproj-Qwen3-ASR-1.7B")
    assert trial.model_hash != standard.model_hash


def test_labels_name_the_model_a_meeting_used() -> None:
    assert model_name("llama.cpp-b11960/qwen3-asr-1.7b-q8_0-gguf;language=auto;policy=A") == "Qwen3-ASR 1.7B"
    assert model_name("llama.cpp-b11960/qwen3-asr-0.6b-q8_0-gguf") == "Qwen3-ASR 0.6B"
    assert model_name("fake-engine") == "Qwen3-ASR"
    assert engine_text("llama.cpp-b11960/qwen3-asr-1.7b-q8_0-gguf;language=auto;policy=A") == \
        "Qwen3-ASR 1.7B on this PC (automatic language)"


def test_manifest_text_has_no_control_characters() -> None:
    raw = (APP / "models" / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    for entry in manifest["models"]:
        for value in (entry.get("notes", ""), entry.get("conversion_revision", "")):
            assert not any(ord(ch) < 32 for ch in value), entry["id"]
