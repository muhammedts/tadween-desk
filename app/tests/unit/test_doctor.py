from __future__ import annotations

import json
import os
import re

import pytest

from meeting_minutes.diagnostics.doctor import REPORT_SCHEMA, collect, format_text
from meeting_minutes.domain.policy import DEFAULT_EGRESS_POLICY

CANARY = "sk-synthetic-canary-7f3a9e"


def test_report_shape_with_network_blocked(block_network):
    report = collect()
    assert report["schema"] == REPORT_SCHEMA
    assert report["capture"].startswith("not performed")
    assert report["python"]["pointer_bits"] == 64
    assert report["models"]["manifest_found"] is True
    assert all(not e["shippable"] for e in report["models"]["entries"])
    assert block_network == []


def test_all_egress_choices_default_off():
    assert collect()["egress_defaults"] == DEFAULT_EGRESS_POLICY.as_dict()
    assert not any(DEFAULT_EGRESS_POLICY.as_dict().values())


def test_report_contains_no_secrets_paths_user_names_or_serials(monkeypatch):
    monkeypatch.setenv("MM_TEST_API_KEY", CANARY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", CANARY)
    rendered = json.dumps(collect()) + format_text(collect())
    assert CANARY not in rendered
    assert not re.search(r"[A-Za-z]:\\\\|[A-Za-z]:\\|/Users/|/home/", rendered)
    user = os.environ.get("USERNAME") or os.environ.get("USER")
    if user:
        assert user.lower() not in rendered.lower()
    assert "serial" not in rendered.lower()
    assert not re.search(r"\b([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", rendered)  # MAC address


def test_git_state_found_from_app_subfolder():
    from pathlib import Path

    import meeting_minutes

    package = Path(meeting_minutes.__file__).resolve()
    if not any((p / ".git").exists() for p in package.parents):
        pytest.skip("package is not inside a Git checkout")  # reported, not silent
    git = collect()["git"]
    assert git["available"] is True
    assert len(git["revision"]) == 40


def test_missing_manifest_is_reported_not_invented(tmp_path):
    report = collect(tmp_path / "absent.json")
    # A missing manifest is reported honestly and is not a validation error.
    assert report["models"] == {"manifest_found": False, "error": None, "entries": []}


def test_doctor_reports_installed_versions_without_importing(monkeypatch):
    import sys

    before = set(sys.modules)
    collect()
    newly = set(sys.modules) - before
    assert not {m for m in newly if m.split(".")[0] in {"PySide6", "pyaudiowpatch", "sherpa_onnx", "soxr", "docx"}}


# Malformed manifests yield a sanitized, fixed error code, never their contents.

MARKER = "SYNTHETIC_MANIFEST_VALUE_4471"
INVALID_MANIFESTS = {
    "unexpected_field": json.dumps({"manifest_version": 1, "models": [], "unexpected": MARKER}),
    "wrong_type": json.dumps({"manifest_version": 1, "models": MARKER}),
    "bad_entry_value": json.dumps({"manifest_version": 1, "models": [{"id": MARKER, "status": MARKER}]}),
    "malformed_json": '{"manifest_version": 1, "models": [' + MARKER,
    "wrong_version": json.dumps({"manifest_version": MARKER, "models": []}),
}


@pytest.mark.parametrize("name", sorted(INVALID_MANIFESTS))
def test_invalid_manifest_reports_fixed_code_without_contents(tmp_path, name):
    path = tmp_path / "manifest.json"
    path.write_text(INVALID_MANIFESTS[name], encoding="utf-8")
    report = collect(path)
    assert report["models"] == {"manifest_found": True, "error": "manifest_invalid", "entries": []}
    assert MARKER not in json.dumps(report) + format_text(report)


def test_non_utf8_manifest_is_invalid_not_a_crash(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_bytes(b'{"manifest_version": 1, "models": [], "x": "\xff\xfe' + MARKER.encode() + b'"}')
    report = collect(path)
    assert report["models"]["error"] == "manifest_invalid"
    assert MARKER not in json.dumps(report)


def test_unreadable_manifest_reports_fixed_code(tmp_path, monkeypatch):
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")

    def refuse(self):
        raise PermissionError(f"denied {MARKER}")

    monkeypatch.setattr(type(path), "read_bytes", refuse)
    report = collect(path)
    assert report["models"] == {"manifest_found": True, "error": "manifest_unreadable", "entries": []}
    assert MARKER not in json.dumps(report)


def test_valid_manifest_reports_no_error():
    assert collect()["models"]["error"] is None


# The existence/metadata check is inside the same boundary: no path leaks from a stat error.

PATH_MARKER = "SYNTHETIC_PRIVATE_PATH_5830"


def deny_stat_for(monkeypatch, target, error):
    original = type(target).stat

    def stat(self, *args, **kwargs):
        if self == target:
            raise error
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(target), "stat", stat)


@pytest.mark.parametrize(
    "error",
    [
        PermissionError(13, "synthetic metadata access denial"),
        OSError(5, "synthetic I/O error"),
        OSError(123, "synthetic invalid name"),
    ],
    ids=["permission", "io", "invalid-name"],
)
def test_metadata_errors_report_unreadable_without_path(tmp_path, monkeypatch, error):
    path = tmp_path / (PATH_MARKER + ".json")
    path.write_text(json.dumps({"manifest_version": 1, "models": []}), encoding="utf-8")
    error.filename = str(path)
    deny_stat_for(monkeypatch, path, error)
    report = collect(path)
    assert report["models"] == {"manifest_found": True, "error": "manifest_unreadable", "entries": []}
    assert PATH_MARKER not in json.dumps(report) + format_text(report)


def test_embedded_nul_path_reports_unreadable(tmp_path):
    report = collect(tmp_path / ("bad\x00" + PATH_MARKER + ".json"))
    assert report["models"]["error"] == "manifest_unreadable"
    assert PATH_MARKER not in json.dumps(report)


@pytest.mark.parametrize("make", ["missing_file", "missing_parent", "under_a_file", "directory"])
def test_absent_or_non_file_manifest_is_still_not_found(tmp_path, make):
    regular = tmp_path / "regular.txt"
    regular.write_text("x", encoding="utf-8")
    path = {
        "missing_file": tmp_path / "absent.json",
        "missing_parent": tmp_path / "no-such-dir" / "manifest.json",
        "under_a_file": regular / "manifest.json",
        "directory": tmp_path,
    }[make]
    assert collect(path)["models"] == {"manifest_found": False, "error": None, "entries": []}
