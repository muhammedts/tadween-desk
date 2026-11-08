"""The Quiet Desk interface in the real window, offscreen: theme tokens and runtime
switching, size classes, the meeting bar at 683x336, accessible names, painted
transcript rows, the explicit one-source start, follow latest, truthful meters, Copy
History location, lengths from real timestamps, and the integration points for the
optional Minutes/Action items workspace.

Not covered (needs a person on a real PC): how it looks with Windows' own fonts and
scaling, Narrator, real devices/models and the native Save As dialog."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import time  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import QObject, QPoint, QRect, Qt, Signal  # noqa: E402
from PySide6.QtGui import QGuiApplication, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QKeySequenceEdit,
    QLabel,
    QMessageBox,
    QScrollBar,
    QStyleOptionViewItem,
    QWidget,
)
from test_desktop_app import MIC, SYSTEM, fake_engine, lines, say, wait_until, window_for  # noqa: E402

from meeting_minutes.capture.fake import RealtimeFakeBackend  # noqa: E402
from meeting_minutes.domain.contracts import Source  # noqa: E402
from meeting_minutes.storage.store import TranscriptStore  # noqa: E402
from meeting_minutes.ui import copy as C  # noqa: E402
from meeting_minutes.ui import theme  # noqa: E402
from meeting_minutes.ui.desktop import MainWindow, _duration  # noqa: E402
from meeting_minutes.ui.transcript import ROW_ROLE, TEXT_ROLE  # noqa: E402

NOW = datetime.now(timezone.utc)
ARABIC = "الميزانية تمت الموافقة عليها يوم الخميس"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds")


def seed(path: Path, meeting_id: str, *, minutes: int | None = 42, texts=("budget review", ARABIC),
         flags=None) -> None:
    store = TranscriptStore(path)
    started = NOW - timedelta(hours=3)
    store.create_meeting(meeting_id, title=f"Meeting {meeting_id}", timezone_name="UTC", started_at=iso(started))
    for source in ("mic", "system"):
        store.save_epoch(meeting_id, source=source, epoch=1, device_id="d", device_label="d", native_rate=48_000,
                         native_channels=1, start_ms=0, end_ms=None)
    with store.held():
        for i, text in enumerate(texts):
            store.add_final(meeting_id, source="system" if i % 2 else "mic", epoch=1, start_ms=i * 4000,
                            end_ms=i * 4000 + 3000, text=text, language=None, engine_id="e", model_hash="h",
                            flags=flags)
        ended = iso(started + timedelta(minutes=minutes)) if minutes is not None else None
        store._conn.execute("UPDATE meetings SET status = ?, ended_at = ? WHERE id = ?",
                            ("stopped" if minutes is not None else "interrupted", ended, meeting_id))
    store.add_timeline(meeting_id, "gap", "mic_excluded", 1000, 2000, "mic", {"loss": False})
    store.close()


def process(n: int = 10) -> None:
    for _ in range(n):
        QApplication.processEvents()


# ------------------------------------------------------------------------ theme


def test_the_style_sheet_resolves_every_token_and_refuses_unknown_placeholders() -> None:
    for name in ("light", "dark"):
        qss = theme.render_qss(name)
        assert "{c:" not in qss and "{z:" not in qss and theme.tokens()["color"][name]["accent.bg"] in qss
    with pytest.raises(ValueError):
        theme.render_qss("light", "QWidget { color: {c:no.such.token}; }")
    with pytest.raises(ValueError):
        theme.render_qss("light", "QWidget { color: {c:broken")
    assert theme.render_qss("light", "/* {c:no.such.token} */ QWidget { min-height: {z:control.height}px; }") \
        .strip() == "QWidget { min-height: 32px; }"
    assert all((theme.ICON_DIR / f"{n}.svg").is_file() for n in ("mic", "mic-off", "stop", "minutes", "warning"))


def test_theme_choice_applies_at_once_and_is_remembered(app, tmp_path: Path) -> None:
    window, store = window_for(tmp_path)
    assert window._theme_choice == "system" and window.theme_buttons["system"].isChecked()
    before = window.stop_button.icon().cacheKey()
    window.theme_buttons["dark"].click()
    assert theme.current() == "dark" and app.property("mm_theme") == "dark"
    assert store.get_setting("theme") == "dark" and "Theme: Dark" in window.settings_notice.text()
    assert app.palette().window().color() == theme.color("bg.canvas", "dark")
    assert window.stop_button.icon().cacheKey() != before  # icons re-rendered for the new theme
    window.close()
    store.close()
    window, store = window_for(tmp_path)  # restart: the choice is kept
    assert window._theme_choice == "dark" and theme.current() == "dark"
    window.set_theme("light")
    assert theme.current() == "light" and store.get_setting("theme") == "light"
    window.set_theme("system", save=True)
    window.close()


# --------------------------------------------------------- shell and size classes


def test_size_classes_rearrange_the_shell(app, tmp_path: Path) -> None:
    seed(tmp_path / "meetings.db", "m")
    window, _ = window_for(tmp_path)
    window.open_meeting("m")
    window.resize(1366, 768)
    process()
    assert window.size_class() == "wide" and window.nav_rail.width() == theme.size("nav.expanded")
    assert window.coverage_panel.isVisible() and not window.coverage_toggle.isVisible()
    window.resize(1000, 700)
    process()
    assert window.size_class() == "medium" and window.nav_rail.width() == theme.size("nav.compact")
    assert window.nav_history.text() == "History" and window.nav_settings.text() == "Settings"
    assert not window.coverage_panel.isVisible() and window.coverage_toggle.isVisible()
    window.coverage_toggle.click()
    assert window.coverage_panel.isVisible()
    window.resize(700, 600)
    process()
    assert window.size_class() == "xnarrow"
    assert window.copy_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
    assert window.copy_button.accessibleName() == "Copy"  # icon-only buttons keep their names
    assert window.nav_settings.accessibleName() == "Settings and help"
    window.resize(1366, 768)
    process()
    assert window.copy_button.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonTextBesideIcon
    window.close()


def test_mute_and_stop_stay_visible_at_683_by_336(app, tmp_path: Path) -> None:
    window, store = window_for(tmp_path)
    window.start_button.click()
    window.resize(683, 336)
    wait_until(lambda: window.size_class() == "xnarrow")
    process(20)
    assert (window.width(), window.height()) == (683, 336)
    area = window.rect()
    for b in (window.mic_button, window.stop_button, window.pause_button):
        assert b.isVisible()
        rect = QRect(b.mapTo(window, QPoint(0, 0)), b.size())
        assert area.contains(rect), (b.text(), rect)
    assert window.mic_button.text() == "Mute" and window.mic_button.accessibleName() == "Mute mic"
    assert window.stop_button.text() == "Stop" and window.meeting_bar.height() <= 110  # two rows at most
    window.mic_button.click()
    assert not window.session.mic_included and window.mic_button.text() == "Unmute"
    assert "excluded" in window.mic_button.accessibleDescription()
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


def _focusable_without_name(root: QWidget) -> list[str]:
    missing = []
    for w in root.findChildren(QWidget):
        if w.focusPolicy() == Qt.FocusPolicy.NoFocus or isinstance(w, QScrollBar):
            continue
        parent = w.parentWidget()
        owner = parent.parentWidget() if parent is not None else None
        if isinstance(parent, (QComboBox, QKeySequenceEdit, QAbstractSpinBox)) or isinstance(owner, QComboBox) \
                or w.objectName().startswith("qt_"):
            continue  # internal parts of a named control (e.g. a combo box's drop-down list)
        if isinstance(w, QAbstractButton) and w.text().strip():
            continue
        if isinstance(w, QLabel):
            continue
        if not w.accessibleName().strip():
            missing.append(f"{type(w).__name__} {w.objectName()!r} in {type(parent).__name__}")
    return missing


def test_every_focusable_control_without_text_has_an_accessible_name(app, tmp_path: Path) -> None:
    seed(tmp_path / "meetings.db", "m")
    window, _ = window_for(tmp_path)
    window.open_meeting("m")
    window.start_button.click()
    process()
    assert _focusable_without_name(window) == []
    assert window.meeting_bar.accessibleName() == "Current meeting controls"
    assert window.live_list.accessibleName() == "Live transcript"
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


def test_navigation_shortcut_moves_focus_to_the_page_heading(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    window.activateWindow()
    window._go(window.show_history, window.history_heading)
    assert window.pages.currentWidget() is window.history_page and window.history_heading.hasFocus()
    assert window.nav_history.isChecked() and not window.nav_live.isVisible()
    window.close()


# ------------------------------------------------------------- transcript rows


def test_rows_paint_at_three_widths_with_quality_marks_and_events(app, tmp_path: Path) -> None:
    seed(tmp_path / "meetings.db", "m", texts=("budget review", ARABIC, "next steps"), flags=["low_confidence"])
    window, _ = window_for(tmp_path)
    window.open_meeting("m")
    items = [window.saved_list.item(i) for i in range(window.saved_list.count())]
    assert [i.data(TEXT_ROLE) for i in items] == ["budget review", ARABIC, "next steps"]
    assert all("uncertain recognition" in i.text() for i in items)  # the mark stays in the plain text
    assert all("uncertain recognition" in i.data(ROW_ROLE)["marks"] for i in items)
    assert items[1].data(ROW_ROLE)["events"]  # the exclusion note is drawn above the line after it
    assert "Mic excluded (muted in this app)" in items[1].data(Qt.ItemDataRole.AccessibleTextRole)
    delegate = window.saved_list.itemDelegate()
    for width in (420, 900, 1600):
        window.resize(width + 400, 700)
        window.saved_list.setFixedWidth(width)
        process()
        image = QImage(width, 400, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        for row, item in enumerate(items):
            option = QStyleOptionViewItem()
            option.rect = QRect(0, 0, width, 120)
            index = window.saved_list.model().index(row, 0)
            hint = delegate.sizeHint(option, index)
            assert hint.height() >= 40
            option.rect = QRect(0, 0, width, hint.height())
            delegate.paint(painter, option, index)
        painter.end()
    window.saved_list.setMinimumWidth(0)
    window.saved_list.setMaximumWidth(16777215)
    window.close()


def test_copy_still_takes_only_the_original_text(app, tmp_path: Path) -> None:
    seed(tmp_path / "meetings.db", "m", flags=["low_confidence"])
    window, _ = window_for(tmp_path)
    window.open_meeting("m")
    window.saved_list.selectAll()
    window.copy_selected()
    assert QGuiApplication.clipboard().text() == f"budget review\n{ARABIC}"
    assert "Copied the text of 2 line(s)" in window.saved_notice.text()
    window.close()


# ----------------------------------------------------------- one-source start


def failing_system_backend():
    backend = RealtimeFakeBackend()
    backend.fail_open.add(SYSTEM)
    return backend


@pytest.mark.parametrize("answer", ["continue", "cancel"])
def test_a_source_that_cannot_open_asks_before_starting_with_one(app, tmp_path: Path, monkeypatch, answer) -> None:
    store = TranscriptStore(tmp_path / "meetings.db")
    window = MainWindow(store, failing_system_backend, fake_engine, save_dialog=lambda *_: None)
    window.show()
    wait_until(lambda: "ready" in window.model_label.text())
    window.mic_combo.setCurrentIndex(window.mic_combo.findData(MIC))
    window.system_combo.setCurrentIndex(window.system_combo.findData(SYSTEM))
    asked: list[tuple] = []
    monkeypatch.setattr(window, "_ask_one_source", lambda *a: asked.append(a) or answer)
    assert not window.allow_degraded.isChecked()
    window.start_button.click()
    assert asked and asked[0][0] == "system"
    if answer == "cancel":
        assert window.session is None and store.list_meetings() == []
        assert "Could not start capture" in window.start_error.text()
        window.close()
        return
    assert window.session is not None and window.session.state.value == "degraded"
    assert [h.source for h in window.session_backend.opened if not h.closed] == [Source.MIC]
    wait_until(lambda: any(k.startswith("m:degraded:") for k in window.alerts.keys()))
    banner = window.alerts.banners["m:degraded:system"]
    assert banner.title.text() == C.ALERT_DEGRADED["mic"][0]
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


# ------------------------------------------------------ follow latest, meters


def test_follow_latest_keeps_the_reading_position_when_scrolled_up(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    window.start_button.click()
    meeting = window.session.meeting_id

    def final(i: int) -> None:
        window._on_session_event("final", {"meeting": meeting, "id": f"s{i}", "source": "system",
                                           "start_ms": i * 1000, "end_ms": i * 1000 + 900,
                                           "text": f"line {i} of a long meeting", "hypothesis_id": f"h:{i}"})

    for i in range(60):
        final(i)
    process()
    bar = window.live_list.verticalScrollBar()
    assert bar.maximum() > 0 and bar.value() == bar.maximum()  # follows while at the end
    bar.setValue(0)
    final(60)
    process()
    assert bar.value() == 0 and window.jump_latest.isVisible()  # reading position kept
    window.jump_latest.click()
    process()
    assert bar.value() == bar.maximum() and not window.jump_latest.isVisible()
    window.follow_latest.setChecked(False)
    final(61)
    process()
    assert bar.value() < bar.maximum() and window.jump_latest.isVisible()
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


def test_follow_latest_follows_rows_that_grow_in_place(app, tmp_path: Path) -> None:
    """A draft that grows and wraps, and a draft replaced by a longer final line,
    stay in view while following at the end; a reader who scrolled up keeps the place."""
    window, _ = window_for(tmp_path)
    window.start_button.click()
    meeting = window.session.meeting_id
    long_text = "Synthetic evolving transcript with English and العربية that wraps over several lines. " * 6

    def event(kind: str, i: int, text: str) -> None:
        window._on_session_event(kind, {"meeting": meeting, "id": f"system:{i}", "hypothesis_id": f"system:{i}",
                                        "source": "system", "start_ms": i * 1000, "end_ms": i * 1000 + 900,
                                        "text": text})
        process()

    for i in range(45):
        event("final", i, f"line {i} of a long meeting")
    bar = window.live_list.verticalScrollBar()
    assert bar.value() == bar.maximum() > 0
    event("partial", 50, "Short draft")
    event("partial", 50, long_text)  # grows in place
    assert bar.value() == bar.maximum() and window.follow_latest.isChecked()
    event("partial", 51, "Another short draft")
    grown = bar.maximum()
    event("final", 51, long_text + long_text)  # draft replaced by a longer final line
    assert bar.maximum() > grown and bar.value() == bar.maximum()
    event("partial", 52, "Reading earlier now")
    bar.setValue(0)
    event("partial", 52, long_text)
    event("final", 52, long_text + long_text)
    assert bar.value() == 0 and window.follow_latest.isChecked()  # manual reading position kept
    window.jump_latest.click()
    process()
    assert bar.value() == bar.maximum()
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


def test_an_excluded_mic_is_shown_truthfully_and_never_read_for_a_meter(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    window.start_muted.setChecked(True)
    window.start_button.click()
    say(window, Source.SYSTEM, [(0.3, "hello")])
    wait_until(lambda: any(line.endswith("\nhello") for line in lines(window.live_list)))
    window._refresh_live_status()
    card = window.source_cards["mic"]
    assert card.chip.text() == "Excluded" and card.meter.value() == 0 and card.meter.property("meter") == "excluded"
    assert card.meter_caption.text() == C.METER_EXCLUDED
    assert not [h for h in window.session_backend.opened if h.source is Source.MIC and not h.closed]
    assert window.source_cards["system"].chip.text() == "Transcribing"
    events = [window.live_list.item(i).data(ROW_ROLE) for i in range(window.live_list.count())]
    assert any(e and e.get("kind") == "event" and e["label"] == C.EVENT_LABEL["mic_excluded"] for e in events)
    assert window.mic_banner.text() == "Mic excluded" and window.windowTitle().startswith("Tadween Desk — ")
    window.stop_meeting()
    wait_until(lambda: window.pages.currentWidget() is window.saved_page)
    window.close()


# ----------------------------------------------------- History path, length


def test_copy_history_location_puts_the_folder_on_the_clipboard(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    assert window.copy_history_path() == str(tmp_path)
    assert QGuiApplication.clipboard().text() == str(tmp_path)
    assert window.settings_notice.text() == C.TOAST_PATH
    window.close()


def test_history_lengths_come_only_from_recorded_start_and_stop_times(app, tmp_path: Path) -> None:
    seed(tmp_path / "meetings.db", "a", minutes=125)
    seed(tmp_path / "meetings.db", "b", minutes=None)  # recovered: no stop time was recorded
    window, _ = window_for(tmp_path)
    window.show_history()
    rows = {window.meeting_list.item(i).data(Qt.ItemDataRole.UserRole): window.meeting_list.item(i)
            for i in range(window.meeting_list.count())}
    assert rows["a"].data(ROW_ROLE)["length"] == "2 h 05 min"
    assert rows["b"].data(ROW_ROLE)["length"] == C.LENGTH_UNKNOWN
    assert C.LENGTH_UNKNOWN_ACCESSIBLE in rows["b"].data(Qt.ItemDataRole.AccessibleTextRole)
    assert _duration(iso(NOW), iso(NOW + timedelta(seconds=20))) == ("< 1 min", "less than a minute")
    assert _duration(iso(NOW), None) is None and _duration(iso(NOW), iso(NOW - timedelta(minutes=1))) is None
    window.open_meeting("a")
    assert "(2 h 05 min)" in window.saved_title.text()
    window.close()


def test_search_hits_are_grouped_by_meeting_and_no_results_explain_why(app, tmp_path: Path) -> None:
    seed(tmp_path / "meetings.db", "a", texts=("budget review", "budget again"))
    window, _ = window_for(tmp_path)
    window.show_history()
    window.search_edit.setText("budget")
    window.run_search()
    rows = [window.search_results.item(i).data(ROW_ROLE) for i in range(window.search_results.count())]
    assert [r["header"] for r in rows] == [True, False]
    assert "2 match(es) in 1 meeting(s)" in window.results_summary.text()
    assert window.history_stack.currentWidget() is window._history_results_panel
    window.search_edit.setText("nothing-like-this")
    window.run_search()
    assert lines(window.search_results) == ["No matches in current transcript text or titles."]
    assert window.search_results.item(0).data(ROW_ROLE)["title"] == "No matches for “nothing-like-this”"
    window.clear_search()
    assert window.history_stack.currentWidget() is window._history_list_panel
    window.close()


# ---------------------------------------------------- language and readiness


def test_forced_language_choices_warn_that_other_speech_may_be_invented(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    assert window.language_combo.currentData() == "mixed" and window.language_help.property("role") == "help"
    window.language_combo.setCurrentIndex(window.language_combo.findData("en"))
    assert "may be invented" in window.language_help.text() and window.language_help.property("role") == "warn"
    assert "Use Automatic for meetings with Arabic" in window.language_help.text()
    window.close()


def test_readiness_follows_the_start_guards(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    assert window.model_item.title.text() == C.MODEL_TITLE["ready"]
    assert window.mic_item.title.text() == C.READY_MIC["selected"] and not window.start_button.property("blocked")
    window.start_muted.setChecked(True)
    assert window.mic_item.title.text() == C.READY_MIC["excluded"]
    window.mic_combo.setCurrentIndex(0)
    window.system_combo.setCurrentIndex(0)
    assert window.start_button.property("blocked") and window.start_hint.text() == C.START_WHY_DEVICES
    window.start_button.click()  # still focusable and clickable: it says why
    assert window.session is None and "Select a microphone" in window.start_error.text()
    window.close()


# --------------------------------------------- Minutes / Action items integration


class FakeWorkspace(QObject):
    evidence_requested = Signal(str)
    badges_changed = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self.minutes_tab = QLabel("minutes")
        self.actions_tab = QLabel("actions")
        self.calls: list[tuple] = []

    def set_meeting(self, meeting_id, revision) -> None:
        self.calls.append((meeting_id, revision))


def test_the_minutes_workspace_follows_the_open_meeting_and_its_revision(app, tmp_path: Path, monkeypatch) -> None:
    from PySide6.QtWidgets import QInputDialog

    seed(tmp_path / "meetings.db", "m")
    window, store = window_for(tmp_path)
    assert not window.workspace_tabs.isVisible()  # no placeholder tabs before installation
    workspace = FakeWorkspace()
    window.install_minutes_workspace(workspace)
    assert [window.workspace_tabs.tabText(i) for i in range(3)] == ["Transcript", "Minutes", "Action items"]
    window.open_meeting("m")
    revision = store.meeting("m").transcript_revision
    assert workspace.calls[-1] == ("m", revision)
    window.saved_list.setCurrentRow(0)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("budget agreed", True))
    window.correct_selected()
    assert workspace.calls[-1] == ("m", revision + 1)
    window.workspace_tabs.setCurrentIndex(1)
    assert window.workspace_stack.currentWidget() is workspace.minutes_tab
    segment = window.saved_list.item(1).data(Qt.ItemDataRole.UserRole)
    workspace.evidence_requested.emit(segment)
    assert window.workspace_tabs.currentIndex() == 0 and window._selected_segment() == segment
    workspace.badges_changed.emit("Draft", "3")
    assert window.workspace_tabs.tabText(1) == "Minutes · Draft" and window.workspace_tabs.tabText(2) == \
        "Action items · 3"
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    window.delete_open_meeting()
    assert workspace.calls[-1] == (None, None)
    window.close()


def test_settings_and_readiness_extension_points(app, tmp_path: Path) -> None:
    window, _ = window_for(tmp_path)
    toc = [window.settings_toc.item(i).text() for i in range(window.settings_toc.count())]
    assert toc[-2:] == [C.SET_PRIVACY, C.SET_ABOUT] and window.notices_view.toPlainText().strip()
    window.install_settings_section(QLabel("extra"), "OpenAI minutes")
    toc = [window.settings_toc.item(i).text() for i in range(window.settings_toc.count())]
    assert toc.index("OpenAI minutes") == toc.index(C.SET_PRIVACY) - 1
    assert not window.optional_group.isVisibleTo(window)
    window.set_optional_readiness("OpenAI minutes: not configured")
    assert window.optional_group.isVisibleTo(window) and "not configured" in window.optional_label.text()
    window.set_optional_readiness(None)
    assert not window.optional_group.isVisibleTo(window)
    window.close()


def test_the_window_stays_quick_to_build(app, tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "meetings.db")
    started = time.perf_counter()
    window = MainWindow(store, RealtimeFakeBackend, fake_engine)
    assert time.perf_counter() - started < 5.0
    window.close()
