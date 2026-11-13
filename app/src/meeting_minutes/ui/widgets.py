"""Small Quiet Desk building blocks: labels and buttons with style properties, chips,
banners with a "Technical details" disclosure, cards, read-only blocks, source cards
with meters, readiness rows, a toast and page notices. Presentation only: they hold no
meeting state of their own."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAccessible, QAccessibleEvent, QWheelEvent
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from meeting_minutes.ui import copy as C
from meeting_minutes.ui import theme

TONE_ICON = {"ok": ("check-circle", "status.success.fg"), "info": ("info", "status.info.fg"),
             "warn": ("warning", "status.warning.fg"), "bad": ("error", "status.danger.fg")}


def label(text: str = "", *, wrap: bool = True, selectable: bool = False, **props: Any) -> QLabel:
    widget = QLabel(text)
    widget.setWordWrap(wrap)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    for key, value in props.items():
        widget.setProperty(key, value)
    if selectable:
        widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                       | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    return widget


def heading(text: str, role: str = "h1") -> QLabel:
    """A page or card heading; focusable (Tab skips it) so navigation shortcuts can move
    keyboard focus to the top of the page."""
    widget = label(text, role=role)
    widget.setAccessibleName(text)
    widget.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
    return widget


def button(text: str, icon: str | None = None, *, variant: str | None = None, size: str | None = None,
           tip: str = "", accessible: str = "") -> QPushButton:
    widget = QPushButton(text)
    if variant:
        widget.setProperty("variant", variant)
    if size:
        widget.setProperty("size", size)
    if icon:
        role = {"primary": "text.onAccent", "danger": "text.onDanger",
                "dangerText": "status.danger.fg"}.get(variant or "", "text.primary")
        theme.set_icon(widget, icon, role, 20 if size in ("large", "cta") else 16)
    if tip:
        widget.setToolTip(tip)
    widget.setAccessibleName(accessible or text)
    return widget


def tool_button(text: str, icon: str | None = None, *, tip: str = "", variant: str | None = None,
                checkable: bool = False) -> QToolButton:
    widget = QToolButton()
    widget.setText(text)
    widget.setProperty("toolbar", True)
    if variant:
        widget.setProperty("variant", variant)
    widget.setCheckable(checkable)
    widget.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
    widget.setAccessibleName(text)
    widget.setToolTip(tip or text)
    if icon:
        theme.set_icon(widget, icon, "status.danger.fg" if variant == "dangerText" else "text.primary", 16)
    return widget


def link_button(text: str, action: Callable[[], Any]) -> QToolButton:
    widget = QToolButton()
    widget.setText(text)
    widget.setProperty("link", True)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAccessibleName(text)
    widget.clicked.connect(lambda: action())
    return widget


class ComboBox(QComboBox):
    """A drop-down the mouse wheel cannot change. Scrolling a page with the pointer over
    it scrolls the page; the choice changes only by opening the list, or from the keyboard.
    The open list still scrolls with the wheel."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)  # the wheel does not move focus here either

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override
        event.ignore()  # left for the scrolling page behind


