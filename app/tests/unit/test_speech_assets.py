"""Speech asset readiness (setup-required state), resampling and the manifest roles."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from meeting_minutes.models.assets import V1_REQUIRED_ROLES, check_speech_assets
from meeting_minutes.models.manifest import ModelManifest, default_manifest_path, load_manifest
from meeting_minutes.speech.resample import Passthrough, downmix, soxr_factory
from meeting_minutes.speech.sherpa import SpeechAssetsMissing, load_speech_models


def tiny_manifest(tmp_path: Path, payloads: dict[str, bytes], stt_role: str = "stt") -> ModelManifest:
    def entry(role: str, files: dict[str, bytes]) -> dict:
        return {
            "id": f"{role}-model", "role": role, "engine": "sherpa-onnx", "engine_version": "1.13.8",
            "upstream_checkpoint": "x", "source_revision": "r", "conversion_revision": "r", "languages": [],
            "task": "t", "install_subdir": f"{role}/sub", "status": "unresolved-not-shippable",
            "files": [{"role": name, "basename": f"{name}.bin", "source_url": "https://example.invalid/x",
                       "revision": "r", "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                      for name, data in files.items()],
        }
    manifest = ModelManifest.model_validate({"manifest_version": 1, "models": [
        entry(stt_role, {k: v for k, v in payloads.items() if k not in ("model", "server")}),
        entry("stt-runtime", {"server": payloads["server"]}),
        entry("vad", {"model": payloads["model"]}),
    ]})
    for model in manifest.models:
        folder = tmp_path.joinpath(*model.install_subdir.split("/"))
        folder.mkdir(parents=True)
        for f in model.files:
            (folder / f.basename).write_bytes(payloads[f.role])
    return manifest


PAYLOADS = {"encoder": b"enc", "decoder": b"decoder", "tokens": b"tok", "model": b"vad", "server": b"srv"}


def test_ready_when_every_required_file_matches(tmp_path: Path) -> None:
    manifest = tiny_manifest(tmp_path, PAYLOADS)
    report = check_speech_assets(manifest, tmp_path, verify_hashes=True)
    assert report["ready"] and [r["role"] for r in report["roles"]] == list(V1_REQUIRED_ROLES)


def test_missing_resized_and_altered_files_give_fixed_codes(tmp_path: Path) -> None:
    manifest = tiny_manifest(tmp_path, PAYLOADS)
    (tmp_path / "stt" / "sub" / "tokens.bin").unlink()
    (tmp_path / "stt" / "sub" / "encoder.bin").write_bytes(b"encoder-longer")
    (tmp_path / "vad" / "sub" / "model.bin").write_bytes(b"VAD")  # same size, different content
    size_only = check_speech_assets(manifest, tmp_path)
    roles = {r["role"]: r for r in size_only["roles"]}
    assert roles["stt"]["problems"] == ["encoder:size_mismatch", "tokens:missing"]
    assert roles["vad"]["ready"] and roles["stt-runtime"]["ready"]  # size matches; hashes not checked
    hashed = {r["role"]: r for r in check_speech_assets(manifest, tmp_path, verify_hashes=True)["roles"]}
    assert hashed["vad"]["problems"] == ["model:sha256_mismatch"]
    assert str(tmp_path) not in json.dumps(hashed)


def test_a_missing_or_changed_runtime_file_makes_the_default_engine_not_ready(tmp_path: Path) -> None:
    manifest = tiny_manifest(tmp_path, PAYLOADS)
    (tmp_path / "stt-runtime" / "sub" / "server.bin").write_bytes(b"SRV")
    report = check_speech_assets(manifest, tmp_path, verify_hashes=True)
    assert not report["ready"]
    assert next(r for r in report["roles"] if r["role"] == "stt-runtime")["problems"] == ["server:sha256_mismatch"]


def test_loading_refuses_unready_assets_without_touching_the_runtime(tmp_path: Path) -> None:
    manifest = tiny_manifest(tmp_path, PAYLOADS, stt_role="stt-legacy")  # Whisper (legacy) loader
    (tmp_path / "vad" / "sub" / "model.bin").unlink()
    with pytest.raises(SpeechAssetsMissing) as caught:
        load_speech_models(manifest, tmp_path)
    assert "vad:model:missing" in str(caught.value) and str(tmp_path) not in str(caught.value)


def test_repository_manifest_lists_exactly_the_speech_roles() -> None:
    manifest = load_manifest(default_manifest_path())
    # Qwen3-ASR 0.6B (default), 1.7B (experimental), its llama-server, Whisper (legacy) and Silero; no LLM.
    assert sorted(m.role for m in manifest.models) == ["stt", "stt-alt", "stt-legacy", "stt-runtime", "vad"]
    roles = {m.role: m for m in manifest.models}
    stt = roles["stt"]
    assert stt.id.startswith("qwen3-asr-0.6b") and stt.task == "transcribe" and set(stt.languages) == {"en", "ar"}
    assert {f.role for f in stt.files} == {"model", "mmproj"}
    assert stt.verified_total_bytes == 1_019_141_728  # model + mmproj together
    assert "server" in {f.role for f in roles["stt-runtime"].files}
    assert {f.role for f in roles["stt-legacy"].files} == {"encoder", "decoder", "tokens"}
    for model in manifest.models:
        assert model.verified_total_bytes == sum(f.bytes for f in model.files)


def test_unsafe_install_subdir_is_rejected() -> None:
    with pytest.raises(ValueError):
        ModelManifest.model_validate({"manifest_version": 1, "models": [{
            "id": "x", "role": "stt", "engine": "e", "engine_version": "1", "upstream_checkpoint": "u",
            "source_revision": "r", "conversion_revision": "r", "languages": [], "task": "t",
            "status": "unresolved-not-shippable", "install_subdir": "../outside"}]})


def test_downmix_and_passthrough_copy_nothing_shared() -> None:
    stereo = np.array([[1.0, 0.0], [0.5, 0.5]], dtype=np.float32)
    np.testing.assert_array_equal(downmix(stereo), [0.5, 0.5])
    mono = np.ones((3, 1), dtype=np.float32)
    out = downmix(mono)
    out[0] = 9
    assert mono[0, 0] == 1
    assert isinstance(soxr_factory(16_000), Passthrough)


@pytest.mark.parametrize(("rate", "block"), [(48_000, 480), (44_100, 441), (48_000, 1024)])
def test_soxr_stream_keeps_length_and_timing(rate: int, block: int) -> None:
    x = np.zeros(rate * 2, dtype=np.float32)
    x[rate:] = 0.5  # step at exactly 1 s
    resampler = soxr_factory(rate)
    out = np.concatenate([resampler.process(x[i:i + block]) for i in range(0, x.shape[0], block)]
                         + [resampler.flush()])
    assert out.dtype == np.float32 and out.shape[0] == 32_000
    assert int(np.argmax(out > 0.25)) == 16_000  # delay-compensated: the step stays at 1 s
