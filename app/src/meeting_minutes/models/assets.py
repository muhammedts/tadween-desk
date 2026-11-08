"""Readiness of the locally installed speech assets: the Qwen3-ASR speech model ("stt"),
the llama-server that runs it ("stt-runtime") and Silero VAD ("vad").

Checks each required role's manifest files on disk: present, exact byte count and,
when asked, SHA-256. Nothing is downloaded; a missing or mismatched asset is reported
with fixed codes (no paths) so the app can show an honest setup-required state.
Shippability (licences, provenance) is a separate question: see manifest.py.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from meeting_minutes.models.manifest import ModelEntry, ModelManifest

V1_REQUIRED_ROLES = ("stt", "stt-runtime", "vad")
LEGACY_WHISPER_ROLES = ("stt-legacy", "vad")  # Whisper small: the `transcribe-test` CLI command only
_CHUNK = 1 << 20


def entry_dir(models_dir: Path, entry: ModelEntry) -> Path:
    return models_dir.joinpath(*entry.install_subdir.split("/")) if entry.install_subdir else models_dir


def file_path(models_dir: Path, entry: ModelEntry, role: str) -> Path:
    match = next(f for f in entry.files if f.role == role)
    return entry_dir(models_dir, entry) / match.basename


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_CHUNK):
            digest.update(block)
    return digest.hexdigest()


def check_role(manifest: ModelManifest, models_dir: Path, role: str, *, verify_hashes: bool) -> dict[str, Any]:
    entry = next((m for m in manifest.models if m.role == role), None)
    if entry is None:
        return {"role": role, "id": None, "ready": False, "problems": ["no_manifest_entry"]}
    problems: list[str] = []
    if not entry.files:
        problems.append("no_files_listed")
    for model_file in entry.files:
        path = entry_dir(models_dir, entry) / model_file.basename
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            problems.append(f"{model_file.role}:missing")
            continue
        except OSError:
            problems.append(f"{model_file.role}:unreadable")
            continue
        if size != model_file.bytes:
            problems.append(f"{model_file.role}:size_mismatch")
        elif verify_hashes:
            try:
                if _sha256(path) != model_file.sha256:
                    problems.append(f"{model_file.role}:sha256_mismatch")
            except OSError:
                problems.append(f"{model_file.role}:unreadable")
    return {"role": role, "id": entry.id, "ready": not problems, "problems": problems,
            "hashes_verified": verify_hashes and not problems}


def check_speech_assets(manifest: ModelManifest, models_dir: Path, *, verify_hashes: bool = False,
                        required: tuple[str, ...] = V1_REQUIRED_ROLES) -> dict[str, Any]:
    roles = [check_role(manifest, models_dir, role, verify_hashes=verify_hashes) for role in required]
    return {"ready": all(r["ready"] for r in roles), "roles": roles}
