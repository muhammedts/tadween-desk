"""Tadween Desk's main window: preflight, live transcript, saved transcript, History
and Settings in one PySide6 window.

Everything shown comes from `MeetingSession` (live) or `TranscriptStore` (saved).
Session callbacks arrive on worker threads and are forwarded to the UI thread through a
queued Qt signal; the window never blocks on capture, recognition or saving. Every
store access from the UI thread waits at most UI_STORE_WAIT_S for another thread's
operation: if a save is stalled, the window says so and retries later instead of
freezing. Nothing here writes audio, logs transcript text or contacts the network.

Save As: meetings are saved automatically in History (SQLite). After Stop and save,
after Close and save, and from the saved view, the user can also write one saved
revision to a .txt or .md file in a folder they choose (see `export`).

The meeting controls (mic state, Mute/Unmute, Pause/Resume, Stop) sit in a bar above
every page while a meeting runs; Settings and help hold the retention choice (see
`retention`), the mute shortcut and the plain-language explanations; transcript lines
take their paragraph direction from their own text.

The look comes from the Quiet Desk theme (`theme`): a navigation rail, alert banners,
painted transcript rows (`transcript`), side panels and responsive size classes.
Window-only wording is in `copy`; `wording` stays shared with the exported file.
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QStandardPaths, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QGuiApplication, QKeySequence, QResizeEvent, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from meeting_minutes import retention
from meeting_minutes.capture.app_loopback import UNSUPPORTED as APP_UNSUPPORTED
from meeting_minutes.capture.app_loopback import is_application, is_application_id
from meeting_minutes.capture.base import CaptureBackend
from meeting_minutes.domain.contracts import Direction, Source
from meeting_minutes.export import ExportError, UnsavedLine, render, suggest_filename, write_file
from meeting_minutes.session.meeting import EngineInfo, MeetingSession, SessionState
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.segmenter import FrameVad
from meeting_minutes.storage.search import MAX_PAGE, QueryError
from meeting_minutes.storage.store import MaintenanceError, MeetingSnapshot, StorageError, TimelineRow, TranscriptStore
from meeting_minutes.ui import copy as C
from meeting_minutes.ui import theme
from meeting_minutes.ui.presenter import MIC_EXCLUDED_TEXT, MIC_INCLUDED_TEXT, ROUTING_NOTE
from meeting_minutes.ui.transcript import (
    LINE_SEPARATOR,
    LRM,
    ROW_ROLE,
    RTL_ROLE,
    SIG_ROLE,
    TEXT_ROLE,
    set_event,
    set_hit,
    set_line,
    set_meeting,
    set_message,
    transcript_list,
)
from meeting_minutes.ui.widgets import (
    AlertArea,
    Banner,
    Card,
    EmptyState,
    Notice,
    ReadinessItem,
    ReadOnlyBlock,
    SourceCard,
    Toast,
    announce,
    button,
    chip,
    heading,
    icon_label,
    label,
    link_button,
    set_chip,
    tool_button,
)
from meeting_minutes.ui.wording import (
    GAP_TEXT,
    SOURCE_NAMES,
    STATE_TEXT,
    clock_text,
    coverage_summary,
    event_line,
    is_rtl,
    local_time,
    model_name,
    quality_note,
)

__all__ = ["APP_TITLE", "Engine", "EngineLoader", "FILE_FILTERS", "LANGUAGES", "LANGUAGE_NOTE", "LINE_SEPARATOR",
           "LRM", "MainWindow", "QWEN_MODES", "RTL_ROLE", "TEXT_ROLE", "close_local_engines", "default_data_dir",
           "export_target", "load_local_engine", "native_save_dialog", "run", "self_check"]

APP_TITLE = "Tadween Desk — transcript"
DATA_DIR_ENV = "MEETING_MINUTES_DATA_DIR"
LANGUAGES = (("mixed", "English and Arabic (automatic)"),
             ("mixed_r", "English and Arabic, experimental retry (R)"),
             ("en", "English only"), ("ar", "Arabic only"))
# Qwen3-ASR names one language per chunk (up to 8-12 s of speech), not per sentence,
# and a forced language rewrites other speech into that language; the notes say so.
LANGUAGE_NOTE = ("Automatic: the speech model decides the language of each stretch of speech (up to about 12 "
                 "seconds) and writes it in that language, so speech that mixes English and Arabic may come out in "
                 "one language, and now and then it names another language. Experimental retry (R), for testing: "
                 "when that happens, the stretch is recognized once more as English or Arabic, whichever the model "
                 "rated more likely, and the line is marked. English only writes all speech as English — Arabic or "
                 "unclear speech becomes English words that may be invented. Arabic only writes all speech in "
                 "Arabic letters — English speech becomes Arabic-script words that may be invented. Use Automatic "
                 "for meetings with Arabic.")
# The selected choice's own line under the selector (the full note is its tooltip).
LANGUAGE_HELP = {
    "mixed": ("Detects English or Arabic for each stretch of speech and keeps the original script; nothing is "
              "translated. Mixed sentences may come out in one language.", "help"),
    "mixed_r": ("Experimental, for testing: stretches the model is unsure about are recognized once more as English "
                "or Arabic, and those lines are marked.", "help"),
    "en": ("English only writes all speech as English — Arabic or unclear speech becomes English words that may be "
           "invented. Use Automatic for meetings with Arabic.", "warn"),
    "ar": ("Arabic only writes all speech in Arabic letters — English speech becomes Arabic-script words that may be "
           "invented. Use Automatic for meetings with English.", "warn"),
}
# Language choice -> (Qwen language, policy); fixed for a meeting once it starts.
QWEN_MODES = {"mixed": ("auto", "A"), "mixed_r": ("auto", "R"), "en": ("en", "A"), "ar": ("ar", "A")}
_ENGINE_CODE = re.compile(r"qwen_[a-z0-9_.]{1,60}")  # content-free start-up failure codes
UI_STORE_WAIT_S = 0.25  # longest the UI thread waits for another thread's store operation
PENDING_POLL_MS = 500
FILE_FILTERS = ("Text file, UTF-8 (*.txt)", "Markdown (*.md)")
HISTORY_NOTE = ("Saved automatically in this app's History on this PC. Save As… also writes a separate text file "
                "(.txt, or Markdown .md) to a folder you choose.")
RETENTION_CHECK_MS = 60 * 60 * 1000  # retention cleanup: at startup, then hourly while the app runs
RETENTION_BATCH = 20  # meetings deleted per cleanup transaction; more follow shortly after
MIC_SHORTCUT_KEY = "mic_shortcut"
DEFAULT_MIC_SHORTCUT = "Ctrl+M"
# Fixed shortcuts (the mute shortcut is configurable in Settings).
SHORTCUTS = (
    ("Ctrl+P", "Pause all / Resume (while a meeting runs)"),
    ("Ctrl+Shift+S", "Stop and save (while a meeting runs)"),
    ("Ctrl+1", "New meeting"), ("Ctrl+2", "Current meeting"), ("Ctrl+3", "History"), ("F1", "Settings and help"),
    ("Ctrl+F", "Search History"),
    ("Ctrl+S", "Save As… (saved transcript)"), ("Ctrl+C", "Copy the selected lines' text (saved transcript)"),
    ("Ctrl+E", "Correct the selected line"), ("F2", "Rename the meeting"),
)
HELP_TEXT = (
    "<p><b>Mute mic</b> stops this app from transcribing your microphone. It does not mute you in Teams, Zoom "
    "or Meet: the meeting still hears you. System audio keeps being transcribed while the mic is excluded. "
    "Your voice can still reach System audio through sidetone, mic monitoring or echo; use <b>Pause all</b> "
    "for a fully private moment.</p>"
    "<p><b>System audio</b> is everything this computer plays on the selected output, including the meeting "
    "app and any other sound. Close music or other calls you do not want transcribed. Headphones help.</p>"
    "<p><b>History</b> keeps each line once it is saved, on this PC only. If saving fails (for example, the "
    "disk is full), capture pauses and lines not yet saved are shown as NOT SAVED; they can be lost if the app "
    "closes before they are saved. How soon text appears after speech depends on this computer's load.</p>"
    "<p><b>Save As…</b> writes a separate file (.txt or .md) where you choose. Such files are independent "
    "copies: deleting, redacting or automatically expiring a meeting in History does not change them, and the "
    "app does not track where they are. No other copies of transcript text are kept, and no audio is saved.</p>"
    "<p><b>Correct</b> adds a new version of a line and keeps the original recognition in its edit history. "
    "<b>Redact</b> removes the line's text, its original and every correction from History and search.</p>"
    "<p><b>Mic</b> and <b>System</b> are audio sources, not identified people.</p>"
)
# Size classes from the window's logical width (see MainWindow._apply_size_class).
WIDE, MEDIUM, NARROW, XNARROW = "wide", "medium", "narrow", "xnarrow"
SHORT_HEIGHT = 520
HIGHLIGHT_MS = 2000
# Returns (path, chosen filter) or None if cancelled. Replaceable for tests.
SaveDialog = Callable[[QWidget, str], "tuple[str, str] | None"]


def native_save_dialog(parent: QWidget, suggested: str) -> tuple[str, str] | None:
    """Windows' own Save As dialog. Overwrite is confirmed by the window afterwards,
    once the final name (with its extension) is known."""
    path, chosen = QFileDialog.getSaveFileName(parent, "Save transcript as", suggested, ";;".join(FILE_FILTERS),
                                               FILE_FILTERS[0], QFileDialog.Option.DontConfirmOverwrite)
    return (path, chosen) if path else None


def export_target(path: str, chosen_filter: str) -> tuple[Path, str]:
    """The file to write and its format: from the typed extension, else the filter."""
    target = Path(path)
    suffix = target.suffix.lower()
    if suffix == ".txt":
        return target, "txt"
    if suffix in (".md", ".markdown"):
        return target, "md"
    fmt = "md" if chosen_filter.startswith("Markdown") else "txt"
    return target.with_name(f"{target.name}.{fmt}"), fmt


def default_data_dir() -> Path:
    override = os.environ.get(DATA_DIR_ENV)
    if override:
        return Path(override)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "MeetingMinutes"


# ------------------------------------------------------------------- engine loading


@dataclass(frozen=True)
class Engine:
    recognizer: Any
    vad_factory: Callable[[], FrameVad]
    info: EngineInfo
    language_mode: str


EngineLoader = Callable[[str], Engine]


_SERVICE: Any = None  # the app's one local Qwen server (speech/qwen.py QwenService)
_SERVICE_LOCK = threading.Lock()


def _qwen_service() -> Any:
    global _SERVICE
    with _SERVICE_LOCK:
        if _SERVICE is None:
            from meeting_minutes.models.manifest import default_manifest_path, load_manifest
            from meeting_minutes.speech.qwen import QwenService

            path = default_manifest_path()
            _SERVICE = QwenService(load_manifest(path), path.parent)
        return _SERVICE


def _manifest_model_label() -> str:
    """The bundled speech model's name for Settings (read from the manifest, no checks)."""
    try:
        from meeting_minutes.models.manifest import default_manifest_path, load_manifest, stt_label

        manifest = load_manifest(default_manifest_path())
        return stt_label(next(m for m in manifest.models if m.role == "stt"))
    except Exception:  # noqa: BLE001 - a missing/unreadable manifest is reported elsewhere
        return "Qwen3-ASR"


def close_local_engines() -> None:
    """Stop the local speech server (bounded); a start still running is cancelled."""
    with _SERVICE_LOCK:
        service = _SERVICE
    if service is not None:
        service.close()


def load_local_engine(language_mode: str) -> Engine:
    """Check the local Qwen3-ASR, llama-server and Silero files in app/models (SHA-256 once
    per run), start the shared local server if needed and return a recognizer for this
    language choice. Nothing is downloaded and there is no other model to fall back to."""
    from meeting_minutes.models.assets import file_path
    from meeting_minutes.speech.qwen import engine_id
    from meeting_minutes.speech.sherpa import SileroVad

    language, policy = QWEN_MODES[language_mode]
    service = _qwen_service()
    recognizer = service.recognizer(language, policy)
    vad = next(m for m in service._manifest.models if m.role == "vad")
    vad_model = file_path(service._models_dir, vad, "model")
    return Engine(recognizer, lambda: SileroVad(vad_model),
                  EngineInfo(engine_id(service.base_id, language, policy), service.model_hash), language_mode)


class _Bridge(QObject):
    """Carries callbacks from worker threads to the UI thread."""

    session_event = Signal(str, object)
    engine_loaded = Signal(str, object, str)  # language mode, engine or None, error code


class _TaggedListener:
    """Session listener that stamps each event with its meeting, so the window can
    drop late events from a meeting it no longer shows (e.g. after deletion)."""

    def __init__(self, signal: Any) -> None:
        self.signal = signal
        self.meeting: str | None = None

    def __call__(self, kind: str, data: dict[str, Any]) -> None:
        self.signal.emit(kind, {**data, "meeting": self.meeting})


def _scroll_page(body: QWidget, name: str) -> QScrollArea:
    """A page that scrolls instead of clipping when the window is small or text is large."""
    scroll = QScrollArea()
    scroll.setObjectName("Page")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    body.setObjectName("PageBody")
    scroll.setWidget(body)
    scroll.setAccessibleName(name)
    return scroll


def _timeline_event(event: TimelineRow) -> dict[str, Any] | None:
    """How a saved timeline note is drawn between transcript lines, or None if it only
    belongs to the coverage panel (finalized, end-of-exclusion and resume markers)."""
    if event.code in ("finalized", "mic_included", "resumed_by_user"):
        return None
    loss = bool(event.metadata.get("loss")) or (event.kind == "error" and event.code != "event_log_overflow")
    if event.kind == "gap" and not event.metadata.get("loss", True):
        loss = False
    tag = None
    if event.code == "segment_redacted":
        label, icon, tag = f"{C.ROW_REDACTED} — {C.ROW_REDACTED_CHIP.lower()}", "redact", ""
    elif event.code == "mic_excluded":
        label, icon = C.EVENT_LABEL["mic_excluded"], "mic-off"
    elif event.kind == "pause" or event.code == "paused_by_user":
        label, icon = C.EVENT_LABEL["paused"], "pause"
    elif event.code == "device_lost" and event.source in C.DEVICE_LOST_LABEL:
        label, icon = C.DEVICE_LOST_LABEL[event.source], "gap"
    elif event.code in C.EVENT_LABEL:
        label, icon = C.EVENT_LABEL[event.code], "gap" if loss else "info"
    else:
        reason = GAP_TEXT.get(event.code, event.code.replace("_", " "))
        who = f" {SOURCE_NAMES[event.source]}" if event.source in SOURCE_NAMES else ""
        label, icon = (f"No{who} text — {reason}", "gap") if loss or event.kind == "gap" else (reason, "info")
    return {"t": event.start_ms, "end": event.end_ms, "label": label, "loss": loss, "icon": icon, "tag": tag}


def _duration(started: str, ended: str | None) -> tuple[str, str] | None:
    """(short, spoken) length from the stored start/stop timestamps, or None if the stop
    time was never recorded (e.g. a meeting recovered after a crash)."""
    if not ended:
        return None
    try:
        start = datetime.fromisoformat(started.replace("Z", "+00:00"))
        stop = datetime.fromisoformat(ended.replace("Z", "+00:00"))
    except ValueError:
        return None
    seconds = (stop - start).total_seconds()
    if seconds < 0:
        return None
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "< 1 min", "less than a minute"
    if minutes < 60:
        return f"{minutes} min", f"{minutes} minutes"
    return f"{minutes // 60} h {minutes % 60:02d} min", f"{minutes // 60} hours {minutes % 60} minutes"


def _clock_of(iso_utc: str | None) -> str:
    """HH:MM local time of a stored UTC timestamp."""
    if not iso_utc:
        return ""
    return local_time(iso_utc)[-5:]


# ------------------------------------------------------------------------ window


