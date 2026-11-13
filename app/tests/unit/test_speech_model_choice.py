"""Which installed speech model a run uses: the standard Qwen3-ASR 0.6B, or the larger
1.7B when that is the one installed. There is no choice in the app; it follows the files
in app/models. Tiny stand-in files with the sizes of a shrunken manifest; no model is
loaded."""

from __future__ import annotations

from pathlib import Path

import pytest

from meeting_minutes.models import manifest as manifest_module
from meeting_minutes.models.assets import choose_speech_model, entry_dir, installed
from meeting_minutes.models.manifest import ModelManifest, default_manifest_path, load_manifest, speech_models

SMALL = "qwen3-asr-0.6b-q8_0-gguf"
LARGE = "qwen3-asr-1.7b-q8_0-gguf"


@pytest.fixture
def manifest() -> ModelManifest:
    """The repository manifest with every speech-model file shrunk to four bytes."""
    real = load_manifest(default_manifest_path())
    models = [m.model_copy(update={"files": [f.model_copy(update={"bytes": 4}) for f in m.files]})
              if m.id in (SMALL, LARGE) else m for m in real.models]
    return real.model_copy(update={"models": models})


def install(manifest: ModelManifest, models_dir: Path, model_id: str, *, size: int = 4) -> None:
    entry = next(m for m in manifest.models if m.id == model_id)
    folder = entry_dir(models_dir, entry)
    folder.mkdir(parents=True, exist_ok=True)
    for file in entry.files:
        (folder / file.basename).write_bytes(b"x" * size)


def used(manifest: ModelManifest) -> str:
    return next(m.id for m in manifest.models if m.role == "stt")


def test_the_manifest_lists_the_standard_model_first_then_the_larger_one(manifest):
    assert [m.id for m in speech_models(manifest)] == [SMALL, LARGE]


def test_with_nothing_installed_the_standard_model_is_reported_missing(manifest, tmp_path):
    assert choose_speech_model(manifest, tmp_path) is manifest
    assert used(manifest) == SMALL


def test_only_the_standard_model_installed_is_used(manifest, tmp_path):
    install(manifest, tmp_path, SMALL)
    assert choose_speech_model(manifest, tmp_path) is manifest


def test_only_the_larger_model_installed_is_used(manifest, tmp_path):
    install(manifest, tmp_path, LARGE)
    chosen = choose_speech_model(manifest, tmp_path)
    assert used(chosen) == LARGE
    assert {m.id: m.role for m in chosen.models if m.id == SMALL} == {SMALL: "stt-alt"}
    assert [m.role for m in chosen.models if m.id not in (SMALL, LARGE)] == [
        m.role for m in manifest.models if m.id not in (SMALL, LARGE)]  # runtime and VAD untouched


def test_with_both_installed_the_standard_model_is_used(manifest, tmp_path):
    install(manifest, tmp_path, SMALL)
    install(manifest, tmp_path, LARGE)
    assert used(choose_speech_model(manifest, tmp_path)) == SMALL


def test_a_partial_or_wrong_size_download_is_not_installed(manifest, tmp_path):
    large = next(m for m in manifest.models if m.id == LARGE)
    install(manifest, tmp_path, LARGE, size=3)
    assert not installed(tmp_path, large)
    install(manifest, tmp_path, LARGE)
    assert installed(tmp_path, large)
    (entry_dir(tmp_path, large) / large.files[0].basename).unlink()
    assert not installed(tmp_path, large)
    assert choose_speech_model(manifest, tmp_path) is manifest


def test_the_app_starts_the_installed_model_and_names_it(manifest, tmp_path, monkeypatch):
    """The desktop app's speech service and its Settings label follow the same rule."""
    from meeting_minutes.ui import desktop

    install(manifest, tmp_path, LARGE)
    monkeypatch.setattr(manifest_module, "default_manifest_path", lambda: tmp_path / "manifest.json")
    monkeypatch.setattr(manifest_module, "load_manifest", lambda path: manifest)
    chosen, models_dir = desktop._speech_manifest()
    assert used(chosen) == LARGE and models_dir == tmp_path
    assert desktop._manifest_model_label() == "Qwen3-ASR 1.7B"
    install(manifest, tmp_path, SMALL)
    assert desktop._manifest_model_label() == "Qwen3-ASR 0.6B"
