"""Transcript, search-hit and meeting rows painted for the Quiet Desk design on plain
QListWidget lists.

Each item keeps a plain item text ("[mm:ss] Source · marks\\ntext"), its own text in
TEXT_ROLE and its direction in RTL_ROLE, so copy, search, tests and the export see the
plain content rather than the painted row. The look comes from a row description in
ROW_ROLE and is drawn by `RowDelegate`: a fixed LTR time column, a Mic/System chip column and the
text laid out in its own paragraph direction. Display-only marks (LRM) exist only in
the style option used for drawing, never in item data or the clipboard.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from PySide6.QtCore import QModelIndex, QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPen, QTextCharFormat, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from meeting_minutes.ui import copy as C
from meeting_minutes.ui import theme
from meeting_minutes.ui.wording import QUALITY_TEXT, SOURCE_NAMES, clock_text, is_rtl

TEXT_ROLE = Qt.ItemDataRole.UserRole + 1  # a line's own text, without the time/source label
RTL_ROLE = Qt.ItemDataRole.UserRole + 2  # whether that text starts right-to-left (Arabic)
ROW_ROLE = Qt.ItemDataRole.UserRole + 10  # the row description the delegate draws
SIG_ROLE = Qt.ItemDataRole.UserRole + 11  # cache key for the row's height
LRM = "‎"
LINE_SEPARATOR = " "  # what Qt shows a line break in item text as
_ACCESSIBLE = Qt.ItemDataRole.AccessibleTextRole
_SELECTABLE = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
_STATIC = Qt.ItemFlag.ItemIsEnabled


# ------------------------------------------------------------------- item setup


def _store(item: QListWidgetItem, row: dict[str, Any], accessible: str) -> None:
    item.setData(ROW_ROLE, row)
    item.setData(SIG_ROLE, f"{item.text()}\x1f{sorted(row.items())!r}")
    item.setData(_ACCESSIBLE, accessible)


def set_line(item: QListWidgetItem, label: str, text: str, *, source: str, start_ms: int, state: str = "final",
             code: str | None = None, edited: bool = False, flags: Sequence[str] = (),
             events: Sequence[dict[str, Any]] = (), after: Sequence[dict[str, Any]] = ()) -> None:
    """A transcript line: plain label above its text (item text), drawn as a row.
    `state` is final, draft or unsaved; `events`/`after` are timeline notes (pauses,
    exclusions, gaps, redactions) drawn above/below it inside the same row, so the
    list keeps one item per line."""
    item.setText(f"{label}\n{text}" if label else text)
    item.setData(TEXT_ROLE, text)
    item.setData(RTL_ROLE, is_rtl(text))
    marks = [QUALITY_TEXT[f] for f in flags if f in QUALITY_TEXT]
    row = {"kind": "line", "t": start_ms, "source": source, "state": state, "code": code, "edited": edited,
           "marks": tuple(marks), "events": tuple(tuple(sorted(e.items())) for e in events),
           "after": tuple(tuple(sorted(e.items())) for e in after)}
    status = {"draft": "Draft, not saved yet", "unsaved": f"NOT SAVED ({code})" if code else "NOT SAVED"}.get(
        state, "Saved")
    spoken = [f"{event_accessible(e)}." for e in events]
    spoken.append(f"{clock_text(start_ms)}, {SOURCE_NAMES[source]}, {status}. {text}")
    if edited:
        spoken.append(C.ROW_EDITED + ".")
    if marks:
        spoken.append(", ".join(marks) + ".")
    spoken.extend(f"{event_accessible(e)}." for e in after)
    _store(item, row, " ".join(spoken))
    item.setFlags(_STATIC if state == "draft" else _SELECTABLE)


def event_accessible(event: dict[str, Any]) -> str:
    when = clock_text(event["t"]) + (f"–{clock_text(event['end'])}" if event.get("end") is not None else "")
    tag = _tag(event)
    return f"{when}. {event['label']}" + (f". {tag}" if tag else "")


def _tag(event: dict[str, Any]) -> str:
    """The chip after an event: "Not a loss" for intentional notes unless the event names
    its own (an empty tag means none)."""
    if event.get("tag") is not None:
        return event["tag"]
    return "" if event.get("loss") else C.NOT_A_LOSS


def set_event(item: QListWidgetItem, text: str, *, start_ms: int, end_ms: int | None, label: str, loss: bool,
              icon: str = "gap", tag: str | None = None) -> None:
    """A note between lines (gap, exclusion, pause); `text` is the plain item text."""
    item.setText(text)
    row = {"kind": "event", "t": start_ms, "end": end_ms, "label": label, "loss": loss, "icon": icon, "tag": tag}
    _store(item, row, event_accessible(row))
    item.setFlags(_STATIC)


def set_message(item: QListWidgetItem, text: str, *, title: str, body: str = "", tone: str = "neutral") -> None:
    """A list's own state (no results, busy, error). The item keeps its plain `text`."""
    item.setText(text)
    _store(item, {"kind": "message", "title": title, "body": body, "tone": tone}, f"{title}. {body}".strip())
    item.setFlags(_STATIC)


