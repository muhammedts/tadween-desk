"""`doctor --no-capture`: environment report without capture or network.

Reports metadata only. It does not enumerate or open audio devices, import audio,
UI or recognizer libraries, read environment variable values, or include absolute
paths, user names or hardware serial numbers.
"""

from __future__ import annotations

import ctypes
import importlib.metadata
import os
import platform
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from meeting_minutes import PROTOCOL_VERSION, __version__
from meeting_minutes.domain.policy import DEFAULT_EGRESS_POLICY
from meeting_minutes.models.assets import check_speech_assets, choose_speech_model
from meeting_minutes.models.manifest import default_manifest_path, load_manifest, unshippable_reasons

REPORT_SCHEMA = "meeting_minutes.doctor/1"

# Reported by installed version only; never imported here.
TRACKED_DISTRIBUTIONS = (
    "numpy",
    "pydantic",
    "PySide6-Essentials",
    "PyAudioWPatch",
    "sherpa-onnx",
    "soxr",
    "python-docx",
)

NOT_CHECKED = (
    "audio endpoint enumeration and format probing (run `devices`)",
    "microphone or loopback capture",
    "speech recognition and VAD inference (asset files are checked by size, not hash; hashes are verified when models load)",
    "network reachability",
)


def _repo_root() -> Path | None:
    # The package lives in <repo>/app/src/meeting_minutes; the Git root is an ancestor.
    for candidate in Path(__file__).resolve().parents:
        if (candidate / ".git").exists():
            return candidate
    return None


