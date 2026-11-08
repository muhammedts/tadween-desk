"""Bundled notices for Settings > About and licences (meeting_minutes/notices.py)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from meeting_minutes import notices

# The bundled licence texts (normalised to LF): llama.cpp at 8642541b, Silero VAD
# at tag v4.0 (915dd3d6), and the standard Apache-2.0 text for Qwen3-ASR (its repositories
# have no licence file; this is the apache.org text, byte-identical to sherpa-onnx's).
PINNED = {
    "llama.cpp-MIT-LICENSE.txt": "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d",
    "silero-vad-MIT-LICENSE.txt": "2e63e9a38b6e8fc0c7bc37ce174caca1862870856c6daf5697cfb785e925520b",
    "Qwen3-ASR-Apache-2.0-LICENSE.txt": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    # official gnu.org texts for Qt/PySide6
    "Qt-PySide6-LGPL-3.0.txt": "e3a994d82e644b03a792a930f574002658412f62407f5fee083f2555c5f23118",
    "Qt-PySide6-GPL-3.0.txt": "3972dc9744f6499f0f9b2dbf76696f2ae7ad8af9b23dde66d6af86c9dfb36986",
}


def test_source_tree_carries_the_summary_and_the_speech_licences() -> None:
    files = notices.notice_files()
    assert files[0].name == "NOTICES.md"
    names = {p.name for p in files}
    assert set(PINNED) <= names
    for name, digest in PINNED.items():
        assert hashlib.sha256((notices.LICENCES_DIR / name).read_bytes()).hexdigest() == digest


def test_notices_text_starts_with_the_summary_and_includes_each_text() -> None:
    text = notices.notices_text()
    assert text.startswith("# Tadween Desk | تدوين: components and licences")
    assert "===== llama.cpp-MIT-LICENSE.txt =====" in text and "The ggml authors" in text
    assert "===== silero-vad-MIT-LICENSE.txt =====" in text and "Silero Team" in text
    assert "Apache License" in text and "Version 2.0, January 2004" in text
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text and "GNU GENERAL PUBLIC LICENSE" in text
    assert "api.openai.com" in text  # the optional minutes exception is stated, not "loopback only"
    assert "\r" not in text


def test_build_added_third_party_texts_are_listed_after_the_summary(tmp_path: Path) -> None:
    (tmp_path / "third_party").mkdir()
    (tmp_path / "third_party" / "numpy-2.4.6-LICENSE.txt").write_bytes(b"BSD\r\nlicence")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    (tmp_path / "NOTICES.md").write_text("# Summary", encoding="utf-8")
    assert [p.relative_to(tmp_path).as_posix() for p in notices.notice_files(tmp_path)] == [
        "NOTICES.md", "b.txt", "third_party/numpy-2.4.6-LICENSE.txt"]
    text = notices.notices_text(tmp_path)
    assert text.startswith("# Summary")
    assert "===== third_party/numpy-2.4.6-LICENSE.txt =====\n\nBSD\nlicence" in text


def test_missing_folder_gives_a_plain_message(tmp_path: Path) -> None:
    assert notices.notice_files(tmp_path / "absent") == []
    assert notices.notices_text(tmp_path / "absent") == notices.UNAVAILABLE
