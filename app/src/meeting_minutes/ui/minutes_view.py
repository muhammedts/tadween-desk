"""Minutes and Action items tabs of a saved meeting.

`MinutesWorkspace` owns two widgets, `minutes_tab` and `actions_tab`, which the main
window places beside the Transcript tab, and talks only to `MinutesService`. Generation
runs on the service's worker thread; its progress/finish callbacks are forwarded to the
GUI thread through a queued signal, so nothing here blocks on the network.

"No request was made" is shown only when the failure is proven to have happened
before sending (GenerationError.request_sent is False). Invalidated minutes (after a
redaction) cannot be viewed, copied or saved.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import QDate, QObject, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from meeting_minutes.export import ExportError
from meeting_minutes.minutes.credentials import CredentialError
from meeting_minutes.minutes.provider import GenerationError
from meeting_minutes.minutes.render import (
    CAUTION,
    COMMITMENT_LABEL,
    DECISION_LABEL,
    NOT_SPECIFIED,
    STATE_LABEL,
    due_text,
    evidence_label,
    usage_text,
    version_label,
)
from meeting_minutes.minutes.provider import CostEstimate
from meeting_minutes.minutes.service import GenerationJob, MinutesDocument, MinutesService, MinutesUnavailable
from meeting_minutes.storage.store import MinutesItemRow, MinutesRejected, StorageError
from meeting_minutes.ui.widgets import ComboBox, DateEdit
from meeting_minutes.ui.wording import is_rtl, local_time

GENERATE_NOTICE = ("Sends this meeting's saved transcript and related details to OpenAI: title, date, time zone, "
                   "line times, Mic/System labels and gap or recognition marks. API charges apply.")
GENERATE_NOTICE_2 = "Live transcription stays on this PC; audio and live draft text are never sent."
NO_KEY_TEXT = "Add an OpenAI API key in Settings › OpenAI minutes first. Transcription works without it."
UNCHANGED = "Your transcript and current minutes are unchanged."
ESTIMATE_NOTE = "Estimated by this app, not an OpenAI bill; prices and limit are in Settings."
SECTION_TITLES = {"summary": "Meeting overview", "topic": "Discussion points", "decision": "Decisions",
                  "action": "Action items", "question": "Open questions"}
SECTION_ORDER = ("summary", "topic", "decision", "action", "question")
STEP_TEXT = {"preparing": "Preparing saved transcript (revision {rev})",
             "sending": "Sending to OpenAI — part {i} of {n}",
             "validating": "Checking the result against the transcript"}
ACTION_FOOT = ("Owners and dates are only filled in when the transcript states them. “Suggested” items were "
               "proposed but not agreed. Mic and System are audio sources, not people.")
FILTERS = ("All", "Committed", "Suggested or unclear", "Needs review")
COMMIT_CHOICES = (("explicit", "Committed — someone agreed to do it"), ("proposed", "Suggested — proposed, not agreed"),
                  ("uncertain", "Unclear"))
MINUTES_FILTERS = ("Markdown (*.md)", "Text file, UTF-8 (*.txt)")
UI_WAIT_S = 0.25  # longest the GUI thread waits for another thread's store operation

# Calls (parent, suggested_path) -> (path, chosen_filter) or None; replaceable in tests.
SaveDialog = Callable[[QWidget, str], "tuple[str, str] | None"]
Confirm = Callable[[QWidget, str, str, str], bool]  # (parent, title, text, accept label)
RunDialog = Callable[[QDialog], bool]


def native_minutes_save_dialog(parent: QWidget, suggested: str) -> tuple[str, str] | None:
    path, chosen = QFileDialog.getSaveFileName(parent, "Save minutes as", suggested, ";;".join(MINUTES_FILTERS),
                                               MINUTES_FILTERS[0])
    return (path, chosen) if path else None


def default_confirm(parent: QWidget, title: str, text: str, accept: str) -> bool:
    box = QMessageBox(QMessageBox.Icon.Question, title, title, parent=parent)
    box.setInformativeText(text)
    ok = box.addButton(accept, QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    box.exec()
    return box.clickedButton() is ok


def default_run_dialog(dialog: QDialog) -> bool:
    return dialog.exec() == QDialog.DialogCode.Accepted


def sent_phrase(request_sent: bool | None) -> str:
    """Only a proven pre-send stop says that no request was made."""
    if request_sent is False:
        return "No request was made."
    if request_sent is None:
        return "The request may have reached OpenAI, and your API project may be charged."
    return "A request reached OpenAI; your API project may be charged."


def estimate_text(estimate: CostEstimate | None, model_id: str) -> str:
    """One line before sending: parts, tokens, cost range and the local limit."""
    if estimate is None:
        return ""
    parts = f"{estimate.parts} request{'s' if estimate.parts != 1 else ''}"
    tokens = f"about {estimate.input_tokens:,} input tokens"
    limit = ("no limit set in this app" if estimate.budget_usd is None
             else f"your limit ${estimate.budget_usd:.2f}")
    if estimate.first_pass_usd is None:
        cost = f"cost unknown: no price is set for “{model_id}”"
    else:
        cost = (f"at most ${estimate.first_pass_usd:.2f} (up to ${estimate.worst_case_usd:.2f} with "
                f"{estimate.requests_max - estimate.parts} allowed retries and repairs)")
    return f"Estimate: {parts}, {tokens}, {cost}; {limit}. {ESTIMATE_NOTE}"


def error_copy(error: GenerationError, model_id: str = "") -> tuple[str, str, str]:
    """(title, body, action) for a failed job; content-free, ending with the unchanged note."""
    code, sent = error.code, error.request_sent
    settings, retry = "Open OpenAI settings", "Try again"
    table: dict[str, tuple[str, str, str]] = {
        "no_key": ("Add an OpenAI API key to generate minutes", "", settings),
        "vault_unavailable": ("Windows Credential Manager couldn't be read",
                              "The saved key could not be read for this Windows account.", settings),
        "offline": ("Can't reach OpenAI", "Check your internet connection or proxy, then try again.", retry),
        "auth": ("OpenAI rejected the API key",
                 "Authentication failed. Replace the key in Settings, then try again.", settings),
        "model_unavailable": ("The selected model isn't available",
                              f"“{model_id}” isn't available to your API project or doesn't support structured "
                              "outputs. Choose another model in Settings.", settings),
        "rate_limit": ("OpenAI is limiting requests right now", "Rate limit reached. Wait a minute, then try again.",
                       retry),
        "quota": ("Your OpenAI project can't take more requests",
                  "Quota exceeded or billing isn't set up for this API project. Check your OpenAI account, then "
                  "try again.", retry),
        "timeout": ("OpenAI didn't respond in time",
                    "No minutes were saved. Try again; long meetings are sent in parts.", retry),
        "refusal": ("OpenAI declined to write these minutes", "No minutes were saved.", retry),
        "invalid_output": ("The response couldn't be used",
                           f"{max(1, error.invalid_count)} problem(s) were found, such as items citing transcript "
                           "lines that don't exist, and the one repair attempt did not fix them. No minutes were "
                           "saved.", retry),
        "cancelled": ("Generation cancelled", "No minutes were saved.", "Generate MoM"),
        "too_large": ("This meeting is too long to send" if sent is False else "The request or reply was too large",
                      "The transcript would need too many parts." if sent is False
                      else "OpenAI could not process the request or the reply exceeded the output limit. "
                           "No minutes were saved.", retry),
        "server_error": ("OpenAI had a problem", "Try again in a few minutes. No minutes were saved.", retry),
        "request_rejected": ("OpenAI refused the request",
                             "Technical details show the status. No minutes were saved.", retry),
        "transcript_changed": ("The transcript changed",
                               "Reopen the meeting to see the current saved revision, then generate again.", retry),
        "transcript_invalidated": ("The transcript changed while generating",
                                   "Text was redacted or the meeting was deleted, so the result was discarded and "
                                   "not saved.", retry),
        "not_saved": ("Finish the meeting first", "Minutes are made from a stopped, saved meeting.", retry),
        "empty_transcript": ("There is no saved text to use", "This meeting has no saved transcript text.", retry),
        "busy": ("Another generation is running", "Wait for it to finish or cancel it.", retry),
        "storage": ("The minutes couldn't be saved", "History could not be written. No minutes were saved.", retry),
        "budget": ("This would pass your cost limit",
                   "The estimated cost of this generation is above the limit set in Settings › OpenAI minutes. "
                   "Raise or remove the limit there, or choose a cheaper model. No minutes were saved."
                   if sent is False else
                   "The next request would have passed the cost limit set in Settings, so generation stopped. "
                   "No minutes were saved.", settings),
        "price_unknown": ("Set a price for this model first",
                          f"The cost of “{model_id}” is unknown, so your cost limit can't be checked. Enter its "
                          "prices in Settings › OpenAI minutes, or remove the limit.", settings),
    }
    title, body, action = table.get(code, ("Generation failed", "No minutes were saved.", retry))
    if code == "cancelled" and sent is not False:
        body += " A request already sent to OpenAI may still be charged."
    else:
        body = f"{body} {sent_phrase(sent)}".strip()
    return title, f"{body} {UNCHANGED}", action


def readiness_text(service: MinutesService) -> str:
    """One line for the readiness panel ("Optional: OpenAI minutes")."""
    try:
        status = service.key_status()
    except (CredentialError, StorageError, OSError):
        return "Optional: OpenAI minutes — key status unavailable. Transcription works without it."
    if status.configured:
        return (f"Optional: OpenAI minutes — key saved, model {service.model_id()}. Used only when you press "
                "Generate MoM.")
    return "Optional: OpenAI minutes — not set up. Transcription works without it."


def _label(text: str = "", role: str | None = None, *, wrap: bool = True) -> QLabel:
    label = QLabel(text)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if role:
        label.setProperty("role", role)
    return label


def _set_prop(widget: QWidget, name: str, value: Any) -> None:
    widget.setProperty(name, value)
    style = widget.style()
    style.unpolish(widget)
    style.polish(widget)


def _button(text: str, variant: str | None = None, size: str | None = None) -> QPushButton:
    button = QPushButton(text)
    if variant:
        button.setProperty("variant", variant)
    if size:
        button.setProperty("size", size)
    return button


def _content_label(text: str) -> QLabel:
    """Transcript-derived text: plain, selectable, right-to-left when it starts in Arabic."""
    label = _label(text)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    rtl = is_rtl(text)
    label.setLayoutDirection(Qt.LayoutDirection.RightToLeft if rtl else Qt.LayoutDirection.LeftToRight)
    label.setAlignment((Qt.AlignmentFlag.AlignRight if rtl else Qt.AlignmentFlag.AlignLeft)
                       | Qt.AlignmentFlag.AlignVCenter)
    return label


class Banner(QFrame):
    """QFrame[banner=tone] with a title, body, one action and a technical-details line."""

    action_clicked = Signal(str)

    def __init__(self, tone: str = "info") -> None:
        super().__init__()
        self.setProperty("banner", tone)
        self.setProperty("inline", True)
        layout = QVBoxLayout(self)
        self.title = _label()
        self.title.setStyleSheet("font-weight: 600;")
        self.body = _label()
        self.details = _label(role="mono")
        self.details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.action = _button("")
        self.action.clicked.connect(lambda: self.action_clicked.emit(self.action.text()))
        row = QHBoxLayout()
        row.addWidget(self.action)
        row.addStretch(1)
        for widget in (self.title, self.body, self.details):
            layout.addWidget(widget)
        layout.addLayout(row)
        self.hide()

    def show_message(self, tone: str, title: str, body: str = "", action: str = "", details: str = "") -> None:
        _set_prop(self, "banner", tone)
        self.title.setText(title)
        self.body.setText(body)
        self.body.setVisible(bool(body))
        self.details.setText(f"Technical details: {details}" if details else "")
        self.details.setVisible(bool(details))
        self.action.setText(action)
        self.action.setVisible(bool(action))
        self.setAccessibleName(title)
        self.show()


class ItemEditDialog(QDialog):
    """Edit a summary/topic/decision/question item, or remove it."""

    def __init__(self, parent: QWidget | None, kind: str, item: MinutesItemRow | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.removed = False
        self.setWindowTitle(("Edit " if item else "Add ") + SECTION_TITLES[kind].lower().rstrip("s") + " item")
        form = QFormLayout(self)
        self.text = QPlainTextEdit(item.text if item else "")
        self.text.setAccessibleName("Text")
        form.addRow("Title" if kind == "topic" else "Text", self.text)
        self.detail = QPlainTextEdit((item.detail or "") if item else "")
        self.status = ComboBox()
        for value, label in DECISION_LABEL.items():
            self.status.addItem(label, value)
        self.owner = QLineEdit((item.owner or "") if item else "")
        if kind == "topic":
            form.addRow("Discussion", self.detail)
        if kind == "decision":
            self.status.setCurrentIndex(max(0, self.status.findData(item.status if item else "uncertain")))
            form.addRow("Status", self.status)
        if kind == "question":
            self.owner.setPlaceholderText("Leave empty if the meeting didn't name one.")
            form.addRow("Owner", self.owner)
        buttons = QDialogButtonBox()
        buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        if item is not None:
            remove = buttons.addButton("Remove item", QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.setProperty("variant", "dangerText")
            remove.clicked.connect(self._remove)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    def values(self) -> dict[str, Any]:
        out: dict[str, Any] = {"text": self.text.toPlainText()}
        if self.kind == "topic":
            out["detail"] = self.detail.toPlainText()
        if self.kind == "decision":
            out["status"] = self.status.currentData()
        if self.kind == "question":
            out["owner"] = self.owner.text()
        return out


class ActionEditDialog(QDialog):
    """Edit action item: task, owner (blank = Not specified), due date or none,
    commitment, and "I've checked this against the transcript"."""

    def __init__(self, parent: QWidget | None, item: MinutesItemRow | None = None) -> None:
        super().__init__(parent)
        self.removed = False
        self.setWindowTitle("Edit action item" if item else "Add action item")
        form = QFormLayout(self)
        self.task = QPlainTextEdit(item.text if item else "")
        self.task.setAccessibleName("Task")
        form.addRow("Task", self.task)
        self.owner = QLineEdit((item.owner or "") if item else "")
        self.owner.setPlaceholderText("Leave empty if the meeting didn't name one.")
        self.owner.setAccessibleName("Owner")
        form.addRow("Owner", self.owner)
        self.no_due = QCheckBox("Not specified")
        self.due = DateEdit()
        self.due.setCalendarPopup(True)
        self.due.setDisplayFormat("yyyy-MM-dd")
        self.due.setAccessibleName("Due date")
        if item is not None and item.due_date:
            self.due.setDate(QDate.fromString(item.due_date, "yyyy-MM-dd"))
            self.no_due.setChecked(False)
        else:
            self.due.setDate(QDate.currentDate())
            self.no_due.setChecked(True)
        self.due.setEnabled(not self.no_due.isChecked())
        self.no_due.toggled.connect(lambda checked: self.due.setEnabled(not checked))
        due_row = QHBoxLayout()
        due_row.addWidget(self.due)
        due_row.addWidget(self.no_due)
        form.addRow("Due date", due_row)
        phrase = item.due_date_text if item else None
        self.phrase = _label(f"Said in the meeting: “{phrase}”" if phrase else "Leave empty if not specified.",
                             "help")
        form.addRow("", self.phrase)
        self.commitment = ComboBox()
        for value, label in COMMIT_CHOICES:
            self.commitment.addItem(label, value)
        self.commitment.setCurrentIndex(max(0, self.commitment.findData(item.commitment if item else "uncertain")))
        self.commitment.setAccessibleName("Commitment")
        form.addRow("Commitment", self.commitment)
        self.reviewed = QCheckBox("I've checked this against the transcript")
        self.reviewed.setChecked(bool(item and item.reviewed))
        form.addRow("", self.reviewed)
        evidence = ", ".join(evidence_label(ev) for ev in item.evidence) if item and item.evidence else (
            "Added by you" if item else "")
        if evidence:
            form.addRow("Evidence", _label(evidence))
        buttons = QDialogButtonBox()
        buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Cancel", QDialogButtonBox.ButtonRole.RejectRole)
        if item is not None:
            remove = buttons.addButton("Remove item", QDialogButtonBox.ButtonRole.DestructiveRole)
            remove.setProperty("variant", "dangerText")
            remove.clicked.connect(self._remove)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _remove(self) -> None:
        self.removed = True
        self.accept()

    def values(self) -> dict[str, Any]:
        return {
            "text": self.task.toPlainText(),
            "owner": self.owner.text(),
            "due_date": None if self.no_due.isChecked() else self.due.date().toString("yyyy-MM-dd"),
            "commitment": self.commitment.currentData(),
            "reviewed": self.reviewed.isChecked(),
        }


class MinutesWorkspace(QObject):
    evidence_requested = Signal(str)  # segment id to show in the Transcript tab
    badges_changed = Signal(str, str)  # (minutes badge, actions badge)
    open_settings_requested = Signal()  # "Open OpenAI settings"
    show_actions_requested = Signal()  # "Review all n in Action items"
    generation_finished = Signal(str)  # job state: done | failed | cancelled (GUI thread)
    _job_event = Signal(object)  # worker thread -> GUI thread (queued)

    def __init__(self, service: MinutesService, parent: QObject | None = None, *,
                 save_dialog: SaveDialog | None = None, confirm: Confirm | None = None,
                 clipboard: Callable[[str], None] | None = None, run_dialog: RunDialog | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.save_dialog = save_dialog or native_minutes_save_dialog
        self.confirm = confirm or default_confirm
        self.clipboard = clipboard or (lambda text: QGuiApplication.clipboard().setText(text))
        self.run_dialog = run_dialog or default_run_dialog
        self.meeting_id: str | None = None
        self.revision: int | None = None
        self.version_id: str | None = None
        self.editing = False
        self.document: MinutesDocument | None = None
        self.item_labels: dict[str, QLabel] = {}
        self.item_edit_buttons: dict[str, QToolButton] = {}
        self.evidence_buttons: list[QToolButton] = []
        self.action_filter = "All"
        self.last_notice = ""
        self._job_event.connect(self._on_job_event, Qt.ConnectionType.QueuedConnection)
        self.minutes_tab = QWidget()
        self.minutes_tab.setAccessibleName("Minutes")
        self.actions_tab = QWidget()
        self.actions_tab.setAccessibleName("Action items")
        self._build_minutes_tab()
        self._build_actions_tab()
        self._show_page()

    # ------------------------------------------------------------------ build

    def _build_minutes_tab(self) -> None:
        outer = QVBoxLayout(self.minutes_tab)
        self.error_banner = Banner("bad")
        self.error_banner.action_clicked.connect(self._banner_action)
        self.notice_banner = Banner("ok")
        outer.addWidget(self.error_banner)
        outer.addWidget(self.notice_banner)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)

        # empty / no key
        self.empty_page = QWidget()
        empty = QVBoxLayout(self.empty_page)
        empty.addStretch(1)
        empty.addWidget(_label("Minutes and action items", "h2"), 0, Qt.AlignmentFlag.AlignHCenter)
        body = _label("Create an editable draft with an overview, discussion points, decisions, action items and "
                      "open questions — each linked to the transcript lines it comes from.", "sub")
        body.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(body)
        self.generate_button = _button("Generate MoM", "primary", "cta")
        self.generate_button.clicked.connect(self.generate)
        empty.addWidget(self.generate_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.notice_label = _label(GENERATE_NOTICE, "help")
        self.notice_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self.notice_label)
        self.notice_label_2 = _label(GENERATE_NOTICE_2, "help")
        self.notice_label_2.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self.notice_label_2)
        self.estimate_label = _label("", "help")
        self.estimate_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.estimate_label.setAccessibleName("Cost estimate")
        empty.addWidget(self.estimate_label)
        self.no_key_label = _label(NO_KEY_TEXT, "help")
        self.no_key_label.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self.no_key_label)
        self.settings_button = _button("Open OpenAI settings", "subtle")
        self.settings_button.clicked.connect(self.open_settings_requested.emit)
        empty.addWidget(self.settings_button, 0, Qt.AlignmentFlag.AlignHCenter)
        self.incomplete_empty = _label("", "help")
        self.incomplete_empty.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        empty.addWidget(self.incomplete_empty)
        empty.addStretch(2)
        self.pages.addWidget(self.empty_page)

        # running
        self.running_page = QWidget()
        running = QVBoxLayout(self.running_page)
        card = QFrame()
        card.setProperty("card", True)
        card_layout = QVBoxLayout(card)
        card_layout.addWidget(_label("Generating minutes and action items…", "h2"))
        self.step_label = _label("")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Generation progress")
        card_layout.addWidget(self.step_label)
        card_layout.addWidget(self.progress)
        card_layout.addWidget(_label("You can keep reading or editing the Transcript tab. The current minutes stay "
                                     "unchanged until this finishes.", "help"))
        self.cancel_button = _button("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        card_layout.addWidget(self.cancel_button, 0, Qt.AlignmentFlag.AlignLeft)
        running.addWidget(card)
        running.addStretch(1)
        self.pages.addWidget(self.running_page)

        # document
        self.doc_page = QWidget()
        doc = QVBoxLayout(self.doc_page)
        strip = QHBoxLayout()
        self.state_chip = QLabel("")
        self.state_chip.setProperty("chip", "neutral")
        self.version_combo = ComboBox()
        self.version_combo.setAccessibleName("Minutes version")
        self.version_combo.activated.connect(self._version_chosen)
        self.edit_button = _button("Edit")
        self.edit_button.clicked.connect(self.toggle_edit)
        self.review_button = _button("Start review", "primary")
        self.review_button.clicked.connect(self.advance_state)
        self.copy_button = _button("Copy")
        self.copy_button.clicked.connect(self.copy)
        self.save_button = _button("Save As…")
        self.save_button.clicked.connect(self.save_as)
        self.regenerate_button = _button("Regenerate")
        self.regenerate_button.clicked.connect(self.generate)
        for widget in (self.state_chip, self.version_combo):
            strip.addWidget(widget)
        strip.addStretch(1)
        for widget in (self.edit_button, self.review_button, self.copy_button, self.save_button,
                       self.regenerate_button):
            strip.addWidget(widget)
        doc.addLayout(strip)
        self.meta_label = _label("", "help")
        doc.addWidget(self.meta_label)
        self.stale_banner = Banner("warn")
        self.stale_banner.action_clicked.connect(lambda _: self.generate())
        self.removed_banner = Banner("warn")
        self.removed_banner.action_clicked.connect(lambda _: self.generate())
        self.incomplete_banner = Banner("info")
        self.caution_label = _label(CAUTION, "help")
        for widget in (self.stale_banner, self.removed_banner, self.incomplete_banner, self.caution_label):
            doc.addWidget(widget)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sections = QWidget()
        self.sections_layout = QVBoxLayout(self.sections)
        self.scroll.setWidget(self.sections)
        doc.addWidget(self.scroll, 1)
        self.pages.addWidget(self.doc_page)

    def _build_actions_tab(self) -> None:
        layout = QVBoxLayout(self.actions_tab)
        row = QHBoxLayout()
        self.filter_group = QButtonGroup(self.actions_tab)
        self.filter_group.setExclusive(True)
        self.filter_buttons: dict[str, QPushButton] = {}
        for name in FILTERS:
            button = _button(name)
            button.setCheckable(True)
            button.setChecked(name == "All")
            button.clicked.connect(lambda _=False, n=name: self.set_action_filter(n))
            self.filter_group.addButton(button)
            self.filter_buttons[name] = button
            row.addWidget(button)
        row.addStretch(1)
        self.action_edit_button = _button("Edit…")
        self.action_edit_button.clicked.connect(self.edit_selected_action)
        self.action_add_button = _button("Add action item")
        self.action_add_button.clicked.connect(self.add_action)
        row.addWidget(self.action_edit_button)
        row.addWidget(self.action_add_button)
        layout.addLayout(row)
        self.actions_empty = _label("", "sub")
        layout.addWidget(self.actions_empty)
        self.action_table = QTableWidget(0, 6)
        self.action_table.setHorizontalHeaderLabels(["Task", "Owner", "Due", "Commitment", "Review", "Evidence"])
        self.action_table.setAccessibleName("Action items")
        self.action_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.action_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.action_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.action_table.setWordWrap(True)
        self.action_table.verticalHeader().setVisible(False)
        header = self.action_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for col in range(1, 6):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        self.action_table.cellDoubleClicked.connect(lambda r, _c: self.edit_action_row(r))
        layout.addWidget(self.action_table, 1)
        layout.addWidget(_label(ACTION_FOOT, "help"))
        self.action_rows: list[MinutesItemRow] = []

    # ---------------------------------------------------------------- state

    def set_meeting(self, meeting_id: str | None, revision: int | None) -> None:
        """Show the minutes of a saved meeting at its displayed saved revision."""
        if meeting_id != self.meeting_id:
            self.version_id = None
            self.editing = False
            self.error_banner.hide()
            self.notice_banner.hide()
        self.meeting_id = meeting_id
        self.revision = revision
        self.refresh()

    def refresh(self) -> None:
        self.document = None
        if self.meeting_id is None:
            self._show_page()
            self._emit_badges()
            return
        try:
            with self.service.store.held(UI_WAIT_S):
                versions = self.service.versions(self.meeting_id)
        except StorageError as exc:
            self.error_banner.show_message("bad", "History is busy", "Try again in a moment.", "", exc.code)
            versions = []
        valid = [v for v in versions if not v.row.invalidated]
        ids = [v.row.id for v in versions]
        if self.version_id not in ids:
            self.version_id = (valid[-1] if valid else versions[-1]).row.id if versions else None
        self.version_combo.blockSignals(True)
        self.version_combo.clear()
        for v in reversed(versions):
            text = version_label(v.row, v.latest)
            if v.row.invalidated:
                text = f"Version {v.row.version} — removed"
            elif v.stale:
                text += " (stale)"
            self.version_combo.addItem(text, v.row.id)
        self.version_combo.setCurrentIndex(max(0, self.version_combo.findData(self.version_id)))
        self.version_combo.blockSignals(False)
        self._load_document()
        self._show_page()
        self._render_actions()
        self._emit_badges()

    def _load_document(self) -> None:
        self.document = None
        self.removed_banner.hide()
        if self.version_id is None:
            return
        try:
            with self.service.store.held(UI_WAIT_S):
                self.document = self.service.document(self.version_id)
        except MinutesUnavailable as exc:
            self.document = None
            if exc.code == "invalidated":
                self.removed_banner.show_message(
                    "warn", "These minutes were removed",
                    "Transcript text was redacted after they were generated, so their text and action items "
                    "were deleted from the app. Regenerate to create new minutes from the current transcript. "
                    "Copies you saved earlier are not affected.", "Regenerate")
        except StorageError as exc:
            self.error_banner.show_message("bad", "History is busy", "Try again in a moment.", "", exc.code)

    def _key_ready(self) -> bool:
        try:
            return self.service.key_status().configured
        except (CredentialError, OSError):
            return False

    def _show_page(self) -> None:
        job = self.service.running_job(self.meeting_id) if self.meeting_id else None
        key = self._key_ready()
        if job is not None:
            self.pages.setCurrentWidget(self.running_page)
            self._show_step(job)
            return
        has_versions = self.version_id is not None
        self.pages.setCurrentWidget(self.doc_page if has_versions else self.empty_page)
        busy = self.service.running_job() is not None
        self.generate_button.setEnabled(self.meeting_id is not None and key and not busy)
        self.no_key_label.setVisible(not key)
        self.settings_button.setVisible(not key)
        self.regenerate_button.setEnabled(self.meeting_id is not None and key and not busy)
        estimate = ""
        if key and self.meeting_id is not None and self.revision is not None:
            estimate = estimate_text(self.service.estimate(self.meeting_id, self.revision), self.service.model_id())
        self.estimate_label.setText(estimate)
        self.estimate_label.setVisible(bool(estimate))
        tip = f"{GENERATE_NOTICE}\n{estimate}".strip() if key else NO_KEY_TEXT
        self.regenerate_button.setToolTip(tip)
        self.generate_button.setToolTip(tip)
        self.incomplete_empty.setText(self._incomplete_text())
        self.incomplete_empty.setVisible(bool(self.incomplete_empty.text()))
        if has_versions:
            self._render_document()

    def _incomplete_text(self) -> str:
        if self.meeting_id is None:
            return ""
        try:
            snap = self.service.store.snapshot(self.meeting_id, wait_s=UI_WAIT_S)
        except StorageError:
            return ""
        if snap is None:
            return ""
        gaps = sum(1 for t in snap.timeline if t.metadata.get("loss") or (t.kind == "error"
                                                                          and t.code != "event_log_overflow"))
        if not gaps:
            return ""
        return f"The transcript has {gaps} interval(s) without text. Minutes can't include what was said then."

    # -------------------------------------------------------------- document

    def _render_document(self) -> None:
        doc = self.document
        self._clear_sections()
        self.item_labels.clear()
        self.item_edit_buttons.clear()
        self.evidence_buttons.clear()
        can_view = doc is not None
        for widget in (self.edit_button, self.review_button, self.copy_button, self.save_button):
            widget.setEnabled(can_view)
        self.caution_label.setVisible(can_view and doc.version.state != "approved")
        if doc is None:
            self.state_chip.setText("Removed")
            _set_prop(self.state_chip, "chip", "warn")
            self.meta_label.setText("")
            self.stale_banner.hide()
            self.incomplete_banner.hide()
            return
        v = doc.version
        state_tone = {"draft": "draft", "review": "warn", "approved": "ok"}[v.state]
        self.state_chip.setText(STATE_LABEL[v.state])
        _set_prop(self.state_chip, "chip", state_tone)
        approved = v.state == "approved"
        self.edit_button.setText("Done editing" if self.editing else "Edit")
        self.edit_button.setVisible(not approved)
        self.review_button.setText({"draft": "Start review", "review": "Approve",
                                    "approved": "Edit as new draft"}[v.state])
        self.meta_label.setText(
            f"Generated {local_time(v.created_at)} from transcript revision {v.transcript_revision} · {v.provider} "
            f"{v.model_id} · {usage_text(v)} · Regenerate sends the saved transcript to OpenAI; API charges apply.")
        if doc.stale:
            self.stale_banner.show_message(
                "warn", "The transcript changed after these minutes were generated",
                f"Saved revision {doc.meeting.transcript_revision} is newer than revision {v.transcript_revision} "
                "used here. Regenerate to create a new draft from the current transcript — this version and your "
                "edits are kept.", "Regenerate")
            self.stale_banner.action.setEnabled(self._key_ready())
        else:
            self.stale_banner.hide()
        if doc.gaps or v.incomplete:
            self.incomplete_banner.show_message(
                "info", "Incomplete source",
                f"The transcript has {doc.gaps} interval(s) without text. Minutes can't include what was said then.")
        else:
            self.incomplete_banner.hide()
        editable = self.editing and not approved
        for kind in SECTION_ORDER:
            self.sections_layout.addWidget(_label(SECTION_TITLES[kind], "h2"))
            items = doc.of(kind)
            if kind == "action":
                self._add_action_summary(items)
            elif not items:
                self.sections_layout.addWidget(_label("None recorded.", "sub"))
            for item in items if kind != "action" else []:
                self.sections_layout.addWidget(self._item_row(item, editable))
            if editable and kind != "action":
                add = _button(f"Add to {SECTION_TITLES[kind].lower()}", "subtle")
                add.clicked.connect(lambda _=False, k=kind: self.add_item(k))
                self.sections_layout.addWidget(add, 0, Qt.AlignmentFlag.AlignLeft)
        if v.warnings:
            self.sections_layout.addWidget(_label("Warnings from generation", "h2"))
            for warning in v.warnings:
                self.sections_layout.addWidget(_content_label(f"• {warning}"))
        self.sections_layout.addStretch(1)

    def _clear_sections(self) -> None:
        while self.sections_layout.count():
            child = self.sections_layout.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.deleteLater()

    def _item_text(self, item: MinutesItemRow) -> str:
        if item.kind == "topic":
            return f"{item.text} — {item.detail}" if item.detail else item.text
        if item.kind == "decision":
            return f"[{DECISION_LABEL.get(item.status or '', 'Uncertain')}] {item.text}"
        if item.kind == "question":
            return item.text + (f" (owner: {item.owner})" if item.owner else "")
        return item.text

    def _item_row(self, item: MinutesItemRow, editable: bool) -> QWidget:
        row = QWidget()
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 2, 0, 2)
        label = _content_label(self._item_text(item))
        if item.kind == "decision" and item.status == "superseded":
            font = label.font()
            font.setStrikeOut(True)
            label.setFont(font)
        self.item_labels[item.id] = label
        layout.addWidget(label)
        chips = QHBoxLayout()
        self._add_evidence(chips, item)
        if editable:
            edit = QToolButton()
            edit.setText("Edit")
            edit.setAccessibleName("Edit item")
            edit.clicked.connect(lambda _=False, i=item: self.edit_item(i))
            self.item_edit_buttons[item.id] = edit
            chips.addWidget(edit)
        chips.addStretch(1)
        layout.addLayout(chips)
        return row

    def _add_evidence(self, layout: QHBoxLayout, item: MinutesItemRow) -> None:
        if item.origin == "user" and not item.evidence:
            chip = QLabel("Added by you")
            chip.setProperty("chip", "info")
            layout.addWidget(chip)
            return
        for ev in item.evidence:
            button = QToolButton()
            button.setText(evidence_label(ev))
            button.setAccessibleName(f"Evidence: {evidence_label(ev)}")
            button.setToolTip("Show this line in the transcript")
            button.setProperty("segment_id", ev["segment_id"])
            button.clicked.connect(lambda _=False, s=ev["segment_id"]: self.evidence_requested.emit(s))
            self.evidence_buttons.append(button)
            layout.addWidget(button)

    def _add_action_summary(self, items: list[MinutesItemRow]) -> None:
        if not items:
            self.sections_layout.addWidget(_label("None recorded.", "sub"))
            return
        for item in items:
            row = QWidget()
            layout = QVBoxLayout(row)
            layout.setContentsMargins(0, 2, 0, 2)
            label = _content_label(f"{item.text} — Owner: {item.owner or NOT_SPECIFIED} · Due: {due_text(item)} · "
                                   f"{COMMITMENT_LABEL.get(item.commitment or '', 'Unclear')}")
            self.item_labels[item.id] = label
            layout.addWidget(label)
            chips = QHBoxLayout()
            self._add_evidence(chips, item)
            chips.addStretch(1)
            layout.addLayout(chips)
            self.sections_layout.addWidget(row)
        link = _button(f"Review all {len(items)} in Action items", "subtle")
        link.clicked.connect(self.show_actions_requested.emit)
        self.sections_layout.addWidget(link, 0, Qt.AlignmentFlag.AlignLeft)

    # --------------------------------------------------------------- actions

    def set_action_filter(self, name: str) -> None:
        self.action_filter = name
        self.filter_buttons[name].setChecked(True)
        self._render_actions()

    def _matches(self, item: MinutesItemRow) -> bool:
        return {"All": True, "Committed": item.commitment == "explicit",
                "Suggested or unclear": item.commitment in ("proposed", "uncertain"),
                "Needs review": not item.reviewed}[self.action_filter]

    def _render_actions(self) -> None:
        doc = self.document
        items = doc.of("action") if doc else []
        counts = {"All": len(items), "Committed": sum(i.commitment == "explicit" for i in items),
                  "Suggested or unclear": sum(i.commitment in ("proposed", "uncertain") for i in items),
                  "Needs review": sum(not i.reviewed for i in items)}
        for name, button in self.filter_buttons.items():
            button.setText(f"{name} ({counts[name]})")
        self.action_rows = [i for i in items if self._matches(i)]
        table = self.action_table
        table.setRowCount(0)
        editable = doc is not None and doc.version.state != "approved"
        self.action_edit_button.setEnabled(editable)
        self.action_add_button.setEnabled(editable)
        if doc is None:
            self.actions_empty.setText(NO_KEY_TEXT if not self._key_ready() and self.version_id is None else
                                       "No minutes yet. Generate MoM on the Minutes tab first."
                                       if self.version_id is None else "These minutes were removed.")
            self.actions_empty.show()
            return
        self.actions_empty.setText("" if items else "No action items in this version.")
        self.actions_empty.setVisible(not items)
        table.setRowCount(len(self.action_rows))
        for r, item in enumerate(self.action_rows):
            cells = [item.text, item.owner or NOT_SPECIFIED, due_text(item),
                     COMMITMENT_LABEL.get(item.commitment or "", "Unclear"),
                     "Confirmed" if item.reviewed else "Needs review"]
            for c, text in enumerate(cells):
                cell = QTableWidgetItem(text)
                if c == 0 and is_rtl(text):
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if text.startswith(NOT_SPECIFIED):
                    font = cell.font()
                    font.setItalic(True)
                    cell.setFont(font)
                table.setItem(r, c, cell)
            chips = QWidget()
            chip_row = QHBoxLayout(chips)
            chip_row.setContentsMargins(2, 0, 2, 0)
            self._add_evidence(chip_row, item)
            table.setCellWidget(r, 5, chips)
        table.resizeRowsToContents()

    def edit_selected_action(self) -> None:
        row = self.action_table.currentRow()
        if 0 <= row < len(self.action_rows):
            self.edit_action_row(row)

    def edit_action_row(self, row: int) -> None:
        if self.document is None or self.document.version.state == "approved":
            return
        item = self.action_rows[row]
        dialog = ActionEditDialog(self.actions_tab, item)
        if not self.run_dialog(dialog):
            return
        if dialog.removed:
            self._change(lambda: self.service.remove_item(item.id), "Action item removed from this draft.")
        else:
            self._change(lambda: self.service.edit_item(item.id, **dialog.values()),
                         "Action item saved in this draft.")

    def add_action(self) -> None:
        if self.document is None or self.document.version.state == "approved":
            return
        dialog = ActionEditDialog(self.actions_tab)
        if self.run_dialog(dialog):
            values = dialog.values()
            text = values.pop("text")
            self._change(lambda: self._add_action(text, values), "Action item added to this draft.")

    def _add_action(self, text: str, values: dict[str, Any]) -> None:
        item_id = self.service.add_item(self.document.version.id, "action", text)
        self.service.edit_item(item_id, **values)

    # ---------------------------------------------------------------- edits

    def toggle_edit(self) -> None:
        self.editing = not self.editing and self.document is not None and self.document.version.state != "approved"
        self._render_document()

    def edit_item(self, item: MinutesItemRow) -> None:
        if item.kind == "action":
            row = next((r for r, i in enumerate(self.action_rows) if i.id == item.id), None)
            if row is not None:
                self.edit_action_row(row)
            return
        dialog = ItemEditDialog(self.minutes_tab, item.kind, item)
        if not self.run_dialog(dialog):
            return
        if dialog.removed:
            self._change(lambda: self.service.remove_item(item.id), "Item removed from this draft.")
        else:
            self._change(lambda: self.service.edit_item(item.id, **dialog.values()), "Saved in this draft.")

    def add_item(self, kind: str) -> None:
        if self.document is None:
            return
        dialog = ItemEditDialog(self.minutes_tab, kind)
        if self.run_dialog(dialog):
            values = dialog.values()
            text = values.pop("text")
            self._change(lambda: self.service.add_item(self.document.version.id, kind, text, **values),
                         "Item added to this draft.")

    def advance_state(self) -> None:
        doc = self.document
        if doc is None:
            return
        v = doc.version
        if v.state == "draft":
            self._change(lambda: self.service.start_review(v.id), "Minutes are in review.")
        elif v.state == "review":
            unspecified = self.service.unspecified_actions(v.id)
            text = "Approved minutes become read-only. To change them later, you'll edit a new draft version."
            if unspecified:
                text += (f" {unspecified} action item(s) still have an owner or due date “Not specified”. That's "
                         "fine if the meeting didn't decide them.")
            if self.confirm(self.minutes_tab, "Approve these minutes?", text, "Approve"):
                self.editing = False
                self._change(lambda: self.service.approve(v.id), "Minutes approved.")
        else:
            try:
                self.version_id = self.service.edit_as_new_draft(v.id)
            except (MinutesRejected, StorageError) as exc:
                self._refused(exc)
                return
            self.editing = True
            self._notify("A new draft version was created from the approved minutes.")
            self.refresh()

    def _change(self, action: Callable[[], Any], done: str) -> None:
        try:
            action()
        except (MinutesRejected, StorageError) as exc:
            self._refused(exc)
            return
        except ValueError:
            self.error_banner.show_message("bad", "That change couldn't be saved",
                                           "Check the text and date, then try again.")
            return
        self._notify(done)
        self.refresh()

    def _refused(self, exc: Exception) -> None:
        code = getattr(exc, "code", "failed")
        title = {"invalidated": "These minutes were removed", "read_only": "Approved minutes are read-only"}.get(
            code, "That change couldn't be saved")
        self.error_banner.show_message("bad", title, "Nothing was changed.", "", code)
        self.refresh()

    def _notify(self, text: str) -> None:
        self.last_notice = text
        self.error_banner.hide()
        self.notice_banner.show_message("ok", text)

    def _version_chosen(self, index: int) -> None:
        self.version_id = self.version_combo.itemData(index)
        self.editing = False
        self.refresh()

    # ----------------------------------------------------------- copy/export

    def copy(self) -> None:
        if self.document is None:
            return
        try:
            text = self.service.export_text(self.document.version.id, "txt")
        except (MinutesUnavailable, StorageError) as exc:
            self._refused(exc)
            return
        self.clipboard(text)
        self._notify("Minutes copied as plain text, including Draft/Approved status.")

    def save_as(self) -> None:
        if self.document is None:
            return
        version_id = self.document.version.id
        try:
            suggested = self.service.suggested_filename(version_id, "md")
        except (MinutesUnavailable, StorageError) as exc:
            self._refused(exc)
            return
        answer = self.save_dialog(self.minutes_tab, suggested)
        if not answer:
            return
        path, chosen = answer
        target = Path(path)
        suffix = target.suffix.lower()
        if suffix in (".md", ".markdown", ".txt"):
            fmt = "txt" if suffix == ".txt" else "md"
        else:
            fmt = "txt" if chosen.startswith("Text") else "md"
            target = target.with_name(f"{target.name}.{fmt}")
        try:
            self.service.save_as(version_id, target, fmt)
        except ExportError as exc:
            self.error_banner.show_message("bad", "The minutes file wasn't saved",
                                           "Choose another folder or name, then try again.", "", exc.code)
            return
        except (MinutesUnavailable, StorageError) as exc:
            self._refused(exc)
            return
        self._notify(f"Saved a copy of the minutes: {target}")

    # ------------------------------------------------------------ generation

    def generate(self) -> None:
        if self.meeting_id is None or self.revision is None:
            return
        self.error_banner.hide()
        self.notice_banner.hide()
        if not self._key_ready():
            self._show_error(GenerationError("no_key", request_sent=False))
            return
        try:
            self.service.start(self.meeting_id, self.revision, on_progress=self._job_event.emit,
                               on_finished=self._job_event.emit)
        except GenerationError as exc:
            self._show_error(exc)
            return
        self.editing = False
        self._show_page()
        self._emit_badges()

    def cancel(self) -> None:
        job = self.service.running_job(self.meeting_id)
        if job is not None:
            job.cancel()
            self.step_label.setText("Cancelling…")
            self.cancel_button.setEnabled(False)

    def _show_step(self, job: GenerationJob) -> None:
        step, i, n = job.step
        self.cancel_button.setEnabled(not job.cancel_event.is_set())
        text = STEP_TEXT.get(step, "").format(rev=job.revision, i=max(1, i), n=max(1, n))
        if job.cancel_event.is_set():
            text = "Cancelling…"
        self.step_label.setText(text)
        if n:
            self.progress.setRange(0, n * 2)
            self.progress.setValue(min(n * 2, (i - 1) * 2 + (2 if step == "validating" else 1)))
        else:
            self.progress.setRange(0, 0)

    def _on_job_event(self, job: GenerationJob) -> None:
        if not job.done.is_set():
            if job.meeting_id == self.meeting_id:
                self._show_step(job)
            return
        if job.meeting_id != self.meeting_id:
            self._emit_badges()
            return
        if job.state == "done":
            self.version_id = job.version_id
            self._notify("A new draft was created. Check it against the transcript before sharing.")
        elif job.error is not None:
            self._show_error(job.error)
        self.refresh()
        self.generation_finished.emit(job.state)

    def _show_error(self, error: GenerationError) -> None:
        title, body, action = error_copy(error, self.service.model_id())
        self.notice_banner.hide()
        self.error_banner.show_message("bad", title, body, action, error.detail or error.code)
        self.error_banner.setProperty("code", error.code)

    def _banner_action(self, action: str) -> None:
        if action == "Open OpenAI settings":
            self.open_settings_requested.emit()
        elif action in ("Try again", "Generate MoM", "Regenerate"):
            self.generate()

    def _emit_badges(self) -> None:
        try:
            minutes, actions = self.service.badge(self.meeting_id)
        except StorageError:
            minutes, actions = "", ""
        self.badges_changed.emit(minutes, actions)
