"""Bundled third-party notices and licence texts (Settings, "About and licences").

The texts live in the `licences` folder next to this module: `NOTICES.md` (the summary,
always first) and the licence files of the speech model, its server, the VAD and Qt.
Reading the texts never touches the network.
"""

from __future__ import annotations

from pathlib import Path

LICENCES_DIR = Path(__file__).resolve().parent / "licences"
SUMMARY = "NOTICES.md"
UNAVAILABLE = "Licence notices are not available in this build."


def notice_files(folder: Path | None = None) -> list[Path]:
    """The summary first, then every other licence file in a stable (path) order."""
    root = folder or LICENCES_DIR
    if not root.is_dir():
        return []
    files = sorted((p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts),
                   key=lambda p: p.relative_to(root).as_posix().casefold())
    summary = [p for p in files if p.relative_to(root).as_posix() == SUMMARY]
    return summary + [p for p in files if p not in summary]


def notices_text(folder: Path | None = None) -> str:
    """The summary followed by each licence text under a `===== name =====` heading."""
    root = folder or LICENCES_DIR
    parts: list[str] = []
    for path in notice_files(root):
        text = path.read_bytes().decode("utf-8", errors="replace").replace("\r\n", "\n").strip()
        name = path.relative_to(root).as_posix()
        parts.append(text if name == SUMMARY else f"===== {name} =====\n\n{text}")
    return "\n\n\n".join(parts) + "\n" if parts else UNAVAILABLE
