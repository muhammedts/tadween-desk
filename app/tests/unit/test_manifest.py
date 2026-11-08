from __future__ import annotations

import pytest
from pydantic import ValidationError

from meeting_minutes.models.manifest import (
    ModelEntry,
    ModelFile,
    default_manifest_path,
    is_shippable,
    load_manifest,
    unshippable_reasons,
)

SHA = "a" * 64


def complete_entry(**overrides) -> ModelEntry:
    values = dict(
        id="synthetic", role="stt", engine="sherpa-onnx", engine_version="2.0.1",
        upstream_checkpoint="example/model", source_revision="abc123", conversion_revision="def456",
        languages=("en", "ar"), task="transcribe",
        files=(
            ModelFile(role="encoder", basename="enc.onnx", source_url="https://example.invalid/enc", revision="abc123", sha256=SHA, bytes=10),
            ModelFile(role="tokens", basename="tokens.txt", source_url="https://example.invalid/tok", revision="abc123", sha256=SHA, bytes=5),
        ),
        licence_files=("LICENSE",), verified_total_bytes=15, status="verified",
    )
    values.update(overrides)
    return ModelEntry(**values)


def test_repository_manifest_loads_and_every_entry_is_unshippable():
    manifest = load_manifest(default_manifest_path())
    ids = {m.id for m in manifest.models}
    assert {"whisper-small-int8-sherpa", "silero-vad"} <= ids
    for entry in manifest.models:
        assert entry.status == "unresolved-not-shippable"
        assert not is_shippable(entry)


def test_fully_specified_entry_is_shippable():
    assert unshippable_reasons(complete_entry()) == []


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"status": "unresolved-not-shippable"}, "status"),
        ({"source_revision": "REQUIRED_AT_IMPLEMENTATION"}, "source_revision unresolved"),
        ({"files": ()}, "no files"),
        ({"licence_files": ()}, "no licence"),
        ({"verified_total_bytes": 14}, "verified_total_bytes"),
    ],
)
def test_claiming_verified_is_not_enough(overrides, reason):
    reasons = unshippable_reasons(complete_entry(**overrides))
    assert any(reason in r for r in reasons)


@pytest.mark.parametrize(
    "field,value",
    [("source_url", "http://example.invalid/x"), ("sha256", "ABC"), ("bytes", 0), ("basename", "dir/x.onnx")],
)
def test_file_entries_are_strict(field, value):
    values = dict(role="encoder", basename="enc.onnx", source_url="https://example.invalid/enc",
                  revision="r", sha256=SHA, bytes=1)
    values[field] = value
    with pytest.raises(ValidationError):
        ModelFile(**values)


# Destination names and per-file revisions.

UNSAFE_NAMES = [
    ".", "..", "", " ", "C:escape.onnx", "model.onnx:stream", "dir/x.onnx", r"dir\x.onnx",
    "CON", "con.onnx", "NUL", "nul.tar.gz", "AUX.txt", "PRN", "COM1", "com9.bin", "LPT1.txt",
    "CONIN$", "COM¹", "a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "tab\tname", "nul\x00byte",
    "trailing.", "trailing ", " leading", "x" * 256,
]
SAFE_NAMES = ["encoder.onnx", "tokens.txt", "LICENSE", "CONSOLE.onnx", "com10.bin", "con-fig.json", "a.b.c"]


def file_with(**changes) -> ModelFile:
    values = dict(role="encoder", basename="enc.onnx", source_url="https://example.invalid/enc",
                  revision="abc123", sha256=SHA, bytes=10)
    values.update(changes)
    return ModelFile(**values)


@pytest.mark.parametrize("name", UNSAFE_NAMES)
def test_unsafe_windows_basenames_are_rejected(name):
    with pytest.raises(ValidationError):
        file_with(basename=name)
    with pytest.raises(ValidationError):
        complete_entry(licence_files=(name,))


@pytest.mark.parametrize("name", SAFE_NAMES)
def test_ordinary_basenames_are_accepted(name):
    assert file_with(basename=name).basename == name


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"revision": "REQUIRED_AT_IMPLEMENTATION"}, "file 0 revision unresolved"),
        ({"source_url": "https://REQUIRED_AT_IMPLEMENTATION/x"}, "file 0 source_url unresolved"),
        ({"role": "REQUIRED_AT_IMPLEMENTATION"}, "file 0 role unresolved"),
    ],
)
def test_placeholder_in_any_file_field_blocks_shipping(changes, reason):
    entry = complete_entry(files=(file_with(**changes),), verified_total_bytes=10)
    assert reason in unshippable_reasons(entry)
    assert not is_shippable(entry)


def test_second_file_placeholder_is_also_caught():
    entry = complete_entry(
        files=(file_with(basename="a.onnx"), file_with(basename="b.onnx", revision="REQUIRED_AT_IMPLEMENTATION")),
        verified_total_bytes=20,
    )
    assert "file 1 revision unresolved" in unshippable_reasons(entry)


def test_case_insensitive_duplicate_destinations_block_shipping():
    entry = complete_entry(
        files=(file_with(basename="Model.onnx"), file_with(basename="model.ONNX")), verified_total_bytes=20,
    )
    assert "duplicate file basenames" in unshippable_reasons(entry)


def test_placeholder_licence_name_blocks_shipping():
    entry = complete_entry(licence_files=("REQUIRED_AT_IMPLEMENTATION",))
    assert "licence file name unresolved" in unshippable_reasons(entry)
