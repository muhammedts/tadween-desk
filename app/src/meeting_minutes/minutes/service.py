"""Minutes service: key status, model choice, generation jobs, versions, review, export.

One job at a time runs on a background thread. It reads the key (pre-send checks
report request_sent=False), takes one consistent snapshot of the explicitly selected
saved revision, generates through the provider, then saves a NEW draft version in one
store transaction that refuses the result if the meeting was deleted or redacted
meanwhile. Failures never change the transcript or earlier minutes.
Callbacks run on the worker thread; the UI forwards them through queued Qt signals.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from meeting_minutes.export import is_complete, write_file
from meeting_minutes.minutes.credentials import CredentialError, CredentialStore, WindowsCredentialStore
from meeting_minutes.minutes.openai_provider import DEFAULT_MODEL, KNOWN_PRICES, OpenAIMinutesGenerator
from meeting_minutes.minutes.provider import (
    RECOGNITION_FLAGS,
    CostEstimate,
    GenerationError,
    GenerationOptions,
    GenerationResult,
    GenerationSnapshot,
    MinutesGenerator,
    SnapshotGap,
    SnapshotSegment,
    estimate_cost,
)
from meeting_minutes.minutes.render import render_minutes
from meeting_minutes.storage.store import (
    MeetingRow,
    MinutesItemRow,
    MinutesRejected,
    MinutesVersionRow,
    StorageError,
    TranscriptStore,
    zone_offset,
)
from meeting_minutes.ui.wording import coverage_summary

MODEL_KEY = "minutes.model_id"
LANGUAGE_KEY = "minutes.output_language"
PRICES_KEY = "minutes.prices"  # {model id: [USD per 1M input tokens, per 1M output tokens]} entered by the user
BUDGET_KEY = "minutes.budget_usd"  # local limit per generation (USD); never set = default, "off" = none
DEFAULT_BUDGET_USD = 1.00
MAX_PRICE = 1_000.0
MAX_BUDGET = 1_000.0
UNCERTAIN_WARNING = ("{n} transcript line(s) sent were marked “uncertain recognition” by the speech model, so "
                     "their words may be misheard or invented. {k} generated item(s) cite them (marked "
                     "“uncertain”); check those against the transcript before relying on them.")
UNKNOWN_DATE_WARNING = ("This meeting's original date and time zone aren't recorded (an older recording), so "
                        "relative deadlines such as “tomorrow” were left for you to set.")
LANGUAGES = {"en": "English", "ar": "Arabic"}
FINISHED = ("stopped", "interrupted")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,99}$")
SNAPSHOT_WAIT_S = 5.0  # worker thread: how long to wait for the store


class MinutesUnavailable(RuntimeError):
    """The version cannot be viewed, copied or exported (`code`: invalidated or not_found)."""

    def __init__(self, code: str) -> None:
        super().__init__(f"minutes unavailable: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class KeyStatus:
    configured: bool
    saved_at: datetime | None
    vault_available: bool


@dataclass(frozen=True, slots=True)
class VersionInfo:
    row: MinutesVersionRow
    stale: bool
    latest: bool


@dataclass(frozen=True, slots=True)
class MinutesDocument:
    meeting: MeetingRow
    version: MinutesVersionRow
    items: tuple[MinutesItemRow, ...] = field(repr=False)
    stale: bool
    gaps: int
    coverage: str

    def of(self, kind: str) -> list[MinutesItemRow]:
        return [i for i in self.items if i.kind == kind]


class GenerationJob:
    def __init__(self, meeting_id: str, revision: int) -> None:
        self.meeting_id = meeting_id
        self.revision = revision
        self.cancel_event = threading.Event()
        self.done = threading.Event()
        self.state = "running"  # running | done | failed | cancelled
        self.step: tuple[str, int, int] = ("preparing", 0, 0)
        self.version_id: str | None = None
        self.error: GenerationError | None = None
        self.thread: threading.Thread | None = None

    def cancel(self) -> None:
        self.cancel_event.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.done.wait(timeout)


def snapshot_for(store: TranscriptStore, meeting_id: str, revision: int, *,
                 wait_s: float | None = SNAPSHOT_WAIT_S) -> GenerationSnapshot:
    """The selected saved revision as one consistent read; refuses a different revision,
    a meeting still being recorded, or an empty transcript (all before sending)."""
    try:
        with store.held(wait_s):
            snap = store.snapshot(meeting_id)
            redactions = store.redaction_count(meeting_id) if snap else 0
    except StorageError as exc:
        raise GenerationError("busy" if exc.code == "busy" else "storage", request_sent=False,
                              detail=exc.code) from None
    if snap is None:
        raise GenerationError("transcript_invalidated", request_sent=False, detail="meeting deleted")
    m = snap.meeting
    if m.transcript_revision != revision:
        raise GenerationError("transcript_changed", request_sent=False)
    if m.status not in FINISHED:
        raise GenerationError("not_saved", request_sent=False)
    segments = tuple(SnapshotSegment(s.id, s.revision, s.source, s.start_ms, s.end_ms, s.text, s.language,
                                     tuple(f for f in s.flags if f in RECOGNITION_FLAGS), s.origin == "user")
                     for s in snap.segments if s.text.strip())
    if not segments:
        raise GenerationError("empty_transcript", request_sent=False)
    gaps = tuple(SnapshotGap(t.start_ms, t.end_ms, t.source, t.code, bool(t.metadata.get("loss", t.kind != "gap")))
                 for t in snap.timeline if _is_gap(t))
    local_date, local_time_text, offset = original_local_start(m.started_at, m.timezone)
    return GenerationSnapshot(m.id, m.title, local_date, local_time_text, offset, m.timezone, m.transcript_revision,
                              segments, gaps, redactions, is_complete(snap))


def original_local_start(started_at: str, zone: str) -> tuple[str, str, str]:
    """(local date, local time, UTC offset) of the meeting start where it was recorded.
    Never this PC's current zone: an older meeting without a recorded offset
    gives ("", "", ""), i.e. unknown, so relative deadlines stay unresolved."""
    offset = zone_offset(zone)
    try:
        start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except ValueError:
        return "", "", ""
    if offset is None or start.tzinfo is None:
        return "", "", ""
    sign = -1 if offset[0] == "-" else 1
    local = start.astimezone(timezone(sign * timedelta(hours=int(offset[1:3]), minutes=int(offset[4:6]))))
    return local.date().isoformat(), local.strftime("%H:%M"), offset


def _is_gap(t) -> bool:
    return (t.kind in ("gap", "pause") or (t.kind == "error" and t.code != "event_log_overflow")
            or t.code == "segment_redacted")


def content_to_items(content: dict[str, Any], snapshot: GenerationSnapshot) -> list[dict[str, Any]]:
    where = {s.id: (s.start_ms, s.source) for s in snapshot.segments}
    uncertain = {s.id for s in snapshot.segments if s.uncertain}

    def ev(item: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"segment_id": e["segment_id"], "revision": e["revision"],
                 "start_ms": where[e["segment_id"]][0], "source": where[e["segment_id"]][1],
                 **({"uncertain": True} if e["segment_id"] in uncertain else {})}
                for e in item["evidence"]]

    items: list[dict[str, Any]] = []
    for s in content["summary"]:
        items.append({"kind": "summary", "text": s["text"], "evidence": ev(s)})
    for t in content["topics"]:
        items.append({"kind": "topic", "text": t["title"] or "Discussion", "detail": t["discussion"],
                      "evidence": ev(t)})
    for d in content["decisions"]:
        items.append({"kind": "decision", "text": d["text"], "status": d["status"], "evidence": ev(d)})
    for q in content["open_questions"]:
        items.append({"kind": "question", "text": q["text"], "owner": q["owner"], "evidence": ev(q)})
    for a in content["action_items"]:
        due, status = a["due_date"], a["date_status"]
        if status == "resolved_relative" and not snapshot.local_date:
            due, status = None, "ambiguous"  # the meeting's own date is unknown: leave it to the user
        items.append({"kind": "action", "text": a["task"], "owner": a["owner"], "due_date": due,
                      "due_date_text": a["due_date_text"], "date_status": status,
                      "commitment": a["commitment"], "reviewed": False, "evidence": ev(a)})
    return items


def source_warnings(snapshot: GenerationSnapshot, items: list[dict[str, Any]]) -> list[str]:
    """Warnings the app adds itself, whatever the model returned: doubtful recognition
    and an unknown original meeting date."""
    out = []
    uncertain = sum(1 for s in snapshot.segments if s.uncertain)
    if uncertain:
        citing = sum(1 for i in items if any(e.get("uncertain") for e in i["evidence"]))
        out.append(UNCERTAIN_WARNING.format(n=uncertain, k=citing))
    if not snapshot.local_date and any(i["kind"] == "action" and i["due_date_text"] for i in items):
        out.append(UNKNOWN_DATE_WARNING)
    return out


class MinutesService:
    def __init__(self, store: TranscriptStore, credentials: CredentialStore,
                 generator_factory: Callable[[CredentialStore], MinutesGenerator]) -> None:
        self.store = store
        self.credentials = credentials
        self._generator_factory = generator_factory
        self._lock = threading.Lock()
        self._job: GenerationJob | None = None

    @classmethod
    def default(cls, store: TranscriptStore) -> MinutesService:
        """Windows Credential Manager + OpenAI. Constructing it makes no request."""
        return cls(store, WindowsCredentialStore(), lambda creds: OpenAIMinutesGenerator(creds.get))

    def __repr__(self) -> str:
        return "MinutesService()"

    @property
    def provider_name(self) -> str:
        return getattr(self._generator_factory(self.credentials), "provider_name", "provider")

    # ------------------------------------------------------------- key and model

    def key_status(self) -> KeyStatus:
        available = getattr(self.credentials, "available", True)
        configured = self.credentials.exists()
        return KeyStatus(configured, self.credentials.saved_at() if configured else None, available)

    def save_key(self, key: str) -> None:
        """Save or replace the key in the credential store. No network request."""
        self.credentials.replace(key) if self.credentials.exists() else self.credentials.save(key)

    def remove_key(self) -> None:
        self.credentials.remove()

    def model_id(self) -> str:
        value = self._setting(MODEL_KEY)
        return value if isinstance(value, str) and _MODEL_RE.match(value) else DEFAULT_MODEL

    def set_model_id(self, model_id: str) -> str:
        model_id = (model_id or "").strip()
        if not _MODEL_RE.match(model_id):
            raise ValueError("invalid model id")
        self.store.set_setting(MODEL_KEY, model_id)
        return model_id

    def output_language(self) -> str:
        value = self._setting(LANGUAGE_KEY)
        return value if value in LANGUAGES else "en"

    def set_output_language(self, language: str) -> None:
        if language not in LANGUAGES:
            raise ValueError("unsupported language")
        self.store.set_setting(LANGUAGE_KEY, language)

    def _setting(self, key: str) -> Any:
        try:
            return self.store.get_setting(key)
        except StorageError:
            return None

    def prices(self, model_id: str | None = None) -> tuple[float, float] | None:
        """(USD per 1M input tokens, per 1M output tokens) for the model: the user's
        entry in Settings, else the built-in figure for the default model, else None."""
        model_id = model_id or self.model_id()
        saved = self._setting(PRICES_KEY)
        entry = saved.get(model_id) if isinstance(saved, dict) else None
        if (isinstance(entry, list) and len(entry) == 2
                and all(isinstance(x, (int, float)) and 0 <= x <= MAX_PRICE for x in entry)):
            return float(entry[0]), float(entry[1])
        return KNOWN_PRICES.get(model_id)

    def set_prices(self, price_in: float | None, price_out: float | None, model_id: str | None = None) -> None:
        """Save the prices for a model; None for both removes the user's entry."""
        model_id = model_id or self.model_id()
        saved = self._setting(PRICES_KEY)
        saved = dict(saved) if isinstance(saved, dict) else {}
        if price_in is None and price_out is None:
            saved.pop(model_id, None)
        else:
            if not all(isinstance(x, (int, float)) and 0 <= x <= MAX_PRICE for x in (price_in, price_out)):
                raise ValueError("bad price")
            saved[model_id] = [float(price_in), float(price_out)]
        self.store.set_setting(PRICES_KEY, saved)

    def budget_usd(self) -> float | None:
        value = self._setting(BUDGET_KEY)
        if value == "off":
            return None
        return float(value) if isinstance(value, (int, float)) and 0 < value <= MAX_BUDGET else DEFAULT_BUDGET_USD

    def set_budget_usd(self, value: float | None) -> None:
        """The local limit per generation; None means no limit set in this app."""
        if value is not None and not (isinstance(value, (int, float)) and 0 < value <= MAX_BUDGET):
            raise ValueError("bad limit")
        self.store.set_setting(BUDGET_KEY, "off" if value is None else float(value))

    def options(self) -> GenerationOptions:
        model = self.model_id()
        prices = self.prices(model)
        return GenerationOptions(model_id=model, output_language=self.output_language(),
                                 price_in_per_m=prices[0] if prices else None,
                                 price_out_per_m=prices[1] if prices else None, budget_usd=self.budget_usd())

    def estimate(self, meeting_id: str, revision: int, options: GenerationOptions | None = None, *,
                 wait_s: float = 0.25) -> CostEstimate | None:
        """Pre-send estimate for the saved revision (no request); None if it can't be made now."""
        try:
            return estimate_cost(snapshot_for(self.store, meeting_id, revision, wait_s=wait_s),
                                 options or self.options())
        except GenerationError:
            return None

    # -------------------------------------------------------------------- jobs

    @property
    def job(self) -> GenerationJob | None:
        return self._job

    def running_job(self, meeting_id: str | None = None) -> GenerationJob | None:
        job = self._job
        if job is None or job.done.is_set():
            return None
        return job if meeting_id is None or job.meeting_id == meeting_id else None

    def start(self, meeting_id: str, revision: int, *,
              on_progress: Callable[[GenerationJob], None] | None = None,
              on_finished: Callable[[GenerationJob], None] | None = None,
              options: GenerationOptions | None = None) -> GenerationJob:
        """Start generating from saved revision `revision` (explicit user action).
        `on_finished` is called once with job.state done/failed/cancelled."""
        with self._lock:
            if self.running_job() is not None:
                raise GenerationError("busy", request_sent=False)
            job = GenerationJob(meeting_id, revision)
            self._job = job
        opts = options or self.options()

        def progress(step: str, i: int, n: int) -> None:
            job.step = (step, i, n)
            if on_progress:
                on_progress(job)

        def run() -> None:
            try:
                job.version_id = self._run(job, opts, progress)
                job.state = "done"
            except GenerationError as exc:
                job.error = exc
                job.state = "cancelled" if exc.code == "cancelled" else "failed"
            except Exception:  # noqa: BLE001 - reported as a content-free failure, never with content
                job.error = GenerationError("invalid_output", request_sent=None, detail="internal error")
                job.state = "failed"
            finally:
                job.done.set()
                if on_finished:
                    on_finished(job)

        job.thread = threading.Thread(target=run, name="minutes-generation", daemon=True)
        job.thread.start()
        return job

    def _run(self, job: GenerationJob, options: GenerationOptions,
             progress: Callable[[str, int, int], None]) -> str:
        progress("preparing", 0, 0)
        try:
            has_key = self.credentials.exists() and bool(self.credentials.get())
        except CredentialError:
            raise GenerationError("vault_unavailable", request_sent=False) from None
        if not has_key:
            raise GenerationError("no_key", request_sent=False)
        if job.cancel_event.is_set():
            raise GenerationError("cancelled", request_sent=False)
        snapshot = snapshot_for(self.store, job.meeting_id, job.revision)
        generator = self._generator_factory(self.credentials)
        result = generator.generate(snapshot, options, job.cancel_event, progress)
        if job.cancel_event.is_set():
            raise GenerationError("cancelled", request_sent=True)
        return self._save(snapshot, result)

    def _save(self, snapshot: GenerationSnapshot, result: GenerationResult) -> str:
        content = result.content
        items = content_to_items(content, snapshot)
        try:
            return self.store.save_minutes_version(
                snapshot.meeting_id, transcript_revision=snapshot.transcript_revision,
                snapshot_hash=snapshot.snapshot_hash(), snapshot_refs=sorted(snapshot.refs().items()),
                redactions_at_snapshot=snapshot.redactions, provider=result.provider, model_id=result.model_id,
                prompt_version=result.prompt_version, schema_version=result.schema_version,
                output_language=content["output_language"], input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens, requests=result.usage.requests, parts=result.parts,
                incomplete=not snapshot.source_complete or any(g.loss for g in snapshot.gaps),
                warnings=list(content["warnings"]) + source_warnings(snapshot, items), items=items)
        except MinutesRejected as exc:
            raise GenerationError("transcript_invalidated", request_sent=True, detail=exc.code) from None
        except StorageError as exc:
            raise GenerationError("storage", request_sent=True, detail=exc.code) from None

    # ---------------------------------------------------------------- versions

    def versions(self, meeting_id: str) -> list[VersionInfo]:
        meeting = self.store.meeting(meeting_id)
        if meeting is None:
            return []
        rows = self.store.minutes_versions(meeting_id)
        latest = max((r.version for r in rows if not r.invalidated), default=None)
        return [VersionInfo(r, meeting.transcript_revision > r.transcript_revision, r.version == latest) for r in rows]

    def document(self, version_id: str) -> MinutesDocument:
        """A viewable version. Invalidated (redacted/deleted) content is refused."""
        version = self.store.minutes_version(version_id)
        if version is None:
            raise MinutesUnavailable("not_found")
        if version.invalidated:
            raise MinutesUnavailable("invalidated")
        snap = self.store.snapshot(version.meeting_id)
        if snap is None:
            raise MinutesUnavailable("not_found")
        items = tuple(self.store.minutes_items(version_id))
        gaps = sum(1 for t in snap.timeline if t.metadata.get("loss") or (t.kind == "error"
                                                                          and t.code != "event_log_overflow"))
        return MinutesDocument(snap.meeting, version, items, snap.meeting.transcript_revision >
                               version.transcript_revision, gaps, coverage_summary(snap.meeting.status, snap.timeline))

    def badge(self, meeting_id: str | None) -> tuple[str, str]:
        """(minutes badge, actions badge) for the workspace tabs."""
        if meeting_id is None:
            return "", ""
        if self.running_job(meeting_id):
            return "Generating…", ""
        valid = [v for v in self.versions(meeting_id) if not v.row.invalidated]
        if not valid:
            return "Not generated", ""
        latest = valid[-1]
        actions = sum(1 for i in self.store.minutes_items(latest.row.id) if i.kind == "action")
        if latest.stale:
            return "Stale", str(actions)
        return {"draft": "Draft", "review": "In review", "approved": "Approved"}[latest.row.state], str(actions)

    # ------------------------------------------------------------------ review

    def edit_item(self, item_id: str, **changes: Any) -> None:
        """Validated in-place edit of a draft/in-review item (owner blank -> Not
        specified; due date ISO or None; commitment; reviewed)."""
        clean: dict[str, Any] = {}
        for name, value in changes.items():
            if name in ("owner", "detail", "due_date_text"):
                clean[name] = value.strip() if isinstance(value, str) and value.strip() else None
            elif name == "text":
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("text is required")
                clean[name] = value.strip()
            elif name == "due_date":
                if value is not None:
                    value = value.isoformat() if isinstance(value, date) else str(value)
                    date.fromisoformat(value)
                clean[name] = value
            elif name == "commitment":
                if value not in ("explicit", "proposed", "uncertain"):
                    raise ValueError("bad commitment")
                clean[name] = value
            elif name == "status":
                if value not in ("agreed", "superseded", "uncertain"):
                    raise ValueError("bad status")
                clean[name] = value
            elif name == "reviewed":
                clean[name] = bool(value)
            else:
                raise ValueError("unknown field")
        if "due_date" in clean or "due_date_text" in clean:
            current = self.store.minutes_item(item_id)
            due = clean.get("due_date", current.due_date if current else None)
            phrase = clean.get("due_date_text", current.due_date_text if current else None)
            if due:
                clean["date_status"] = ("resolved_relative" if current and current.date_status == "resolved_relative"
                                        and "due_date" not in clean else "explicit")
            else:
                clean["date_status"] = "ambiguous" if phrase else "unspecified"
        self.store.update_minutes_item(item_id, clean)

    def add_item(self, version_id: str, kind: str, text: str, **fields: Any) -> str:
        if not text or not text.strip():
            raise ValueError("text is required")
        defaults = {"action": {"date_status": "unspecified", "commitment": "uncertain"},
                    "decision": {"status": "uncertain"}}.get(kind, {})
        return self.store.add_minutes_item(version_id, kind, {**defaults, **fields, "text": text.strip()})

    def remove_item(self, item_id: str) -> None:
        self.store.delete_minutes_item(item_id)

    def start_review(self, version_id: str) -> None:
        self.store.set_minutes_state(version_id, "review")

    def back_to_draft(self, version_id: str) -> None:
        self.store.set_minutes_state(version_id, "draft")

    def approve(self, version_id: str) -> None:
        self.store.set_minutes_state(version_id, "approved")

    def edit_as_new_draft(self, version_id: str) -> str:
        return self.store.copy_minutes_version(version_id)

    def unspecified_actions(self, version_id: str) -> int:
        return sum(1 for i in self.store.minutes_items(version_id)
                   if i.kind == "action" and (not i.owner or not i.due_date))

    # ------------------------------------------------------------------ export

    def export_text(self, version_id: str, fmt: str, *, exported_at: datetime | None = None) -> str:
        doc = self.document(version_id)
        return render_minutes(doc.meeting, doc.version, doc.items, fmt, coverage=doc.coverage, gaps=doc.gaps,
                              exported_at=exported_at)

    def save_as(self, version_id: str, path: Path, fmt: str) -> None:
        write_file(path, self.export_text(version_id, fmt))

    def suggested_filename(self, version_id: str, fmt: str = "md") -> str:
        doc = self.document(version_id)
        from meeting_minutes.export import suggest_filename

        base = suggest_filename(doc.meeting.title, doc.meeting.started_at, fmt).rsplit(".", 1)[0]
        state = "approved" if doc.version.state == "approved" else "draft"
        return f"{base} — minutes ({state}).{fmt}"
