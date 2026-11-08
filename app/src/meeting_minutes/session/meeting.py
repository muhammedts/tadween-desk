"""One meeting: capture -> speech -> saved transcript.

`MeetingSession` wires the capture controller and the speech pipeline to the
transcript store, and is what the desktop window drives. It has no Qt dependency.

Threads (when `threaded`): the capture worker, the single speech worker, and one
writer thread that is the only one writing this meeting to the database while it
runs. The speech pipeline's text consumer only queues events (it can run under the
privacy lock), so saving never delays capture or mute; a full queue refuses the
event, which the pipeline reports as a delivery fault and a gap. A final reaches the
queue only after the privacy commit check, so text from excluded mic audio is never
saved; text admitted before an exclusion is legitimately saved.

Storage failures:
- A final the store refuses pauses capture at once (no new audio is taken while text
  cannot be kept) and is kept in memory, marked NOT SAVED, up to UNSAVED_TEXT_MAX
  finals; beyond that only its interval is kept. Unsaved finals and failed timeline
  writes are retried in order on every writer cycle; once saving works again they are
  saved (or, without text, recorded as `text_not_saved` intervals). Capture resumes
  only when the user presses Resume, and only if saving works.
- A failed timeline (coverage/history) write is kept for retry in a bounded list;
  overflow is counted, and anything unresolved at the end makes coverage incomplete.
- Text dropped at the UNSAVED_TEXT_MAX cap is lost for good even after its interval is
  saved: `text_lost` only ever grows and keeps `complete` false, while
  `drain_complete` can still be true.

Stop: status 'stopping' (shown as finalizing) at once, then within ONE
overall budget (30 s by default): close capture, finish speech (anything still waiting
or decoding at the deadline becomes a stopped_undrained gap and can never publish),
then save the queued text and coverage on a helper thread. At the deadline the outcome
is decided whatever storage is doing: 'stopped' only if everything drained and saved,
otherwise 'interrupted' with the unresolved text counted as unsaved. A save that
finishes later still records the meeting as 'interrupted'; a save that never finishes
leaves it 'stopping', which the next start recovers as interrupted. `save_settled` is
set once no final save can still be writing, so the window knows when reading is safe.
Pause and mic exclusion are intentional and are never queued for replay.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from meeting_minutes.capture.base import CaptureBackend, Clock
from meeting_minutes.capture.controller import CaptureConfig, CaptureController, CaptureWorker, SwitchOutcome
from meeting_minutes.domain.contracts import NS_PER_SECOND, AudioFrame, Source
from meeting_minutes.domain.events import (
    CaptureGapEvent,
    CaptureStateEvent,
    GapReason,
    MicPrivacyCode,
    MicPrivacyStateEvent,
    StateReason,
)
from meeting_minutes.domain.states import MeetingState
from meeting_minutes.domain.transcript import PartialsCleared, SpeechGap, TextEvent, TextKind, TranscriptText
from meeting_minutes.speech.config import SpeechConfig
from meeting_minutes.speech.pipeline import Recognizer, SpeechPipeline, SpeechWorker
from meeting_minutes.speech.resample import ResamplerFactory, soxr_factory
from meeting_minutes.speech.segmenter import FrameVad
from meeting_minutes.storage.store import MaintenanceError, StorageError, TranscriptStore, local_zone_label

STOP_BUDGET_S = 30.0
TEXT_QUEUE_MAX = 2_000
# Capture's own drain is a quick hand-off to the speech inbox; bound it well inside
# the stop budget so speech finalization gets most of it.
CAPTURE_DRAIN_NS = 5 * NS_PER_SECOND
WRITER_INTERVAL_S = 0.1
UNSAVED_TEXT_MAX = 100  # unsaved finals whose text is kept in memory for the window
PENDING_META_MAX = 500  # failed timeline writes kept for retry


class SessionState(StrEnum):
    READY = "ready"
    CAPTURING = "capturing"
    DEGRADED = "degraded"  # a selected source is not delivering
    PAUSED = "paused"  # by the user, or because saving failed (see `save_blocked`)
    FINALIZING = "finalizing"  # stored as 'stopping'
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class EngineInfo:
    engine_id: str
    model_hash: str


@dataclass(frozen=True, slots=True)
class StartOutcome:
    started: bool
    failed: dict[str, str]  # source -> fixed failure code
    meeting_id: str | None


@dataclass(slots=True)
class UnsavedFinal:
    """A recognized final the store refused. Its text is kept in memory (for the
    window and for a retry) unless UNSAVED_TEXT_MAX was reached, then only the
    interval is kept."""

    source: str
    start_ms: int
    end_ms: int
    code: str
    event: TranscriptText | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _Marker:
    """A user action saved in order with the text (pause/resume)."""

    kind: str
    code: str
    at_ms: int


@dataclass(frozen=True, slots=True)
class _Meta:
    """A timeline write waiting for retry."""

    kind: str
    code: str
    start_ms: int
    end_ms: int | None
    source: str | None
    metadata: dict[str, Any]


Listener = Callable[[str, dict[str, Any]], None]


def capture_gap_is_loss(source: Source, reason: GapReason) -> bool:
    """Discarding pending mic audio on exclusion is intentional, and a playback
    endpoint sends nothing while nothing plays; every other capture gap is loss."""
    if reason is GapReason.MIC_EXCLUDED_PENDING_DISCARDED:
        return False
    return not (reason is GapReason.NO_DATA_DELIVERED and source is Source.SYSTEM)


class _FormatTap:
    """FrameSink in front of the speech pipeline that remembers each epoch's native
    format (needed for source_epochs) without keeping any samples."""

    def __init__(self, inner: SpeechPipeline) -> None:
        self.inner = inner
        self.formats: dict[tuple[Source, int], tuple[int, int]] = {}

    def accept(self, frame: AudioFrame) -> None:
        key = (frame.source, frame.epoch)
        if key not in self.formats:
            self.formats[key] = (frame.sample_rate, frame.channels)
        self.inner.accept(frame)

    def reset(self, source: Source) -> int:
        return self.inner.reset(source)


class MeetingSession:
    def __init__(
        self,
        store: TranscriptStore,
        backend: CaptureBackend,
        recognizer: Recognizer,
        vad_factory: Callable[[], FrameVad],
        *,
        engine: EngineInfo,
        speech_config: SpeechConfig = SpeechConfig(),
        resampler_factory: ResamplerFactory = soxr_factory,
        clock: Clock = time.monotonic_ns,
        threaded: bool = True,
        listener: Listener | None = None,
        stop_budget_s: float = STOP_BUDGET_S,
        capture_config: CaptureConfig | None = None,
    ) -> None:
        self.store = store
        self.engine = engine
        self._clock = clock
        self._threaded = threaded
        self._listener = listener or (lambda kind, data: None)
        self._stop_budget_ns = int(stop_budget_s * NS_PER_SECOND)
        self.meeting_uuid: UUID = uuid4()
        self.meeting_id = str(self.meeting_uuid)
        self.title = ""
        self.pipeline = SpeechPipeline(recognizer, vad_factory, on_text=self._on_text,
                                       resampler_factory=resampler_factory, config=speech_config, clock=clock)
        self._tap = _FormatTap(self.pipeline)
        config = capture_config or CaptureConfig(drain_timeout_ns=CAPTURE_DRAIN_NS)
        self.controller = CaptureController(backend, mic_included=True, sink=self._tap, clock=clock,
                                            config=config, meeting_id=self.meeting_uuid)
        self.pipeline.attach(self.controller)
        self._texts: queue.Queue[TextEvent | _Marker] = queue.Queue(maxsize=TEXT_QUEUE_MAX)
        # Lock order: self._write_lock, then self._lock. Only the holder of
        # self._write_lock writes this meeting to the store.
        self._write_lock = threading.RLock()
        self._lock = threading.RLock()
        self._state = SessionState.READY
        self._saved_epochs: set[tuple[str, int]] = set()
        self._event_seq = 0
        self._stored_status: str | None = None
        self.unsaved: list[UnsavedFinal] = []
        self._pending_meta: deque[_Meta] = deque()
        self.metadata_lost = 0  # timeline writes dropped because the retry list was full
        self.text_lost = 0  # refused finals whose text was not kept (cap reached); never decreases
        self._writing = False  # a final is inside store.add_final right now
        self.storage_error: str | None = None
        self.save_blocked: str | None = None  # capture paused because saving failed
        self._decided = False  # the stop deadline passed before the final save finished
        self.finish_report: dict[str, Any] | None = None
        self._capture_worker: CaptureWorker | None = None
        self._speech_worker: SpeechWorker | None = None
        self._writer: threading.Thread | None = None
        self._writer_stop = threading.Event()
        self._finalizer: threading.Thread | None = None
        self.finished = threading.Event()
        self.save_settled = threading.Event()  # the final save has returned (or never started)

    # ------------------------------------------------------------------ queries

    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def mic_included(self) -> bool:
        return self.controller.mic_included

    def unresolved_saves(self) -> int:
        """Finals not (yet) saved: refused, still queued, or being written."""
        queued = sum(1 for e in list(self._texts.queue) if isinstance(e, TranscriptText))
        return len(self.unsaved) + queued + (1 if self._writing else 0)

    def status(self) -> dict[str, Any]:
        """Content-free live status for the window: sources, lag, losses, counters."""
        speech = self.pipeline.status()
        capture = self.controller.status()
        return {
            "state": self._state.value,
            "mic_included": self.controller.mic_included,
            "sources": capture["sources"],
            "pending_ms": max(s["pending_ms"] for s in speech["sources"].values()),
            "oldest_pending_age_ms": max(s["oldest_pending_age_ms"] for s in speech["sources"].values()),
            "speech_gaps": speech["speech_gaps"],
            "faults": speech["faults"],
            "text_queue": self._texts.qsize(),
            "unsaved_finals": len(self.unsaved),
            "unsaved_timeline": len(self._pending_meta) + self.metadata_lost,
            "text_lost": self.text_lost,
            "storage_error": self.storage_error,
            "save_blocked": self.save_blocked,
        }

    # ----------------------------------------------------------------- commands

    def start(self, *, mic: str | None, system: str | None, mic_included: bool = True, allow_degraded: bool = False,
              title: str = "", timezone_name: str | None = None) -> StartOutcome:
        """Create the meeting record and open the selected sources. Nothing is captured
        before this call; if capture cannot start, the empty record is removed."""
        with self._lock:
            if self._state is not SessionState.READY:
                raise RuntimeError("this session was already started")
            self.title = title.strip() or _default_title()
            tz = timezone_name or local_zone_label()
            self.store.create_meeting(self.meeting_id, title=self.title, timezone_name=tz)
            self._stored_status = "starting"
            if not mic_included:
                self.controller.set_mic_included(False, expected_generation=self.controller.privacy_generation)
            result = self.controller.start(mic=mic, system=system, allow_degraded=allow_degraded)
            failed = {s.value: code for s, code in result.failed.items()}
            if not result.started:
                self.controller.close()
                try:
                    self.store.delete_meeting(self.meeting_id)
                except MaintenanceError:
                    pass  # the record (title only, never any transcript) is deleted; the index tidy-up
                    # runs again with the next deletion, redaction or retention cleanup
                self._state = SessionState.INTERRUPTED
                return StartOutcome(False, failed, None)
            self._state = SessionState.DEGRADED if result.failed else SessionState.CAPTURING
            if self._threaded:
                self._capture_worker = CaptureWorker(self.controller)
                self._speech_worker = SpeechWorker(self.pipeline)
                self._writer = threading.Thread(target=self._write_loop, name="transcript-writer", daemon=True)
                self._capture_worker.start()
                self._speech_worker.start()
                self._writer.start()
        if not self._threaded:
            self._write_pending()
        self._notify_state()
        return StartOutcome(True, failed, self.meeting_id)

    def set_mic_included(self, included: bool) -> bool:
        """Mute/unmute this app's transcription of the mic (not the meeting app's)."""
        with self._lock:
            if self._state not in (SessionState.CAPTURING, SessionState.DEGRADED, SessionState.PAUSED):
                return False
            transition = self.controller.set_mic_included(included,
                                                          expected_generation=self.controller.privacy_generation)
        self._listener("mic", {"included": self.controller.mic_included})
        return transition.changed

    def pause(self) -> bool:
        with self._lock:
            if not self.controller.pause():
                return False
            self._state = SessionState.PAUSED
            self._mark(_Marker("pause", "paused_by_user", self._now_ms()))
        self._notify_state()
        return True

    def resume(self) -> bool:
        """Resume capture. After a save failure this first retries the unsaved text
        and history; if saving still fails, capture stays paused."""
        if self.save_blocked is not None:
            if not self._write_lock.acquire(timeout=2.0):
                return False
            try:
                saved = self._retry()
            finally:
                self._write_lock.release()
            if not saved:
                self._listener("storage_error", {"code": self.storage_error or self.save_blocked})
                return False
            self.save_blocked = None
        with self._lock:
            if not self.controller.resume():
                return False
            self._state = self._capture_state()
            self._mark(_Marker("resume", "resumed_by_user", self._now_ms()))
        self._notify_state()
        return True

    def reconnect(self, source: Source) -> bool:
        """Try to reopen a lost source on its selected device."""
        with self._lock:
            if self._state not in (SessionState.CAPTURING, SessionState.DEGRADED):
                return False
            result = self.controller.reconnect(source)
            self._state = self._capture_state()
        self._notify_state()
        return result.outcome is SwitchOutcome.APPLIED

    def stop(self) -> None:
        """Begin finalizing; returns at once when threaded (the window stays usable)."""
        with self._lock:
            if self._state in (SessionState.READY, SessionState.FINALIZING, SessionState.STOPPED,
                               SessionState.INTERRUPTED):
                return
            self._state = SessionState.FINALIZING
            deadline = self._clock() + self._stop_budget_ns
        self._notify_state()
        if self._threaded:
            self._finalizer = threading.Thread(target=self._finalize, args=(deadline,), name="meeting-finalizer",
                                               daemon=True)
            self._finalizer.start()
        else:
            self._finalize(deadline)

    def _mark(self, marker: _Marker) -> None:
        try:
            self._texts.put_nowait(marker)
        except queue.Full:  # the writer is far behind; keep the record for retry
            self._keep_meta(_Meta(marker.kind, marker.code, marker.at_ms, None, None, {}))

    # -------------------------------------------------- deterministic driving (tests)

    def pump(self) -> None:
        """Unthreaded mode: forward captured audio, run speech and save, once."""
        self.controller.process_pending()
        self.controller.check_health()
        while self.pipeline.step():
            pass
        self._write_pending()

    # ---------------------------------------------------------------- internals

    def _on_text(self, event: TextEvent) -> None:
        # Runs on the speech worker, or under the privacy lock: never blocks.
        if isinstance(event, TranscriptText) and event.kind is TextKind.PARTIAL:
            self._listener("partial", {"id": event.hypothesis_id, "source": event.source.value,
                                       "start_ms": event.start_ms, "text": event.text})
            return
        if isinstance(event, PartialsCleared):
            self._listener("partials_cleared", {"source": event.source.value,
                                                "through": event.through_chunk_index})
            return
        self._texts.put_nowait(event)  # queue.Full -> the pipeline records a fault and a gap

    def _write_loop(self) -> None:
        while not self._writer_stop.is_set():
            self._write_pending()
            self._writer_stop.wait(WRITER_INTERVAL_S)
        self._write_pending()

    def _write_pending(self, deadline_ns: int | None = None) -> bool:
        """Retry earlier failures, then save everything queued; returns whether all of
        it is saved. With a deadline, waits for another writer only until then."""
        if deadline_ns is None:
            self._write_lock.acquire()
        elif not self._write_lock.acquire(timeout=max(0, deadline_ns - self._clock()) / NS_PER_SECOND):
            return False
        try:
            return self._drain_queue(deadline_ns)
        finally:
            self._write_lock.release()

    def _drain_queue(self, deadline_ns: int | None) -> bool:
        self._retry()
        self._sync()
        while deadline_ns is None or self._clock() < deadline_ns:
            try:
                event = self._texts.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, _Marker):
                self._save_timeline(event.kind, event.code, event.at_ms, None, None, {})
            elif isinstance(event, TranscriptText):
                self._save_final(event)
            elif isinstance(event, SpeechGap):
                self._save_timeline("gap", event.reason.value, event.start_ms, event.end_ms,
                                    event.source.value, {"stage": "speech", "loss": True})
                self._listener("gap", {"source": event.source.value, "start_ms": event.start_ms,
                                       "end_ms": event.end_ms, "reason": event.reason.value})
        return self._texts.empty() and not self.unsaved and not self._pending_meta

    def _save_final(self, event: TranscriptText) -> None:
        if self.unsaved:  # keep the order: earlier refused finals go first
            self._refused(event, self.unsaved[0].code)
            return
        self._sync_epoch(event.source, event.epoch)
        try:
            segment_id = self._add_final(event)
        except StorageError as exc:
            self._refused(event, exc.code)
            return
        self._listener("final", self._final_data(segment_id, event))

    def _add_final(self, event: TranscriptText) -> str:
        flags = [name for name, on in (("forced_cut", event.forced_cut),
                                       ("boundary_ambiguous", event.boundary_ambiguous)) if on]
        flags += [f for f in event.quality_flags if f not in flags]
        self._writing = True
        try:
            return self.store.add_final(
                self.meeting_id, source=event.source.value, epoch=event.epoch, start_ms=event.start_ms,
                end_ms=event.end_ms, text=event.text, language=event.language, engine_id=self.engine.engine_id,
                model_hash=self.engine.model_hash, flags=flags)
        finally:
            self._writing = False

    def _refused(self, event: TranscriptText, code: str) -> None:
        """Keep a refused final (text only up to the cap), show it, stop capturing."""
        keep = sum(1 for u in self.unsaved if u.event is not None) < UNSAVED_TEXT_MAX
        if not keep:
            self.text_lost += 1
        self.unsaved.append(UnsavedFinal(event.source.value, event.start_ms, event.end_ms, code,
                                         event if keep else None))
        self.storage_error = code
        self._listener("final_unsaved", {"source": event.source.value, "start_ms": event.start_ms,
                                         "end_ms": event.end_ms, "language": event.language,
                                         "text": event.text if keep else "", "code": code,
                                         "hypothesis_id": event.hypothesis_id, "text_kept": keep})
        self._block_capture(code)

    def _block_capture(self, code: str) -> None:
        """Saving failed: pause capture so no more audio is taken while text cannot be
        kept. Resume is the user's explicit choice (see `resume`)."""
        if self.save_blocked is not None:
            return
        self.save_blocked = code
        with self._lock:
            paused = self._state in (SessionState.CAPTURING, SessionState.DEGRADED) and self.controller.pause()
            if paused:
                self._state = SessionState.PAUSED
                self._keep_meta(_Meta("pause", "paused_save_failed", self._now_ms(), None, None,
                                      {"storage_error": code}))
        self._listener("save_blocked", {"code": code})
        self._notify_state()

    def _retry(self) -> bool:
        """Retry failed timeline writes, then refused finals, in order. Returns whether
        nothing is left. Called with self._write_lock held."""
        while self._pending_meta:
            m = self._pending_meta[0]
            try:
                self.store.add_timeline(self.meeting_id, m.kind, m.code, m.start_ms, m.end_ms, m.source, m.metadata)
            except StorageError as exc:
                self.storage_error = exc.code
                return False
            self._pending_meta.popleft()
        while self.unsaved:
            lost = self.unsaved[0]
            try:
                if lost.event is not None:
                    self._sync_epoch(lost.event.source, lost.event.epoch)
                    segment_id = self._add_final(lost.event)
                    self._listener("final", self._final_data(segment_id, lost.event))
                else:
                    self.store.add_timeline(self.meeting_id, "error", "text_not_saved", lost.start_ms, lost.end_ms,
                                            lost.source, {"storage_error": lost.code, "loss": True})
            except StorageError as exc:
                self.storage_error = exc.code
                return False
            self.unsaved.pop(0)
        if self.storage_error is not None:
            self.storage_error = None
            self._listener("save_recovered", {"capture_paused": self.save_blocked is not None})
        return True

    @staticmethod
    def _final_data(segment_id: str, event: TranscriptText) -> dict[str, Any]:
        return {"id": segment_id, "source": event.source.value, "start_ms": event.start_ms,
                "end_ms": event.end_ms, "language": event.language, "text": event.text,
                "hypothesis_id": event.hypothesis_id, "flags": list(event.quality_flags)}

    def _keep_meta(self, meta: _Meta) -> None:
        if len(self._pending_meta) < PENDING_META_MAX:
            self._pending_meta.append(meta)
        else:
            self.metadata_lost += 1

    def _save_timeline(self, kind: str, code: str, start_ms: int, end_ms: int | None, source: str | None,
                       metadata: dict[str, Any]) -> bool:
        """Write one coverage/history row; a refused write is kept for retry, never
        dropped silently, and writes queued behind an earlier failure keep their order."""
        meta = _Meta(kind, code, start_ms, end_ms, source, metadata)
        if self._pending_meta:
            self._keep_meta(meta)
            return False
        try:
            self.store.add_timeline(self.meeting_id, kind, code, start_ms, end_ms, source, metadata)
            return True
        except StorageError as exc:
            self.storage_error = exc.code
            self._keep_meta(meta)
            self._listener("storage_error", {"code": exc.code})
            return False

    def _sync(self) -> None:
        """Persist new capture epochs, gaps, mic-state and source-state changes, and
        the meeting status. Called with self._write_lock held. An event is taken off
        the controller's log once its write succeeded or was kept for retry."""
        for record in list(self.controller.epochs):
            if (record.source.value, record.epoch) not in self._saved_epochs:
                self._sync_epoch(record.source, record.epoch)
        for event in self.controller.events():
            if event.stream_seq < self._event_seq:
                continue
            if event.stream_seq > self._event_seq:
                self._save_timeline("error", "event_log_overflow", self._now_ms(), None, None,
                                    {"missed_events": event.stream_seq - self._event_seq, "loss": True})
            self._event_seq = event.stream_seq + 1
            self._persist_event(event)
        status = {SessionState.CAPTURING: "capturing", SessionState.DEGRADED: "degraded",
                  SessionState.PAUSED: "paused", SessionState.FINALIZING: "stopping"}.get(self._state)
        if status is not None and status != self._stored_status:
            try:
                self.store.set_status(self.meeting_id, status)
                self._stored_status = status
            except StorageError as exc:
                self.storage_error = exc.code  # retried on the next cycle

    def _sync_epoch(self, source: Source, epoch: int) -> None:
        key = (source.value, epoch)
        if key in self._saved_epochs:
            return
        record = next((e for e in list(self.controller.epochs) if e.source is source and e.epoch == epoch), None)
        if record is None:
            return
        rate, channels = self._tap.formats.get((source, epoch), (record.native_rate, record.native_channels))
        if not rate or not channels:
            return  # format unknown until audio arrives; retried on the next sync
        try:
            self.store.save_epoch(self.meeting_id, source=source.value, epoch=epoch, device_id=record.device_id,
                                  device_label=record.device_label, native_rate=rate, native_channels=channels,
                                  start_ms=record.start_ms, end_ms=record.end_ms)
            self._saved_epochs.add(key)
        except StorageError as exc:
            self.storage_error = exc.code  # retried on the next sync

    def _persist_event(self, event: Any) -> None:
        if isinstance(event, CaptureGapEvent):
            p = event.payload
            loss = capture_gap_is_loss(p.source, p.reason)
            self._save_timeline("gap", p.reason.value, p.start_ms, p.end_ms, p.source.value,
                                {"stage": "capture", "loss": loss, "duration_known": p.duration_known})
            if loss:
                self._listener("gap", {"source": p.source.value, "start_ms": p.start_ms, "end_ms": p.end_ms,
                                       "reason": p.reason.value})
        elif isinstance(event, MicPrivacyStateEvent):
            p = event.payload
            code = "mic_excluded" if p.code is MicPrivacyCode.MIC_EXCLUDED else "mic_included"
            self._save_timeline("notice", code, p.at_ms, None, "mic",
                                {"discarded_pending_ms": p.discarded_pending_ms})
        elif isinstance(event, CaptureStateEvent):
            p = event.payload
            if p.reason in (StateReason.DEVICE_LOST, StateReason.OPEN_FAILED):
                self._save_timeline("device_change", p.reason.value, p.at_ms or self._now_ms(), None,
                                    p.source.value, {"epoch": p.epoch})
                with self._lock:
                    if self._state in (SessionState.CAPTURING, SessionState.DEGRADED):
                        self._state = self._capture_state()
                self._listener("source_lost", {"source": p.source.value})
                self._notify_state()

    def _finalize(self, deadline: int) -> None:
        def remaining_s() -> float:
            return max(0, deadline - self._clock()) / NS_PER_SECOND

        save_started = False
        try:
            self.controller.stop(deadline_ns=deadline)
            if self._capture_worker is not None:
                self._capture_worker.stop(timeout_s=min(2.0, remaining_s()))
            worker_stopped = True
            if self._speech_worker is not None:
                worker_stopped = self._speech_worker.stop(timeout_s=remaining_s())
            self.pipeline.finish(deadline)
            speech = dict(self.pipeline.finish_report or {})
            self._writer_stop.set()
            done = threading.Event()
            saved: dict[str, Any] = {}

            def save() -> None:
                # Storage can stall (a busy or failing disk): the final save runs here
                # so the deadline below is kept whatever the store does.
                try:
                    saved["report"] = self._final_save(speech, worker_stopped)
                except Exception as exc:  # noqa: BLE001 - recorded as a content-free code
                    self.storage_error = getattr(exc, "code", type(exc).__name__)
                finally:
                    done.set()
                    self.save_settled.set()

            threading.Thread(target=save, name="meeting-final-save", daemon=True).start()
            save_started = True
            done.wait(remaining_s())
            with self._lock:
                report = saved.get("report")
                if report is None:
                    self._decided = True  # a save finishing later records 'interrupted'
                    report = self._report(speech, worker_stopped, saved_all=False)
                    report["save_timed_out"] = done.is_set() is False
                self.finish_report = report
                self._state = SessionState.STOPPED if report["drain_complete"] else SessionState.INTERRUPTED
        finally:
            self.controller.close()
            if not save_started:
                self.save_settled.set()
            self.finished.set()
        self._notify_state()

    def _final_save(self, speech: dict[str, Any], worker_stopped: bool) -> dict[str, Any]:
        """Save what is queued and the coverage notice (on the final-save thread)."""
        if self._writer is not None:
            self._writer.join()
        with self._write_lock:
            saved_all = self._drain_queue(None)
            for record in list(self.controller.epochs):
                self._sync_epoch(record.source, record.epoch)
                if (record.source.value, record.epoch) in self._saved_epochs and record.end_ms is not None:
                    try:
                        self.store.close_epoch(self.meeting_id, record.source.value, record.epoch, record.end_ms)
                    except StorageError as exc:
                        self.storage_error = exc.code
                        saved_all = False
            with self._lock:
                late = self._decided
                report = self._report(speech, worker_stopped, saved_all=saved_all and not late)
            if late:
                report["completed_after_stop_deadline"] = True
            if not self._save_timeline("notice", "finalized", self._now_ms(), None, None, report):
                report["complete"] = report["drain_complete"] = False
            try:
                self.store.set_status(self.meeting_id, "stopped" if report["drain_complete"] else "interrupted",
                                      ended=True)
            except StorageError as exc:
                self.storage_error = exc.code  # left 'stopping': recovered as interrupted next start
                report["complete"] = report["drain_complete"] = False
            return report

    def _report(self, speech: dict[str, Any], worker_stopped: bool, *, saved_all: bool) -> dict[str, Any]:
        capture_losses = sum(1 for g in self.controller.gaps if capture_gap_is_loss(g.source, g.reason))
        unsaved_meta = len(self._pending_meta) + self.metadata_lost
        unsaved = self.unresolved_saves()
        drained = bool(speech.get("drain_complete")) and saved_all and unsaved == 0 and unsaved_meta == 0
        return {
            # Text dropped at the cap stays lost after the retry queue clears.
            "complete": bool(speech.get("complete")) and drained and capture_losses == 0 and self.text_lost == 0,
            "drain_complete": drained,
            "speech_worker_stopped": worker_stopped,
            "speech_engine": self.engine.engine_id,
            "speech_gaps": speech.get("speech_gaps", 0),
            "speech_faults": speech.get("faults", 0),
            "undrained_chunks": speech.get("undrained_chunks", 0),
            "abandoned_in_flight": speech.get("abandoned_in_flight", False),
            "capture_loss_gaps": capture_losses,
            "unsaved_finals": unsaved,
            "unsaved_timeline": unsaved_meta,
            "text_not_saved": self.text_lost,
        }

    def _capture_state(self) -> SessionState:
        state = self.controller.state
        if state is MeetingState.PAUSED:
            return SessionState.PAUSED
        return SessionState.DEGRADED if state is MeetingState.DEGRADED else SessionState.CAPTURING

    def _notify_state(self) -> None:
        self._listener("state", {"state": self._state.value, "report": self.finish_report,
                                 "storage_error": self.storage_error, "save_blocked": self.save_blocked})

    def _now_ms(self) -> int:
        origin = self.controller.origin_ns
        return 0 if origin is None else max(0, (self._clock() - origin) // 1_000_000)


def _default_title() -> str:
    return datetime.now().strftime("Meeting %Y-%m-%d %H:%M")