class DateEdit(QDateEdit):
    """A date field the mouse wheel cannot change, for the same reason as `ComboBox`."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override
        event.ignore()


def chip(text: str = "", kind: str = "neutral") -> QLabel:
    widget = label(text, wrap=False, chip=kind)
    widget.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return widget


def set_chip(widget: QLabel, text: str, kind: str) -> None:
    widget.setText(text)
    widget.setAccessibleName(text)
    theme.set_props(widget, chip=kind)


def icon_label(name: str, role: str = "text.secondary", px: int = 16) -> QLabel:
    widget = QLabel()
    widget.setFixedSize(QSize(px, px))
    theme.set_icon(widget, name, role, px)
    return widget


def announce(widget: QWidget, text: str, *, alert: bool = False) -> None:
    """Tell assistive technology about a change once (polite name change, or an alert)."""
    widget.setAccessibleDescription(text)
    try:
        event = QAccessibleEvent(widget, QAccessible.Event.Alert if alert else QAccessible.Event.DescriptionChanged)
        QAccessible.updateAccessibility(event)
    except Exception:  # noqa: BLE001 - accessibility bridge unavailable (e.g. offscreen); nothing to tell
        pass


class Card(QFrame):
    def __init__(self, title: str = "", parent: QWidget | None = None, *, spacing: int = 12) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(16, 16, 16, 16)
        self.box.setSpacing(spacing)
        self.title_row = QHBoxLayout()
        self.title_label = None
        if title:
            self.title_label = heading(title, "h2")
            self.title_row.addWidget(self.title_label, 1)
            self.box.addLayout(self.title_row)
            self.setAccessibleName(title)


class ReadOnlyBlock(QFrame):
    """Sunken label/value grid with a "Read-only" tag; values are selectable text."""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(heading(title, "h2"))
        top.addWidget(chip(C.READ_ONLY, "readonly"))
        top.addStretch(1)
        outer.addLayout(top)
        self.block = QFrame()
        self.block.setProperty("readonly", True)
        self.form = QFormLayout(self.block)
        self.form.setContentsMargins(12, 12, 12, 12)
        self.form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        outer.addWidget(self.block)
        self.outer = outer
        self.setAccessibleName(title)

    def add_row(self, name: str, value: str = "") -> QLabel:
        key = label(name, role="sub")
        shown = label(value, selectable=True)
        shown.setAccessibleName(name)
        self.form.addRow(key, shown)
        return shown


class TechDetails(QWidget):
    """"Technical details" disclosure: codes only, never transcript text."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)
        self.toggle = QToolButton()
        self.toggle.setText(C.DETAILS)
        self.toggle.setProperty("details", True)
        self.toggle.setCheckable(True)
        self.toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setAccessibleName(C.DETAILS)
        self.text = label("", selectable=True, role="mono")
        self.text.setVisible(False)
        box.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignLeft)
        box.addWidget(self.text)
        self.toggle.toggled.connect(self._toggled)

    def _toggled(self, on: bool) -> None:
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)
        self.text.setVisible(on)

    def set_text(self, text: str | None) -> None:
        self.text.setText(text or "")
        self.setVisible(bool(text))


class Banner(QFrame):
    """Icon · title · body · actions · Technical details · (dismiss). Tone ok/info/warn/bad."""

    def __init__(self, tone: str = "info", title: str = "", body: str = "", *, inline: bool = False,
                 dismissable: bool = False, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("banner", tone)
        if inline:
            self.setProperty("inline", True)
        row = QHBoxLayout(self)
        row.setContentsMargins(16, 10, 12, 10)
        row.setSpacing(12)
        self.icon = QLabel()
        self.icon.setFixedSize(20, 20)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop)
        column = QVBoxLayout()
        column.setSpacing(4)
        self.title = label("", role="strong")
        self.body = label("")
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addStretch(1)
        self.details = TechDetails()
        self.details.setVisible(False)
        column.addWidget(self.title)
        column.addWidget(self.body)
        column.addLayout(self.actions)
        column.addWidget(self.details)
        row.addLayout(column, 1)
        self.dismiss = None
        if dismissable:
            self.dismiss = QToolButton()
            self.dismiss.setProperty("dismiss", True)
            theme.set_icon(self.dismiss, "close", "text.secondary", 16)
            self.dismiss.setAccessibleName(C.DISMISS)
            self.dismiss.setToolTip(C.DISMISS)
            self.dismiss.clicked.connect(self.hide)
            row.addWidget(self.dismiss, 0, Qt.AlignmentFlag.AlignTop)
        self._buttons: list[QWidget] = []
        self.set_content(tone, title, body)

    def set_content(self, tone: str, title: str, body: str = "", details: str | None = None) -> None:
        changed = (tone, title, body) != (self.property("banner"), self.title.text(), self.body.text())
        theme.set_props(self, banner=tone)
        name, role = TONE_ICON.get(tone, TONE_ICON["info"])
        theme.set_icon(self.icon, name, role, 20)
        self.title.setText(title)
        self.title.setVisible(bool(title))
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.details.set_text(details)
        self.setAccessibleName(title or body)
        if changed and self.isVisible():
            announce(self, body or title, alert=tone == "bad")

    def set_actions(self, actions: Sequence[tuple[str, Callable[[], Any]]]) -> list[QPushButton]:
        for widget in self._buttons:
            self.actions.removeWidget(widget)
            widget.deleteLater()
        self._buttons = []
        for i, (text, action) in enumerate(actions):
            b = button(text, size="compact")
            b.clicked.connect(lambda _=False, act=action: act())
            self.actions.insertWidget(i, b)
            self._buttons.append(b)
        return list(self._buttons)

    def showEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        if self.property("banner") == "bad":
            announce(self, self.body.text() or self.title.text(), alert=True)


