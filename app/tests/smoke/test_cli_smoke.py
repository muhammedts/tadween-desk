from __future__ import annotations

import json
import subprocess
import sys


def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "meeting_minutes", *args], capture_output=True, text=True, timeout=120
    )


def test_doctor_no_capture_json():
    proc = run("doctor", "--no-capture", "--json")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["schema"] == "meeting_minutes.doctor/1"
    assert report["network"] == "not used"


def test_doctor_text_output_lists_unshippable_models():
    proc = run("doctor", "--no-capture")
    assert proc.returncode == 0, proc.stderr
    assert "NOT SHIPPABLE" in proc.stdout
    assert "whisper-small-int8-sherpa" in proc.stdout


def test_doctor_without_no_capture_refuses_instead_of_pretending():
    proc = run("doctor")
    assert proc.returncode == 2
    assert "only --no-capture" in proc.stdout


def test_doctor_out_writes_only_the_requested_report(tmp_path):
    out = tmp_path / "nested" / "doctor.json"
    proc = run("doctor", "--no-capture", "--json", "--out", str(out))
    assert proc.returncode == 0, proc.stderr
    assert json.loads(out.read_text(encoding="utf-8")) == json.loads(proc.stdout)
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == ["doctor.json"]


def test_unknown_command_fails():
    # `benchmark` is not an implemented command.
    assert run("benchmark").returncode == 2


MARKER = "SYNTHETIC_MANIFEST_VALUE_F2_9902"


def write_invalid_manifest(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"manifest_version": 1, "models": [], "unexpected": MARKER}), encoding="utf-8")
    return path


def test_doctor_json_invalid_manifest_is_structured_sanitized_and_nonzero(tmp_path):
    out = tmp_path / "report.json"
    proc = run("doctor", "--no-capture", "--json", "--manifest", str(write_invalid_manifest(tmp_path)), "--out", str(out))
    assert proc.returncode == 3
    assert proc.stderr == "", "no traceback or exception chain on the diagnostic path"
    assert MARKER not in proc.stdout + out.read_text(encoding="utf-8")
    assert json.loads(proc.stdout)["models"]["error"] == "manifest_invalid"


def test_doctor_text_invalid_manifest_is_sanitized_and_nonzero(tmp_path):
    proc = run("doctor", "--no-capture", "--manifest", str(write_invalid_manifest(tmp_path)))
    assert proc.returncode == 3
    assert proc.stderr == ""
    assert "ERROR manifest_invalid" in proc.stdout
    assert MARKER not in proc.stdout


def test_doctor_metadata_denial_is_sanitized_including_out_file(tmp_path):
    """A manifest whose metadata cannot be read is reported sanitized, in stdout and the --out report."""
    marker = "SYNTHETIC_PRIVATE_PATH_F21_7716"
    manifest = tmp_path / (marker + ".json")
    manifest.write_text(json.dumps({"manifest_version": 1, "models": []}), encoding="utf-8")
    out = tmp_path / "report.json"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from meeting_minutes.cli import main\n"
        "target = Path(sys.argv[1]); original = Path.stat\n"
        "def stat(self, *a, **k):\n"
        "    if self == target: raise PermissionError(13, 'denied', str(target))\n"
        "    return original(self, *a, **k)\n"
        "Path.stat = stat\n"
        "raise SystemExit(main(['doctor', '--no-capture', '--json', '--manifest', sys.argv[1], '--out', sys.argv[2]]))\n"
    )
    proc = subprocess.run([sys.executable, "-B", "-c", script, str(manifest), str(out)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 3
    assert proc.stderr == ""
    assert marker not in proc.stdout + out.read_text(encoding="utf-8")
    assert json.loads(proc.stdout)["models"]["error"] == "manifest_unreadable"


# Capture commands, on the synthetic backend (no audio hardware).

FAKE_MIC, FAKE_SYSTEM = "fake:mic-48k-mono", "fake:system-44k1-stereo"


def test_devices_lists_fake_endpoints_as_json():
    proc = run("devices", "--backend", "fake", "--json")
    assert proc.returncode == 0, proc.stderr
    devices = json.loads(proc.stdout)
    # The fake backend also lists one fake application source.
    assert {d["device_id"] for d in devices} == {FAKE_MIC, FAKE_SYSTEM, "app:fake-player"}
    assert {d["direction"] for d in devices} == {"capture", "render"}
    assert {d["backend"] for d in devices if d["device_id"] == "app:fake-player"} == {"app_loopback"}


def test_capture_test_reports_counts_states_and_mute_without_audio():
    proc = run(
        "capture-test", "--backend", "fake", "--mic", FAKE_MIC, "--system", FAKE_SYSTEM,
        "--seconds", "1.5", "--exclude-mic-at", "0.4", "--include-mic-at", "1.0", "--json",
    )
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    status = report["status"]
    assert status["meeting_state"] == "stopped" and status["privacy_generation"] == 2
    mic, system = status["sources"]["mic"], status["sources"]["system"]
    assert mic["epoch"] == 2 and system["epoch"] == 1
    mic_s, system_s = mic["samples_admitted"] / 48_000, system["samples_admitted"] / 44_100
    assert system_s > 1.2 and mic_s < system_s - 0.4  # ~0.6 s excluded, system continued
    assert [e["source"] for e in report["epochs"]].count("mic") == 2
    assert len(report["commands"]) == 2
    assert status["pcm_bytes"] == 0 and report["stop"]["undrained_frames"] == 0
    assert "nothing is saved" in proc.stderr


def test_capture_test_with_missing_device_does_not_capture():
    proc = run("capture-test", "--backend", "fake", "--mic", "fake:missing", "--system", "none", "--json")
    assert proc.returncode == 4
    assert json.loads(proc.stdout) == {"started": False, "failed": {"mic": "device_unavailable"}}
