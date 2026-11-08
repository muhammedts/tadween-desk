"""Minutes widgets, offscreen: Settings › OpenAI minutes and the Minutes /
Action items tabs, driven with a fake provider, injected dialogs and synthetic meetings.
Not covered: real rendering/DPI/screen readers and a live OpenAI request (NOT RUN)."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import threading  # noqa: E402
import time  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLineEdit  # noqa: E402

from meeting_minutes.minutes.credentials import MemoryCredentialStore  # noqa: E402
from meeting_minutes.minutes.provider import FakeMinutesGenerator, GenerationError  # noqa: E402
from meeting_minutes.minutes.schema import empty_content  # noqa: E402
from meeting_minutes.minutes.service import MinutesService  # noqa: E402
from meeting_minutes.storage.store import TranscriptStore  # noqa: E402
from meeting_minutes.ui.minutes_view import (  # noqa: E402
    GENERATE_NOTICE,
    ActionEditDialog,
    MinutesWorkspace,
    error_copy,
    readiness_text,
)
from meeting_minutes.ui.openai_settings import OpenAISettingsSection  # noqa: E402

KEY = "sk-ui-canary-3c9d-NOT-REAL"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def make_meeting(store: TranscriptStore, lines: list[tuple[str, str]], meeting_id: str = "m1") -> list[str]:
    store.create_meeting(meeting_id, title="Design review", timezone_name="EET")
    for source in ("mic", "system"):
        store.save_epoch(meeting_id, source=source, epoch=1, device_id=source, device_label=source,
                         native_rate=48_000, native_channels=1, start_ms=0, end_ms=None)
    ids = [store.add_final(meeting_id, source=s, epoch=1, start_ms=i * 1000, end_ms=i * 1000 + 800, text=t,
                           language="en", engine_id="fake", model_hash="0" * 64) for i, (s, t) in enumerate(lines)]
    store.set_status(meeting_id, "stopped", ended=True)
    store.add_timeline(meeting_id, "notice", "finalized", 9000, metadata={"complete": True})
    return ids


LINES = [("mic", "We will ship the beta on Friday"), ("system", "ممكن حد يراجع التصميم؟"),
         ("mic", "Nadia agreed to update the icons")]


def model(envelope, repair=None):
    seg = envelope["segments"]

    def ev(i: int) -> list[dict]:
        return [{"segment_id": seg[i]["id"], "revision": seg[i]["revision"]}]

    out = empty_content()
    out["summary"] = [{"text": "Beta timing and design review", "evidence": ev(0)}]
    out["topics"] = [{"title": "مراجعة التصميم", "discussion": "اقتراح بدون التزام", "evidence": ev(1)}]
    out["action_items"] = [
        {"task": "Update the icons", "owner": "Nadia", "due_date": None, "due_date_text": None,
         "date_status": "unspecified", "commitment": "explicit", "evidence": ev(2)},
        {"task": "مراجعة التصميم", "owner": None, "due_date": None, "due_date_text": None,
         "date_status": "unspecified", "commitment": "proposed", "evidence": ev(1)},
    ]
    return out


class Rig:
    def __init__(self, tmp_path: Path, *, key: bool = True, generator=None) -> None:
        self.store = TranscriptStore(tmp_path / "m.db")
        self.ids = make_meeting(self.store, LINES)
        self.creds = MemoryCredentialStore()
        if key:
            self.creds.save(KEY)
        self.gen = generator or FakeMinutesGenerator(model)
        self.service = MinutesService(self.store, self.creds, lambda c: self.gen)
        self.saved: list[tuple[str, str]] = []
        self.copied: list[str] = []
        self.confirms: list[str] = []
        self.dialog_hook: Callable | None = None
        self.save_answer: tuple[str, str] | None = None
        self.ws = MinutesWorkspace(self.service, save_dialog=self._save, confirm=self._confirm,
                                   clipboard=self.copied.append, run_dialog=self._run)
        self.badges: list[tuple[str, str]] = []
        self.evidence: list[str] = []
        self.finished: list[str] = []
        self.ws.generation_finished.connect(self.finished.append)
        self.ws.badges_changed.connect(lambda a, b: self.badges.append((a, b)))
        self.ws.evidence_requested.connect(self.evidence.append)
        self.ws.minutes_tab.show()
        self.ws.actions_tab.show()

    def _save(self, parent, suggested):
        self.saved.append(suggested)
        return self.save_answer

    def _confirm(self, parent, title, text, accept):
        self.confirms.append(f"{title} {text}")
        return True

    def _run(self, dialog):
        if self.dialog_hook is None:
            return False
        return self.dialog_hook(dialog)

    def open(self) -> None:
        self.ws.set_meeting("m1", self.store.meeting("m1").transcript_revision)

    def generate(self) -> str:
        before = len(self.finished)
        on_empty = self.ws.pages.currentWidget() is self.ws.empty_page
        (self.ws.generate_button if on_empty else self.ws.regenerate_button).click()
        wait_until(lambda: len(self.finished) > before)
        return self.finished[-1]


def test_without_a_key_generate_is_unavailable_and_explains(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path, key=False)
    rig.open()
    ws = rig.ws
    assert ws.pages.currentWidget() is ws.empty_page and not ws.generate_button.isEnabled()
    assert ws.no_key_label.isVisible() and "Settings › OpenAI minutes" in ws.no_key_label.text()
    assert ws.notice_label.text() == GENERATE_NOTICE
    assert GENERATE_NOTICE.startswith("Sends this meeting's saved transcript and related details to OpenAI")
    assert all(w in GENERATE_NOTICE for w in ("title", "date", "time zone", "line times", "gap or recognition marks"))
    assert rig.badges[-1] == ("Not generated", "")
    opened = []
    ws.open_settings_requested.connect(lambda: opened.append(True))
    ws.settings_button.click()
    assert opened == [True]
    assert "No minutes yet" in ws.actions_empty.text() or "Settings" in ws.actions_empty.text()
    assert "not set up" in readiness_text(rig.service)


def test_generate_view_evidence_and_actions(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.open()
    ws = rig.ws
    assert ws.generate_button.isEnabled()
    assert rig.generate() == "done"
    assert ws.pages.currentWidget() is ws.doc_page and ws.state_chip.text() == "Draft — not reviewed"
    assert rig.badges[-1] == ("Draft", "2") and "key saved" in readiness_text(rig.service)
    assert "100 input / 20 output tokens" in ws.meta_label.text() and "transcript revision 3" in ws.meta_label.text()
    arabic = next(lbl for lbl in ws.item_labels.values() if "مراجعة التصميم" in lbl.text())
    assert arabic.layoutDirection() == Qt.LayoutDirection.RightToLeft
    english = next(lbl for lbl in ws.item_labels.values() if lbl.text().startswith("Beta timing"))
    assert english.layoutDirection() == Qt.LayoutDirection.LeftToRight
    chip = next(b for b in ws.evidence_buttons if b.text() == "00:02 Mic")
    assert chip.accessibleName() == "Evidence: 00:02 Mic"
    chip.click()
    assert rig.evidence == [rig.ids[2]]
    # Action items tab
    assert ws.filter_buttons["All"].text() == "All (2)" and ws.action_table.rowCount() == 2
    row = [ws.action_table.item(1, c).text() for c in range(5)]
    assert row == ["مراجعة التصميم", "Not specified", "Not specified", "Suggested — not agreed", "Needs review"]
    ws.set_action_filter("Committed")
    assert ws.action_table.rowCount() == 1 and ws.action_table.item(0, 0).text() == "Update the icons"
    ws.set_action_filter("All")

    def edit(dialog: ActionEditDialog) -> bool:
        assert dialog.no_due.isChecked() and not dialog.due.isEnabled()
        dialog.owner.setText("  ")
        dialog.no_due.setChecked(False)
        dialog.due.setDate(dialog.due.date().fromString("2026-11-20", "yyyy-MM-dd"))
        dialog.commitment.setCurrentIndex(dialog.commitment.findData("explicit"))
        dialog.reviewed.setChecked(True)
        return True

    rig.dialog_hook = edit
    ws.edit_action_row(1)
    row = [ws.action_table.item(1, c).text() for c in range(5)]
    assert row == ["مراجعة التصميم", "Not specified", "2026-11-20", "Committed", "Confirmed"]
    assert ws.last_notice == "Action item saved in this draft."
    assert "(your edits)" in ws.version_combo.currentText()


def test_review_approve_copy_save_and_new_draft(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.open()
    assert rig.generate() == "done"
    ws = rig.ws
    ws.review_button.click()
    assert ws.state_chip.text() == "In review" and ws.review_button.text() == "Approve"
    ws.review_button.click()
    assert "Approve these minutes?" in rig.confirms[0] and "2 action item(s)" in rig.confirms[0]
    assert ws.state_chip.text() == "Approved" and not ws.edit_button.isVisible()
    assert ws.review_button.text() == "Edit as new draft" and not ws.action_edit_button.isEnabled()
    ws.copy_button.click()
    assert rig.copied and "Approved · Version 1" in rig.copied[0]
    target = tmp_path / "out"
    rig.save_answer = (str(target), "Markdown (*.md)")
    ws.save_button.click()
    assert rig.saved[0].endswith("— minutes (approved).md")
    assert (tmp_path / "out.md").read_text(encoding="utf-8").startswith("# Design review — minutes")
    ws.review_button.click()  # Edit as new draft
    assert ws.state_chip.text() == "Draft — not reviewed" and ws.version_combo.count() == 2
    assert rig.badges[-1][0] == "Draft"


def test_regenerate_after_a_correction_and_stale_banner(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.open()
    assert rig.generate() == "done"
    rig.store.correct_segment(rig.ids[0], "We will ship the beta on Monday")
    rig.ws.refresh()
    assert rig.ws.stale_banner.isVisible() and "Saved revision 4 is newer than revision 3" in \
        rig.ws.stale_banner.body.text()
    assert rig.badges[-1][0] == "Stale"
    rig.ws.set_meeting("m1", 4)
    assert rig.generate() == "done"
    assert rig.ws.version_combo.count() == 2 and "latest" in rig.ws.version_combo.currentText()
    assert not rig.ws.stale_banner.isVisible()


def test_redaction_removes_the_minutes_from_view(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.open()
    assert rig.generate() == "done"
    rig.store.redact_segment(rig.ids[1])
    rig.ws.set_meeting("m1", rig.store.meeting("m1").transcript_revision)
    ws = rig.ws
    assert ws.removed_banner.isVisible() and ws.removed_banner.title.text() == "These minutes were removed"
    assert not ws.copy_button.isEnabled() and not ws.save_button.isEnabled() and ws.item_labels == {}
    ws.copy()
    assert rig.copied == []
    assert ws.action_table.rowCount() == 0 and rig.badges[-1] == ("Not generated", "")


def test_redaction_while_showing_blocks_copy_and_save_of_the_cached_view(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.open()
    assert rig.generate() == "done"
    rig.store.redact_segment(rig.ids[0])  # the view still shows the old text until refreshed
    rig.save_answer = (str(tmp_path / "late.md"), "Markdown (*.md)")
    rig.ws.copy()
    rig.ws.save_as()
    assert rig.copied == [] and not (tmp_path / "late.md").exists()
    assert rig.ws.error_banner.title.text() == "These minutes were removed" and rig.ws.item_labels == {}


def test_edit_mode_edits_adds_and_removes_items(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.open()
    assert rig.generate() == "done"
    ws = rig.ws
    assert ws.item_edit_buttons == {}
    ws.edit_button.click()
    assert ws.edit_button.text() == "Done editing" and ws.item_edit_buttons
    summary_id = next(i for i, lbl in ws.item_labels.items() if lbl.text().startswith("Beta timing"))

    def change_text(dialog) -> bool:
        dialog.text.setPlainText("Beta moved; design review pending")
        return True

    rig.dialog_hook = change_text
    ws.item_edit_buttons[summary_id].click()
    assert ws.item_labels[summary_id].text() == "Beta moved; design review pending"

    def add_decision(dialog) -> bool:
        dialog.text.setPlainText("Keep the current icon set")
        dialog.status.setCurrentIndex(dialog.status.findData("agreed"))
        return True

    rig.dialog_hook = add_decision
    ws.add_item("decision")
    assert any(lbl.text() == "[Agreed] Keep the current icon set" for lbl in ws.item_labels.values())

    def remove(dialog) -> bool:
        dialog._remove()
        return True

    rig.dialog_hook = remove
    ws.item_edit_buttons[summary_id].click()
    assert summary_id not in ws.item_labels and "(your edits)" in ws.version_combo.currentText()


def test_progress_cancel_and_error_banners(app, tmp_path: Path) -> None:
    gate = threading.Event()
    rig = Rig(tmp_path, generator=FakeMinutesGenerator(model, gate=gate))
    rig.open()
    ws = rig.ws
    ws.generate_button.click()
    wait_until(lambda: ws.pages.currentWidget() is ws.running_page and "part 1 of 1" in ws.step_label.text())
    assert rig.badges[-1][0] == "Generating…"
    ws.cancel_button.click()
    wait_until(lambda: rig.finished == ["cancelled"])
    assert ws.error_banner.title.text() == "Generation cancelled"
    assert "may still be charged" in ws.error_banner.body.text()
    assert ws.pages.currentWidget() is ws.empty_page


def offline(envelope, repair=None):
    raise GenerationError("offline", request_sent=False, detail="gaierror")


def test_failure_wording_for_pre_send_and_unknown_failures(app, tmp_path: Path) -> None:
    rig = Rig(tmp_path, generator=FakeMinutesGenerator(offline))
    rig.open()
    assert rig.generate() == "failed"
    body = rig.ws.error_banner.body.text()
    assert rig.ws.error_banner.title.text() == "Can't reach OpenAI" and "No request was made." in body
    assert body.endswith("Your transcript and current minutes are unchanged.")
    assert "gaierror" in rig.ws.error_banner.details.text()
    unknown = error_copy(GenerationError("offline", request_sent=None))[1]
    sent = error_copy(GenerationError("timeout", request_sent=True))[1]
    assert "No request was made" not in unknown and "may have reached OpenAI" in unknown
    assert "No request was made" not in sent and "may be charged" in sent
    assert "No request was made." in error_copy(GenerationError("no_key", request_sent=False))[1]
    for code in ("auth", "model_unavailable", "quota", "rate_limit", "refusal", "invalid_output", "too_large",
                 "server_error", "request_rejected", "transcript_invalidated"):
        title, text, action = error_copy(GenerationError(code, request_sent=True), "my-model")
        assert title and action and "No request was made" not in text


def test_settings_section_save_replace_remove_and_model(app, tmp_path: Path, block_network) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    creds = MemoryCredentialStore()
    service = MinutesService(store, creds, lambda c: FakeMinutesGenerator())
    confirms = []
    section = OpenAISettingsSection(service, confirm=lambda *a: confirms.append(a[1]) or True)
    section.show()
    assert section.status_chip.text() == "Not configured" and section.entry.isVisible()
    assert section.key_field.echoMode() == QLineEdit.EchoMode.Password
    section.save_key()
    assert section.result.title.text() == "Paste a key first"
    section.key_field.setText(KEY)
    section.save_button.click()
    assert creds.get() == KEY and section.key_field.text() == ""
    assert section.status_chip.text() == "Configured" and section.saved_block.isVisible()
    assert KEY not in section.saved_label.text() and "•" in section.saved_label.text()
    assert section.result.title.text() == "Key saved" and "No request was made." in section.result.body.text()
    section.replace_button.click()
    assert section.entry.isVisible() and section.save_button.text() == "Save new key"
    section.key_field.setText(KEY + "B")
    section.save_key()
    assert creds.get() == KEY + "B" and section.saved_block.isVisible()
    section.remove_button.click()
    assert confirms == ["Remove the OpenAI key?"] and not creds.exists()
    assert section.result.title.text() == "Key removed" and section.status_chip.text() == "Not configured"
    section.model_field.setText("my-project-model")
    section.model_button.click()
    assert service.model_id() == "my-project-model" and section.result.title.text() == "Model saved"
    section.model_field.setText("bad id with spaces")
    section.save_model()
    assert service.model_id() == "my-project-model" and section.result.title.text() == "Enter a model ID"
    assert block_network == []
    store.close()
    assert KEY.encode() not in b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("m.db"))


def test_settings_section_unavailable_vault(app, tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    service = MinutesService(store, MemoryCredentialStore(unavailable=True), lambda c: FakeMinutesGenerator())
    section = OpenAISettingsSection(service)
    section.key_field.setText(KEY)
    section.save_key()
    assert section.result.title.text() == "The key was not saved"
    assert "nothing was stored" in section.result.body.text() and section.key_field.text() == ""
    assert section.status_chip.text() == "Not configured"
    store.close()


def test_estimate_line_budget_refusal_and_cost_settings(app, tmp_path: Path, block_network) -> None:
    """The estimate is shown before sending; an over-limit generation is refused
    with request_sent=False; prices and the limit are visible and editable in Settings."""
    rig = Rig(tmp_path)
    rig.open()
    ws = rig.ws
    text = ws.estimate_label.text()
    assert text.startswith("Estimate: 1 request, about ") and "your limit $1.00" in text
    assert "with 5 allowed retries and repairs" in text and "not an OpenAI bill" in text
    section = OpenAISettingsSection(rig.service)
    assert (section.price_in_field.text(), section.price_out_field.text(), section.budget_field.text()) == \
        ("1", "5", "1.00")
    section.budget_field.setText("0.01")
    section.cost_button.click()
    assert rig.service.budget_usd() == 0.01 and section.result.title.text() == "Cost settings saved"
    rig.open()
    assert rig.generate() == "failed"
    assert ws.error_banner.property("code") == "budget" and "No request was made" in ws.error_banner.body.text()
    section.price_in_field.setText("2")
    section.price_out_field.setText("")
    section.save_cost()
    assert section.result.title.text() == "Check the numbers" and rig.service.prices() == (1.0, 5.0)
    section.model_field.setText("custom-model")
    section.save_model()
    assert section.price_in_field.text() == "" and section.price_in_field.placeholderText() == "Unknown"
    rig.open()
    assert "cost unknown: no price is set for “custom-model”" in ws.estimate_label.text()
    section.budget_field.setText("")
    section.save_cost()
    assert rig.service.budget_usd() is None
    rig.open()
    assert "no limit set in this app" in ws.estimate_label.text()
    assert block_network == []
