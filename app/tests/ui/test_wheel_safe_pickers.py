"""Scrolling a page must not change a drop-down or a date under the pointer (offscreen).
The wheel goes to the page behind; the keyboard and the open list still change the value."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest  # noqa: E402
from PySide6.QtCore import QDate, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QComboBox,
    QDateEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from speech_fakes import EnergyVad, ToneRecognizer  # noqa: E402

from meeting_minutes.capture.fake import DEFAULT_FAKE_DEVICES, FAKE_APP_DEVICES, RealtimeFakeBackend  # noqa: E402
from meeting_minutes.session.meeting import EngineInfo  # noqa: E402
from meeting_minutes.speech import qwen  # noqa: E402,F401 - imported before the network guard patches socket
from meeting_minutes.storage.store import TranscriptStore  # noqa: E402
from meeting_minutes.ui.desktop import Engine, MainWindow  # noqa: E402
from meeting_minutes.ui.minutes_view import ActionEditDialog, ItemEditDialog  # noqa: E402
from meeting_minutes.ui.widgets import ComboBox, DateEdit  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wheel(widget: QWidget, notches: int = -1) -> None:
    """One wheel step over the middle of `widget`, sent through the window system the way
    a real mouse sends it, so Qt offers it to the widget and then to what is behind it."""
    QApplication.processEvents()
    top = widget.window()
    spot = QPointF(widget.mapTo(top, widget.rect().center()))
    QTest.wheelEvent(top.windowHandle(), spot, QPoint(0, 120 * notches))
    QApplication.processEvents()


def page_with(*fields: QWidget) -> QScrollArea:
    body = QWidget()
    box = QVBoxLayout(body)
    for field in fields:
        box.addWidget(field)
    box.addSpacing(3000)  # taller than the window, so the page can scroll
    page = QScrollArea()
    page.setWidget(body)
    page.resize(400, 300)
    page.show()
    QApplication.processEvents()
    return page


def choices(combo: QComboBox) -> QComboBox:
    combo.addItems(["first", "second", "third"])
    combo.setCurrentIndex(1)
    return combo


def test_plain_qt_pickers_do_change_under_the_wheel(app):
    """The behaviour being removed: without the fix the same wheel step changes both."""
    combo, date = choices(QComboBox()), QDateEdit(QDate(2026, 11, 12))
    page = page_with(combo, date)
    wheel(combo)
    wheel(date)
    assert combo.currentIndex() != 1
    assert date.date() != QDate(2026, 11, 12)
    page.close()


def test_the_wheel_scrolls_the_page_and_leaves_the_picker_alone(app):
    combo, date = choices(ComboBox()), DateEdit()
    date.setDate(QDate(2026, 11, 12))
    page = page_with(combo, date)
    bar = page.verticalScrollBar()
    page.setFocus()  # a new window gives its first field the focus
    for field in (combo, date):
        bar.setValue(0)  # both fields in view again
        wheel(field)
        assert bar.value() > 0  # the page moved
        assert not field.hasFocus()  # and the field did not take focus
    assert combo.currentIndex() == 1
    assert date.date() == QDate(2026, 11, 12)
    page.close()


def test_the_keyboard_and_the_open_list_still_choose(app):
    combo = choices(ComboBox())
    page = page_with(combo)
    combo.setFocus()
    QTest.keyClick(combo, Qt.Key.Key_Down)
    assert combo.currentIndex() == 2
    combo.showPopup()
    QApplication.processEvents()
    assert combo.view().isVisible()  # the list opens and scrolls on its own
    combo.hidePopup()
    page.close()


def test_every_picker_in_the_app_is_wheel_safe(app, tmp_path):
    """No screen builds a plain Qt drop-down or date field any more."""
    store = TranscriptStore(tmp_path / "meetings.db")
    window = MainWindow(store, lambda: RealtimeFakeBackend(devices=DEFAULT_FAKE_DEVICES + FAKE_APP_DEVICES),
                        lambda mode: Engine(ToneRecognizer(), EnergyVad, EngineInfo("fake-tone-engine", "0" * 64), mode))
    try:
        found = window.findChildren(QComboBox)
        assert len(found) >= 6  # New meeting has five, Settings at least one
        assert all(isinstance(combo, ComboBox) for combo in found)
        assert window.findChildren(QDateEdit) == []
        window.show()
        QApplication.processEvents()
        window.sound_mode.setCurrentIndex(window.sound_mode.findData("none"))
        wheel(window.sound_mode, notches=1)
        wheel(window.sound_mode, notches=-1)
        assert window.sound_mode.currentData() == "none"
    finally:
        window.close()
        store.close()
    for dialog in (ItemEditDialog(None, "decision"), ActionEditDialog(None)):
        assert dialog.findChildren(QComboBox) + dialog.findChildren(QDateEdit)
        assert all(isinstance(combo, ComboBox) for combo in dialog.findChildren(QComboBox))
        assert all(isinstance(date, DateEdit) for date in dialog.findChildren(QDateEdit))
        dialog.close()