def set_hit(item: QListWidgetItem, label: str, text: str, *, meeting_title: str, header: bool, query: str,
            source: str | None, start_ms: int | None) -> None:
    item.setText(f"{label}\n{text}" if label else text)
    item.setData(TEXT_ROLE, text)
    item.setData(RTL_ROLE, is_rtl(text))
    row = {"kind": "hit", "title": meeting_title, "header": header, "query": query, "source": source,
           "t": start_ms}
    where = C.HIST_TITLE_MATCH if start_ms is None else f"{clock_text(start_ms)}, {SOURCE_NAMES.get(source, '')}"
    _store(item, row, f"{meeting_title}. {where}. {text}")
    item.setFlags(_SELECTABLE)


def set_meeting(item: QListWidgetItem, text: str, *, title: str, date: str, length: str, length_spoken: str,
                status: str, status_kind: str) -> None:
    item.setText(text)
    row = {"kind": "meeting", "title": title, "date": date, "length": length, "status": status, "chip": status_kind}
    _store(item, row, f"{title}. {date}. {length_spoken}. {status}")
    item.setFlags(_SELECTABLE)


def transcript_list(name: str, description: str = "", *, object_name: str = "Transcript") -> QListWidget:
    widget = QListWidget()
    widget.setObjectName(object_name)
    widget.setWordWrap(True)
    widget.setAccessibleName(name)
    if description:
        widget.setAccessibleDescription(description)
    widget.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    widget.setUniformItemSizes(False)
    widget.setResizeMode(QListWidget.ResizeMode.Adjust)
    widget.setLayoutMode(QListWidget.LayoutMode.Batched)
    widget.setBatchSize(200)
    widget.setMouseTracking(True)
    widget.setFrameShape(QListWidget.Shape.NoFrame)
    widget.setItemDelegate(RowDelegate(widget))
    return widget


# --------------------------------------------------------------------- painting


def _ui_font(points: float = 10.5, bold: bool = False) -> QFont:
    f = theme.ui_font()
    f.setPointSizeF(points)
    f.setBold(bold)
    return f