def _git_state() -> dict[str, Any]:
    root = _repo_root()
    if root is None:
        return {"available": False}
    try:
        def run(*args: str) -> str:
            return subprocess.run(
                # --no-optional-locks: `status` must not rewrite the index.
                ["git", "--no-optional-locks", "-C", str(root), *args],
                capture_output=True, text=True, timeout=10, check=True,
            ).stdout
        revision = run("rev-parse", "HEAD").strip()
        dirty = [line for line in run("status", "--porcelain").splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        return {"available": False}
    return {"available": True, "revision": revision, "dirty_paths": len(dirty)}


def _registry_string(subkey: str, name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
            return str(winreg.QueryValueEx(key, name)[0]).strip()
    except OSError:
        return None


def _os_info() -> dict[str, Any]:
    # platform.version()/release()/machine() shell out to `cmd /c ver` on Windows,
    # so read the same facts directly.
    if sys.platform != "win32":
        return {"system": platform.system(), "version": platform.version(), "machine": platform.machine()}
    winver = sys.getwindowsversion()
    current = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"
    return {
        "system": "Windows",
        "version": f"{winver.major}.{winver.minor}.{winver.build}",
        "build": winver.build,
        "edition_id": _registry_string(current, "EditionID"),
        "display_version": _registry_string(current, "DisplayVersion"),
        "machine": os.environ.get("PROCESSOR_ARCHITECTURE"),
    }


def _cpu_name() -> str | None:
    if sys.platform != "win32":
        return None
    return _registry_string(r"HARDWARE\DESCRIPTION\System\CentralProcessor\0", "ProcessorNameString")


def _total_memory_bytes() -> int | None:
    if sys.platform != "win32":
        return None

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.ullTotalPhys)


def _distributions() -> dict[str, str | None]:
    found: dict[str, str | None] = {}
    for name in TRACKED_DISTRIBUTIONS:
        try:
            found[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            found[name] = None
    return found


MANIFEST_UNREADABLE = "manifest_unreadable"
MANIFEST_INVALID = "manifest_invalid"


def _models(manifest_path: Path) -> dict[str, Any]:
    # Filesystem errors can carry the supplied path and validation messages can quote
    # the manifest's values, so every touch of the path - including this existence
    # check - sits inside the boundary and only fixed codes leave it. Exceptions are
    # dropped, not chained. Path.is_file() is not used: it can raise from stat().
    try:
        info = manifest_path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return {"manifest_found": False, "error": None, "entries": []}
    except (OSError, ValueError):  # access denied, invalid path syntax, embedded NUL
        return {"manifest_found": True, "error": MANIFEST_UNREADABLE, "entries": []}
    if not stat.S_ISREG(info.st_mode):
        return {"manifest_found": False, "error": None, "entries": []}
    try:
        manifest = load_manifest(manifest_path)
    except OSError:
        return {"manifest_found": True, "error": MANIFEST_UNREADABLE, "entries": []}
    except ValueError:  # includes pydantic.ValidationError and malformed JSON
        return {"manifest_found": True, "error": MANIFEST_INVALID, "entries": []}
    try:
        # The installed speech model: the standard one, or the larger alternative alone.
        speech = check_speech_assets(choose_speech_model(manifest, manifest_path.parent), manifest_path.parent)
    except (OSError, ValueError):
        speech = {"ready": False, "roles": [], "error": MANIFEST_UNREADABLE}
    return {
        "manifest_found": True,
        "error": None,
        # Transcription needs only the STT and VAD roles; fixed problem codes, no paths.
        "speech_assets": speech,
        "entries": [
            {
                "id": entry.id,
                "role": entry.role,
                "status": entry.status,
                "shippable": not (reasons := unshippable_reasons(entry)),
                "unshippable_reasons": reasons,
            }
            for entry in manifest.models
        ],
    }


def collect(manifest_path: Path | None = None) -> dict[str, Any]:
    memory = _total_memory_bytes()
    return {
        "schema": REPORT_SCHEMA,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "capture": "not performed (--no-capture)",
        "network": "not used",
        "app": {"version": __version__, "protocol_version": PROTOCOL_VERSION},
        "git": _git_state(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "pointer_bits": ctypes.sizeof(ctypes.c_void_p) * 8,
            "in_virtualenv": sys.prefix != sys.base_prefix,
        },
        "os": _os_info(),
        "hardware": {
            "cpu_name": _cpu_name(),
            "logical_processors": os.cpu_count(),
            "total_memory_gib": None if memory is None else round(memory / 2**30, 1),
        },
        "distributions": _distributions(),
        "models": _models(manifest_path or default_manifest_path()),
        "egress_defaults": DEFAULT_EGRESS_POLICY.as_dict(),
        "not_checked": list(NOT_CHECKED),
    }


def format_text(report: dict[str, Any]) -> str:
    lines = [
        f"Tadween Desk doctor {report['app']['version']} ({report['capture']}; network {report['network']})",
        f"Python {report['python']['version']} {report['python']['pointer_bits']}-bit, "
        f"virtualenv={report['python']['in_virtualenv']}",
        f"OS {report['os']['system']} {report['os']['version']} "
        f"{report['os'].get('edition_id') or ''} {report['os'].get('display_version') or ''} "
        f"{report['os']['machine']}",
        f"CPU {report['hardware']['cpu_name']} ({report['hardware']['logical_processors']} logical), "
        f"RAM {report['hardware']['total_memory_gib']} GiB",
    ]
    git = report["git"]
    lines.append(
        f"Git {git['revision']} ({git['dirty_paths']} dirty paths)" if git["available"] else "Git unavailable"
    )
    lines.append("Distributions:")
    lines += [f"  {name}: {version or 'not installed'}" for name, version in report["distributions"].items()]
    lines.append("Models:")
    if not report["models"]["manifest_found"]:
        lines.append("  manifest not found")
    elif report["models"]["error"]:
        lines.append(f"  ERROR {report['models']['error']} (contents withheld; fix or replace the manifest)")
    speech = report["models"].get("speech_assets")
    if speech is not None:
        problems = [f"{r['role']}:{p}" for r in speech["roles"] for p in r["problems"]]
        lines.append("  Speech assets (stt, vad): " + ("present, sizes match" if speech["ready"]
                                                         else "SETUP REQUIRED: " + ", ".join(problems)))
    for entry in report["models"]["entries"]:
        state = "shippable" if entry["shippable"] else "NOT SHIPPABLE: " + "; ".join(entry["unshippable_reasons"])
        lines.append(f"  {entry['id']} ({entry['role']}): {state}")
    lines.append("Egress defaults: " + ", ".join(f"{k}={v}" for k, v in report["egress_defaults"].items()))
    lines.append("Not checked: " + "; ".join(report["not_checked"]))
    return "\n".join(lines)