class AlertArea(QWidget):
    """Meeting-level banners above every page; each condition (key) appears once."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(0)
        self.banners: dict[str, Banner] = {}
        self._dismissed: set[str] = set()
        self._signature: dict[str, tuple] = {}
        self.setVisible(False)

    def show_alert(self, key: str, tone: str, title: str, body: str = "", *,
                   actions: Sequence[tuple[str, Callable[[], Any]]] = (), details: str | None = None,
                   dismissable: bool = False) -> Banner | None:
        if key in self._dismissed:
            return None
        banner = self.banners.get(key)
        if banner is None:
            banner = Banner(tone, title, body, dismissable=dismissable)
            if banner.dismiss is not None:
                banner.dismiss.clicked.connect(lambda _=False, k=key: self._dismiss(k))
            self.banners[key] = banner
            self.box.addWidget(banner)
        signature = (tone, title, body, details, tuple(a[0] for a in actions))
        if self._signature.get(key) != signature:
            self._signature[key] = signature
            banner.set_content(tone, title, body, details)
            banner.set_actions(actions)
        banner.setVisible(True)
        self.setVisible(True)
        return banner

    def _dismiss(self, key: str) -> None:
        self._dismissed.add(key)
        self.hide_alert(key)

    def hide_alert(self, key: str) -> None:
        banner = self.banners.pop(key, None)
        self._signature.pop(key, None)
        if banner is not None:
            self.box.removeWidget(banner)
            banner.deleteLater()
        self.setVisible(bool(self.banners))

    def clear(self, *, prefix: str = "") -> None:
        for key in [k for k in self.banners if k.startswith(prefix)]:
            self.hide_alert(key)
        self._dismissed = {k for k in self._dismissed if not k.startswith(prefix)}

    def keys(self) -> list[str]:
        return list(self.banners)


class Notice(Banner):
    """A persistent inline page notice: the last result of an action stays
    readable here after any toast has gone. Offers QLabel-like text()/setText()."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("info", inline=True, dismissable=True, parent=parent)
        self.setVisible(False)
        self.dismiss.clicked.disconnect()
        self.dismiss.clicked.connect(lambda: self.setText(""))

    def text(self) -> str:
        return self.body.text()

    def setText(self, text: str, tone: str = "info") -> None:  # noqa: N802 - QLabel-like
        self.set_content(tone, "", text)
        self.setVisible(bool(text))
        if text:
            announce(self, text)