class RowDelegate(QStyledItemDelegate):
    """Draws every row kind. Heights are cached per (row, viewport width)."""

    EVENT_H = 30

    def __init__(self, view: QListWidget) -> None:
        super().__init__(view)
        self.view = view
        self._heights: dict[tuple[str, int], int] = {}
        self.highlight: str | None = None  # SIG of a row flashed after a search jump

    # The default item drawing (and screen readers asking the style) get the text's
    # own paragraph direction.
    def initStyleOption(self, option: QStyleOptionViewItem, index: QModelIndex) -> None:  # noqa: N802 - Qt API
        super().initStyleOption(option, index)
        vertical = Qt.AlignmentFlag.AlignVCenter
        if index.data(RTL_ROLE):
            option.direction = Qt.LayoutDirection.RightToLeft
            option.displayAlignment = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignAbsolute | vertical
            label, sep, text = option.text.partition(LINE_SEPARATOR if LINE_SEPARATOR in option.text else "\n")
            if sep:
                option.text = f"{LRM}{label}{LRM}{sep}{text}"
        else:
            option.direction = Qt.LayoutDirection.LeftToRight
            option.displayAlignment = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignAbsolute | vertical

    # ---------------------------------------------------------------- geometry

    def _columns(self, width: int) -> tuple[int, int, int, int]:
        """(pad, time column, source column, gap) for a viewport width."""
        if width < 560:
            return 8, 44, 70, 6
        return 24, theme.size("transcript.timeColumn"), theme.size("transcript.sourceColumn"), 12

    def _text_box(self, left: int, width: int) -> tuple[int, int]:
        pad, time_w, src_w, gap = self._columns(width)
        x = left + pad + time_w + gap + src_w + gap
        return x, max(80, min(theme.size("transcript.maxMeasure"), left + width - pad - x))

    def _layout(self, text: str, width: int, *, rtl: bool, italic: bool = False,
                font: QFont | None = None, line_h: int | None = None,
                marks: Sequence[tuple[int, int]] = ()) -> tuple[QTextLayout, float]:
        font = QFont(font) if font is not None else theme.transcript_font(rtl)
        font.setItalic(italic)
        layout = QTextLayout(text, font)
        opt = QTextOption()
        opt.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        opt.setTextDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
        opt.setAlignment((Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
                         | Qt.AlignmentFlag.AlignAbsolute)
        layout.setTextOption(opt)
        if marks:
            fmt = QTextCharFormat()
            fmt.setBackground(theme.color("transcript.highlight"))
            ranges = []
            for start, length in marks:
                r = QTextLayout.FormatRange()
                r.start, r.length, r.format = start, length, fmt
                ranges.append(r)
            layout.setFormats(ranges)
        step = line_h or (28 if rtl else 24)
        layout.beginLayout()
        y = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            line.setPosition(QPointF(0, y + (step - line.height()) / 2))
            y += step
        layout.endLayout()
        return layout, max(y, float(step))

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:  # noqa: N802 - Qt API
        width = max(200, self.view.viewport().width())
        key = (index.data(SIG_ROLE) or "", width)
        cached = self._heights.get(key)
        if cached is None:
            if len(self._heights) > 30000:
                self._heights.clear()
            cached = self._measure(index.data(ROW_ROLE) or {}, index, width)
            self._heights[key] = cached
        return QSize(width, cached)

    def _measure(self, row: dict[str, Any], index: QModelIndex, width: int) -> int:
        kind = row.get("kind")
        if kind == "line":
            _, w = self._text_box(0, width)
            text = index.data(TEXT_ROLE) or ""
            _, h = self._layout(text, w, rtl=bool(index.data(RTL_ROLE)))
            meta = 28 if self._chips(row) else 0
            return int((len(row["events"]) + len(row.get("after", ()))) * self.EVENT_H + h + meta + 16)
        if kind == "event":
            return self.EVENT_H + 4
        if kind == "hit":
            _, w = self._text_box(0, width)
            text = index.data(TEXT_ROLE) or ""
            _, h = self._layout(text, max(80, w - 120), rtl=bool(index.data(RTL_ROLE)))
            return int(h + 16 + (40 if row["header"] else 0))
        if kind == "meeting":
            return 56 if width >= 700 else 64
        if kind == "message":
            fm = QFontMetricsF(_ui_font())
            body = fm.boundingRect(QRectF(0, 0, width - 48, 10000), Qt.TextFlag.TextWordWrap, row.get("body", ""))
            return int(32 + 24 + (body.height() if row.get("body") else 0))
        fm = QFontMetricsF(_ui_font())
        return int(fm.boundingRect(QRectF(0, 0, width - 24, 10000), Qt.TextFlag.TextWordWrap,
                                   index.data(Qt.ItemDataRole.DisplayRole) or "").height() + 16)

    # ---------------------------------------------------------------- pieces

    def _chips(self, row: dict[str, Any]) -> list[tuple[str, str, str | None]]:
        """(text, kind, icon) chips under a line: state first, then quality marks."""
        chips: list[tuple[str, str, str | None]] = []
        if row["state"] == "draft":
            chips.append((C.ROW_DRAFT, "draft", None))
        elif row["state"] == "unsaved":
            chips.append((C.ROW_UNSAVED + (f" ({row['code']})" if row.get("code") else ""), "badsolid", "error"))
            chips.append((C.ROW_UNSAVED_HELP, "plain", None))
        if row.get("edited"):
            chips.append((C.ROW_EDITED, "outline", "versions"))
        for mark in row.get("marks", ()):
            chips.append((mark, "warn", "warning"))
        return chips

    def _chip(self, p: QPainter, x: float, y: float, text: str, kind: str, icon: str | None = None,
              max_w: float | None = None) -> float:
        styles = {"mic": ("source.mic.fg", "source.mic.bg", "source.mic.border"),
                  "system": ("source.system.fg", "source.system.bg", "source.system.border"),
                  "warn": ("status.warning.fg", "status.warning.bg", "status.warning.border"),
                  "ok": ("status.success.fg", "status.success.bg", "status.success.border"),
                  "bad": ("status.danger.fg", "status.danger.bg", "status.danger.border"),
                  "info": ("status.info.fg", "status.info.bg", "status.info.border"),
                  "neutral": ("status.neutral.fg", "status.neutral.bg", "status.neutral.border"),
                  "badsolid": ("text.onDanger", "status.danger.solid", "status.danger.solid"),
                  "draft": ("text.secondary", None, "border.strong"),
                  "outline": ("text.secondary", None, "border.default"),
                  "plain": ("status.danger.fg", None, None)}
        fg, bg, border = styles[kind]
        f = _ui_font(9, bold=True)
        fm = QFontMetricsF(f)
        w = fm.horizontalAdvance(text) + 16 + (18 if icon else 0)
        if max_w is not None:
            w = min(w, max_w)
        rect = QRectF(x, y, w, 22)
        if border or bg:
            pen = QPen(theme.color(border), 1, Qt.PenStyle.DashLine if kind == "draft" else Qt.PenStyle.SolidLine) \
                if border else Qt.PenStyle.NoPen
            p.setPen(pen)
            p.setBrush(theme.color(bg) if bg else Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(0.5, 0.5, -0.5, -0.5), 4, 4)
        tx = x + (8 if border or bg else 0)
        if icon:
            theme.icon(icon, fg, 14).paint(p, QRect(int(tx), int(y + 4), 14, 14))
            tx += 18
        p.setFont(f)
        p.setPen(theme.color(fg))
        shown = fm.elidedText(text, Qt.TextElideMode.ElideRight, max(10.0, x + w - tx - 6))
        p.drawText(QRectF(tx, y, x + w - tx, 22), Qt.AlignmentFlag.AlignVCenter, shown)
        return w

    def _event_band(self, p: QPainter, left: int, top: int, width: int, event: dict[str, Any]) -> None:
        pad, time_w, _, gap = self._columns(width)
        loss = bool(event.get("loss"))
        col = "status.warning.fg" if loss else "text.secondary"
        p.setFont(theme.mono_font())
        p.setPen(theme.color("text.secondary"))
        p.drawText(QRect(left + pad, top, time_w, self.EVENT_H), Qt.AlignmentFlag.AlignVCenter,
                   clock_text(event["t"]))
        x = left + pad + time_w + gap
        theme.icon(event.get("icon", "gap"), col, 16).paint(p, QRect(x, top + 7, 16, 16))
        x += 24
        f = _ui_font(9.5, bold=loss)
        p.setFont(f)
        p.setPen(theme.color(col))
        fm = QFontMetricsF(f)
        rng = (f"  {clock_text(event['t'])}–{clock_text(event['end'])}" if event.get("end") is not None
               else "")
        right = left + width - pad
        tag = _tag(event)
        chip_w = QFontMetricsF(_ui_font(9, True)).horizontalAdvance(tag) + 24 if tag else 0.0
        text = fm.elidedText(event["label"] + rng, Qt.TextElideMode.ElideRight, max(40.0, right - x - chip_w))
        p.drawText(QRectF(x, top, right - x, self.EVENT_H), Qt.AlignmentFlag.AlignVCenter, text)
        if tag:
            self._chip(p, x + fm.horizontalAdvance(text) + 8, top + 4, tag, "outline")

    def _background(self, p: QPainter, option: QStyleOptionViewItem, row: dict[str, Any], sig: str) -> None:
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if row.get("state") == "unsaved":
            p.fillRect(rect, theme.color("status.danger.bg"))
            p.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), theme.color("status.danger.solid"))
        elif self.highlight is not None and sig == self.highlight:
            p.fillRect(rect, theme.color("transcript.highlight"))
        elif selected:
            p.fillRect(rect, theme.color("bg.selected"))
            p.fillRect(QRect(rect.left(), rect.top(), 3, rect.height()), theme.color("accent.bg"))
        elif option.state & QStyle.StateFlag.State_MouseOver and row.get("kind") != "message":
            p.fillRect(rect, theme.color("bg.surfaceAlt"))
        else:
            p.fillRect(rect, theme.color("bg.surface"))

    def _focus(self, p: QPainter, option: QStyleOptionViewItem) -> None:
        if option.state & QStyle.StateFlag.State_HasFocus and self.view.hasFocus():
            p.setPen(QPen(theme.color("focus.ring"), 2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(option.rect.adjusted(1, 1, -1, -1))

    def paint(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        row = index.data(ROW_ROLE)
        if not row:
            super().paint(p, option, index)
            return
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._background(p, option, row, index.data(SIG_ROLE) or "")
        kind = row["kind"]
        if kind == "line":
            self._paint_line(p, option, index, row)
        elif kind == "event":
            self._event_band(p, option.rect.left(), option.rect.top() + 2, option.rect.width(), row)
        elif kind == "hit":
            self._paint_hit(p, option, index, row)
        elif kind == "meeting":
            self._paint_meeting(p, option, row)
        elif kind == "message":
            self._paint_message(p, option, row)
        self._focus(p, option)
        if kind in ("meeting", "hit"):
            p.setPen(QPen(theme.color("border.subtle"), 1))
            p.drawLine(option.rect.bottomLeft(), option.rect.bottomRight())
        p.restore()

    def _source_chip(self, p: QPainter, x: int, y: int, source: str | None, width: int) -> None:
        if source not in ("mic", "system"):
            return
        compact = width < 560
        self._chip(p, x, y, SOURCE_NAMES[source], source, None if compact else ("mic" if source == "mic"
                                                                                  else "speaker"))

    def _paint_line(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex, row: dict[str, Any]) -> None:
        rect = option.rect
        width = rect.width()
        pad, time_w, _, gap = self._columns(width)
        top = rect.top()
        for event in row["events"]:
            self._event_band(p, rect.left(), top, width, dict(event))
            top += self.EVENT_H
        top += 8
        p.setFont(theme.mono_font())
        p.setPen(theme.color("text.secondary"))
        p.drawText(QRect(rect.left() + pad, top, time_w, 24), Qt.AlignmentFlag.AlignVCenter, clock_text(row["t"]))
        self._source_chip(p, rect.left() + pad + time_w + gap, top + 1, row["source"], width)
        tx, tw = self._text_box(rect.left(), width)
        text = index.data(TEXT_ROLE) or ""
        draft = row["state"] == "draft"
        layout, h = self._layout(text + (" …" if draft else ""), tw, rtl=bool(index.data(RTL_ROLE)), italic=draft)
        p.setPen(theme.color("transcript.draft" if draft else "text.primary"))
        layout.draw(p, QPointF(tx, top))
        y = top + h + 4
        x = float(tx)
        for text_, kind, icon in self._chips(row):
            x += self._chip(p, x, y, text_, kind, icon, max_w=max(60.0, tx + tw - x)) + 6
            if x > tx + tw:
                break
        y += 28 if self._chips(row) else 0
        for event in row.get("after", ()):
            self._event_band(p, rect.left(), int(y) + 4, width, dict(event))
            y += self.EVENT_H

    def _paint_hit(self, p: QPainter, option: QStyleOptionViewItem, index: QModelIndex, row: dict[str, Any]) -> None:
        rect = option.rect
        width = rect.width()
        pad, time_w, _, gap = self._columns(width)
        top = rect.top()
        if row["header"]:
            p.fillRect(QRect(rect.left(), top, width, 40), theme.color("bg.surfaceAlt"))
            f = _ui_font(10.5, bold=True)
            p.setFont(f)
            p.setPen(theme.color("accent.text"))
            title = QFontMetricsF(f).elidedText(row["title"], Qt.TextElideMode.ElideRight, width - 2 * pad)
            rtl = is_rtl(row["title"])
            p.drawText(QRect(rect.left() + pad, top, width - 2 * pad, 40), Qt.AlignmentFlag.AlignVCenter
                       | (Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft), title)
            top += 40
        top += 8
        x = rect.left() + pad
        if row["t"] is None:
            self._chip(p, x, top + 1, C.HIST_TITLE_MATCH, "info")
        else:
            p.setFont(theme.mono_font())
            p.setPen(theme.color("text.secondary"))
            p.drawText(QRect(x, top, time_w, 24), Qt.AlignmentFlag.AlignVCenter, clock_text(row["t"]))
            self._source_chip(p, x + time_w + gap, top + 1, row["source"], width)
        tx, tw = self._text_box(rect.left(), width)
        text = index.data(TEXT_ROLE) or ""
        layout, _ = self._layout(text, max(80, tw - 120), rtl=bool(index.data(RTL_ROLE)),
                                 marks=_matches(text, row["query"]))
        p.setPen(theme.color("text.primary"))
        layout.draw(p, QPointF(tx, top))
        f = _ui_font(9.5, bold=True)
        p.setFont(f)
        p.setPen(theme.color("accent.text"))
        link = (C.HIST_OPEN_MEETING if row["t"] is None else C.HIST_OPEN_AT.format(time=clock_text(row["t"]))) + " ›"
        p.drawText(QRect(rect.left(), top, width - pad, 24), Qt.AlignmentFlag.AlignVCenter
                   | Qt.AlignmentFlag.AlignRight, link)

    def _paint_meeting(self, p: QPainter, option: QStyleOptionViewItem, row: dict[str, Any]) -> None:
        rect = option.rect
        width = rect.width()
        pad = 16
        strong = _ui_font(10.5, bold=True)
        normal = _ui_font(9.5)
        rtl = is_rtl(row["title"])
        if width >= 700:
            status_w, length_w, date_w = 170, 80, 190
            title_w = width - 2 * pad - status_w - length_w - date_w
            x = rect.left() + pad
            p.setFont(strong)
            p.setPen(theme.color("text.primary"))
            title = QFontMetricsF(strong).elidedText(row["title"], Qt.TextElideMode.ElideRight, title_w - 12)
            p.drawText(QRect(x, rect.top(), title_w - 12, rect.height()), Qt.AlignmentFlag.AlignVCenter
                       | (Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft), title)
            x += title_w
            p.setFont(normal)
            p.setPen(theme.color("text.secondary"))
            p.drawText(QRect(x, rect.top(), date_w, rect.height()), Qt.AlignmentFlag.AlignVCenter, row["date"])
            x += date_w
            p.setFont(theme.mono_font())
            p.drawText(QRect(x, rect.top(), length_w, rect.height()), Qt.AlignmentFlag.AlignVCenter, row["length"])
            x += length_w
            self._chip(p, x, rect.top() + (rect.height() - 22) / 2, row["status"], row["chip"],
                       _CHIP_ICON.get(row["chip"]), max_w=status_w - 8)
            return
        p.setFont(strong)
        p.setPen(theme.color("text.primary"))
        title = QFontMetricsF(strong).elidedText(row["title"], Qt.TextElideMode.ElideRight, width - 2 * pad)
        p.drawText(QRect(rect.left() + pad, rect.top() + 8, width - 2 * pad, 22), Qt.AlignmentFlag.AlignVCenter
                   | (Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft), title)
        p.setFont(normal)
        p.setPen(theme.color("text.secondary"))
        meta = f"{row['date']} · {row['length']} · {row['status']}"
        p.drawText(QRect(rect.left() + pad, rect.top() + 32, width - 2 * pad, 22), Qt.AlignmentFlag.AlignVCenter,
                   QFontMetricsF(normal).elidedText(meta, Qt.TextElideMode.ElideRight, width - 2 * pad))

    def _paint_message(self, p: QPainter, option: QStyleOptionViewItem, row: dict[str, Any]) -> None:
        rect = option.rect.adjusted(24, 12, -24, -8)
        tone = {"bad": "status.danger.fg", "warn": "status.warning.fg", "info": "status.info.fg"}.get(
            row.get("tone", ""), "text.primary")
        p.setFont(_ui_font(10.5, bold=True))
        p.setPen(theme.color(tone))
        p.drawText(QRect(rect.left(), rect.top(), rect.width(), 24), Qt.AlignmentFlag.AlignVCenter, row["title"])
        if row.get("body"):
            p.setFont(_ui_font())
            p.setPen(theme.color("text.secondary"))
            p.drawText(QRect(rect.left(), rect.top() + 26, rect.width(), rect.height() - 26),
                       Qt.TextFlag.TextWordWrap, row["body"])


_CHIP_ICON = {"ok": "check-circle", "warn": "warning", "bad": "error", "info": "info"}


def _matches(text: str, query: str) -> list[tuple[int, int]]:
    """Ranges of the query's words in the text (case-insensitive), for highlighting only."""
    folded = text.lower()
    found: list[tuple[int, int]] = []
    for word in {w for w in query.lower().split() if w}:
        start = folded.find(word)
        while start != -1 and len(found) < 50:
            found.append((start, len(word)))
            start = folded.find(word, start + len(word))
    return sorted(found)
