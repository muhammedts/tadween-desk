"""Render the README screenshots from the real Tadween Desk window with a fictional meeting.

    cd app
    .\\.venv\\Scripts\\python.exe scripts\\demo_screenshots.py <empty-demo-folder> <output-folder>

The window, session, store and export code are the app's own. Only the inputs are
stand-ins, so no microphone, playback device, speech model or network is used:
synthetic capture devices (meeting_minutes.capture.fake) play test tones, and a scripted
recognizer turns each tone into a line of the fictional demo script below. The History
database and the Save As file go only to <empty-demo-folder>; the user's own History is
never opened. Writes live-transcript.png and saved-transcript.png to <output-folder>.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tests"))  # speech_fakes: tone signals and their decoder

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QInputDialog  # noqa: E402
from speech_fakes import EnergyVad, decode_words, scripted  # noqa: E402

from meeting_minutes.capture.fake import RealtimeFakeBackend  # noqa: E402
from meeting_minutes.domain.contracts import Source  # noqa: E402
from meeting_minutes.session.meeting import EngineInfo  # noqa: E402
from meeting_minutes.speech.pipeline import RecognizedText  # noqa: E402
from meeting_minutes.storage.store import TranscriptStore  # noqa: E402
from meeting_minutes.ui.desktop import Engine, MainWindow  # noqa: E402

MIC, SYSTEM = "fake:mic-48k-mono", "fake:system-44k1-stereo"
RATES = {Source.MIC: 48_000, Source.SYSTEM: 44_100}
ENGINE_ID = "llama.cpp-b11960/qwen3-asr-0.6b-q8_0-gguf;language=auto;policy=A"

# Fictional demo script: tone word -> (line, language). Names and figures are invented.
LINES = {
    "hello": ("Good morning, everyone. Let's start with the launch checklist for the Riyadh office.", "en"),
    "مرحبا": ("صباح الخير، جاهزين نبدأ. الفريق خلّص الترجمة العربية للواجهة أمس.", "ar"),
    "budget": ("Great. The vendor quote came in at 48,000 riyals, under the budget we agreed.", "en"),
    "الميزانية": ("ممتاز، بس نحتاج موافقة المالية على الميزانية قبل يوم الخميس.", "ar"),
    "deadline": ("Then let's keep Thursday as the deadline and send the summary to Sara today.", "en"),
    "system": ("I'll book the training room for next week and share the agenda.", "en"),
}
CORRECTION = "Then let's keep Thursday as the deadline and send the summary to Sarah today."


class ScriptedRecognizer:
    """Returns the demo line for the tone it hears (stands in for the speech model)."""

    def transcribe(self, chunk):
        words = decode_words(chunk.data)
        found = [LINES[w] for w in words if w in LINES]
        if not found:
            return RecognizedText("", None)
        return RecognizedText(" ".join(text for text, _ in found), found[0][1])


def wait_until(predicate, timeout: float = 20.0, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise SystemExit(f"demo did not reach the expected state in time: {what}")


def settle(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.02)


def say(window: MainWindow, source: Source, words: list[tuple[float, str]]) -> None:
    handle = [h for h in window.session_backend.opened if h.source is source and not h.closed][-1]
    elapsed = handle._next_index / RATES[source]
    handle.signal = scripted(RATES[source], [(elapsed + t, w) for t, w in words])


def main(demo_dir: Path, out_dir: Path) -> int:
    demo_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(demo_dir.iterdir()):
        raise SystemExit(f"{demo_dir} must be empty (a fresh demo History folder)")
    app = QApplication.instance() or QApplication([])
    store = TranscriptStore(demo_dir / "meetings.db")
    store.set_setting("theme", "light")

    def loader(mode: str) -> Engine:
        return Engine(ScriptedRecognizer(), EnergyVad, EngineInfo(ENGINE_ID, "0" * 64), mode)

    def save_dialog(parent, suggested: str):
        return str(demo_dir / Path(suggested).with_suffix(".md").name), "Markdown (*.md)"

    window = MainWindow(store, RealtimeFakeBackend, loader, save_dialog=save_dialog)
    window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)  # rendered, never shown on the desktop
    window.resize(1366, 800)
    window.show()
    wait_until(lambda: "ready" in window.model_label.text(), what="engine ready")
    window.mic_combo.setCurrentIndex(window.mic_combo.findData(MIC))
    window.system_combo.setCurrentIndex(window.system_combo.findData(SYSTEM))
    window.title_edit.setText("Launch planning — demo")
    QTest.mouseClick(window.start_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.pages.currentWidget() is window.live_page, what="live page")

    say(window, Source.SYSTEM, [(0.3, "hello"), (2.6, "budget"), (6.6, "deadline")])
    say(window, Source.MIC, [(1.4, "مرحبا"), (4.2, "الميزانية")])
    wait_until(lambda: window.live_list.count() >= 4, what="four live lines")
    window.mic_button.click()  # mute the mic (the same button as Ctrl+M)
    wait_until(lambda: not window.session.mic_included, what="mic muted")
    live = lambda: [window.live_list.item(i).text() for i in range(window.live_list.count())]  # noqa: E731
    wait_until(lambda: any("Thursday as the deadline" in t for t in live()), what="system line after mute")
    settle(1.0)
    window.grab().save(str(out_dir / "live-transcript.png"))

    QTest.mouseClick(window.stop_button, Qt.MouseButton.LeftButton)
    wait_until(lambda: window.pages.currentWidget() is window.saved_page, what="saved page")
    settle(0.5)
    rows = [window.saved_list.item(i).text() for i in range(window.saved_list.count())]
    target = next(i for i, text in enumerate(rows) if "Thursday as the deadline" in text)
    window.saved_list.setCurrentRow(target)
    original = QInputDialog.getMultiLineText
    QInputDialog.getMultiLineText = staticmethod(lambda *a, **k: (CORRECTION, True))
    try:
        window.correct_selected()
    finally:
        QInputDialog.getMultiLineText = original
    window.save_as()
    settle(0.5)
    window.saved_list.setCurrentRow(target)
    settle(0.3)
    window.grab().save(str(out_dir / "saved-transcript.png"))
    window.close()
    store.close()
    print(f"wrote {out_dir / 'live-transcript.png'} and {out_dir / 'saved-transcript.png'}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(sys.argv[1]), Path(sys.argv[2])))