class Toast(QLabel):
    """Brief confirmation at the bottom of the window; always mirrored in a page notice."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Toast")
        self.setWordWrap(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str) -> None:
        parent = self.parentWidget()
        self.setText(text)
        width = min(560, max(200, parent.width() - 48))
        self.setFixedWidth(width)
        self.adjustSize()
        self.move((parent.width() - width) // 2, parent.height() - self.height() - 24)
        self.raise_()
        self.show()
        self._timer.start(3200)


class Meter(QProgressBar):
    def __init__(self, name: str) -> None:
        super().__init__()
        self.setRange(0, 100)
        self.setTextVisible(False)
        self.setProperty("meter", True)
        self.setAccessibleName(f"{name} signal level")
        self.setFixedHeight(6)

    def set_level(self, dbfs: float | None, *, excluded: bool = False) -> None:
        """dBFS −60…0 → 0…100; None shows an empty meter (nothing is being read)."""
        value = 0 if dbfs is None else int(max(0.0, min(1.0, (dbfs + 60.0) / 60.0)) * 100)
        theme.set_props(self, meter="excluded" if excluded else True)
        self.setValue(value)


class SourceCard(QFrame):
    """Mic / System audio card for the live side panel."""

    def __init__(self, source: str, reconnect: QPushButton) -> None:
        super().__init__()
        self.setProperty("card", True)
        self.setAccessibleName(C.SOURCE_HEADING[source])
        box = QVBoxLayout(self)
        box.setContentsMargins(12, 12, 12, 12)
        box.setSpacing(6)
        top = QHBoxLayout()
        top.addWidget(icon_label("mic" if source == "mic" else "speaker", "text.primary", 18))
        name = label(C.SOURCE_HEADING[source], role="strong", wrap=False)
        top.addWidget(name, 1)
        self.chip = chip("", "neutral")
        top.addWidget(self.chip)
        box.addLayout(top)
        self.device = label("", role="help")
        box.addWidget(self.device)
        self.meter_caption = label(C.METER_LEVEL, role="help")
        self.meter = Meter(C.SOURCE_HEADING[source])
        box.addWidget(self.meter_caption)
        box.addWidget(self.meter)
        # What the System card covers: a whole output device, or one application.
        self.note = label(C.SYSTEM_NOTE if source == "system" else "", role="help")
        self.note.setVisible(source == "system")
        box.addWidget(self.note)
        box.addWidget(reconnect, 0, Qt.AlignmentFlag.AlignLeft)


class ReadinessItem(QWidget):
    """Icon + title + help (+ action) row in the "Ready to start" panel."""

    def __init__(self, title: str = "", help_text: str = "", *, help_label: QLabel | None = None) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        self.icon = QLabel()
        self.icon.setFixedSize(18, 18)
        row.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        column = QVBoxLayout()
        column.setSpacing(2)
        self.title = label(title, role="strong")
        self.help = help_label if help_label is not None else label(help_text, role="help")
        self.help.setProperty("role", "help")
        self.help.setWordWrap(True)
        for widget in (self.title, self.help):
            widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        column.addWidget(self.title)
        column.addWidget(self.help)
        self.actions = QHBoxLayout()
        column.addLayout(self.actions)
        row.addLayout(column, 1)

    def set_state(self, kind: str, title: str, help_text: str | None = None) -> None:
        name, role = {"ok": ("check-circle", "status.success.fg"), "busy": ("clock", "status.info.fg"),
                      "bad": ("error", "status.danger.fg"), "warn": ("warning", "status.warning.fg"),
                      "neutral": ("info", "text.secondary")}[kind]
        theme.set_icon(self.icon, name, role, 18)
        self.title.setText(title)
        self.setAccessibleName(title)
        if help_text is not None:
            self.help.setText(help_text)
            self.help.setVisible(bool(help_text))


class EmptyState(QWidget):
    def __init__(self, icon: str, title: str, body: str, action: tuple[str, Callable[[], Any]] | None = None) -> None:
        super().__init__()
        box = QVBoxLayout(self)
        box.setContentsMargins(16, 16, 16, 16)
        box.setSpacing(8)
        # Short windows clip the illustration rather than pushing the meeting bar away.
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Ignored)
        box.addStretch(1)
        mark = icon_label(icon, "text.secondary", 40)
        box.addWidget(mark, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = label(title, role="h2")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.body = label(body, role="sub")
        self.body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        box.addWidget(self.title)
        box.addWidget(self.body)
        self.button = None
        if action is not None:
            self.button = button(action[0], variant="primary")
            self.button.clicked.connect(lambda: action[1]())
            box.addWidget(self.button, 0, Qt.AlignmentFlag.AlignHCenter)
        box.addStretch(2)
