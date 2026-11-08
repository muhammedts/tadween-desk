"""Model manifest (models/manifest.json) loading and shippability.

An entry is shippable only when every file has an HTTPS source, immutable revision,
SHA-256 and byte count, licences are listed and the byte total matches. Declaring
status "verified" does not by itself make an entry shippable.

File and licence names are destination basenames on Windows, so they must be a
single safe path component: no separators, dot segments, drive or stream syntax
(`:`), reserved device names, invalid characters or trailing dot/space.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLACEHOLDER = "REQUIRED_AT_IMPLEMENTATION"

_INVALID_NAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}
    | {f"COM{d}" for d in "¹²³"} | {f"LPT{d}" for d in "¹²³"}
)


def require_safe_windows_basename(name: str) -> str:
    if not name or len(name) > 255:
        raise ValueError("file name must be 1-255 characters")
    if name in {".", ".."} or _INVALID_NAME_CHARS.search(name):
        raise ValueError("file name must be a single component without reserved characters")
    if name != name.strip() or name.endswith("."):
        raise ValueError("file name must not start/end with whitespace or end with a dot")
    if name.split(".", 1)[0].rstrip(" ").upper() in _RESERVED_NAMES:
        raise ValueError("file name uses a reserved Windows device name")
    return name


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelFile(_Strict):
    role: str = Field(min_length=1)
    basename: str
    source_url: str = Field(pattern=r"^https://")
    revision: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)

    @field_validator("basename")
    @classmethod
    def _safe_basename(cls, value: str) -> str:
        return require_safe_windows_basename(value)


class ModelEntry(_Strict):
    id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    engine: str
    engine_version: str
    upstream_checkpoint: str
    source_revision: str
    conversion_revision: str
    languages: tuple[str, ...]
    task: str
    files: tuple[ModelFile, ...] = ()
    licence_files: tuple[str, ...] = ()
    verified_total_bytes: int | None = None
    status: Literal["unresolved-not-shippable", "verified"]
    notes: str = ""
    # Folder under the models directory holding `files`, as "/"-separated safe names.
    install_subdir: str = ""

    @field_validator("install_subdir")
    @classmethod
    def _safe_subdir(cls, value: str) -> str:
        if value:
            for part in value.split("/"):
                require_safe_windows_basename(part)
        return value

    @field_validator("licence_files")
    @classmethod
    def _safe_licence_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(require_safe_windows_basename(name) for name in value)


class ModelManifest(_Strict):
    manifest_version: Literal[1]
    models: tuple[ModelEntry, ...]


def unshippable_reasons(entry: ModelEntry) -> list[str]:
    reasons: list[str] = []
    if entry.status != "verified":
        reasons.append(f"status is {entry.status}")
    for name in ("engine", "engine_version", "upstream_checkpoint", "source_revision", "conversion_revision"):
        value = getattr(entry, name)
        if not value or PLACEHOLDER in value:
            reasons.append(f"{name} unresolved")
    if not entry.files:
        reasons.append("no files listed")
    for index, model_file in enumerate(entry.files):
        # Each artifact needs its own immutable revision; parent fields are not enough.
        for name in ("role", "source_url", "revision"):
            if PLACEHOLDER in getattr(model_file, name):
                reasons.append(f"file {index} {name} unresolved")
    names = [f.basename.casefold() for f in entry.files]
    if len(names) != len(set(names)):
        reasons.append("duplicate file basenames")
    if any(PLACEHOLDER in name for name in entry.licence_files):
        reasons.append("licence file name unresolved")
    if not entry.licence_files:
        reasons.append("no licence files listed")
    if entry.verified_total_bytes is None or entry.verified_total_bytes != sum(f.bytes for f in entry.files):
        reasons.append("verified_total_bytes missing or does not match files")
    return reasons


def is_shippable(entry: ModelEntry) -> bool:
    return not unshippable_reasons(entry)


def load_manifest(path: Path) -> ModelManifest:
    return ModelManifest.model_validate_json(path.read_bytes())


ALT_STT_ROLE = "stt-alt"  # a speech model listed (and hash-pinned) but not used by the app


def with_stt(manifest: ModelManifest, model_id: str) -> ModelManifest:
    """The same manifest with entry `model_id` as the speech model ("stt") and the
    previous speech model kept as an alternative ("stt-alt"), for evaluating another
    pinned model; the app itself always takes the manifest's "stt" entry."""
    if not any(m.id == model_id and m.role in ("stt", ALT_STT_ROLE) for m in manifest.models):
        raise ValueError(f"no speech model {model_id!r} in the manifest")
    models = [m.model_copy(update={"role": "stt" if m.id == model_id else ALT_STT_ROLE})
              if m.role in ("stt", ALT_STT_ROLE) else m for m in manifest.models]
    return manifest.model_copy(update={"models": tuple(models) if isinstance(manifest.models, tuple) else models})


def stt_label(entry: ModelEntry) -> str:
    """Display name of a speech model entry, e.g. "Qwen3-ASR 1.7B" (from its checkpoint)."""
    name = entry.upstream_checkpoint.rsplit("/", 1)[-1]
    return name.replace("Qwen3-ASR-", "Qwen3-ASR ", 1)


def default_manifest_path() -> Path:
    # Source checkout layout: <repo>/app/src/meeting_minutes/models/manifest.py -> <repo>/app/models
    return Path(__file__).resolve().parents[3] / "models" / "manifest.json"