class MainWindow(QMainWindow):
    def __init__(self, store: TranscriptStore, backend_factory: Callable[[], CaptureBackend],
                 engine_loader: EngineLoader = load_local_engine, *, recovered: Sequence[str] = (),
                 stop_budget_s: float = 30.0, save_dialog: SaveDialog = native_save_dialog,
                 clock: Callable[[], datetime] = retention.utc_now) -> None:
        super().__init__()
        self.store = store
        self._clock = clock
        self._save_dialog = save_dialog
        self._export_dir: Path | None = None  # last folder chosen, for this run of the app
        self._offer_for: str | None = None  # meeting whose Stop and save should offer Save As
        self._close_requested = False  # Close and save: close once the file choice is made
        self._force_close = False
        self._pending: MeetingSession | None = None  # finished, but its save may still be writing
        self._retention: int | None | object = retention.NOT_CHOSEN  # days; None = keep until deleted
        self._maintenance_pending = False  # a committed deletion's index tidy-up failed; retried
        self._mic_keys = DEFAULT_MIC_SHORTCUT
        self._backend_factory = backend_factory
        self._engine_loader = engine_loader
        self._stop_budget_s = stop_budget_s
        self._engines: dict[str, Engine] = {}
        self._engine_errors: dict[str, str] = {}
        self._engine_retry: set[str] = set()  # failed loads worth trying again (not missing files)
        self._loading: set[str] = set()
        self.session: MeetingSession | None = None
        self.session_backend: CaptureBackend | None = None
        self._started_at = 0.0
        self._ended_elapsed: str | None = None  # elapsed time frozen when capture stopped
        self._stopped_at: float | None = None
        self._partials: dict[str, QListWidgetItem] = {}
        self._open_meeting: str | None = None
        self._open_revision: int | None = None
        self._last_query: str | None = None
        self._session_mode: str | None = None
        self._session_devices: dict[str, str] = {}
        # Engines whose recognizer may still be running an abandoned decode: never
        # reused, and at most one may be busy at a time (see start_meeting).
        self._quarantine: list[tuple[Engine, Any]] = []
        self._theme_choice = "system"
        self._size: tuple[str, bool] | None = None
        self._live_open = False  # "Current meeting" exists: a meeting runs or is finalizing
        self._mic_event: QListWidgetItem | None = None  # open "Mic excluded" row in the live list
        self._pause_event: QListWidgetItem | None = None
        self._live_counts = {"excluded": 0, "paused": 0, "gaps": 0}
        self._source_states: dict[str, str] = {}
        self._recovered = list(recovered)
        self._titles: dict[str, str] = {}
        self._minutes: Any = None  # optional Minutes/Action items workspace (install_minutes_workspace)
        self._block_note = False  # live_health shows the saving-failed explanation
        self._history_error = False
        self._bridge = _Bridge()
        self._bridge.session_event.connect(self._on_session_event, Qt.ConnectionType.QueuedConnection)
        self._bridge.engine_loaded.connect(self._on_engine_loaded, Qt.ConnectionType.QueuedConnection)

        self._apply_stored_theme()
        self.setWindowTitle(C.WINDOW_TITLE)
        self.setWindowIcon(theme.icon("minutes", "accent.bg", 32))
        self._fit_to_screen(1366, 768)
        # An explicit minimum lets the window shrink to 683x336 (1366x768 at 200 %); the size
        # classes then rearrange the layout (two-row bar, compact rail, drawers).
        self.setMinimumSize(560, 300)
        root = QWidget()
        root.setObjectName("Root")
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._build_nav()
        layout.addWidget(self.nav_rail)
        column = QVBoxLayout()
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        self._build_meeting_bar()
        column.addWidget(self.meeting_bar)
        self.alerts = AlertArea()
        column.addWidget(self.alerts)
        # The bar and the alerts sit outside the pages, so they never scroll away; each
        # page scrolls itself (forms) or lets its list scroll (transcripts).
        self.pages = QStackedWidget()
        column.addWidget(self.pages, 1)
        layout.addLayout(column, 1)
        self.setCentralWidget(root)
        self.toast = Toast(root)

        self._build_preflight()
        self._build_live()
        self._build_saved()
        self._build_history()
        self._build_settings()
        self.pages.currentChanged.connect(self._page_changed)
        self.nav_new.clicked.connect(self.show_preflight)
        self.nav_live.clicked.connect(self.show_live)
        self.nav_history.clicked.connect(self.show_history)
        self.nav_settings.clicked.connect(self.show_settings)
        self.mic_shortcut = QShortcut(QKeySequence(DEFAULT_MIC_SHORTCUT), self)
        self.mic_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        self.mic_shortcut.activated.connect(self.toggle_mic)
        self._add_shortcuts()
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._scheme_changed)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_live_status)
        self._timer.start(500)
        self._pending_timer = QTimer(self)
        self._pending_timer.timeout.connect(self._poll_pending)
        self._retention_timer = QTimer(self)
        self._retention_timer.timeout.connect(self.run_retention)
        self._retention_timer.start(RETENTION_CHECK_MS)
        self._highlight_timer = QTimer(self)
        self._highlight_timer.setSingleShot(True)
        self._highlight_timer.timeout.connect(self._clear_highlight)
        self.nav_live.setVisible(False)
        if recovered:
            self.recovered_label.setText(C.RECOVERED_TITLE.format(n=len(recovered)))
            self.recovered_banner.setVisible(True)
        self._load_settings()
        self.refresh_devices()
        self.show_preflight()
        self.run_retention()
        self._apply_size_class()

    def _fit_to_screen(self, width: int, height: int) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width, height = min(width, available.width()), min(height, available.height())
        self.resize(width, height)

    def _add_shortcuts(self) -> None:
        def add(keys: str, action: Callable[[], Any], widget: QWidget | None = None) -> QShortcut:
            shortcut = QShortcut(QKeySequence(keys), widget or self)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if widget is not None
                                else Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(action)
            return shortcut

        add("Ctrl+P", self.toggle_pause)
        add("Ctrl+Shift+S", self.stop_meeting)
        add("Ctrl+1", lambda: self._go(self.show_preflight, self.preflight_heading))
        add("Ctrl+2", lambda: self._go(self.show_live, self.live_title))
        add("Ctrl+3", lambda: self._go(self.show_history, self.history_heading))
        add("F1", lambda: self._go(self.show_settings, self.settings_heading))
        add("Ctrl+F", self.focus_search)
        add("Ctrl+S", lambda: self.save_as(), self.saved_page)
        add("Ctrl+C", self.copy_selected, self.saved_list)
        add("Ctrl+C", self.copy_live_selected, self.live_list)
        add("Ctrl+E", self.correct_selected, self.saved_page)
        add("F2", self.rename_meeting, self.saved_page)
        # Esc closes an opened side panel (narrower windows show them on demand).
        add("Esc", lambda: self.size_class() != WIDE and self.live_side_toggle.setChecked(False), self.live_page)
        add("Esc", lambda: self.size_class() != WIDE and self.coverage_toggle.setChecked(False), self.saved_page)

    def _go(self, show: Callable[[], None], target: QWidget) -> None:
        """A navigation shortcut: show the page, then put keyboard focus on its heading."""
        before = self.pages.currentWidget()
        show()
        if self.pages.currentWidget() is not before or target.isVisible():
            target.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _feedback(self, notice: Notice | QLabel, text: str, tone: str = "ok") -> None:
        """A result stays in the page notice (and is announced); a toast echoes it."""
        if isinstance(notice, Notice):
            notice.setText(text, tone)
        else:
            notice.setText(text)
            announce(notice, text)
        if self.isVisible():
            self.toast.show_message(text)

    # ------------------------------------------------------------------- theme

    def _apply_stored_theme(self) -> None:
        try:
            with self.store.held(UI_STORE_WAIT_S):
                choice = self.store.get_setting(theme.SETTING_KEY)
        except StorageError:
            choice = None
        self._theme_choice = theme.parse_choice(choice)
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, self._theme_choice)

    def set_theme(self, choice: str, *, save: bool = True) -> None:
        """Match Windows / Light / Dark, applied at once (no restart) and stored."""
        choice = theme.parse_choice(choice)
        if save and not self._guard(lambda: self.store.set_setting(theme.SETTING_KEY, choice)):
            self._sync_theme_buttons()
            return
        self._theme_choice = choice
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, choice)
        theme.refresh_icons(self)
        self._search_action.setIcon(theme.icon("search", "text.secondary", 16))
        self._sync_theme_buttons()
        self._refresh_live_status()
        for widget in (self.live_list, self.saved_list, self.search_results, self.meeting_list):
            widget.viewport().update()
        if save:
            self._feedback(self.settings_notice, C.TOAST_THEME.format(name=theme.THEME_LABELS[choice]))

    def _scheme_changed(self, *_: Any) -> None:
        if self._theme_choice == "system":
            self.set_theme("system", save=False)

    def _sync_theme_buttons(self) -> None:
        for choice, radio in self.theme_buttons.items():
            radio.blockSignals(True)
            radio.setChecked(choice == self._theme_choice)
            radio.blockSignals(False)

    # ----------------------------------------------------------- shell widgets

    def _build_nav(self) -> None:
        rail = QFrame()
        rail.setObjectName("NavRail")
        rail.setAccessibleName("Main")
        box = QVBoxLayout(rail)
        box.setContentsMargins(8, 8, 8, 8)
        box.setSpacing(4)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        self.nav_buttons: dict[str, QToolButton] = {}
        keys = {"new": "Ctrl+1", "live": "Ctrl+2", "history": "Ctrl+3", "settings": "F1"}
        for key, icon in (("new", "plus"), ("live", "live"), ("history", "history"), ("settings", "settings")):
            full, _ = C.NAV[key]
            b = QToolButton()
            b.setProperty("nav", True)
            b.setCheckable(True)
            b.setText(full)
            b.setAccessibleName(full)
            b.setToolTip(f"{full} ({keys[key]})")
            b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            theme.set_icon(b, icon, "text.primary", 20)
            self.nav_group.addButton(b)
            box.addWidget(b)
            self.nav_buttons[key] = b
        self.nav_new, self.nav_live = self.nav_buttons["new"], self.nav_buttons["live"]
        self.nav_history, self.nav_settings = self.nav_buttons["history"], self.nav_buttons["settings"]
        box.addStretch(1)
        footer = QFrame()
        footer.setObjectName("NavFooter")
        row = QHBoxLayout(footer)
        row.setContentsMargins(6, 0, 0, 4)
        row.addWidget(icon_label("shield", "text.secondary", 16), 0, Qt.AlignmentFlag.AlignTop)
        row.addWidget(label(C.NAV_FOOTER), 1)
        box.addWidget(footer)
        self.nav_footer = footer
        self.nav_rail = rail

    def _set_nav_compact(self, compact: bool) -> None:
        self.nav_rail.setFixedWidth(theme.size("nav.compact" if compact else "nav.expanded"))
        for key, b in self.nav_buttons.items():
            full, short = C.NAV[key]
            b.setText(short if compact else full)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon if compact
                                 else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            theme.set_props(b, compact=compact)

    def _page_changed(self, _index: int) -> None:
        page = self.pages.currentWidget()
        key = {self.preflight_page: "new", self.live_page: "live", self.history_page: "history",
               self.saved_page: "history", self.settings_page: "settings"}.get(page)
        if key is not None:
            self.nav_buttons[key].setChecked(True)
        self._refresh_bar_buttons()

    def _build_meeting_bar(self) -> None:
        """Always-visible controls while a meeting runs, whichever page is shown."""
        bar = QFrame()
        bar.setObjectName("MeetingBar")
        bar.setAccessibleName("Current meeting controls")
        self._bar_layout = QBoxLayout(QBoxLayout.Direction.LeftToRight, bar)
        self._bar_layout.setContentsMargins(16, 8, 16, 8)
        self._bar_layout.setSpacing(8)
        status = QWidget()
        row = QHBoxLayout(status)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.live_state = QLabel("")
        self.live_state.setTextFormat(Qt.TextFormat.RichText)
        self.live_state.setProperty("state", "rec")
        self.elapsed_label = label("", wrap=False, role="elapsed")
        self.mic_banner = label("", wrap=False, micstate="included")
        self.bar_title = label("", wrap=False, role="sub")
        self.bar_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.bar_title.setMinimumWidth(40)
        row.addWidget(self.live_state)
        row.addWidget(self.elapsed_label)
        row.addWidget(self.mic_banner)
        row.addWidget(self.bar_title, 1)
        buttons = QWidget()
        controls = QHBoxLayout(buttons)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(8)
        self.mic_button = button("", "mic-off", size="large")
        self.mic_button.clicked.connect(self.toggle_mic)
        self.pause_button = button(C.BTN_PAUSE[0], "pause", size="large")
        self.pause_button.clicked.connect(self.toggle_pause)
        self.pause_button.setToolTip("Pause all / Resume (Ctrl+P)")
        self.stop_button = button(C.BTN_STOP[0], "stop", variant="danger", size="large")
        self.stop_button.clicked.connect(self.stop_meeting)
        self.stop_button.setToolTip("Stop and save (Ctrl+Shift+S)")
        self.stop_button.setAccessibleDescription("Shortcut Ctrl+Shift+S")
        self.show_live_button = button(C.BTN_LIVE[0], "live", size="large", accessible=C.BTN_LIVE_ACCESSIBLE)
        self.show_live_button.clicked.connect(self.show_live)
        for b in (self.mic_button, self.pause_button, self.stop_button, self.show_live_button):
            controls.addWidget(b)
        controls.addStretch(1)
        self._bar_status, self._bar_buttons = status, buttons
        self._bar_layout.addWidget(status, 1)
        self._bar_layout.addWidget(buttons, 0)
        bar.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)  # never squeezed
        bar.setVisible(False)
        self.meeting_bar = bar

    # ------------------------------------------------------------- preflight page

    def _build_preflight(self) -> None:
        body = QWidget()
        box = QVBoxLayout(body)
        box.setContentsMargins(24, 20, 24, 24)
        box.setSpacing(16)
        self.preflight_heading = heading(C.NEW_H1)
        self.preflight_sub = label(C.NEW_SUB, role="sub")
        box.addWidget(self.preflight_heading)
        box.addWidget(self.preflight_sub)
        self.recovered_banner = Banner("warn", "", C.RECOVERED_BODY, inline=True, dismissable=True)
        self.recovered_banner.set_actions([(C.RECOVERED_OPEN, self._open_recovered)])
        self.recovered_label = self.recovered_banner.title
        self.recovered_banner.setVisible(False)
        box.addWidget(self.recovered_banner)
        self.retention_prompt = Banner("info", C.RETENTION_TITLE,
                                       C.RETENTION_BODY.format(days=retention.PROPOSED_DAYS), inline=True)
        choose = self.retention_prompt.set_actions([(C.RETENTION_BTN, self.choose_retention)])[0]
        choose.setAccessibleName("Choose how long History keeps transcripts")
        self.retention_prompt.setVisible(False)
        box.addWidget(self.retention_prompt)

        self._preflight_grid = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self._preflight_grid.setSpacing(16)
        form = QVBoxLayout()
        form.setSpacing(16)

        meeting = Card(C.CARD_MEETING)
        self.title_edit = QLineEdit()
        self.title_edit.setAccessibleName("Meeting title")
        self.title_edit.setMaxLength(300)
        self.title_edit.textChanged.connect(lambda t: self.title_edit.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft if is_rtl(t) else Qt.LayoutDirection.LeftToRight))
        meeting.box.addWidget(self._field("Title", self.title_edit, C.TITLE_HELP))
        form.addWidget(meeting)

        sources = Card(C.CARD_SOURCES)
        refresh = button(C.REFRESH, "refresh", size="compact")
        refresh.clicked.connect(lambda: self.refresh_devices(announce_result=True))
        sources.title_row.addWidget(refresh)
        self.mic_combo = QComboBox()
        # Computer sound: one application, everything on an output device, or
        # none. The output-device and application pickers hold real choices only; "None" is
        # the third choice, so an empty picker never starts capture silently.
        self.sound_mode = QComboBox()
        for mode, text in C.SOUND_MODES:
            self.sound_mode.addItem(text, mode)
        self.sound_mode.setCurrentIndex(self.sound_mode.findData("device"))  # today's behaviour
        self.system_combo = QComboBox()
        self.system_combo.setPlaceholderText(C.OUTPUT_PLACEHOLDER)
        self.app_combo = QComboBox()
        self.app_combo.setPlaceholderText(C.APP_PLACEHOLDER)
        self._app_names: dict[str, str] = {}
        self._session_app = False
        self.language_combo = QComboBox()
        for combo in (self.mic_combo, self.sound_mode, self.system_combo, self.app_combo, self.language_combo):
            # Long device names must not widen the window; the open list shows them in full.
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(18)
        for widget, name in ((self.mic_combo, "Microphone"), (self.sound_mode, C.SOUND_LABEL),
                             (self.system_combo, "System audio (playback device)"),
                             (self.app_combo, C.APP_LABEL), (self.language_combo, "Language")):
            widget.setAccessibleName(name)
        self.start_muted = QCheckBox(C.START_MUTED)
        self.start_muted_help = label("", role="help")
        self.allow_degraded = QCheckBox(C.ONE_SOURCE)
        self.allow_degraded.setAccessibleDescription(C.ONE_SOURCE_HELP)
        sources.box.addWidget(self._field(C.MIC_LABEL, self.mic_combo))
        sources.box.addWidget(self.start_muted)
        sources.box.addWidget(self.start_muted_help)
        sources.box.addWidget(self._field(C.SOUND_LABEL, self.sound_mode))
        self.sound_help = label("", role="help")
        sources.box.addWidget(self.sound_help)
        self.app_field = self._field(C.APP_LABEL, self.app_combo)
        self.app_problem = label("", role="help")
        self.app_problem.setVisible(False)
        self.app_field.layout().addWidget(self.app_problem)
        self.system_field = self._field(C.OUTPUT_LABEL, self.system_combo)
        sources.box.addWidget(self.app_field)
        sources.box.addWidget(self.system_field)
        sources.box.addWidget(self.allow_degraded)
        sources.box.addWidget(label(C.ONE_SOURCE_HELP, role="help"))
        sources.box.addWidget(link_button(C.ROUTING_LINK, self.show_routing_help), 0, Qt.AlignmentFlag.AlignLeft)
        self.routing_note = label("System audio is everything this computer plays on the selected output, including "
                                  "the meeting app. " + ROUTING_NOTE, role="help")
        sources.box.addWidget(self.routing_note)
        form.addWidget(sources)

        language = Card(C.CARD_LANGUAGE)
        for mode, text in LANGUAGES:
            self.language_combo.addItem(text, mode)
        self.language_combo.setToolTip(LANGUAGE_NOTE)
        self.language_combo.setAccessibleDescription(LANGUAGE_NOTE)
        self.language_help = label("", role="help")
        language.box.addWidget(self._field(C.LANG_LABEL, self.language_combo))
        language.box.addWidget(self.language_help)
        self.draft_text = QCheckBox(C.DRAFT)
        self.draft_text.setToolTip("Uses about twice the CPU.")
        self.draft_text.setAccessibleDescription("Uses about twice the CPU.")
        language.box.addWidget(self.draft_text)
        language.box.addWidget(label(C.DRAFT_HELP, role="help"))
        form.addWidget(language)
        form.addStretch(1)
        self._preflight_grid.addLayout(form, 1)

        ready = Card(C.READY_H)
        self.readiness_card = ready
        self.model_label = QLabel("Speech model: not loaded yet")
        self.model_item = ReadinessItem(C.MODEL_TITLE["loading"], help_label=self.model_label)
        self.check_model_button = button(C.CHECK_AGAIN, "refresh", size="compact")
        self.check_model_button.clicked.connect(self.check_model_again)
        self.model_item.actions.addWidget(self.check_model_button)
        self.model_item.actions.addStretch(1)
        self.mic_item = ReadinessItem()
        self.system_item = ReadinessItem()
        for item in (self.model_item, self.mic_item, self.system_item):
            ready.box.addWidget(item)
        self.optional_group = QWidget()
        optional = QVBoxLayout(self.optional_group)
        optional.setContentsMargins(0, 4, 0, 0)
        optional.addWidget(label(C.READY_OPTIONAL, role="sub"))
        self.optional_label = label("", role="help")
        optional.addWidget(self.optional_label)
        self.optional_group.setVisible(False)
        ready.box.addWidget(self.optional_group)
        self.start_button = button(C.START, "record", variant="primary", size="cta")
        self.start_button.clicked.connect(lambda: self.start_meeting())
        ready.box.addWidget(self.start_button)
        self.start_error = label("", role="danger")
        self.start_error.setAccessibleName("Start problem")
        ready.box.addWidget(self.start_error)
        self.start_hint = label(C.START_HINT, role="help")
        ready.box.addWidget(self.start_hint)
        ready.box.addStretch(1)
        ready.setMinimumWidth(280)
        self._preflight_grid.addWidget(ready, 0)
        box.addLayout(self._preflight_grid)
        box.addStretch(1)

        self.language_combo.currentIndexChanged.connect(lambda _: self._language_changed())
        self.mic_combo.currentIndexChanged.connect(lambda _: self._update_readiness())
        self.sound_mode.currentIndexChanged.connect(lambda _: self._sound_mode_changed())
        # Picking an output device or an application is that computer-sound choice.
        self.system_combo.currentIndexChanged.connect(lambda _: self._picked_sound("device", self.system_combo))
        self.app_combo.currentIndexChanged.connect(lambda _: self._picked_sound("app", self.app_combo))
        self.start_muted.toggled.connect(lambda _: self._update_readiness())
        self._language_changed(load=False)
        self.preflight_page = _scroll_page(body, C.NEW_H1)
        self._preflight_body = body
        self.pages.addWidget(self.preflight_page)

    @staticmethod
    def _field(name: str, widget: QWidget, help_text: str = "") -> QWidget:
        holder = QWidget()
        column = QVBoxLayout(holder)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)
        caption = label(name, role="strong")
        caption.setBuddy(widget)
        column.addWidget(caption)
        column.addWidget(widget)
        if help_text:
            column.addWidget(label(help_text, role="help"))
        return holder

    def _language_changed(self, load: bool = True) -> None:
        text, role = LANGUAGE_HELP.get(self.language_combo.currentData(), LANGUAGE_HELP["mixed"])
        self.language_help.setText(text)
        theme.set_props(self.language_help, role=role)
        if load:
            self._ensure_engine()

    def refresh_devices(self, announce_result: bool = False) -> None:
        backend = self._backend_factory()
        titles: dict[str, tuple[str, ...]] = {}
        app_error: str | None = None
        try:
            devices = backend.enumerate()
            window_titles = getattr(backend, "window_titles", None)
            if callable(window_titles):
                titles = {d.device_id: tuple(window_titles(d.device_id)) for d in devices if is_application(d)}
            app_error = getattr(backend, "application_error", None)
        except Exception:  # noqa: BLE001 - shown as a fixed message
            devices = []
            self.start_error.setText("Could not list audio devices. Check that Windows audio is running.")
        finally:
            close = getattr(backend, "close", None)
            if callable(close):
                close()
        outputs = [d for d in devices if d.direction is Direction.RENDER and d.available and not is_application(d)]
        apps = [d for d in devices if is_application(d) and d.available]
        mics = [d for d in devices if d.direction is Direction.CAPTURE and d.available]
        self._fill(self.mic_combo, [(d, self._labelled(d)) for d in mics], none_item=True,
                   default=next((d.device_id for d in mics if "default" in d.default_roles), None))
        self._fill(self.system_combo, [(d, self._labelled(d)) for d in outputs], none_item=False,
                   default=next((d.device_id for d in outputs if "default" in d.default_roles), None))
        self._app_names = {d.device_id: d.display_name for d in apps}
        names = [d.display_name for d in apps]
        entries = []
        for d in apps:
            text, window = d.display_name, titles.get(d.device_id, ())
            if names.count(d.display_name) > 1 and window:
                text = f"{d.display_name} — {window[0][:50]}"  # tells instances apart; never stored
            entries.append((d, text))
        self._fill(self.app_combo, entries, none_item=False, default=None, tips=titles)
        self.app_combo.setPlaceholderText(C.APP_PLACEHOLDER if apps else C.APP_NONE_OPEN)
        unsupported = app_error == APP_UNSUPPORTED
        item = self.sound_mode.model().item(self.sound_mode.findData("app"))
        if item is not None:
            item.setEnabled(not unsupported)
        if unsupported and self.sound_mode.currentData() == "app":
            self.sound_mode.setCurrentIndex(self.sound_mode.findData("device"))
        self.app_problem.setText(C.APP_UNSUPPORTED if unsupported else C.APP_LIST_FAILED if app_error else "")
        self.app_problem.setVisible(bool(app_error))
        self._sound_mode_changed()
        if announce_result:
            text = C.REFRESH_DONE.format(mics=len(mics), outs=len(outputs), apps=len(apps))
            self.start_hint.setText(text)
            announce(self.start_hint, text)

    @staticmethod
    def _labelled(d: Any) -> str:
        return d.display_name + ("  [Windows default]" if "default" in d.default_roles else "")

    @staticmethod
    def _fill(combo: QComboBox, entries: list[tuple[Any, str]], *, none_item: bool, default: str | None,
              tips: dict[str, tuple[str, ...]] | None = None) -> None:
        """Refill a picker, keeping the current choice while it is still listed. A picker
        without a "None" item starts empty unless Windows names a default: nothing else is
        chosen on the user's behalf."""
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        if none_item:
            combo.addItem(C.DEVICE_NONE, None)
        choice = 0 if none_item else -1
        for d, text in entries:
            combo.addItem(text, d.device_id)
            tip = "\n".join((tips or {}).get(d.device_id, ())) or d.display_name
            combo.setItemData(combo.count() - 1, tip, Qt.ItemDataRole.ToolTipRole)
            if d.device_id == previous or (previous is None and d.device_id == default and choice in (0, -1)):
                choice = combo.count() - 1
        combo.setCurrentIndex(choice)
        combo.blockSignals(False)

    def _picked_sound(self, mode: str, combo: QComboBox) -> None:
        if combo.currentIndex() >= 0 and self.sound_mode.currentData() != mode:
            self.sound_mode.setCurrentIndex(self.sound_mode.findData(mode))  # updates readiness
        else:
            self._update_readiness()

    def _sound_mode_changed(self) -> None:
        mode = self.sound_mode.currentData()
        self.app_field.setVisible(mode == "app")
        self.system_field.setVisible(mode == "device")
        self.sound_help.setText(C.SOUND_MODE_HELP.get(mode, ""))
        self.routing_note.setText(C.ROUTING_LEAD.get(mode, "") + ROUTING_NOTE)
        self._update_readiness()

    def _system_choice(self) -> str | None:
        """The computer-sound source id for Start, or None (the "None" choice, or a
        picker with nothing chosen yet: Start then says what to choose)."""
        mode = self.sound_mode.currentData()
        combo = self.app_combo if mode == "app" else self.system_combo if mode == "device" else None
        return None if combo is None else combo.currentData()

    def _system_name(self) -> str:
        choice = self._system_choice()
        if choice is None:
            return ""
        if self.sound_mode.currentData() == "app":
            return self._app_names.get(choice, self.app_combo.currentText())  # the program name only
        return self._device_name(self.system_combo)

    def _sound_missing(self) -> str:
        """Why Start waits for a computer-sound choice ("" when nothing is missing)."""
        mode = self.sound_mode.currentData()
        if mode == "app" and self.app_combo.currentData() is None:
            return C.START_WHY_APP
        if mode == "device" and self.system_combo.currentData() is None:
            return C.START_WHY_OUTPUT
        return ""

    def _device_name(self, combo: QComboBox) -> str:
        return combo.currentText().replace("  [Windows default]", "")

    def _update_readiness(self) -> None:
        """Readiness rows and the Start button's look follow the start guards (which stay
        in start_meeting); Start stays focusable and says why when it cannot start."""
        mode = self.language_combo.currentData()
        if mode in self._engines:
            self.model_item.set_state("ok", C.MODEL_TITLE["ready"])
            why = ""
        elif mode in self._engine_errors:
            missing = mode not in self._engine_retry and "SETUP REQUIRED" in self._engine_errors[mode]
            self.model_item.set_state("bad", C.MODEL_TITLE["missing" if missing else "error"])
            why = C.START_WHY_MODEL
        else:
            self.model_item.set_state("busy", C.MODEL_TITLE["loading"])
            why = C.START_WHY_LOADING
        self.check_model_button.setVisible(mode in self._engine_errors)
        mic, system = self.mic_combo.currentData(), self._system_choice()
        sound_mode, sound_missing = self.sound_mode.currentData(), self._sound_missing()
        if mic is None:
            self.mic_item.set_state("neutral", C.READY_MIC["none"], C.READY_MIC_NONE_HELP)
        else:
            self.mic_item.set_state("ok", C.READY_MIC["excluded" if self.start_muted.isChecked() else "selected"],
                                    self._device_name(self.mic_combo))
        if sound_missing:
            self.system_item.set_state("bad", C.READY_SYSTEM["choose"], C.READY_SYSTEM_CHOOSE_HELP[sound_mode])
        elif system is None:
            self.system_item.set_state("neutral", C.READY_SYSTEM["none"], C.READY_SYSTEM_NONE_HELP)
        else:
            self.system_item.set_state("ok", C.READY_SYSTEM["app" if sound_mode == "app" else "selected"],
                                       self._system_name())
        if sound_missing:
            why = why or sound_missing
        elif mic is None and system is None:
            why = why or C.START_WHY_DEVICES
        theme.set_props(self.start_button, blocked=bool(why))
        self.start_button.setAccessibleDescription(why)
        self.start_hint.setText(why or C.START_HINT)
        self.start_muted_help.setText(C.START_MUTED_HELP.format(keys=self._mic_keys))
        if hasattr(self, "settings_model_status"):
            self.settings_model_status.setText(f"{self.model_item.title.text()}. {self.model_label.text()}")

    def check_model_again(self) -> None:
        """Check again: forget the last start-up result for this choice and load it anew
        (checks the local files only; nothing is ever downloaded)."""
        mode = self.language_combo.currentData()
        self._engine_retry.discard(mode)
        self._engine_errors.pop(mode, None)
        self._ensure_engine()

    def _drop_dead_engines(self) -> None:
        """Forget cached engines whose local speech server has exited, so the next
        load goes through the service, which starts a new one."""
        for mode, engine in list(self._engines.items()):
            alive = getattr(getattr(engine.recognizer, "server", None), "alive", None)
            if alive is not None and not alive():
                del self._engines[mode]

    def _retry_failed_load(self, mode: str) -> None:
        if mode in self._engine_retry:
            self._engine_retry.discard(mode)
            self._engine_errors.pop(mode, None)

    def _ensure_engine(self) -> None:
        self._drop_dead_engines()
        mode = self.language_combo.currentData()
        try:
            if mode in self._engines:
                name = model_name(self._engines[mode].info.engine_id)
                text = f"Speech model ready (local {name} + Silero VAD; nothing leaves this PC)."
                if mode == "mixed_r":
                    text += (" Experimental retry (R) is used for a meeting started with this choice; its lines are "
                             "marked.")
                self.model_label.setText(text)
                return
            if mode in self._engine_errors:
                self.model_label.setText(self._engine_errors[mode])
                return
            self.model_label.setText("Starting the local speech model (the first start checks its files and can "
                                     "take up to a minute)…")
            if mode in self._loading:
                return
            self._loading.add(mode)

            def load() -> None:
                try:
                    engine = self._engine_loader(mode)
                    self._bridge.engine_loaded.emit(mode, engine, "")
                except Exception as exc:  # noqa: BLE001 - reported as a fixed setup message
                    from meeting_minutes.speech.sherpa import SpeechAssetsMissing

                    code = ("missing" if isinstance(exc, SpeechAssetsMissing)
                            else str(exc) if _ENGINE_CODE.fullmatch(str(exc)) else type(exc).__name__)
                    self._bridge.engine_loaded.emit(mode, None, code)

            threading.Thread(target=load, name="engine-loader", daemon=True).start()
        finally:
            self._update_readiness()

    @Slot(str, object, str)
    def _on_engine_loaded(self, mode: str, engine: Engine | None, code: str) -> None:
        self._loading.discard(mode)
        if engine is not None:
            self._engines[mode] = engine
        elif code == "missing":
            self._engine_errors[mode] = ("SETUP REQUIRED: the speech model files in app\\models are missing or "
                                         "damaged. Run `python -m meeting_minutes doctor --no-capture` for details. "
                                         "The app never downloads them automatically.")
        else:
            self._engine_errors[mode] = (f"The speech model could not be started ({code}). Press Start "
                                         "transcribing to try again.")
            self._engine_retry.add(mode)
        self._ensure_engine()

    def show_preflight(self) -> None:
        self._retry_failed_load(self.language_combo.currentData())
        self._ensure_engine()
        self.title_edit.setPlaceholderText(datetime.now().strftime("Meeting %Y-%m-%d %H:%M"))
        self.pages.setCurrentWidget(self.preflight_page)

    def show_live(self) -> None:
        if self._live_open:
            self.pages.setCurrentWidget(self.live_page)

    def _open_recovered(self) -> None:
        if self._recovered:
            self.open_meeting(self._recovered[0])

    def show_routing_help(self) -> None:
        self.show_settings()
        self._scroll_settings_to(self.routing_card)

    # -------------------------------------------------------------- live page

    def _build_live(self) -> None:
        page = QWidget()
        page.setObjectName("Page")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName("Header")
        head = QHBoxLayout(header)
        head.setContentsMargins(24, 14, 24, 10)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.live_title = heading("")
        self.live_title.setWordWrap(False)
        self.live_title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.live_sub = label("", role="sub")
        titles.addWidget(self.live_title)
        titles.addWidget(self.live_sub)
        head.addLayout(titles, 1)
        self.follow_latest = QCheckBox(C.FOLLOW[0])
        self.follow_latest.setChecked(True)
        self.follow_latest.setToolTip("New lines scroll into view while you are at the end of the transcript.")
        self.follow_latest.toggled.connect(lambda on: self._jump_to_latest() if on else None)
        head.addWidget(self.follow_latest, 0, Qt.AlignmentFlag.AlignBottom)
        self.live_side_toggle = tool_button(C.SIDE_TOGGLE, "info", checkable=True)
        self.live_side_toggle.toggled.connect(lambda on: self.live_side.setVisible(on))
        head.addWidget(self.live_side_toggle, 0, Qt.AlignmentFlag.AlignBottom)
        outer.addWidget(header)
        self.live_health = Notice()
        self.live_health.setAccessibleName("Live transcript notice")
        notice_box = QVBoxLayout()
        notice_box.setContentsMargins(24, 0, 24, 8)
        notice_box.addWidget(self.live_health)
        self.finalizing_card = Banner("info", C.FINAL_TITLE, "", inline=True)
        self.finalizing_bar = QProgressBar()
        self.finalizing_bar.setTextVisible(False)
        self.finalizing_bar.setAccessibleName("Finalizing progress")
        self.finalizing_card.layout().itemAt(1).layout().addWidget(self.finalizing_bar)
        self.finalizing_card.setVisible(False)
        notice_box.addWidget(self.finalizing_card)
        outer.addLayout(notice_box)

        self.live_split = QSplitter(Qt.Orientation.Horizontal)
        self.live_split.setChildrenCollapsible(False)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        self.live_list = transcript_list("Live transcript", "Saved lines; draft lines are marked Draft and unsaved "
                                         "lines NOT SAVED. Mic and System are audio sources, not people.")
        self.live_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        grid.addWidget(self.live_list, 0, 0)
        self.live_empty = label(C.EMPTY_LIVE, role="sub")
        self.live_empty.setContentsMargins(24, 16, 24, 16)
        grid.addWidget(self.live_empty, 0, 0, Qt.AlignmentFlag.AlignTop)
        self.jump_latest = button(C.JUMP_LATEST, "chevron-down", variant="primary", size="compact")
        self.jump_latest.clicked.connect(self._jump_to_latest)
        self.jump_latest.setVisible(False)
        grid.addWidget(self.jump_latest, 0, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
        self.live_list.verticalScrollBar().valueChanged.connect(self._live_scrolled)
        self._live_max = 0  # the scroll range before the latest layout change
        self.live_list.verticalScrollBar().rangeChanged.connect(self._live_range_changed)
        self.live_split.addWidget(holder)

        side = QFrame()
        side.setObjectName("SidePanel")
        column = QVBoxLayout(side)
        column.setContentsMargins(16, 16, 16, 16)
        column.setSpacing(12)
        column.addWidget(heading(C.SIDE_SOURCES, "h2"))
        self.source_labels: dict[str, QLabel] = {}
        self.reconnect_buttons: dict[str, QPushButton] = {}
        self.source_cards: dict[str, SourceCard] = {}
        for s in ("mic", "system"):
            reconnect = button(f"Reconnect {SOURCE_NAMES[s].lower()}", "refresh", size="compact")
            reconnect.setVisible(False)
            reconnect.clicked.connect(lambda _=False, src=s: self._reconnect(src))
            card = SourceCard(s, reconnect)
            self.reconnect_buttons[s] = reconnect
            self.source_cards[s] = card
            self.source_labels[s] = card.meter_caption
            column.addWidget(card)
        column.addWidget(heading(C.SIDE_STATUS, "h2"))
        status = QFrame()
        status.setObjectName("StatusList")
        rows = QVBoxLayout(status)
        rows.setContentsMargins(12, 10, 12, 10)
        rows.setSpacing(6)
        self.status_rows: dict[str, tuple[QLabel, QLabel]] = {}
        for key in ("recognition", "saving", "gaps", "excluded", "pauses"):
            line = QHBoxLayout()
            mark = QLabel()
            mark.setFixedSize(16, 16)
            text = label("", role="sub")
            line.addWidget(mark, 0, Qt.AlignmentFlag.AlignTop)
            line.addWidget(text, 1)
            rows.addLayout(line)
            self.status_rows[key] = (mark, text)
        column.addWidget(status)
        column.addWidget(link_button(C.STATUS_HELP, self.show_routing_help), 0, Qt.AlignmentFlag.AlignLeft)
        column.addStretch(1)
        self.live_side = QScrollArea()
        self.live_side.setWidgetResizable(True)
        self.live_side.setFrameShape(QFrame.Shape.NoFrame)
        self.live_side.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.live_side.setWidget(side)
        self.live_side.setAccessibleName(C.SIDE_TOGGLE)
        self.live_side.setMinimumWidth(theme.size("sidePanel.minWidth"))
        self.live_side.setMaximumWidth(theme.size("sidePanel.width") + 40)
        self.live_split.addWidget(self.live_side)
        self.live_split.setStretchFactor(0, 1)
        self.live_split.setStretchFactor(1, 0)
        outer.addWidget(self.live_split, 1)
        self.live_page = page
        self.pages.addWidget(page)

    def _status_row(self, key: str, icon: str, role: str, text: str) -> None:
        mark, line = self.status_rows[key]
        theme.set_icon(mark, icon, role, 16)
        line.setText(text)

    def _live_at_bottom(self) -> bool:
        bar = self.live_list.verticalScrollBar()
        return bar.value() >= bar.maximum() - 40

    def _append_live(self, item: QListWidgetItem) -> None:
        """A new row scrolls into view only while following and at the end; otherwise
        the reading position stays and "Jump to latest" appears."""
        at_bottom = self._live_at_bottom()
        self.live_list.addItem(item)
        self.live_empty.setVisible(False)
        if self.follow_latest.isChecked() and at_bottom:
            self.live_list.scrollToBottom()
        else:
            self.jump_latest.setVisible(True)

    def _jump_to_latest(self) -> None:
        self.live_list.scrollToBottom()
        self.jump_latest.setVisible(False)

    def _live_range_changed(self, _minimum: int, maximum: int) -> None:
        """A row that grows in place (a longer draft, a draft replaced by its final
        text) is laid out later and only extends the range. Follow it when the box is
        checked and the view was at the end of the previous range; a reader who scrolled
        up, or an unchecked box, keeps the position."""
        bar = self.live_list.verticalScrollBar()
        was_at_end = bar.value() >= self._live_max - 40
        self._live_max = maximum
        if maximum > bar.value() and was_at_end and self.follow_latest.isChecked():
            bar.setValue(maximum)

    def _live_scrolled(self, _value: int) -> None:
        if self.jump_latest.isVisible() and self._live_at_bottom():
            self.jump_latest.setVisible(False)

    def _clear_live(self) -> None:
        self._partials.clear()
        self.live_list.clear()
        self._mic_event = self._pause_event = None
        self.live_empty.setVisible(True)
        self.jump_latest.setVisible(False)

    def start_meeting(self, *, degraded: bool = False) -> None:
        if self.session is not None and self.session.state not in (SessionState.STOPPED, SessionState.INTERRUPTED):
            self.pages.setCurrentWidget(self.live_page)
            return
        if self.session is not None and not self.session.save_settled.is_set():
            self.start_error.setText("The previous meeting is still being saved. Wait until its saved transcript "
                                     "appears, or restart the app.")
            return
        mode = self.language_combo.currentData()
        self._drop_dead_engines()
        engine = self._engines.get(mode)
        if engine is None:
            self.start_error.setText("The speech model is not ready yet.")
            self._retry_failed_load(mode)
            self._ensure_engine()
            return
        mic, system = self.mic_combo.currentData(), self._system_choice()
        if self._sound_missing():
            self.start_error.setText(self._sound_missing())
            return
        if mic is None and system is None:
            self.start_error.setText("Select a microphone, a playback device, or both.")
            return
        self._quarantine = [(e, p) for e, p in self._quarantine if p.status()["in_flight"] is not None]
        if self._quarantine:
            self.start_error.setText("The previous meeting's speech recognition is still finishing in the "
                                     "background. Wait a minute and try again, or restart the app.")
            return
        self.start_error.setText("")
        self.session_backend = self._backend_factory()
        self._session_mode = mode
        listener = _TaggedListener(self._bridge.session_event)
        self.session = MeetingSession(
            self.store, self.session_backend, engine.recognizer, engine.vad_factory, engine=engine.info,
            speech_config=SpeechConfig(partials=self.draft_text.isChecked()),
            listener=listener, stop_budget_s=self._stop_budget_s)
        listener.meeting = self.session.meeting_id
        allow_degraded = degraded or self.allow_degraded.isChecked()
        try:
            outcome = self.session.start(mic=mic, system=system, mic_included=not self.start_muted.isChecked(),
                                         allow_degraded=allow_degraded, title=self.title_edit.text())
        except StorageError as exc:
            self.start_error.setText(f"Could not create the meeting record ({exc.code}). Nothing was recorded.")
            self.session = None
            return
        if not outcome.started:
            failed = ", ".join(f"{SOURCE_NAMES[s]}: {code}" for s, code in outcome.failed.items())
            self.start_error.setText(f"Could not start capture ({failed}). Nothing was recorded. Check the device "
                                     "is connected and not used exclusively by another app.")
            self.session = None
            if not allow_degraded and mic is not None and system is not None and len(outcome.failed) == 1:
                self._offer_one_source(*next(iter(outcome.failed.items())))
            return
        if outcome.failed:
            self.start_error.setText("Started with one source missing: "
                                     + ", ".join(f"{SOURCE_NAMES[s]} ({c})" for s, c in outcome.failed.items()))
        self._started_at = time.monotonic()
        self._ended_elapsed = None
        self._stopped_at = None
        self._session_devices = {"mic": self._device_name(self.mic_combo) if mic else "",
                                 "system": self._system_name() if system else ""}
        self._session_app = is_application_id(system)
        self.source_cards["system"].note.setText(C.APP_NOTE if self._session_app else C.SYSTEM_NOTE)
        self.reconnect_buttons["system"].setText(C.APP_RECONNECT if self._session_app
                                                 else f"Reconnect {SOURCE_NAMES['system'].lower()}")
        self._clear_live()
        self._live_counts = {"excluded": 0, "paused": 0, "gaps": 0}
        self._source_states = {}
        self.alerts.clear(prefix="m:")
        self.live_health.setText("")
        self._block_note = False
        self.live_title.setText(self.session.title)
        self.live_title.setAccessibleName(self.session.title)
        self.live_title.setToolTip(self.session.title)
        self.bar_title.setText(self.session.title)
        language = dict(LANGUAGES).get(mode, mode)
        self.live_sub.setText(C.LIVE_SUB.format(time=datetime.now().strftime("%H:%M"), language=language,
                                                draft=C.LIVE_DRAFT_ON if self.draft_text.isChecked() else ""))
        if not self.session.mic_included:
            self._mic_changed(False)
        self._live_open = True
        self.nav_live.setVisible(True)
        self.title_edit.clear()
        self.pages.setCurrentWidget(self.live_page)
        self._refresh_live_status()

    def _offer_one_source(self, failed_source: str, code: str) -> None:
        """A selected source could not open and "continue with the other" was off:
        ask, instead of starting with one source silently."""
        combo = self.mic_combo if failed_source == "mic" else (
            self.app_combo if self.sound_mode.currentData() == "app" else self.system_combo)
        name = self._system_name() if combo is self.app_combo else self._device_name(combo)
        choice = self._ask_one_source(failed_source, name, code)
        if choice == "continue":
            self.start_meeting(degraded=True)
        elif choice == "choose":
            self.pages.setCurrentWidget(self.preflight_page)
            combo.setFocus(Qt.FocusReason.OtherFocusReason)
            combo.showPopup()

    def _ask_one_source(self, failed_source: str, device: str, code: str) -> str:
        """"continue", "choose" or "cancel" (replaceable in tests)."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(C.SOURCE_FAILED_TITLE[failed_source])
        box.setText(C.SOURCE_FAILED_TITLE[failed_source])
        box.setInformativeText(C.SOURCE_FAILED_BODY.format(device=device, code=code) + "\n\n"
                               + C.SOURCE_FAILED_CONSEQUENCE[failed_source])
        cancel = box.addButton(C.CANCEL, QMessageBox.ButtonRole.RejectRole)
        choose = box.addButton(C.SOURCE_FAILED_CHOOSE, QMessageBox.ButtonRole.ActionRole)
        go = box.addButton(C.SOURCE_FAILED_CONTINUE[failed_source], QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(go)
        box.setEscapeButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        return "continue" if clicked is go else "choose" if clicked is choose else "cancel"

    def toggle_mic(self) -> None:
        if self.session is None:
            return
        self.session.set_mic_included(not self.session.mic_included)
        self._refresh_live_status()

    def toggle_pause(self) -> None:
        if self.session is None:
            return
        if self.session.state is SessionState.PAUSED:
            if not self.session.resume() and self.session.save_blocked:
                self.live_health.setText(f"Still cannot save ({self.session.storage_error or self.session.save_blocked})."
                                         " Free disk space and press Resume again, or Stop.", "bad")
                return
        else:
            self.session.pause()
        self._refresh_live_status()

    def stop_meeting(self) -> None:
        """Stop and save: finalize (bounded), show the saved transcript, offer Save As."""
        if self.session is not None and self.session.state in (SessionState.CAPTURING, SessionState.DEGRADED,
                                                               SessionState.PAUSED):
            self._offer_for = self.session.meeting_id
            self.session.stop()
            self._refresh_live_status()

    def _reconnect(self, source: str) -> None:
        if self.session is not None and not self.session.reconnect(Source(source)):
            self.live_health.setText(f"{SOURCE_NAMES[source]} could not be reopened. Check the device and try again.",
                                     "bad")

    def _meeting_ms(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)

    def _open_event(self, label_text: str, icon: str, *, loss: bool = False) -> QListWidgetItem:
        item = QListWidgetItem()
        start = self._meeting_ms()
        set_event(item, f"— {clock_text(start)}: {label_text} —", start_ms=start, end_ms=None, label=label_text,
                  loss=loss, icon=icon)
        self._append_live(item)
        return item

    def _close_event(self, item: QListWidgetItem | None) -> None:
        if item is None or self.live_list.row(item) < 0:
            return
        row = dict(item.data(ROW_ROLE))
        end = self._meeting_ms()
        set_event(item, f"— {clock_text(row['t'])}–{clock_text(end)}: {row['label']} —", start_ms=row["t"],
                  end_ms=end, label=row["label"], loss=row["loss"], icon=row["icon"])

    def _mic_changed(self, included: bool) -> None:
        if not included and self._mic_event is None:
            self._live_counts["excluded"] += 1
            self._mic_event = self._open_event(C.EVENT_LABEL["mic_excluded"], "mic-off")
        elif included and self._mic_event is not None:
            self._close_event(self._mic_event)
            self._mic_event = None

    @Slot(str, object)
    def _on_session_event(self, kind: str, data: dict[str, Any]) -> None:
        if self.session is None or data.get("meeting") != self.session.meeting_id:
            return  # a late event from a meeting no longer shown (e.g. deleted)
        if kind == "partial":
            item = self._partials.get(data["id"])
            if item is None:
                item = QListWidgetItem()
                self._partials[data["id"]] = item
                self._set_line(item, data["source"], data["start_ms"], data["text"], draft=True)
                self._append_live(item)
            else:
                self._set_line(item, data["source"], data["start_ms"], data["text"], draft=True)
        elif kind == "partials_cleared":
            for key, item in list(self._partials.items()):
                if key.startswith(data["source"] + ":") and (
                        data["through"] is None or int(key.split(":")[1]) <= data["through"]):
                    self.live_list.takeItem(self.live_list.row(item))
                    del self._partials[key]
        elif kind in ("final", "final_unsaved"):
            item = self._partials.pop(data.get("hypothesis_id", ""), None)
            self_added = item is None
            if item is None:
                item = QListWidgetItem()
            self._set_line(item, data["source"], data["start_ms"], data["text"],
                           unsaved=data.get("code") if kind == "final_unsaved" else None, flags=data.get("flags", ()))
            if self_added:
                self._append_live(item)
            elif self.follow_latest.isChecked() and self._live_at_bottom():
                self.live_list.scrollToBottom()
        elif kind == "gap":
            item = QListWidgetItem()
            reason = data["reason"]
            if reason == "device_lost":
                label_text = C.DEVICE_LOST_LABEL[data["source"]]
            else:
                label_text = C.EVENT_LABEL.get(reason) or (f"No {SOURCE_NAMES[data['source']]} text — "
                                                           f"{GAP_TEXT.get(reason, reason)}")
            set_event(item, f"— {SOURCE_NAMES[data['source']]} {clock_text(data['start_ms'])}–"
                            f"{clock_text(data['end_ms'])}: no text ({GAP_TEXT.get(reason, reason)}) —",
                      start_ms=data["start_ms"], end_ms=data["end_ms"], label=label_text, loss=True)
            self._live_counts["gaps"] += 1
            self._append_live(item)
        elif kind == "mic":
            self._mic_changed(bool(data.get("included", True)))
        elif kind == "source_lost":
            self.reconnect_buttons[data["source"]].setVisible(True)
        elif kind == "storage_error":
            self.live_health.setText(f"Saving problem: {data['code']}. It will be retried.", "warn")
        elif kind == "save_blocked":
            self._block_note = True
            self.live_health.setText(f"Saving failed ({data['code']}): capture is PAUSED so nothing more is lost. "
                                     "Text not yet saved stays on screen marked NOT SAVED. Free disk space, then "
                                     "press Resume — or Stop.", "bad")
        elif kind == "save_recovered":
            self.live_health.setText("Saving works again; the unsaved text has been saved."
                                     + (" Press Resume to continue." if data.get("capture_paused") else ""), "ok")
        elif kind == "state" and data["state"] == "paused":
            if self._pause_event is None:
                if self.session.save_blocked:
                    self._pause_event = self._open_event(C.EVENT_LABEL["save_failed"], "error", loss=True)
                else:
                    self._live_counts["paused"] += 1
                    self._pause_event = self._open_event(C.EVENT_LABEL["paused"], "pause")
        elif kind == "state" and data["state"] in ("capturing", "degraded"):
            self._close_event(self._pause_event)
            self._pause_event = None
        elif kind == "state" and data["state"] == "finalizing":
            self._stopped_at = time.monotonic()
            self._ended_elapsed = clock_text(self._meeting_ms())
        elif kind == "state" and data["state"] in ("stopped", "interrupted"):
            session = self.session
            report = data.get("report") or {}
            if report.get("abandoned_in_flight") or report.get("save_timed_out"):
                # Its recognizer may still be decoding an abandoned chunk: never reuse it.
                engine = self._engines.pop(self._session_mode, None)
                if engine is not None:
                    self._quarantine.append((engine, session.pipeline))
            self._live_open = False
            self.nav_live.setVisible(False)
            self._clear_live()  # the saved view is now the one copy shown
            self.alerts.clear(prefix="m:")
            self._show_finished(session, report)
        self._refresh_live_status()

    def _show_finished(self, session: MeetingSession, report: dict[str, Any]) -> None:
        """Show a finished meeting without waiting on the store. If its
        save timed out or the store is busy, show what is known in memory and read the
        saved transcript later, once the save has returned."""
        snapshot = None
        if not report.get("save_timed_out") and session.save_settled.is_set():
            try:
                snapshot = self.store.snapshot(session.meeting_id, wait_s=UI_STORE_WAIT_S)
            except StorageError:
                snapshot = None
        if snapshot is None:
            self._show_pending(session, report)
            return
        self._render_saved(snapshot)
        self._finished_notice(snapshot)
        self._after_finish(session.meeting_id)

    def _finished_notice(self, snapshot: MeetingSnapshot) -> None:
        summary = coverage_summary(snapshot.meeting.status, snapshot.timeline)
        complete = summary.startswith("Complete")
        self.saved_notice.setText(C.SAVED_COMPLETE if complete else C.SAVED_INCOMPLETE, "ok" if complete else "warn")

    def _show_pending(self, session: MeetingSession, report: dict[str, Any]) -> None:
        self._pending = session
        self._open_meeting = session.meeting_id
        self._open_revision = None
        self._set_workspace_meeting(None, None)  # no saved revision can be read yet
        self.saved_heading.setText(session.title)
        self.saved_heading.setAccessibleName(session.title)
        set_chip(self.saved_chip, C.CHIP_INTERRUPTED, "bad")
        self.saved_title.setText(f"{STATE_TEXT['interrupted']} · saving has not finished yet")
        unsaved = report.get("unsaved_finals", 0)
        self.saved_coverage.setText(
            "Ended incomplete: saving did not finish within the Stop time limit, so this meeting is NOT fully "
            f"saved ({unsaved} line(s) not saved yet). The app stays usable; the saved transcript appears here "
            "as soon as the disk responds. A file can be saved after that.")
        set_chip(self.coverage_chip, C.CHIP_INTERRUPTED, "bad")
        self.saved_list.clear()
        self._add_unsaved_lines(session)
        self.events_list.clear()
        self.coverage_times.setText("")
        self.saved_notice.setText("")
        self.pending_banner.setVisible(True)
        self.pages.setCurrentWidget(self.saved_page)
        self._update_toolbar()
        self._pending_timer.start(PENDING_POLL_MS)
        if self._close_requested:
            QTimer.singleShot(0, self._ask_close_while_saving)

    def _poll_pending(self) -> None:
        session = self._pending
        if session is None or not session.save_settled.is_set():
            return  # reading now could wait on the stalled save
        try:
            snapshot = self.store.snapshot(session.meeting_id, wait_s=0)
        except StorageError:
            return
        self._pending = None
        self._pending_timer.stop()
        self.pending_banner.setVisible(False)
        if snapshot is None:
            return
        if self._open_meeting == session.meeting_id:
            # Bring it to the front only if it is shown already or Save As is about to be offered for it.
            self._render_saved(snapshot, show=self.pages.currentWidget() is self.saved_page
                               or self._offer_for == session.meeting_id)
        self._after_finish(session.meeting_id)
        self._refresh_live_status()

    def _ask_close_while_saving(self) -> None:
        session = self._pending
        if session is None or not self._close_requested:
            return
        answer = QMessageBox.question(
            self, "Saving has not finished",
            "Saving did not finish within the Stop time limit, so the transcript file cannot be made yet. "
            "Close now anyway? Text not saved yet may be lost. Choose No to wait; the window then offers "
            "Save As when saving finishes.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self._force_close = True
            self.close()

    def _after_finish(self, meeting_id: str) -> None:
        if self._offer_for == meeting_id:
            self._offer_for = None
            QTimer.singleShot(0, lambda: self._offer_save_as(meeting_id))

    def _offer_save_as(self, meeting_id: str) -> None:
        """After Stop and save (or Close and save): ask where to also save a file."""
        if self._open_meeting != meeting_id:
            self._close_requested = False
            return
        written = self.save_as()
        if self._close_requested:
            self._close_requested = False
            if written:
                self.close()
            else:
                self.saved_notice.setText(C.CLOSE_CANCELLED, "warn")

    def _set_line(self, item: QListWidgetItem, source: str, start_ms: int, text: str, *, draft: bool = False,
                  unsaved: str | None = None, flags: Sequence[str] = ()) -> None:
        prefix = f"[{clock_text(start_ms)}] {SOURCE_NAMES[source]}" + quality_note(flags)
        if draft:
            prefix += " (draft)"
        if unsaved:
            prefix += f" — NOT SAVED ({unsaved})"
        set_line(item, prefix, text, source=source, start_ms=start_ms,
                 state="draft" if draft else "unsaved" if unsaved else "final", code=unsaved, flags=flags)

    # ------------------------------------------------------------ live status

    def _pill(self, kind: str, elapsed: str) -> None:
        text = C.PILL[kind]
        mark = {"rec": ("●", "live.recording"), "paused": ("❚❚", "status.warning.fg"),
                "blocked": ("!", "status.danger.fg"), "finalizing": ("⟳", "status.info.fg"),
                "saving": ("◷", "status.info.fg")}[kind]
        self.live_state.setText(f'<span style="color:{theme.color(mark[1]).name()}">{mark[0]}</span>&nbsp;{text}')
        if self.live_state.accessibleName() != text:
            self.live_state.setAccessibleName(text)
            announce(self.live_state, text)
        theme.set_props(self.live_state, state=kind)
        self.elapsed_label.setText(elapsed)
        self.setWindowTitle(f"{C.WINDOW_TITLE} — {C.TITLE_STATE[kind]} {elapsed}")

    def _refresh_bar_buttons(self) -> None:
        self.show_live_button.setVisible(self._live_open and self.pages.currentWidget() is not self.live_page)

    def _refresh_live_status(self) -> None:
        session = self.session
        status = session.status() if session is not None else None
        still_saving = (session is not None and self._pending is session and not session.save_settled.is_set())
        # The bar (mic state, Mute/Unmute, Pause, Stop) stays above every page until
        # the meeting has finished (and, if its save stalled, until that settles).
        self.meeting_bar.setVisible(status is not None and (status["state"] in (
            "capturing", "degraded", "paused", "finalizing") or still_saving))
        self._refresh_bar_buttons()
        if status is None:
            self.setWindowTitle(C.WINDOW_TITLE)
            if self.alerts.keys():
                self.alerts.clear(prefix="m:")
            return
        state = status["state"]
        live = state in ("capturing", "degraded", "paused")
        if live:
            elapsed = clock_text(self._meeting_ms())
        else:
            elapsed = self._ended_elapsed or clock_text(self._meeting_ms())
        if still_saving:
            kind = "saving"
        elif state == "finalizing":
            kind = "finalizing"
        elif status["save_blocked"]:
            kind = "blocked"
        elif state == "paused":
            kind = "paused"
        elif live:
            kind = "rec"
        else:
            self.setWindowTitle(C.WINDOW_TITLE)
            kind = None
        if kind is not None:
            self._pill(kind, elapsed)
        # The rail's "Current meeting" item carries the live colour while transcribing.
        theme.set_icon(self.nav_live, "live", "live.recording" if kind == "rec" else "text.primary", 20)
        if status["save_blocked"]:
            self.live_state.setToolTip(f"Saving failed ({status['storage_error'] or status['save_blocked']})")
        else:
            self.live_state.setToolTip("")
        short = self._size is not None and self._size[0] == XNARROW
        included = status["mic_included"]
        mic_selected = status["sources"].get("mic", {}).get("device_id") is not None
        names = C.BTN_MUTE if included else C.BTN_UNMUTE
        self.mic_button.setText(names[1] if short else f"{names[0]}  ({self._mic_keys})")
        self.mic_button.setAccessibleName(names[0])
        self.mic_button.setToolTip(f"{names[0]} ({self._mic_keys}) — this app's transcription only; it does not "
                                   "mute you in the meeting app.")
        self.mic_button.setAccessibleDescription((MIC_INCLUDED_TEXT if included else MIC_EXCLUDED_TEXT)
                                                 + f". Shortcut {self._mic_keys}.")
        theme.set_icon(self.mic_button, "mic-off" if included else "mic", "text.primary", 20)
        chip_text = C.MIC_CHIP[included][1 if short else 0] if mic_selected or not included else C.MIC_NONE
        self.mic_banner.setText(chip_text)
        self.mic_banner.setAccessibleName(chip_text)
        self.mic_banner.setAccessibleDescription(MIC_INCLUDED_TEXT if included else MIC_EXCLUDED_TEXT)
        self.mic_banner.setToolTip(MIC_INCLUDED_TEXT if included else MIC_EXCLUDED_TEXT)
        theme.set_props(self.mic_banner, micstate="excluded" if not included
                        else "included" if mic_selected else "none")
        self.mic_button.setEnabled(live)
        self.pause_button.setEnabled(live)
        paused = state == "paused"
        self.pause_button.setText(C.BTN_RESUME if paused else C.BTN_PAUSE[1 if short else 0])
        self.pause_button.setAccessibleName(C.BTN_RESUME if paused else C.BTN_PAUSE[0])
        theme.set_icon(self.pause_button, "play" if paused else "pause", "text.primary", 20)
        self.stop_button.setText(C.BTN_STOP[1 if short else 0])
        self.stop_button.setEnabled(live)
        self.show_live_button.setText(C.BTN_LIVE[1 if short else 0])
        self._refresh_sources(status, live)
        self._refresh_finalizing(state)
        self._refresh_status_panel(status)
        self._refresh_alerts(status, live)
        if status["save_blocked"]:
            if status["storage_error"] is None:
                self.live_health.setText("Saving works again; the unsaved text has been saved. Press Resume to "
                                         "continue, or Stop.", "ok")
            else:
                self.live_health.setText(f"Capture is paused so nothing more is lost. {status['unsaved_finals']} "
                                         "line(s) are NOT SAVED yet. Free disk space, then press Resume — or Stop.",
                                         "bad")
            self._block_note = True
        elif self._block_note and live and state != "paused":
            self._block_note = False
            self.live_health.setText("")

    def _refresh_sources(self, status: dict[str, Any], live: bool) -> None:
        state = status["state"]
        included = status["mic_included"]
        for name, s in status["sources"].items():
            card = self.source_cards[name]
            excluded = name == "mic" and not included
            if s["device_id"] is None:
                key, caption, level = "none", "", None
                text = f"{SOURCE_NAMES[name]}: not selected"
            elif state in ("finalizing", "stopped", "interrupted"):
                key, caption, level = "stopped", C.METER_STOPPED, None
                text = f"{SOURCE_NAMES[name]}: stopped"
            elif excluded:
                # The excluded mic is closed (gated before VAD/STT): never read for a meter.
                key, caption, level = "excluded", C.METER_EXCLUDED, None
                text = "Mic: excluded — not transcribed"
            elif state == "paused":
                key, caption, level = "paused", C.METER_PAUSED, None
                text = f"{SOURCE_NAMES[name]}: paused"
            elif s["state"] == "running":
                key, level = "transcribing", s["level_dbfs"]
                caption = C.METER_LEVEL + (" — silent" if level < -90 else f" — {level:.0f} dBFS")
                text = f"{SOURCE_NAMES[name]}: receiving, " + ("silent" if level < -90 else f"level {level:.0f} dBFS")
            elif s["state"] in ("lost",):
                key, caption, level = "lost", C.METER_LOST, None
                text = f"{SOURCE_NAMES[name]}: lost" + (f" ({s['failure']})" if s.get("failure") else "")
            elif s["state"] in ("failed",):
                key, caption, level = "failed", C.METER_LOST, None
                text = f"{SOURCE_NAMES[name]}: failed" + (f" ({s['failure']})" if s.get("failure") else "")
            else:
                key, caption, level = "starting", "", None
                text = f"{SOURCE_NAMES[name]}: {s['state']}"
            chip_text, kind = C.SOURCE_CHIP[key]
            set_chip(card.chip, chip_text, kind)
            card.device.setText(self._session_devices.get(name, ""))
            card.device.setVisible(bool(self._session_devices.get(name)))
            card.meter_caption.setText(caption)
            card.meter_caption.setAccessibleDescription(text)
            card.meter.setVisible(key not in ("none",))
            card.meter.set_level(level, excluded=excluded)
            card.meter.setAccessibleDescription(caption)
            lost = s["state"] in ("lost", "failed") and state in ("capturing", "degraded")
            self.reconnect_buttons[name].setVisible(lost)
            previous = self._source_states.get(name)
            if previous in ("lost", "failed") and s["state"] == "running" and live:
                body = (C.ALERT_RECONNECTED_BODY[included] if name == "mic" else C.ALERT_RECONNECTED_SYSTEM)
                self.alerts.show_alert(f"m:reconnected:{name}", "ok", C.ALERT_RECONNECTED_TITLE[name], body,
                                       dismissable=True)
            if s["state"] in ("lost", "failed"):
                self.alerts.hide_alert(f"m:reconnected:{name}")
            self._source_states[name] = s["state"]

    def _refresh_finalizing(self, state: str) -> None:
        finalizing = state == "finalizing"
        self.finalizing_card.setVisible(finalizing)
        if finalizing:
            budget = max(1, int(self._stop_budget_s))
            spent = 0 if self._stopped_at is None else time.monotonic() - self._stopped_at
            self.finalizing_card.set_content("info", C.FINAL_TITLE, C.FINAL_BODY.format(s=budget))
            self.finalizing_bar.setRange(0, budget * 10)
            self.finalizing_bar.setValue(min(budget * 10, int(spent * 10)))

    def _refresh_status_panel(self, status: dict[str, Any]) -> None:
        lag = status["oldest_pending_age_ms"]
        if lag > 3000:
            self._status_row("recognition", "clock", "status.warning.fg", C.STATUS_BEHIND.format(n=int(lag / 1000)))
        else:
            self._status_row("recognition", "check", "status.success.fg", C.STATUS_KEEPING_UP)
        if status["unsaved_finals"]:
            self._status_row("saving", "error", "status.danger.fg", C.STATUS_UNSAVED.format(n=status["unsaved_finals"]))
        elif status["storage_error"]:
            self._status_row("saving", "warning", "status.warning.fg",
                             C.STATUS_SAVE_RETRY.format(code=status["storage_error"]))
        else:
            self._status_row("saving", "save", "text.secondary", C.STATUS_SAVED)
        gaps = max(status["speech_gaps"], self._live_counts["gaps"])
        self._status_row("gaps", "gap", "status.warning.fg" if gaps else "text.secondary", C.STATUS_GAPS.format(n=gaps))
        self._status_row("excluded", "mic-off", "text.secondary",
                         C.STATUS_EXCLUDED.format(n=self._live_counts["excluded"]))
        self._status_row("pauses", "pause", "text.secondary", C.STATUS_PAUSES.format(n=self._live_counts["paused"]))

    def _refresh_alerts(self, status: dict[str, Any], live: bool) -> None:
        """Meeting-level banners; failure codes appear only under Technical details."""
        state = status["state"]
        capturing = state in ("capturing", "degraded")
        for name, s in status["sources"].items():
            device = self._session_devices.get(name) or SOURCE_NAMES[name]
            lost = s["state"] in ("lost", "failed") and capturing
            other = "system" if name == "mic" else "mic"
            details = f"Failure code: {s['failure']}" if s.get("failure") else None
            if lost and s["state"] == "failed" and status["sources"][other]["state"] == "running":
                title, body = C.ALERT_DEGRADED[other]
                self.alerts.hide_alert(f"m:lost:{name}")
                self.alerts.show_alert(f"m:degraded:{name}", "info", title, body, details=details,
                                       actions=[(C.ALERT_RECONNECT[name], lambda src=name: self._reconnect(src))])
            elif lost:
                self.alerts.hide_alert(f"m:degraded:{name}")
                app = name == "system" and self._session_app
                self.alerts.show_alert(f"m:lost:{name}", "bad", C.APP_LOST_TITLE if app else C.ALERT_LOST_TITLE[name],
                                       (C.APP_LOST_BODY if app else C.ALERT_LOST_BODY[name]).format(device=device),
                                       details=details,
                                       actions=[(C.APP_RECONNECT if app else C.ALERT_RECONNECT[name],
                                                 lambda src=name: self._reconnect(src))])
            else:
                self.alerts.hide_alert(f"m:lost:{name}")
                self.alerts.hide_alert(f"m:degraded:{name}")
        lag = status["oldest_pending_age_ms"]
        if live and lag > 8000:
            self.alerts.show_alert("m:backlog", "warn", C.ALERT_BACKLOG_TITLE.format(n=int(lag / 1000)),
                                   C.ALERT_BACKLOG_BODY, details=f"oldest_pending_age_ms = {lag}")
        else:
            self.alerts.hide_alert("m:backlog")
        if status["save_blocked"] and status["storage_error"] is not None:
            self.alerts.hide_alert("m:save_recovered")
            self.alerts.show_alert("m:save_failed", "bad", C.ALERT_SAVE_FAILED_TITLE,
                                   C.ALERT_SAVE_FAILED_BODY.format(n=status["unsaved_finals"]),
                                   details=f"Storage error: {status['storage_error']}")
        elif status["save_blocked"]:
            self.alerts.hide_alert("m:save_failed")
            self.alerts.show_alert("m:save_recovered", "ok", C.ALERT_SAVE_RECOVERED_TITLE, C.ALERT_SAVE_RECOVERED_BODY)
        else:
            self.alerts.hide_alert("m:save_failed")
            self.alerts.hide_alert("m:save_recovered")
        if not (live or state == "finalizing"):
            self.alerts.clear(prefix="m:")

    def copy_live_selected(self) -> None:
        """Copy the selected live lines' own text (as in the saved view)."""
        items = sorted(self.live_list.selectedItems(), key=self.live_list.row)
        texts = [t for t in (i.data(TEXT_ROLE) for i in items) if t is not None]
        if texts:
            QGuiApplication.clipboard().setText("\n".join(texts))
            self._feedback(self.live_health, C.TOAST_COPIED.format(n=len(texts)))

    # ------------------------------------------------------- saved transcript page

    def _build_saved(self) -> None:
        page = QWidget()
        page.setObjectName("Page")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QFrame()
        header.setObjectName("Header")
        head = QVBoxLayout(header)
        head.setContentsMargins(24, 14, 24, 8)
        head.setSpacing(4)
        top = QHBoxLayout()
        self.saved_heading = heading("", "title")
        self.saved_heading.setWordWrap(False)
        self.saved_heading.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.saved_chip = chip("", "neutral")
        self.rename_button = tool_button(C.RENAME, "rename", tip="Rename the meeting (F2)")
        top.addWidget(self.saved_heading, 1)
        top.addWidget(self.saved_chip)
        top.addSpacing(12)
        top.addWidget(self.rename_button)
        head.addLayout(top)
        self.saved_title = label("", role="sub", selectable=True)
        head.addWidget(self.saved_title)
        self.saved_notice = Notice()
        self.saved_notice.setAccessibleName("Saved transcript notice")
        head.addWidget(self.saved_notice)
        self.pending_banner = Banner("warn", C.STALLED_TITLE, C.STALLED_BODY, inline=True)
        self.pending_banner.set_actions([(C.STALLED_BROWSE, self.show_history)])
        self.pending_banner.setVisible(False)
        head.addWidget(self.pending_banner)
        outer.addWidget(header)
        self.workspace_tabs = QTabBar()
        self.workspace_tabs.setAccessibleName(C.TABS_ACCESSIBLE)
        self.workspace_tabs.setDocumentMode(True)
        self.workspace_tabs.setExpanding(False)
        self.workspace_tabs.addTab(C.MINUTES_TABS[0])
        self.workspace_tabs.setVisible(False)
        tabs_row = QHBoxLayout()
        tabs_row.setContentsMargins(16, 0, 16, 0)
        tabs_row.addWidget(self.workspace_tabs)
        tabs_row.addStretch(1)
        outer.addLayout(tabs_row)
        self.workspace_stack = QStackedWidget()
        outer.addWidget(self.workspace_stack, 1)

        transcript_tab = QWidget()
        tab_box = QVBoxLayout(transcript_tab)
        tab_box.setContentsMargins(0, 0, 0, 0)
        tab_box.setSpacing(0)
        toolbar = QFrame()
        toolbar.setObjectName("Toolbar")
        bar = QHBoxLayout(toolbar)
        bar.setContentsMargins(16, 6, 16, 6)
        bar.setSpacing(4)
        self.save_as_button = button(C.TB_SAVE_AS, "save-as", size="compact", tip=C.TB_SAVE_AS_TIP)
        self.copy_button = tool_button(C.TB["copy"], "copy", tip="Copy the selected lines' text (Ctrl+C)")
        self.edit_button = tool_button(C.TB["correct"], "edit", tip="Correct the selected line (Ctrl+E)")
        self.redact_button = tool_button(C.TB["redact"], "redact", tip="Redact the selected line")
        self.revisions_button = tool_button(C.TB["history"], "versions", tip="Show the selected line's edit history")
        self.selection_label = label(C.TB_HINT, wrap=False, role="help")
        self.selection_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.clear_selection = link_button(C.TB_CLEAR, lambda: self.saved_list.clearSelection())
        self.coverage_toggle = tool_button(C.TB["coverage"], "info", checkable=True, tip="Show or hide coverage")
        self.delete_button = tool_button(C.TB["delete"], "trash", variant="dangerText",
                                         tip="Delete this meeting from History")
        self._toolbar_buttons = [self.copy_button, self.edit_button, self.redact_button, self.revisions_button,
                                 self.coverage_toggle, self.delete_button, self.rename_button]
        bar.addWidget(self.save_as_button)
        bar.addSpacing(8)
        for b in (self.copy_button, self.edit_button, self.redact_button, self.revisions_button):
            bar.addWidget(b)
        bar.addSpacing(8)
        bar.addWidget(self.selection_label, 1)
        bar.addWidget(self.clear_selection)
        bar.addWidget(self.coverage_toggle)
        bar.addWidget(self.delete_button)
        tab_box.addWidget(toolbar)
        self.saved_split = QSplitter(Qt.Orientation.Horizontal)
        self.saved_split.setChildrenCollapsible(False)
        self.saved_list = transcript_list("Saved transcript", "Each line: time, source (Mic or System; not a "
                                          "speaker name), then the text")
        self.saved_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.saved_list.itemSelectionChanged.connect(self._update_toolbar)
        holder = QWidget()
        grid = QGridLayout(holder)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.addWidget(self.saved_list, 0, 0)
        self.saved_empty = label("No saved text in this meeting.", role="sub")
        self.saved_empty.setContentsMargins(24, 16, 24, 16)
        self.saved_empty.setVisible(False)
        grid.addWidget(self.saved_empty, 0, 0, Qt.AlignmentFlag.AlignTop)
        self.saved_split.addWidget(holder)
        panel = QFrame()
        panel.setObjectName("SidePanel")
        column = QVBoxLayout(panel)
        column.setContentsMargins(16, 16, 16, 16)
        column.setSpacing(12)
        column.addWidget(heading(C.COV_H, "h2"))
        summary = Card()
        self.coverage_chip = chip("", "neutral")
        summary.box.addWidget(self.coverage_chip)
        self.saved_coverage = label("", selectable=True)
        summary.box.addWidget(self.saved_coverage)
        column.addWidget(summary)
        facts = Card(spacing=6)
        self.coverage_times = label("", role="sub")
        self.coverage_drain = label("", role="sub")
        facts.box.addWidget(self.coverage_times)
        facts.box.addWidget(self.coverage_drain)
        facts.box.addWidget(label(C.COV_WHERE, role="sub"))
        column.addWidget(facts)
        column.addWidget(heading(C.COV_TIMELINE, "h2"))
        self.events_list = QListWidget()
        self.events_list.setObjectName("Timeline")
        self.events_list.setAccessibleName("Coverage and events")
        self.events_list.setWordWrap(True)
        self.events_list.setMinimumHeight(120)
        self.events_list.itemActivated.connect(self._jump_to_event)
        self.events_list.itemClicked.connect(self._jump_to_event)
        column.addWidget(self.events_list, 1)
        self.coverage_none = label(C.COV_NONE, role="help")
        column.addWidget(self.coverage_none)
        column.addWidget(label(C.COV_EXPORT_NOTE, role="help"))
        column.addWidget(label(HISTORY_NOTE, role="help"))
        self.coverage_panel = QScrollArea()
        self.coverage_panel.setWidgetResizable(True)
        self.coverage_panel.setFrameShape(QFrame.Shape.NoFrame)
        self.coverage_panel.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.coverage_panel.setWidget(panel)
        self.coverage_panel.setAccessibleName(C.COV_H)
        self.coverage_panel.setMinimumWidth(theme.size("sidePanel.minWidth"))
        self.coverage_panel.setMaximumWidth(theme.size("sidePanel.width") + 40)
        self.saved_split.addWidget(self.coverage_panel)
        self.saved_split.setStretchFactor(0, 1)
        self.saved_split.setStretchFactor(1, 0)
        tab_box.addWidget(self.saved_split, 1)
        self.transcript_tab = transcript_tab
        self.workspace_stack.addWidget(transcript_tab)
        self.workspace_tabs.currentChanged.connect(self._tab_changed)
        self.coverage_toggle.toggled.connect(lambda on: self.coverage_panel.setVisible(on))
        self.save_as_button.clicked.connect(lambda: self.save_as())
        self.copy_button.clicked.connect(self.copy_selected)
        self.rename_button.clicked.connect(self.rename_meeting)
        self.edit_button.clicked.connect(self.correct_selected)
        self.revisions_button.clicked.connect(self.show_revisions)
        self.redact_button.clicked.connect(self.redact_selected)
        self.delete_button.clicked.connect(self.delete_open_meeting)
        self.saved_page = page
        self.pages.addWidget(page)
        self._update_toolbar()

    def _tab_changed(self, index: int) -> None:
        if 0 <= index < self.workspace_stack.count():
            self.workspace_stack.setCurrentIndex(index)

    def _update_toolbar(self) -> None:
        items = self.saved_list.selectedItems()
        current = self.saved_list.currentItem()
        segment = current is not None and current.data(Qt.ItemDataRole.UserRole) is not None
        has_meeting = self._open_meeting is not None
        self.copy_button.setEnabled(bool(items) or current is not None)
        self.saved_empty.setVisible(has_meeting and self.saved_list.count() == 0)
        self.edit_button.setEnabled(segment)
        self.redact_button.setEnabled(segment)
        self.revisions_button.setEnabled(segment)
        self.save_as_button.setEnabled(has_meeting)
        self.rename_button.setEnabled(has_meeting and self._pending is None)
        self.delete_button.setEnabled(has_meeting)
        self.selection_label.setText(C.TB_SELECTION.format(n=len(items)) if items else C.TB_HINT)
        self.clear_selection.setVisible(bool(items))

    def open_meeting(self, meeting_id: str, *, focus_segment: str | None = None) -> None:
        if self._pending is not None and self._pending.meeting_id == meeting_id:
            self.pages.setCurrentWidget(self.saved_page)  # still saving: keep the in-memory view
            return
        try:
            snapshot = self.store.snapshot(meeting_id, wait_s=UI_STORE_WAIT_S)
        except StorageError as exc:
            QMessageBox.warning(self, "History busy", f"This meeting cannot be read right now ({exc.code}). "
                                "Try again in a moment.")
            return
        if snapshot is None:
            self.show_history()
            return
        self._render_saved(snapshot, focus_segment=focus_segment)

    def _render_saved(self, snapshot: MeetingSnapshot, *, focus_segment: str | None = None,
                      show: bool = True) -> None:
        row = snapshot.meeting
        meeting_id = row.id
        if self._open_meeting != meeting_id:
            self.saved_notice.setText("")
        self._open_meeting = meeting_id
        self._open_revision = row.transcript_revision
        self._titles[meeting_id] = row.title
        self.pending_banner.setVisible(self._pending is not None and self._pending.meeting_id == meeting_id)
        self.saved_heading.setText(row.title)
        self.saved_heading.setAccessibleName(row.title)
        self.saved_heading.setToolTip(row.title)
        self.saved_heading.setLayoutDirection(Qt.LayoutDirection.RightToLeft if is_rtl(row.title)
                                              else Qt.LayoutDirection.LeftToRight)
        length = _duration(row.started_at, row.ended_at)
        when = local_time(row.started_at)
        if row.ended_at:
            when += f"–{_clock_of(row.ended_at)}"
        if length is not None:
            when += f" ({length[0]})"
        self.saved_title.setText(f"{when} · {STATE_TEXT.get(row.status, row.status)}"
                                 f" · saved revision {row.transcript_revision}")
        timeline = snapshot.timeline
        summary = coverage_summary(row.status, timeline)
        self.saved_coverage.setText(summary)
        losses = sum(1 for t in timeline if t.metadata.get("loss") or (t.kind == "error"
                                                                         and t.code != "event_log_overflow"))
        if summary.startswith("Complete"):
            chip_text, kind = C.CHIP_COMPLETE, "ok"
        elif row.status == "stopped":
            chip_text, kind = C.CHIP_INCOMPLETE.format(n=losses), "warn"
        else:
            chip_text, kind = C.CHIP_INTERRUPTED if row.status in ("interrupted", "stopping") else \
                STATE_TEXT.get(row.status, row.status), "bad"
        for widget in (self.saved_chip, self.coverage_chip):
            set_chip(widget, chip_text, kind)
            widget.setToolTip("Complete means everything captured was processed and saved, with no known loss. It "
                              "does not mean every word was recognized correctly.")
        self.coverage_times.setText(C.COV_TIMES.format(start=_clock_of(row.started_at), stop=_clock_of(row.ended_at))
                                    if row.ended_at else C.COV_TIMES_OPEN.format(start=_clock_of(row.started_at)))
        finalized = next((t for t in reversed(timeline) if t.code == "finalized"), None)
        drained = None if finalized is None else bool(finalized.metadata.get("drain_complete"))
        self.coverage_drain.setText(C.COV_DRAIN[drained])
        # Timeline notes are drawn inside the line rows they precede (one item per line).
        notes = sorted(((e.start_ms, n) for e in timeline if (n := _timeline_event(e)) is not None),
                       key=lambda pair: pair[0])
        self.saved_list.clear()
        focus_row = None
        segments = snapshot.segments
        index = 0
        for i, segment in enumerate(segments):
            before = []
            while index < len(notes) and notes[index][0] <= segment.start_ms:
                before.append(notes[index][1])
                index += 1
            after = [n for _, n in notes[index:]] if i == len(segments) - 1 else []
            item = QListWidgetItem()
            label_text = f"[{clock_text(segment.start_ms)}] {SOURCE_NAMES[segment.source]}"
            if segment.origin == "user":
                label_text += f" · corrected (version {segment.revision}; Show edit history for the original)"
            label_text += quality_note(segment.flags)
            set_line(item, label_text, segment.text, source=segment.source, start_ms=segment.start_ms,
                     edited=segment.origin == "user", flags=segment.flags, events=before, after=after)
            item.setData(Qt.ItemDataRole.UserRole, segment.id)
            self.saved_list.addItem(item)
            if segment.id == focus_segment:
                focus_row = self.saved_list.count() - 1
        session = self.session
        if session is not None and session.meeting_id == meeting_id:
            self._add_unsaved_lines(session)
        self.events_list.clear()
        for event in timeline:
            item = QListWidgetItem(event_line(event))
            note = _timeline_event(event)
            loss = bool(note and note["loss"])
            item.setIcon(theme.icon(note["icon"] if note else "info",
                                    "status.warning.fg" if loss else "text.secondary", 16))
            if loss:
                font = item.font()
                font.setBold(True)
                item.setFont(font)
                item.setForeground(theme.color("status.warning.fg"))
            item.setData(Qt.ItemDataRole.UserRole, event.start_ms)
            self.events_list.addItem(item)
        self.coverage_none.setVisible(not any(_timeline_event(e) for e in timeline))
        if focus_row is not None:
            self._select_row(focus_row, highlight=True)
        self._update_toolbar()
        self._set_workspace_meeting(meeting_id, row.transcript_revision)
        if show:
            self.pages.setCurrentWidget(self.saved_page)
            if focus_row is not None:
                self._show_transcript_tab()
                self.saved_list.setFocus(Qt.FocusReason.OtherFocusReason)

    def _select_row(self, row: int, *, highlight: bool = False) -> None:
        item = self.saved_list.item(row)
        if item is None:
            return
        self.saved_list.setCurrentRow(row)
        self.saved_list.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        if highlight:
            delegate = self.saved_list.itemDelegate()
            delegate.highlight = item.data(SIG_ROLE)
            self.saved_list.viewport().update()
            self._highlight_timer.start(HIGHLIGHT_MS)

    def _clear_highlight(self) -> None:
        self.saved_list.itemDelegate().highlight = None
        self.saved_list.viewport().update()

    def _jump_to_event(self, item: QListWidgetItem) -> None:
        """A coverage timeline entry: select the transcript line at (or after) that time."""
        start = item.data(Qt.ItemDataRole.UserRole)
        if start is None:
            return
        target = None
        for row in range(self.saved_list.count()):
            data = self.saved_list.item(row).data(ROW_ROLE) or {}
            if data.get("kind") == "line" and data.get("t", 0) >= start:
                target = row
                break
        if target is None and self.saved_list.count():
            target = self.saved_list.count() - 1
        if target is not None:
            self._select_row(target, highlight=True)

    def jump_to_segment(self, segment_id: str) -> bool:
        """Show the saved transcript at a segment (e.g. evidence from the minutes view)."""
        for row in range(self.saved_list.count()):
            if self.saved_list.item(row).data(Qt.ItemDataRole.UserRole) == segment_id:
                self._show_transcript_tab()
                self.pages.setCurrentWidget(self.saved_page)
                self._select_row(row, highlight=True)
                self.saved_list.setFocus(Qt.FocusReason.OtherFocusReason)
                return True
        return False

    def _show_transcript_tab(self) -> None:
        if self.workspace_tabs.isVisible():
            self.workspace_tabs.setCurrentIndex(0)

    def _add_unsaved_lines(self, session: MeetingSession) -> None:
        for lost in list(session.unsaved):  # kept in memory only; gone when the app closes
            item = QListWidgetItem()
            text = lost.event.text if lost.event is not None else "(text not kept)"
            set_line(item, f"[{clock_text(lost.start_ms)}] {SOURCE_NAMES[lost.source]} — NOT SAVED ({lost.code}); "
                     "use Save As or copy it now if you need it", text, source=lost.source, start_ms=lost.start_ms,
                     state="unsaved", code=lost.code)
            self.saved_list.addItem(item)

    # ----------------------------------------------------------------- Save As

    def save_as(self) -> bool:
        """Write the open meeting's current saved revision to a file the user picks.
        Returns whether a file was written. Cancelling changes nothing; History keeps
        the meeting either way."""
        meeting_id = self._open_meeting
        if meeting_id is None:
            return False
        session = self.session if self.session is not None and self.session.meeting_id == meeting_id else None
        if session is not None and not session.save_settled.is_set():
            QMessageBox.information(self, "Save As", "The last text is still being saved. Save As becomes "
                                    "available when the saved transcript appears here.")
            return False
        try:
            snapshot = self.store.snapshot(meeting_id, wait_s=UI_STORE_WAIT_S)
        except StorageError as exc:
            QMessageBox.warning(self, "Save As", f"The saved transcript cannot be read right now ({exc.code}). It "
                                "is still in History; try Save As again in a moment.")
            return False
        if snapshot is None:
            return False
        unsaved = [UnsavedLine(u.source, u.start_ms, u.event.text if u.event is not None else None, u.code)
                   for u in (list(session.unsaved) if session is not None else [])]
        folder = self._export_dir or Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation) or Path.home())
        suggested = str(folder / suggest_filename(snapshot.meeting.title, snapshot.meeting.started_at, "txt"))
        while True:
            choice = self._save_dialog(self, suggested)
            if choice is None:
                self.saved_notice.setText(C.SAVEAS_CANCEL, "info")
                return False
            target, fmt = export_target(*choice)
            suggested = str(target)
            if self._is_database_file(target):
                QMessageBox.warning(self, "Save As", "That is the app's own History database. Choose another name.")
                continue
            if target.exists() and not target.is_dir():
                answer = QMessageBox.question(
                    self, C.REPLACE_TITLE.format(name=target.name), C.REPLACE_BODY,
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    continue
            try:
                write_file(target, render(snapshot, fmt, unsaved=unsaved))
            except ExportError as exc:
                QMessageBox.warning(self, C.WRITE_FAIL_TITLE, C.WRITE_FAIL_BODY.format(name=target.name, code=exc.code))
                continue
            self._export_dir = target.parent
            self._feedback(self.saved_notice, f"Saved a copy as “{target.name}” (revision "
                                              f"{snapshot.meeting.transcript_revision}) in {target.parent.name or target.parent}. "
                                              "History is unchanged.")
            return True

    def _is_database_file(self, target: Path) -> bool:
        if self.store.path == ":memory:":
            return False
        db = Path(self.store.path)
        names = {db.name.lower(), f"{db.name}-wal".lower(), f"{db.name}-shm".lower(), f"{db.name}-journal".lower()}
        try:
            return target.parent.resolve() == db.parent.resolve() and target.name.lower() in names
        except OSError:
            return False

    def copy_selected(self) -> None:
        """Copy the selected lines' own text (no labels or layout marks), one per line."""
        items = sorted(self.saved_list.selectedItems(), key=self.saved_list.row)
        if not items and self.saved_list.currentItem() is not None:
            items = [self.saved_list.currentItem()]
        texts = [t for t in (i.data(TEXT_ROLE) for i in items) if t is not None]
        if texts:
            QGuiApplication.clipboard().setText("\n".join(texts))
            self._feedback(self.saved_notice, f"Copied the text of {len(texts)} line(s) (original text, unchanged).")

    def _selected_segment(self) -> str | None:
        item = self.saved_list.currentItem()
        return None if item is None else item.data(Qt.ItemDataRole.UserRole)

    def rename_meeting(self) -> None:
        if self._open_meeting is None:
            return
        try:
            with self.store.held(UI_STORE_WAIT_S):
                row = self.store.meeting(self._open_meeting)
        except StorageError as exc:
            QMessageBox.warning(self, "History busy", f"Try again in a moment ({exc.code}).")
            return
        title, ok = QInputDialog.getText(self, C.RENAME_TITLE, C.RENAME_LABEL, text=row.title if row else "")
        if ok and title.strip():
            if self._guard(lambda: self.store.rename_meeting(self._open_meeting, title.strip())):
                self._refresh_search()
                self.open_meeting(self._open_meeting)
                self._feedback(self.saved_notice, C.TOAST_RENAMED)

    def correct_selected(self) -> None:
        segment_id = self._selected_segment()
        if segment_id is None:
            return
        try:
            with self.store.held(UI_STORE_WAIT_S):
                current = next(s for s in self.store.segments(self._open_meeting) if s.id == segment_id)
                revisions = self.store.revisions(segment_id)
        except StorageError as exc:
            QMessageBox.warning(self, "History busy", f"Try again in a moment ({exc.code}).")
            return
        original = revisions[0].text if revisions else current.text
        text, ok = QInputDialog.getMultiLineText(self, C.CORRECT_TITLE.format(time=clock_text(current.start_ms)),
                                                 C.CORRECT_LABEL.format(original=original), current.text)
        if ok and text.strip() and text.strip() != current.text:
            if self._guard(lambda: self.store.correct_segment(segment_id, text.strip())):
                self._refresh_search()  # search results show current text only
                self.open_meeting(self._open_meeting, focus_segment=segment_id)
                self._feedback(self.saved_notice, C.TOAST_CORRECTED.format(n=self._open_revision))

    def show_revisions(self) -> None:
        segment_id = self._selected_segment()
        if segment_id is None:
            return
        try:
            with self.store.held(UI_STORE_WAIT_S):
                revisions = self.store.revisions(segment_id)
                current = next((s for s in self.store.segments(self._open_meeting) if s.id == segment_id), None)
        except StorageError as exc:
            QMessageBox.warning(self, "History busy", f"Try again in a moment ({exc.code}).")
            return
        when = clock_text(current.start_ms) if current is not None else ""
        dialog = QDialog(self)
        dialog.setWindowTitle(C.HISTORY_TITLE.format(time=when))
        dialog.setMinimumWidth(theme.size("dialog.widthMd"))
        box = QVBoxLayout(dialog)
        box.setContentsMargins(20, 20, 20, 12)
        box.setSpacing(10)
        box.addWidget(label(C.HISTORY_TITLE.format(time=when), role="h2"))
        for i, r in enumerate(revisions):
            card = Card()
            name = C.HISTORY_ORIGINAL if r.origin == "asr" else C.HISTORY_CORRECTION.format(n=i)
            top = QHBoxLayout()
            top.addWidget(label(f"{name} · revision {r.revision} · {local_time(r.edited_at)}", role="strong"), 1)
            if i == len(revisions) - 1:
                top.addWidget(chip(C.HISTORY_CURRENT, "ok"))
            card.box.addLayout(top)
            text = label(r.text, selectable=True)
            text.setLayoutDirection(Qt.LayoutDirection.RightToLeft if is_rtl(r.text) else Qt.LayoutDirection.LeftToRight)
            text.setAlignment(Qt.AlignmentFlag.AlignRight if is_rtl(r.text) else Qt.AlignmentFlag.AlignLeft)
            card.box.addWidget(text)
            box.addWidget(card)
        box.addWidget(label(C.HISTORY_NOTE, role="help"))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        box.addWidget(buttons)
        dialog.exec()

    def redact_selected(self) -> None:
        segment_id = self._selected_segment()
        if segment_id is None:
            return
        answer = QMessageBox.question(self, C.REDACT_TITLE, C.REDACT_BODY + self._minutes_note() + "\n\nRedact this line?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            if self._guard(lambda: self.store.redact_segment(segment_id)):
                self._refresh_search()  # never leave the removed text in visible results
                self.open_meeting(self._open_meeting)
                self._feedback(self.saved_notice, C.TOAST_REDACTED)
            else:
                self._refresh_search()

    def delete_open_meeting(self) -> None:
        if self._open_meeting is not None and self.delete_meeting(self._open_meeting):
            self.show_history()

    def delete_meeting(self, meeting_id: str) -> bool:
        if self.session is not None and self.session.meeting_id == meeting_id and self.session.state not in (
                SessionState.STOPPED, SessionState.INTERRUPTED):
            QMessageBox.warning(self, "Delete meeting", "Stop this meeting before deleting it.")
            return False
        if self.session is not None and self.session.meeting_id == meeting_id and not self.session.save_settled.is_set():
            QMessageBox.warning(self, "Delete meeting", "This meeting is still being saved. Try again when its saved "
                                "transcript appears.")
            return False
        title = self._titles.get(meeting_id, "this meeting")
        answer = QMessageBox.question(self, C.DELETE_TITLE.format(title=title),
                                      C.DELETE_BODY + self._minutes_note() + "\n\nDelete it?",
                                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                      QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return False
        deleted = self._guard(lambda: self.store.delete_meeting(meeting_id))
        self._refresh_search()  # whether or not it worked, show what the store now holds
        if not deleted:
            return False
        if self._open_meeting == meeting_id:
            self._close_open_meeting()
        if self.session is not None and self.session.meeting_id == meeting_id:
            self.session.unsaved.clear()  # its unsaved text was part of the meeting
            self.session = None  # later events from it are ignored
            self._clear_live()
        self._feedback(self.history_notice, C.TOAST_DELETED)
        return True

    def _close_open_meeting(self) -> None:
        self._open_meeting = None
        self._open_revision = None
        self.saved_heading.setText("")
        self.saved_title.setText("")
        self.saved_coverage.setText("")
        self.saved_list.clear()
        self.events_list.clear()
        self._update_toolbar()
        self._set_workspace_meeting(None, None)

    def _guard(self, action: Callable[[], Any]) -> bool:
        try:
            with self.store.held(UI_STORE_WAIT_S):
                action()
            return True
        except MaintenanceError as exc:
            # Committed: the text is gone from the app; only the tidy-up after it failed.
            self._maintenance_pending = True
            QMessageBox.warning(self, "Tidy-up not finished",
                                f"The text was removed from History and search, but the final tidy-up of the "
                                f"search index could not finish ({exc.code}). It is tried again at the next "
                                "automatic cleanup, deletion or redaction.")
            return True
        except StorageError as exc:
            QMessageBox.warning(self, "Not saved", f"The change could not be saved ({exc.code}). Nothing was changed.")
            return False

    # ----------------------------------------------- Minutes / Action items hooks

    def install_minutes_workspace(self, workspace: Any) -> None:
        """Add the Minutes and Action items tabs next to Transcript on the saved page.

        `workspace` provides `minutes_tab` and `actions_tab` (QWidgets),
        `set_meeting(meeting_id: str | None, revision: int | None)`, and the signals
        `evidence_requested(str segment_id)` and `badges_changed(str minutes, str actions)`.
        The window calls `set_meeting` with the open meeting's exact saved revision
        whenever it shows or re-reads it (after corrections, redaction, rename) and with
        (None, None) when no saved revision is shown (deleted, expired, or still saving).
        """
        if self._minutes is not None:
            return
        self._minutes = workspace
        self.workspace_stack.addWidget(workspace.minutes_tab)
        self.workspace_stack.addWidget(workspace.actions_tab)
        self.workspace_tabs.addTab(C.MINUTES_TABS[1])
        self.workspace_tabs.addTab(C.MINUTES_TABS[2])
        self.workspace_tabs.setVisible(True)
        workspace.evidence_requested.connect(self.jump_to_segment)
        workspace.badges_changed.connect(self._set_tab_badges)
        # Optional extras of the workspace: "Open OpenAI settings" and "Review all in Action items".
        if hasattr(workspace, "open_settings_requested"):
            workspace.open_settings_requested.connect(self._open_extra_settings)
        if hasattr(workspace, "show_actions_requested"):
            workspace.show_actions_requested.connect(lambda: self.workspace_tabs.setCurrentIndex(2))
        self._set_workspace_meeting(self._open_meeting if self._open_revision is not None else None,
                                    self._open_revision)

    def _minutes_note(self) -> str:
        """Once minutes are available, the Redact/Delete confirmations say that the
        meeting's generated minutes and action items go too."""
        return ("\n\nGenerated minutes and action items for this meeting are removed too (exported copies are not "
                "changed).") if self._minutes is not None else ""

    def _set_tab_badges(self, minutes: str, actions: str) -> None:
        for index, badge in ((1, minutes), (2, actions)):
            text = C.MINUTES_TABS[index] + (f" · {badge}" if badge else "")
            self.workspace_tabs.setTabText(index, text)
            self.workspace_tabs.setTabToolTip(index, text)

    def _set_workspace_meeting(self, meeting_id: str | None, revision: int | None) -> None:
        if self._minutes is None:
            return
        if meeting_id is None:
            self._show_transcript_tab()
        try:
            self._minutes.set_meeting(meeting_id, revision)
        except Exception as exc:  # noqa: BLE001 - the optional workspace must never break the transcript
            self.saved_notice.setText(f"The minutes view could not be updated ({type(exc).__name__}). The transcript "
                                      "is unaffected.", "warn")

    def install_settings_section(self, widget: QWidget, title: str) -> None:
        """Add a card to Settings (before Privacy), listed in the section index."""
        card = Card(title)
        card.box.addWidget(widget)
        self._extra_settings = card
        index = self._settings_box.indexOf(self.privacy_block)
        self._settings_box.insertWidget(index, card)
        self._add_toc(title, card, before=self.privacy_block)

    def _open_extra_settings(self) -> None:
        self.show_settings()
        card = getattr(self, "_extra_settings", None)
        if card is not None:
            QTimer.singleShot(0, lambda: self._scroll_settings_to(card))

    def set_optional_readiness(self, text: str | None) -> None:
        """An "Optional" line in the New meeting readiness panel (e.g. OpenAI minutes
        status); None hides it. It never affects whether transcription can start."""
        self.optional_label.setText(text or "")
        self.optional_group.setVisible(bool(text))

    # -------------------------------------------------------------- history page

    def _build_history(self) -> None:
        page = QWidget()
        page.setObjectName("Page")
        box = QVBoxLayout(page)
        box.setContentsMargins(24, 16, 24, 16)
        box.setSpacing(10)
        top = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        self.history_heading = heading(C.HIST_H1)
        self.history_sub = label("", role="sub")
        titles.addWidget(self.history_heading)
        titles.addWidget(self.history_sub)
        top.addLayout(titles, 1)
        copy_path = tool_button(C.COPY_HISTORY_PATH, "folder", tip="Copy the folder where History is kept")
        copy_path.clicked.connect(lambda: self.copy_history_path(self.history_notice))
        copy_path.setEnabled(self.store.path != ":memory:")
        top.addWidget(copy_path, 0, Qt.AlignmentFlag.AlignBottom)
        box.addLayout(top)
        search_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(C.HIST_SEARCH_PLACEHOLDER)
        self.search_edit.setMaxLength(500)
        self.search_edit.setClearButtonEnabled(True)
        self._search_action = self.search_edit.addAction(theme.icon("search", "text.secondary", 16),
                                                         QLineEdit.ActionPosition.LeadingPosition)
        self.search_edit.setAccessibleName("Search saved transcripts")
        self.search_edit.setAccessibleDescription(C.HIST_SEARCH_DESCRIPTION)
        self.search_edit.setToolTip("Search History (Ctrl+F)")
        self.search_edit.textChanged.connect(lambda t: self.search_edit.setLayoutDirection(
            Qt.LayoutDirection.RightToLeft if is_rtl(t) else Qt.LayoutDirection.LeftToRight))
        search_button = button(C.HIST_SEARCH, "search")
        self.clear_search_button = button(C.HIST_CLEAR)
        self.clear_search_button.clicked.connect(self.clear_search)
        search_row.addWidget(self.search_edit, 1)
        search_row.addWidget(search_button)
        search_row.addWidget(self.clear_search_button)
        box.addLayout(search_row)
        self.history_notice = Notice()
        self.history_notice.setAccessibleName("History notice")
        box.addWidget(self.history_notice)
        self.history_banner = Banner("info", "", "", inline=True)
        self.history_banner.setVisible(False)
        box.addWidget(self.history_banner)
        self.history_stack = QStackedWidget()
        box.addWidget(self.history_stack, 1)

        meetings = QWidget()
        column = QVBoxLayout(meetings)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.meeting_list = transcript_list("Meetings", "Newest first. Each row: title, date, length and status. "
                                            "Press Enter to open.", object_name="MeetingList")
        column.addWidget(self.meeting_list, 1)
        row = QHBoxLayout()
        open_button = button(C.HIST_OPEN, "file-text")
        delete_button = button(C.HIST_DELETE, "trash", variant="dangerText")
        row.addWidget(open_button)
        row.addWidget(delete_button)
        row.addStretch(1)
        column.addLayout(row)
        self.history_stack.addWidget(meetings)
        self._history_list_panel = meetings

        results = QWidget()
        column = QVBoxLayout(results)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(8)
        self.results_summary = label("", role="sub")
        column.addWidget(self.results_summary)
        self.search_results = transcript_list("Search results", "Current text only; press Enter to open a result "
                                              "at its time", object_name="SearchResults")
        column.addWidget(self.search_results, 1)
        self.history_stack.addWidget(results)
        self._history_results_panel = results
        self.history_empty = EmptyState("history", C.HIST_EMPTY[0], C.HIST_EMPTY[1],
                                        (C.HIST_EMPTY[2], self.show_preflight))
        self.history_stack.addWidget(self.history_empty)

        search_button.clicked.connect(self.run_search)
        self.search_edit.returnPressed.connect(self.run_search)
        self.search_results.itemActivated.connect(self._open_search_hit)
        self.meeting_list.itemActivated.connect(lambda item: self.open_meeting(item.data(Qt.ItemDataRole.UserRole)))
        open_button.clicked.connect(self._open_selected_meeting)
        delete_button.clicked.connect(self._delete_selected_meeting)
        self.history_page = page
        self.pages.addWidget(page)

    def _history_retention_text(self) -> str:
        if self._retention is retention.NOT_CHOSEN:
            return C.HIST_RETENTION["not_chosen"]
        if self._retention is None:
            return C.HIST_RETENTION[None]
        return C.HIST_KEPT.format(days=self._retention)

    def _show_history_panel(self) -> None:
        if self._last_query and self._last_query.strip() or self._history_error:
            self.history_stack.setCurrentWidget(self._history_results_panel)
        elif self.meeting_list.count() == 0:
            self.history_stack.setCurrentWidget(self.history_empty)
        else:
            self.history_stack.setCurrentWidget(self._history_list_panel)
        self.clear_search_button.setVisible(bool(self._last_query))

    def show_history(self) -> None:
        self._history_error = False
        self._refresh_search()
        self.meeting_list.clear()
        self.history_banner.setVisible(False)
        try:
            with self.store.held(UI_STORE_WAIT_S):
                rows = self.store.list_meetings(limit=100)
        except StorageError as exc:
            rows = []
            self._history_error = True
            self.search_results.clear()
            item = QListWidgetItem()
            set_message(item, f"History could not be read ({exc.code}).", title=C.HIST_BUSY[0],
                        body=f"{C.HIST_BUSY[1]} ({exc.code})", tone="info")
            self.search_results.addItem(item)
            self.results_summary.setText("")
            self.history_banner.set_content("info", C.HIST_BUSY[0], C.HIST_BUSY[1], details=f"Storage: {exc.code}")
            self.history_banner.setVisible(True)
        for m in rows:
            self._titles[m.id] = m.title
            length = _duration(m.started_at, m.ended_at)
            status = STATE_TEXT.get(m.status, m.status)
            kind = {"stopped": "ok", "interrupted": "bad", "stopping": "bad"}.get(m.status, "info")
            item = QListWidgetItem()
            set_meeting(item, f"{local_time(m.started_at)}   {m.title}   ·   {status}   ·   {m.segments} segment(s)"
                              f"   ·   {length[0] if length else C.LENGTH_UNKNOWN}",
                        title=m.title, date=local_time(m.started_at), length=length[0] if length else C.LENGTH_UNKNOWN,
                        length_spoken=length[1] if length else C.LENGTH_UNKNOWN_ACCESSIBLE, status=status,
                        status_kind=kind)
            item.setData(Qt.ItemDataRole.UserRole, m.id)
            self.meeting_list.addItem(item)
        self.history_sub.setText(C.HIST_SUB.format(n=len(rows), retention=self._history_retention_text()))
        self._show_history_panel()
        self.pages.setCurrentWidget(self.history_page)

    def focus_search(self) -> None:
        self.show_history()
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    def run_search(self) -> None:
        self._last_query = self.search_edit.text()
        self._refresh_search()
        self._show_history_panel()
        if self._last_query.strip():
            announce(self.results_summary, self.results_summary.text())

    def clear_search(self) -> None:
        self.search_edit.clear()
        self._last_query = None
        self._refresh_search()
        self._show_history_panel()
        self.search_edit.setFocus()

    def _refresh_search(self) -> None:
        """(Re)run the last search against the store, so results never show text that
        was corrected, redacted or deleted since."""
        self.search_results.clear()
        self.results_summary.setText("")
        query = self._last_query
        if not query or not query.strip():
            return
        try:
            with self.store.held(UI_STORE_WAIT_S):
                hits = self.store.search(query, limit=100)
        except QueryError:
            item = QListWidgetItem()
            set_message(item, "Search text is limited to 500 characters.", title=C.HIST_ERROR[0],
                        body="Search text is limited to 500 characters.", tone="bad")
            self.search_results.addItem(item)
            return
        except StorageError as exc:
            item = QListWidgetItem()
            set_message(item, f"Search could not be run ({exc.code}).", title=C.HIST_ERROR[0],
                        body=f"{C.HIST_ERROR[1]} ({exc.code})", tone="bad")
            self.search_results.addItem(item)
            return
        if not hits:
            item = QListWidgetItem()
            set_message(item, "No matches in current transcript text or titles.",
                        title=C.HIST_NONE[0].format(q=query.strip()), body=C.HIST_NONE[1])
            self.search_results.addItem(item)
            self.results_summary.setText(C.HIST_RESULTS.format(n=0, m=0))
            return
        previous = None
        for hit in hits:
            where = (f"[{clock_text(hit.start_ms)}] {SOURCE_NAMES[hit.source]}" if hit.entity_kind == "segment"
                     else "title")
            item = QListWidgetItem()
            segment = hit.entity_kind == "segment"
            set_hit(item, f"{hit.meeting_title} — {where}", hit.text, meeting_title=hit.meeting_title,
                    header=hit.meeting_id != previous, query=query, source=hit.source if segment else None,
                    start_ms=hit.start_ms if segment else None)
            previous = hit.meeting_id
            item.setData(Qt.ItemDataRole.UserRole, (hit.meeting_id, hit.entity_id if segment else None))
            self.search_results.addItem(item)
        self.results_summary.setText(C.HIST_RESULTS.format(n=len(hits), m=len({h.meeting_id for h in hits})))

    def _open_search_hit(self, item: QListWidgetItem) -> None:
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            self.open_meeting(data[0], focus_segment=data[1])

    def _open_selected_meeting(self) -> None:
        item = self.meeting_list.currentItem()
        if item is not None:
            self.open_meeting(item.data(Qt.ItemDataRole.UserRole))

    def _delete_selected_meeting(self) -> None:
        item = self.meeting_list.currentItem()
        if item is not None and self.delete_meeting(item.data(Qt.ItemDataRole.UserRole)):
            self.show_history()

    def copy_history_path(self, notice: Notice | None = None) -> str | None:
        """Put the History folder's path on the clipboard (explicit action only)."""
        if self.store.path == ":memory:":
            return None
        path = str(Path(self.store.path).parent)
        QGuiApplication.clipboard().setText(path)
        self._feedback(notice or self.settings_notice, C.TOAST_PATH)
        return path

    # ---------------------------------------------------------- settings and help

    def _build_settings(self) -> None:
        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(24, 20, 24, 24)
        outer.setSpacing(12)
        self.settings_heading = heading(C.SET_H1)
        outer.addWidget(self.settings_heading)
        self.settings_notice = Notice()
        self.settings_notice.setAccessibleName("Settings notice")
        outer.addWidget(self.settings_notice)
        row = QHBoxLayout()
        row.setSpacing(16)
        self.settings_toc = QListWidget()
        self.settings_toc.setObjectName("Toc")
        self.settings_toc.setAccessibleName("Settings sections")
        self.settings_toc.setFixedWidth(200)
        self.settings_toc.itemActivated.connect(self._toc_jump)
        self.settings_toc.itemClicked.connect(self._toc_jump)
        row.addWidget(self.settings_toc, 0, Qt.AlignmentFlag.AlignTop)
        sections = QVBoxLayout()
        sections.setSpacing(16)
        self._settings_box = sections
        row.addLayout(sections, 1)
        outer.addLayout(row)
        outer.addStretch(1)

        appearance = Card(C.SET_APPEARANCE)
        choices = QHBoxLayout()
        self.theme_group = QButtonGroup(self)
        self.theme_buttons: dict[str, QRadioButton] = {}
        for choice in theme.THEME_CHOICES:
            radio = QRadioButton(theme.THEME_LABELS[choice])
            self.theme_group.addButton(radio)
            self.theme_buttons[choice] = radio
            radio.toggled.connect(lambda on, c=choice: self.set_theme(c) if on and c != self._theme_choice else None)
            choices.addWidget(radio)
        choices.addStretch(1)
        caption = label(C.SET_THEME, role="strong")
        appearance.box.addWidget(caption)
        appearance.box.addLayout(choices)
        appearance.box.addWidget(label(C.SET_APPEARANCE_HELP, role="help"))
        self._sync_theme_buttons()

        keep = Card(C.SET_RETENTION)
        self.retention_current = label("")
        keep.box.addWidget(self.retention_current)
        line = QHBoxLayout()
        self.retention_combo = QComboBox()
        self.retention_combo.setAccessibleName("History retention")
        for _, text in retention.CHOICES:
            self.retention_combo.addItem(text)
        self.retention_apply = button("Save retention choice", variant="primary")
        self.retention_apply.clicked.connect(self.apply_retention)
        line.addWidget(self.retention_combo, 1)
        line.addWidget(self.retention_apply)
        keep.box.addLayout(line)
        self.retention_status = label("")
        self.retention_status.setAccessibleName("Retention status")
        keep.box.addWidget(self.retention_status)
        self.retention_retry = button("Retry now", "refresh", size="compact")
        self.retention_retry.clicked.connect(self.run_retention)
        self.retention_retry.setVisible(False)
        keep.box.addWidget(self.retention_retry, 0, Qt.AlignmentFlag.AlignLeft)
        keep.box.addWidget(label(
            "An expired meeting is deleted from History with its corrections, search entries and coverage notes, "
            "when the app starts and every hour while it runs. A meeting that is running or still being saved is "
            "never deleted. Files you saved with Save As are separate and not affected.", role="help"))
        keep.box.addWidget(label(C.SET_RETENTION_HINT, role="help"))

        model = ReadOnlyBlock(C.SET_MODEL)
        self.settings_model_status = model.add_row("Status", self.model_item.title.text())
        speech_model = _manifest_model_label()
        for name, value in ((n, v.format(model=speech_model)) for n, v in C.MODEL_ROWS):
            model.add_row(name, value)
        check = button(C.CHECK_AGAIN, "refresh", size="compact")
        check.clicked.connect(self.check_model_again)
        model.outer.addWidget(check, 0, Qt.AlignmentFlag.AlignLeft)

        routing = Card(C.SET_ROUTING)
        help_text = QLabel(HELP_TEXT)
        help_text.setWordWrap(True)
        help_text.setTextFormat(Qt.TextFormat.RichText)
        routing.box.addWidget(help_text)
        routing.box.addWidget(label(ROUTING_NOTE, role="help"))
        self.routing_card = routing

        storage = ReadOnlyBlock(C.SET_STORAGE)
        location = "in memory only (test)" if self.store.path == ":memory:" else str(Path(self.store.path))
        self.data_location = storage.add_row("History location", f"History database: {location}")
        for name, value in C.STORAGE_ROWS:
            storage.add_row(name, value)
        storage.add_row("History and Save As", HISTORY_NOTE)
        copy_path = button(C.COPY_PATH, "copy", size="compact")
        copy_path.setAccessibleName("Copy History location")
        copy_path.setEnabled(self.store.path != ":memory:")
        copy_path.clicked.connect(lambda: self.copy_history_path(self.settings_notice))
        storage.outer.addWidget(copy_path, 0, Qt.AlignmentFlag.AlignLeft)

        keys = Card(C.SET_KEYS)
        keys.box.addWidget(label(C.SET_KEYS_MUTE, role="strong"))
        line = QHBoxLayout()
        self.mic_keys_edit = QKeySequenceEdit()
        self.mic_keys_edit.setAccessibleName("Shortcut for mute and unmute mic")
        self.mic_keys_edit.setMaximumSequenceLength(1)
        save_keys = button(C.SET_KEYS_SAVE, variant="primary")
        save_keys.clicked.connect(self.save_mic_shortcut)
        reset_keys = button(C.SET_KEYS_RESET.format(keys=DEFAULT_MIC_SHORTCUT))
        reset_keys.clicked.connect(self.reset_mic_shortcut)
        line.addWidget(self.mic_keys_edit, 1)
        line.addWidget(save_keys)
        line.addWidget(reset_keys)
        keys.box.addLayout(line)
        keys.box.addWidget(label(C.SET_KEYS_HELP, role="help"))
        self.shortcut_status = label("")
        self.shortcut_status.setAccessibleName("Shortcut status")
        keys.box.addWidget(self.shortcut_status)
        table = QGridLayout()
        table.setHorizontalSpacing(16)
        table.setVerticalSpacing(4)
        self.shortcut_keys_label = label("", wrap=False, role="mono")
        table.addWidget(self.shortcut_keys_label, 0, 0)
        table.addWidget(label("Mute / unmute mic (any page, even while typing)"), 0, 1)
        for i, (k, d) in enumerate((*SHORTCUTS, *C.SHORTCUT_EXTRA), start=1):
            table.addWidget(label(k, wrap=False, role="mono"), i, 0)
            table.addWidget(label(d), i, 1)
        table.setColumnStretch(1, 1)
        keys.box.addLayout(table)
        keys.box.addWidget(label("Tab and Shift+Tab move between controls; Space or Enter presses the focused "
                                 "button.", role="help"))

        privacy = ReadOnlyBlock(C.SET_PRIVACY)
        for name, value in C.PRIVACY_ROWS:
            privacy.add_row(name, value)
        self.privacy_block = privacy

        about = Card(C.SET_ABOUT)
        about.box.addWidget(label("Tadween Desk | تدوين — offline transcription on this PC. Icons and design are part of "
                                  "this app; fonts are Windows system fonts.", role="help"))
        self.notices_view = QPlainTextEdit()
        self.notices_view.setReadOnly(True)
        self.notices_view.setAccessibleName("Third-party notices")
        self.notices_view.setMinimumHeight(160)
        self.notices_view.setMaximumHeight(320)
        self.notices_view.setPlainText(self._notices())
        about.box.addWidget(self.notices_view)

        for title, card in ((C.SET_APPEARANCE, appearance), (C.SET_RETENTION, keep), (C.SET_MODEL, model),
                            (C.SET_ROUTING, routing), (C.SET_STORAGE, storage), (C.SET_KEYS, keys),
                            (C.SET_PRIVACY, privacy), (C.SET_ABOUT, about)):
            sections.addWidget(card)
            self._add_toc(title, card)
        self.settings_page = _scroll_page(body, C.SET_H1)
        self._settings_body = body
        self.pages.addWidget(self.settings_page)

    @staticmethod
    def _notices() -> str:
        """Bundled third-party notices, if this build has them (meeting_minutes.notices)."""
        try:
            from meeting_minutes import notices  # type: ignore[attr-defined]

            text = notices.notices_text()
        except Exception:  # noqa: BLE001 - absent module or file: say so instead of failing Settings
            return C.ABOUT_MISSING
        return text if isinstance(text, str) and text.strip() else C.ABOUT_MISSING

    def _add_toc(self, title: str, card: QWidget, before: QWidget | None = None) -> None:
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, card)
        rows = [self.settings_toc.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.settings_toc.count())]
        if before is not None and before in rows:
            self.settings_toc.insertItem(rows.index(before), item)
        else:
            self.settings_toc.addItem(item)
        rows_h = max(28, self.settings_toc.sizeHintForRow(0))
        self.settings_toc.setFixedHeight(rows_h * self.settings_toc.count() + 8)

    def _toc_jump(self, item: QListWidgetItem) -> None:
        card = item.data(Qt.ItemDataRole.UserRole)
        if card is not None:
            self._scroll_settings_to(card)

    def _scroll_settings_to(self, card: QWidget) -> None:
        y = card.mapTo(self._settings_body, card.rect().topLeft()).y()
        self.settings_page.verticalScrollBar().setValue(max(0, y - 12))

    def show_settings(self) -> None:
        self.pages.setCurrentWidget(self.settings_page)

    def choose_retention(self) -> None:
        self.show_settings()
        self._scroll_settings_to(self.retention_combo.parentWidget())
        self.retention_combo.setFocus()

    def _load_settings(self) -> None:
        """The stored retention choice and mute shortcut; unreadable values fall back to
        "not chosen" (nothing expires) and the default shortcut."""
        try:
            with self.store.held(UI_STORE_WAIT_S):
                policy = self.store.get_setting(retention.SETTING_KEY)
                keys = self.store.get_setting(MIC_SHORTCUT_KEY)
                self._maintenance_pending = self.store.maintenance_pending()  # retried by run_retention
        except StorageError as exc:
            policy, keys = None, None
            self.retention_status.setText(f"Settings could not be read ({exc.code}); nothing is deleted "
                                          "automatically until they can be.")
        self._apply_mic_keys(keys if isinstance(keys, str) and _mic_keys_problem(keys) is None
                             else DEFAULT_MIC_SHORTCUT)
        self._show_retention(policy)

    def _show_retention(self, value: Any) -> None:
        self._retention = retention.parse(value)
        chosen = self._retention is not retention.NOT_CHOSEN
        self.retention_prompt.setVisible(not chosen)
        days = [d for d, _ in retention.CHOICES]
        self.retention_combo.setCurrentIndex(days.index(self._retention if chosen else retention.PROPOSED_DAYS))
        if chosen:
            self.retention_current.setText(f"Current choice: {retention.label(self._retention)} (chosen "
                                           f"{local_time(str(value.get('chosen_at', '')))}).")
        else:
            self.retention_current.setText("Not chosen yet: nothing is deleted automatically until you choose. "
                                           f"{retention.PROPOSED_DAYS} days is proposed.")

    def _busy_meetings(self) -> set[str]:
        """Meetings retention must never touch: the current one and one still saving."""
        return {s.meeting_id for s in (self.session, self._pending) if s is not None}

    def apply_retention(self) -> None:
        days = retention.CHOICES[self.retention_combo.currentIndex()][0]
        now = self._clock()
        if days is not None:
            try:
                with self.store.held(UI_STORE_WAIT_S):
                    expired = self.store.expired_meetings(retention.cutoff(days, now),
                                                          exclude=self._busy_meetings(), limit=MAX_PAGE)
            except StorageError as exc:
                QMessageBox.warning(self, "History busy", f"Try again in a moment ({exc.code}). Nothing was changed.")
                return
            if expired:
                count = str(len(expired)) if len(expired) < MAX_PAGE else f"{MAX_PAGE} or more"
                answer = QMessageBox.question(
                    self, f"Apply {days}-day retention?",
                    f"{count} saved meeting(s) ended more than {days} days ago. With this choice they are deleted "
                    "from History now, with their corrections and search entries. This cannot be undone. Files "
                    "you saved with Save As are not affected. Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    self.retention_status.setText("The retention choice was not changed.")
                    return
        value = retention.to_setting(days, now)
        if not self._guard(lambda: self.store.set_setting(retention.SETTING_KEY, value)):
            return
        self._show_retention(value)
        self.retention_status.setText(f"Saved: {retention.label(days)}.")
        announce(self.retention_status, self.retention_status.text())
        self.run_retention()

    def run_retention(self) -> list[str]:
        """Delete meetings past the chosen retention, a bounded batch at a time. Does
        nothing until the user has chosen a policy, or with "keep until I delete".
        Also retries index maintenance that failed after an earlier deletion."""
        self._retry_maintenance()
        days = self._retention
        if days is retention.NOT_CHOSEN or days is None:
            return []
        problem = None
        try:
            with self.store.held(UI_STORE_WAIT_S):
                deleted = self.store.purge_expired(retention.cutoff(days, self._clock()),
                                                   exclude=self._busy_meetings(), limit=RETENTION_BATCH)
        except MaintenanceError as exc:
            deleted, problem = list(exc.deleted), exc.code  # committed: these meetings are gone
            self._maintenance_pending = True
        except StorageError as exc:  # rolled back: nothing was deleted
            self.retention_status.setText(f"Automatic cleanup could not run ({exc.code}); it deleted nothing and "
                                          "will be tried again later.")
            theme.set_props(self.retention_status, role="danger")
            self.retention_retry.setVisible(True)
            return []
        theme.set_props(self.retention_status, role="")
        self.retention_retry.setVisible(self._maintenance_pending)
        if deleted:
            self._forget_meetings(deleted)
            message = (f"Automatic cleanup deleted {len(deleted)} meeting(s) that ended more than {days} days ago "
                       f"({datetime.now().strftime('%Y-%m-%d %H:%M')}).")
            if problem is not None:
                message += (f" The final tidy-up of the search index could not finish ({problem}); it is tried "
                            "again at the next cleanup.")
            self.retention_status.setText(message)
            if len(deleted) == RETENTION_BATCH:
                QTimer.singleShot(1000, self.run_retention)  # the rest, without holding the window
        return deleted

    def _retry_maintenance(self) -> None:
        if not self._maintenance_pending:
            return
        try:
            with self.store.held(UI_STORE_WAIT_S):
                self.store.scrub()
        except StorageError:
            return  # still not possible (or busy); stays pending for the next run
        self._maintenance_pending = False

    def _forget_meetings(self, meeting_ids: Sequence[str]) -> None:
        """Clear every view of meetings that no longer exist."""
        if self._open_meeting in meeting_ids:
            self._close_open_meeting()
            self.saved_notice.setText("This meeting was deleted by automatic cleanup.", "info")
        if self.pages.currentWidget() in (self.history_page, self.saved_page):
            self.show_history()
        else:
            self._refresh_search()

    def save_mic_shortcut(self) -> None:
        keys = self.mic_keys_edit.keySequence().toString(QKeySequence.SequenceFormat.PortableText)
        problem = _mic_keys_problem(keys)
        if problem is not None:
            self.shortcut_status.setText(problem)
            theme.set_props(self.shortcut_status, role="danger")
            theme.set_props(self.mic_keys_edit, invalid=True)
            announce(self.shortcut_status, problem)
            return
        if self._guard(lambda: self.store.set_setting(MIC_SHORTCUT_KEY, keys)):
            self._apply_mic_keys(keys)
            theme.set_props(self.shortcut_status, role="ok")
            theme.set_props(self.mic_keys_edit, invalid=False)
            self.shortcut_status.setText(f"Saved: {self._mic_keys} mutes and unmutes the mic.")
            announce(self.shortcut_status, self.shortcut_status.text())
            self._refresh_live_status()

    def reset_mic_shortcut(self) -> None:
        self.mic_keys_edit.setKeySequence(QKeySequence(DEFAULT_MIC_SHORTCUT))
        self.save_mic_shortcut()

    def _apply_mic_keys(self, keys: str) -> None:
        sequence = QKeySequence(keys)
        self.mic_shortcut.setKey(sequence)
        self.mic_keys_edit.setKeySequence(sequence)
        self._mic_keys = sequence.toString(QKeySequence.SequenceFormat.NativeText)
        self.shortcut_keys_label.setText(self._mic_keys)
        self.start_muted_help.setText(C.START_MUTED_HELP.format(keys=self._mic_keys))

    # ------------------------------------------------------------- size classes

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self._apply_size_class()
        if self.toast.isVisible():
            self.toast.show_message(self.toast.text())

    def size_class(self) -> str:
        return self._size[0] if self._size else WIDE

    def _apply_size_class(self) -> None:
        if not hasattr(self, "settings_page"):
            return
        width, height = self.width(), self.height()
        cls = WIDE if width >= 1280 else MEDIUM if width >= 960 else NARROW if width >= 760 else XNARROW
        short = height < SHORT_HEIGHT
        if (cls, short) == self._size:
            return
        first = self._size is None
        self._size = (cls, short)
        wide = cls == WIDE
        self.centralWidget().setProperty("sizeClass", cls)
        self._set_nav_compact(not wide)
        self.nav_footer.setVisible(not short)
        self._bar_layout.setDirection(QBoxLayout.Direction.LeftToRight if cls in (WIDE, MEDIUM)
                                      else QBoxLayout.Direction.TopToBottom)
        self._bar_layout.setContentsMargins(16, 4 if short else 8, 16, 4 if short else 8)
        self.bar_title.setVisible(cls in (WIDE, MEDIUM))
        for b in (self.mic_button, self.pause_button, self.stop_button, self.show_live_button):
            theme.set_props(b, size="compact" if short else "large")
        icon_only = cls == XNARROW
        for b in self._toolbar_buttons:
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly if icon_only
                                 else Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.live_side_toggle.setToolButtonStyle(self.copy_button.toolButtonStyle())
        self.selection_label.setVisible(cls in (WIDE, MEDIUM))
        self.follow_latest.setText(C.FOLLOW[1] if icon_only else C.FOLLOW[0])
        # Side panels: docked when wide; otherwise opened from the page's toggle button.
        for panel, toggle in ((self.live_side, self.live_side_toggle), (self.coverage_panel, self.coverage_toggle)):
            toggle.setVisible(not wide)
            if wide:
                panel.setVisible(True)
            elif first or toggle.isChecked() is False:
                panel.setVisible(False)
                toggle.setChecked(False)
        self._preflight_grid.setDirection(QBoxLayout.Direction.LeftToRight if wide
                                          else QBoxLayout.Direction.TopToBottom)
        self.readiness_card.setMaximumWidth(340 if wide else 16777215)
        self.settings_toc.setVisible(wide)
        margin = 24 if cls in (WIDE, MEDIUM) else 16
        for body in (self._preflight_body, self._settings_body):
            body.layout().setContentsMargins(margin, 12 if short else 20, margin, 16)
        self.history_page.layout().setContentsMargins(margin, 8 if short else 16, margin, 12)
        for widget in (self.preflight_sub, self.live_sub, self.history_sub):
            widget.setVisible(not short)
        for widget in (self.preflight_heading, self.live_title, self.history_heading, self.settings_heading,
                       self.saved_heading):
            theme.set_props(widget, short=short)
        theme.repolish(self.centralWidget())
        self._refresh_live_status()

    # ------------------------------------------------------------------ closing

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt API
        """Close and save: the window stays open (and usable) while the meeting is
        finalized and the user chooses a file; it closes once a file is written.
        Cancelling the file choice keeps the window open on the saved transcript."""
        session = self.session
        if session is not None and not self._force_close:
            if session.state in (SessionState.CAPTURING, SessionState.DEGRADED, SessionState.PAUSED):
                answer = QMessageBox.question(
                    self, C.CLOSE_ACTIVE_TITLE, C.CLOSE_ACTIVE_BODY.format(s=int(self._stop_budget_s)),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel)
                event.ignore()
                if answer == QMessageBox.StandardButton.Yes:
                    self._close_requested = True
                    self._offer_for = session.meeting_id
                    session.stop()
                    self._refresh_live_status()
                return
            if session.state is SessionState.FINALIZING:
                event.ignore()
                self._close_requested = True
                self._offer_for = session.meeting_id
                self.live_health.setText(C.CLOSE_FINISHING, "info")
                return
            if self._close_requested and not session.save_settled.is_set():
                event.ignore()  # waiting for the save, then Save As (see _ask_close_while_saving)
                return
            if not session.save_settled.is_set():
                answer = QMessageBox.question(
                    self, "Saving has not finished",
                    "The last meeting is still being saved. Close anyway? Text not saved yet may be lost.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
        self._timer.stop()
        self._pending_timer.stop()
        event.accept()


def _mic_keys_problem(keys: str) -> str | None:
    """Why a mute shortcut cannot be used, or None. It works in every page, including
    while typing, so it needs Ctrl or Alt and must not clash with another shortcut."""
    if not keys:
        return "Press a key combination first."
    if "," in keys.replace("Ctrl+,", ""):
        return "Use one key combination."
    if not keys.startswith(("Ctrl+", "Alt+")) and "+Ctrl+" not in keys and "+Alt+" not in keys:
        return "Use a combination with Ctrl or Alt, so typing a title or a search cannot mute the mic."
    taken = next((d for k, d in SHORTCUTS if QKeySequence(k) == QKeySequence(keys)), None)
    if taken is not None or QKeySequence(keys).matches(QKeySequence(QKeySequence.StandardKey.Copy)) \
            == QKeySequence.SequenceMatch.ExactMatch:
        return f"That combination is already used ({taken or 'Copy'}). Choose another."
    return None


# --------------------------------------------------------------------- entry point


def install_minutes(window: MainWindow, service: Any = None) -> Any:
    """Optional OpenAI minutes: the Minutes and Action items tabs, the Settings
    card and the "Optional" readiness line. Nothing is sent until the user presses
    Generate; transcription never depends on it. Returns the workspace."""
    from meeting_minutes.minutes.service import MinutesService
    from meeting_minutes.ui.minutes_view import MinutesWorkspace, readiness_text
    from meeting_minutes.ui.openai_settings import OpenAISettingsSection

    service = service if service is not None else MinutesService.default(window.store)
    workspace = MinutesWorkspace(service, window, save_dialog=window._save_dialog)
    window.install_minutes_workspace(workspace)
    section = OpenAISettingsSection(service, window)
    window.install_settings_section(section, "OpenAI minutes (optional)")

    def refresh() -> None:
        window.set_optional_readiness(readiness_text(service))

    section.status_changed.connect(refresh)
    refresh()
    window.minutes_service = service
    return workspace


def self_check(timeout_s: float = 180.0) -> int:
    """`app --check`: build the window offscreen with synthetic devices and a throwaway
    data folder (never History), wait until the default speech engine (automatic) is
    ready, close, and confirm the local speech server stopped. Prints one content-free
    JSON line; exit code 0 only if all of that held. No audio device is opened."""
    import json
    import shutil
    import tempfile

    from meeting_minutes.cli import make_backend

    app = QApplication.instance() or QApplication(sys.argv[:1])
    folder = Path(tempfile.mkdtemp(prefix="meeting-minutes-check-"))
    started = time.monotonic()
    report: dict[str, Any] = {"ready": False, "engine": None}
    server = None
    try:
        store = TranscriptStore(folder / "meetings.db")
        window = MainWindow(store, lambda: make_backend("fake"))
        window.show()

        def poll() -> None:
            done = "mixed" in window._engines or "mixed" in window._engine_errors
            if done or time.monotonic() - started > timeout_s:
                app.quit()
            else:
                QTimer.singleShot(200, poll)

        QTimer.singleShot(200, poll)
        app.exec()
        engine = window._engines.get("mixed")
        server = getattr(engine.recognizer, "server", None) if engine is not None else None
        report.update(ready=engine is not None, engine=engine.info.engine_id if engine else None,
                      model_hash=engine.info.model_hash if engine else None, error=window._engine_errors.get("mixed"),
                      ready_after_s=round(time.monotonic() - started, 1), server_load_s=getattr(server, "load_s", None),
                      server_tied_to_app=getattr(server, "tied_to_app", None), model_label=window.model_label.text(),
                      theme=theme.current())
        window._force_close = True
        window.close()
        store.close(wait_s=2.0)
    finally:
        close_local_engines()
        report["server_stopped"] = server is None or not server.alive()
        shutil.rmtree(folder, ignore_errors=True)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ready"] and report["server_stopped"] else 1


def run(argv: Sequence[str] | None = None, *, backend: str = "wasapi", data_dir: Path | None = None) -> int:
    from meeting_minutes.cli import make_backend

    if QApplication.instance() is None:
        # Exact 125 % / 175 % scaling instead of rounding to whole factors.
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication.instance() or QApplication(list(argv or sys.argv[:1]))
    app.setApplicationName("Tadween Desk")
    folder = data_dir or default_data_dir()
    try:
        store = TranscriptStore(folder / "meetings.db")
        recovered = store.recover_unfinished()
    except StorageError as exc:
        QMessageBox.critical(None, APP_TITLE, f"The transcript database could not be opened ({exc.code}).")
        return 6
    window = MainWindow(store, lambda: make_backend(backend), recovered=recovered)
    install_minutes(window)
    app.setWindowIcon(window.windowIcon())

    def unexpected(kind: type[BaseException], exc: BaseException, tb: Any) -> None:
        # The launcher has no console: say something visible, without the message
        # text (which could include content), and keep the app running.
        QMessageBox.warning(window, APP_TITLE, f"Unexpected error ({kind.__name__}). Saved transcripts are kept; "
                            "if something looks wrong, stop the meeting and restart the app.")

    sys.excepthook = unexpected
    window.show()
    try:
        code = app.exec()
    finally:
        close_local_engines()
    # A save stalled on the disk may still hold the store: do not wait for it. SQLite
    # rolls back its unfinished transaction the next time the database opens.
    store.close(wait_s=2.0)
    return code
