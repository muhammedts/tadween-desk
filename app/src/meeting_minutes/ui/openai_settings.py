"""Settings › OpenAI minutes.

Masked key entry with Save/Replace/Remove, configured status, model id and minutes
language. Saving or removing the key only touches Windows Credential Manager: no
request is made. The key text is never displayed, logged or kept in the widget after
Save; the configured state shows only dots and the saved time. No connection test is
offered, because a test request could cause a charge the user did not ask for.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from meeting_minutes.minutes.credentials import CredentialError
from meeting_minutes.minutes.service import LANGUAGES, MinutesService
from meeting_minutes.storage.store import StorageError
from meeting_minutes.ui.minutes_view import Banner, _button, _label, _set_prop, default_confirm
from meeting_minutes.ui.widgets import ComboBox

INTRO = ("Optional. Used only when you press Generate MoM on a saved meeting. Transcription never uses OpenAI and "
         "works without a key. API charges from your OpenAI account apply to each generation.")
KEY_HELP = ("Stored in Windows Credential Manager for your Windows account. Saving makes no request and sends "
            "nothing.")
MODEL_HELP = ("Use a model that supports structured outputs and is enabled for your API project. The app doesn't "
              "list models — check your OpenAI account.")
COST_HELP = ("Before sending, Generate MoM shows an estimate and refuses to start, or stops before a further "
             "request, if the most it could cost would pass this limit. The estimate counts every part at the "
             "full output limit, plus allowed retries and one repair. It is this app's own estimate from the prices "
             "below, not an OpenAI bill or an account-wide spending limit; set those in your OpenAI account. "
             "Prices for gpt-6.1-terra are from OpenAI's model page (2 Nov 2026); for any other model enter "
             "them yourself. Leave the limit empty for no limit in this app.")
DATA_NOTE = ("Live transcription runs on this PC, and the app does not save or upload meeting audio. If you choose "
             "Generate MoM, the app sends the selected meeting's saved transcript and related details to OpenAI, "
             "including the title, date, time zone, line timestamps, source labels and gap or recognition markers "
             "(never audio, live drafts or other meetings), with store=false. That is not zero retention: OpenAI may keep API data for "
             "abuse monitoring for up to 30 days, and does not train on API data by default. Charges go to your "
             "own OpenAI API project, not to a ChatGPT or Claude subscription.")


class OpenAISettingsSection(QWidget):
    status_changed = Signal()  # key or model changed (refresh readiness lines)

    def __init__(self, service: MinutesService, parent: QWidget | None = None, *,
                 confirm: Callable[[QWidget, str, str, str], bool] | None = None) -> None:
        super().__init__(parent)
        self.service = service
        self.confirm = confirm or default_confirm
        self._replacing = False
        self.setAccessibleName("OpenAI minutes")
        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        header.addWidget(_label("OpenAI minutes", "h2", wrap=False))
        self.status_chip = QLabel("Not configured")
        self.status_chip.setProperty("chip", "neutral")
        header.addWidget(self.status_chip)
        header.addStretch(1)
        layout.addLayout(header)
        layout.addWidget(_label(INTRO, "sub"))

        # key entry (not configured, or replacing)
        self.entry = QWidget()
        entry = QVBoxLayout(self.entry)
        entry.setContentsMargins(0, 0, 0, 0)
        entry.addWidget(_label("OpenAI API key (OPENAI_API_KEY)"))
        row = QHBoxLayout()
        self.key_field = QLineEdit()
        self.key_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_field.setPlaceholderText("sk-…")
        self.key_field.setAccessibleName("OpenAI API key")
        self.key_field.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.key_field.returnPressed.connect(self.save_key)
        self.save_button = _button("Save key", "primary")
        self.save_button.clicked.connect(self.save_key)
        self.cancel_button = _button("Cancel")
        self.cancel_button.clicked.connect(self.cancel_replace)
        row.addWidget(self.key_field, 1)
        row.addWidget(self.save_button)
        row.addWidget(self.cancel_button)
        entry.addLayout(row)
        entry.addWidget(_label(KEY_HELP, "help"))
        layout.addWidget(self.entry)

        # configured block
        self.saved_block = QFrame()
        self.saved_block.setProperty("readonly", True)
        saved = QVBoxLayout(self.saved_block)
        self.saved_label = _label("")
        saved.addWidget(self.saved_label)
        saved.addWidget(_label("Stored in Windows Credential Manager (your Windows account)", "help"))
        buttons = QHBoxLayout()
        self.replace_button = _button("Replace key")
        self.replace_button.clicked.connect(self.start_replace)
        self.remove_button = _button("Remove key", "dangerText")
        self.remove_button.clicked.connect(self.remove_key)
        buttons.addWidget(self.replace_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        saved.addLayout(buttons)
        layout.addWidget(self.saved_block)

        # model and language
        layout.addWidget(_label("Model"))
        model_row = QHBoxLayout()
        self.model_field = QLineEdit()
        self.model_field.setPlaceholderText("Enter a model ID from your OpenAI project")
        self.model_field.setAccessibleName("Model")
        self.model_field.returnPressed.connect(self.save_model)
        self.model_button = _button("Save model")
        self.model_button.clicked.connect(self.save_model)
        model_row.addWidget(self.model_field, 1)
        model_row.addWidget(self.model_button)
        layout.addLayout(model_row)
        layout.addWidget(_label(MODEL_HELP, "help"))
        layout.addWidget(_label("Cost estimate and limit (US dollars)"))
        cost_row = QHBoxLayout()
        self.price_in_field = QLineEdit()
        self.price_in_field.setAccessibleName("Price per 1 million input tokens, US dollars")
        self.price_out_field = QLineEdit()
        self.price_out_field.setAccessibleName("Price per 1 million output tokens, US dollars")
        self.budget_field = QLineEdit()
        self.budget_field.setAccessibleName("Limit per generation, US dollars")
        self.budget_field.setPlaceholderText("No limit")
        for caption, field in (("Input / 1M tokens", self.price_in_field),
                               ("Output / 1M tokens", self.price_out_field),
                               ("Limit per generation", self.budget_field)):
            cost_row.addWidget(_label(caption, wrap=False))
            field.setMaximumWidth(90)
            field.returnPressed.connect(self.save_cost)
            cost_row.addWidget(field)
        self.cost_button = _button("Save")
        self.cost_button.clicked.connect(self.save_cost)
        cost_row.addWidget(self.cost_button)
        cost_row.addStretch(1)
        layout.addLayout(cost_row)
        layout.addWidget(_label(COST_HELP, "help"))
        language_row = QHBoxLayout()
        language_row.addWidget(_label("Minutes language", wrap=False))
        self.language_combo = ComboBox()
        self.language_combo.setAccessibleName("Minutes language")
        for code, name in LANGUAGES.items():
            self.language_combo.addItem(name, code)
        self.language_combo.activated.connect(self.save_language)
        language_row.addWidget(self.language_combo)
        language_row.addStretch(1)
        layout.addLayout(language_row)
        layout.addWidget(_label(DATA_NOTE, "help"))
        self.result = Banner("ok")
        layout.addWidget(self.result)
        self.refresh()

    # ------------------------------------------------------------------ state

    def refresh(self) -> None:
        try:
            status = self.service.key_status()
        except (CredentialError, OSError):
            status = None
        configured = bool(status and status.configured)
        self.status_chip.setText("Configured" if configured else "Not configured")
        _set_prop(self.status_chip, "chip", "ok" if configured else "neutral")
        when = status.saved_at.astimezone().strftime("%d %b %Y, %H:%M") if status and status.saved_at else ""
        self.saved_label.setText("API key  ••••••••••••••••" + (f"  (saved {when})" if when else ""))
        self.saved_block.setVisible(configured and not self._replacing)
        self.entry.setVisible(not configured or self._replacing)
        self.cancel_button.setVisible(self._replacing)
        self.save_button.setText("Save new key" if self._replacing else "Save key")
        self.model_field.setText(self.service.model_id())
        prices = self.service.prices()
        self.price_in_field.setText(f"{prices[0]:g}" if prices else "")
        self.price_out_field.setText(f"{prices[1]:g}" if prices else "")
        for field in (self.price_in_field, self.price_out_field):
            field.setPlaceholderText("Unknown")
        budget = self.service.budget_usd()
        self.budget_field.setText(f"{budget:.2f}" if budget is not None else "")
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(self.service.output_language())))

    def start_replace(self) -> None:
        self._replacing = True
        self.result.hide()
        self.refresh()
        self.key_field.setFocus()

    def cancel_replace(self) -> None:
        self._replacing = False
        self.key_field.clear()
        self.refresh()

    # ---------------------------------------------------------------- actions

    def save_key(self) -> None:
        key = self.key_field.text()
        self.key_field.clear()  # the widget never keeps the key
        try:
            self.service.save_key(key)
        except CredentialError as exc:
            del key
            if exc.code == "empty":
                self.result.show_message("bad", "Paste a key first", "The key field is empty.")
            elif exc.code == "invalid_key":
                self.result.show_message("bad", "That doesn't look like an API key",
                                         "Paste the key on one line without spaces. Nothing was stored.")
            else:
                self.result.show_message(
                    "bad", "The key was not saved",
                    "Windows Credential Manager is unavailable for this account, so nothing was stored. The key "
                    "was not written anywhere else.", "", exc.code)
            return
        del key
        self._replacing = False
        self.result.show_message("ok", "Key saved", "No request was made. The key is used only when you press "
                                                    "Generate MoM.")
        self.refresh()
        self.status_changed.emit()

    def remove_key(self) -> None:
        if not self.confirm(self, "Remove the OpenAI key?",
                            "The key is deleted from Windows Credential Manager. Generate MoM is unavailable until "
                            "you add a key. Existing minutes stay.", "Remove key"):
            return
        try:
            self.service.remove_key()
        except CredentialError as exc:
            self.result.show_message("bad", "The key was not removed",
                                     "Windows Credential Manager could not be changed. Try again.", "", exc.code)
            return
        self.result.show_message("info", "Key removed",
                                 "Generate MoM is unavailable until you add a key. Existing minutes are kept.")
        self.refresh()
        self.status_changed.emit()

    def save_model(self) -> None:
        try:
            model = self.service.set_model_id(self.model_field.text())
        except ValueError:
            self.result.show_message("bad", "Enter a model ID",
                                     "Use the model's ID exactly as your OpenAI project lists it (letters, digits, "
                                     ". _ - :).")
            return
        except StorageError as exc:
            self.result.show_message("bad", "The model wasn't saved", "Try again in a moment.", "", exc.code)
            return
        self.result.show_message("ok", "Model saved", f"Generate MoM will use “{model}”. No request was made.")
        self.refresh()
        self.status_changed.emit()

    def save_cost(self) -> None:
        def number(text: str) -> float | None:
            text = text.strip().lstrip("$").strip()
            return float(text) if text else None

        try:
            price_in, price_out = number(self.price_in_field.text()), number(self.price_out_field.text())
            budget = number(self.budget_field.text())
            if (price_in is None) != (price_out is None):
                raise ValueError("both prices")
            self.service.set_prices(price_in, price_out)
            self.service.set_budget_usd(budget)
        except ValueError:
            self.result.show_message("bad", "Check the numbers",
                                     "Enter both prices (or neither) as US dollars per 1 million tokens, and a limit "
                                     "above 0 (or leave it empty for no limit).")
            return
        except StorageError as exc:
            self.result.show_message("bad", "The cost settings weren't saved", "Try again in a moment.", "", exc.code)
            return
        limit = "no limit in this app" if budget is None else f"limit ${budget:.2f} per generation"
        self.result.show_message("ok", "Cost settings saved", f"{limit.capitalize()}. No request was made.")
        self.refresh()
        self.status_changed.emit()

    def save_language(self) -> None:
        try:
            self.service.set_output_language(self.language_combo.currentData())
        except (ValueError, StorageError):
            self.result.show_message("bad", "The language wasn't saved", "Try again in a moment.")
            return
        self.result.show_message("ok", "Minutes language saved", "")
