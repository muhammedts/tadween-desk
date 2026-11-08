"""Two-source capture controller.

Owns one meeting's capture: explicit start, pause all/resume, stop with bounded
draining, per-source device switch/reconnect, the mic privacy boundary, bounded
per-source queues under one global owned-PCM budget, source epochs and explicit gaps.

Threads:
- native callback threads call `_on_frame` only; it takes the privacy boundary lock
  and appends loss records, never the controller lock, and never blocks on I/O;
- a worker (`CaptureWorker`, or a test calling the methods directly) runs
  `process_pending` and `check_health`;
- the UI/CLI thread issues commands. Lock order is controller lock -> boundary lock.

Nothing here writes files, opens sockets or keeps audio beyond the bounded queues.
Pause and mic exclusion are intentional and recorded as state/privacy events, not as
loss. Lost data (overflow, budget, no data delivered, device switch/loss, undrained at
stop) is recorded as `capture.gap` with exact capture-time bounds.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from meeting_minutes.audio.bounded_queue import BoundedFrameQueue, DroppedSpan
from meeting_minutes.capture.base import (
    AmbiguousDevice,
    CaptureBackend,
    CaptureHandle,
    Clock,
    DeviceUnavailable,
    FormatUnsupported,
)
from meeting_minutes.capture.clock import MIN_DISCONTINUITY_NS
from meeting_minutes.capture.gate import PrivacyBoundary
from meeting_minutes.capture.sinks import FanoutSink, FrameSink, LevelMeterSink
from meeting_minutes.domain.contracts import (
    NS_PER_SECOND,
    AudioFrame,
    DeviceDescriptor,
    DeviceSelection,
    Source,
)
from meeting_minutes.domain.events import (
    CaptureGapEvent,
    CaptureGapPayload,
    CaptureStateEvent,
    CaptureStatePayload,
    GapReason,
    MicPrivacyCode,
    MicPrivacyPayload,
    MicPrivacyStateEvent,
    SourceChangedEvent,
    SourceChangedPayload,
    StateReason,
)
from meeting_minutes.domain.privacy import CommandOrigin, PrivacyTransition
from meeting_minutes.domain.states import (
    MeetingState,
    SourceState,
    require_meeting_transition,
    require_source_transition,
)

MAX_PCM_BYTES_TOTAL = 64 * 2**20
# Shorter no-data stretches at the edges of an epoch are stream start-up/shut-down
# latency, not an interval worth reporting (matches the sample clock's tolerance).
NO_DATA_MIN_NS = MIN_DISCONTINUITY_NS


class CaptureError(RuntimeError):
    pass


class StaleEpochError(CaptureError):
    """A device command was issued against an older source epoch."""


@dataclass(frozen=True, slots=True)
class CaptureConfig:
    max_pending_seconds_per_source: int = 24
    max_pcm_bytes_total: int = MAX_PCM_BYTES_TOTAL
    # A running microphone delivers buffers continuously (silence included), so a long
    # pause in callbacks means it was lost. Loopback legitimately goes quiet, so it is
    # judged by stream activity only.
    mic_stall_timeout_ns: int | None = 3 * NS_PER_SECOND
    drain_timeout_ns: int = 30 * NS_PER_SECOND
    event_log_size: int = 1000


@dataclass(slots=True)
class SourceEpoch:
    """In-memory record of one source epoch; the session persists it as a `source_epochs` row."""

    source: Source
    epoch: int
    device_id: str
    device_label: str
    native_rate: int | None
    native_channels: int | None
    start_ms: int
    end_ms: int | None = None


@dataclass(frozen=True, slots=True)
class LossRecord:
    source: Source
    epoch: int
    start_ns: int
    end_ns: int
    reason: GapReason
    duration_known: bool = True


@dataclass(frozen=True, slots=True)
class StartResult:
    opened: frozenset[Source]
    failed: dict[Source, str]
    state: MeetingState

    @property
    def started(self) -> bool:
        return self.state in (MeetingState.CAPTURING, MeetingState.DEGRADED)


@dataclass(frozen=True, slots=True)
class StopResult:
    drained_frames: int
    undrained_frames: int
    drain_completed: bool
    # Frames taken from the queues during the drain whose processing failed (recorded
    # as processing_failed gaps); they make the drain incomplete.
    failed_frames: int = 0


class SwitchOutcome(StrEnum):
    # The requested endpoint opened and is capturing.
    APPLIED = "applied"
    # Selection stored without opening it (paused, or mic excluded): not yet tested.
    RECORDED = "recorded"
    # The requested endpoint failed; the previous one was reopened and is capturing.
    ROLLED_BACK = "rolled_back"
    # The requested endpoint failed and the source is not capturing.
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SwitchResult:
    """What a device switch actually did; `effective` is the selection now in force.

    A failed request never becomes the selection: it stays on `previous` (or none).
    Error fields are fixed codes, never device or OS text."""

    source: Source
    requested: str
    previous: str | None
    effective: str | None
    outcome: SwitchOutcome
    state: SourceState
    error: str | None = None
    rollback_error: str | None = None
    # For RECORDED: what opens it ("start", "resume" or "mic_included").
    opens_on: str | None = None


@dataclass(slots=True)
class _SourceRuntime:
    source: Source
    queue: BoundedFrameQueue
    device_id: str | None = None
    state: SourceState = SourceState.CLOSED
    epoch: int = 0
    handle: CaptureHandle | None = None
    failure: str | None = None
    opened_at_ns: int = 0
    # Capture time up to which a closed epoch is accounted (samples or recorded gaps).
    accounted_ns: int | None = None
    # Written on the callback thread under the boundary lock.
    expected: tuple[int, int] | None = None  # (epoch, next sample index)
    last_end_ns: int | None = None
    last_admit_ns: int | None = None
    pending_gap: tuple[int, GapReason] | None = None
    frames_admitted: int = 0
    samples_admitted: int = 0
    frames_forwarded: int = 0
    frames_failed: int = 0
    overflow_reports: int = 0
    lost_ns: dict[GapReason, int] = field(default_factory=dict)


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, AmbiguousDevice):
        return "device_ambiguous"
    if isinstance(exc, DeviceUnavailable):
        return "device_unavailable"
    if isinstance(exc, FormatUnsupported):
        return "format_unsupported"
    return "open_failed"


class CaptureController:
    def __init__(
        self,
        backend: CaptureBackend,
        *,
        mic_included: bool,
        sink: FrameSink | None = None,
        clock: Clock = time.monotonic_ns,
        config: CaptureConfig = CaptureConfig(),
        meeting_id: UUID | None = None,
    ) -> None:
        self.meeting_id = meeting_id or uuid4()
        self._backend = backend
        self._clock = clock
        self._config = config
        self.meter = LevelMeterSink()
        self._sink: FrameSink = FanoutSink([self.meter, sink]) if sink is not None else self.meter
        # No default: the meeting starts from an explicit, visible mic choice.
        self._boundary = PrivacyBoundary(mic_included=mic_included)
        self._lock = threading.RLock()
        self._state = MeetingState.IDLE
        self._origin_ns: int | None = None
        max_duration_ns = config.max_pending_seconds_per_source * NS_PER_SECOND
        self._sources = {
            s: _SourceRuntime(
                s, BoundedFrameQueue(s, max_bytes=config.max_pcm_bytes_total, max_duration_ns=max_duration_ns)
            )
            for s in Source
        }
        self._losses: deque[LossRecord] = deque()
        self._events: deque[Any] = deque(maxlen=config.event_log_size)
        self._stream_seq = 0
        self.epochs: list[SourceEpoch] = []
        self.gaps: list[CaptureGapPayload] = []
        self.peak_pcm_bytes = 0
        self.sink_errors = 0
        self.frames_after_close = 0

    # ----------------------------------------------------------------- properties

    @property
    def state(self) -> MeetingState:
        return self._state

    @property
    def boundary(self) -> PrivacyBoundary:
        return self._boundary

    @property
    def mic_included(self) -> bool:
        return self._boundary.state.mic_included

    @property
    def privacy_generation(self) -> int:
        return self._boundary.generation()

    def source_state(self, source: Source) -> SourceState:
        return self._sources[source].state

    def source_epoch(self, source: Source) -> int:
        return self._sources[source].epoch

    def selected_device(self, source: Source) -> str | None:
        return self._sources[source].device_id

    @property
    def origin_ns(self) -> int | None:
        """Monotonic time of Start: meeting time 0 for gaps, epochs and transcript text."""
        return self._origin_ns

    def events(self) -> list[Any]:
        with self._lock:
            return list(self._events)

    def pcm_bytes(self) -> int:
        return sum(rt.queue.queued_bytes for rt in self._sources.values())

    # ------------------------------------------------------------------ commands

    def start(self, *, mic: str | None, system: str | None, allow_degraded: bool = False) -> StartResult:
        """Open the selected sources. Nothing is captured before this explicit call.

        If a selected source cannot open, the start is rolled back (nothing keeps
        capturing) unless `allow_degraded` records the user's explicit choice to go on
        with what opened. An excluded mic is selected but deliberately not opened.
        """
        with self._lock:
            if self._state not in (MeetingState.IDLE, MeetingState.PREFLIGHT):
                raise CaptureError("capture already started")
            if mic is None and system is None:
                raise ValueError("select at least one source")
            if self._state is MeetingState.IDLE:
                self._set_state(MeetingState.PREFLIGHT)
            self._set_state(MeetingState.STARTING)
            self._origin_ns = self._clock()
            self._sources[Source.MIC].device_id = mic
            self._sources[Source.SYSTEM].device_id = system
            failed: dict[Source, str] = {}
            for source in (Source.SYSTEM, Source.MIC):
                if not self._should_run(source):
                    continue
                code = self._open(source, StateReason.STARTED)
                if code is not None:
                    failed[source] = code
            opened = frozenset(s for s, rt in self._sources.items() if rt.state is SourceState.RUNNING)
            mic_waiting = mic is not None and not self.mic_included
            if failed and (not allow_degraded or not (opened or mic_waiting)):
                for source in opened:
                    self._close(source, StateReason.STOPPED)
                for rt in self._sources.values():
                    rt.state = SourceState.CLOSED
                    rt.queue.clear()
                self._losses.clear()
                self._set_state(MeetingState.PREFLIGHT)
                return StartResult(frozenset(), failed, self._state)
            self._set_state(MeetingState.DEGRADED if failed else MeetingState.CAPTURING)
            return StartResult(opened, failed, self._state)

    def set_mic_included(
        self,
        included: bool,
        *,
        expected_generation: int,
        origin: CommandOrigin = CommandOrigin.LOCAL,
    ) -> PrivacyTransition:
        """Exclude or include the mic for transcription; see gate.PrivacyBoundary.

        Exclusion closes the gate, increments the generation, clears queued and
        downstream mic audio and then closes the mic stream (the OS mic indicator goes
        off). Inclusion opens a new mic epoch with fresh downstream state; nothing from
        before or during the exclusion is carried forward. The state persists through
        pause/resume, device switches and reconnects.
        """
        with self._lock:
            discarded_ns = 0

            def on_change(transition: PrivacyTransition) -> None:
                nonlocal discarded_ns
                mic = self._sources[Source.MIC]
                if not transition.current.mic_included:
                    discarded_ns = mic.queue.queued_duration_ns
                    mic.queue.clear()
                discarded_ns += self._sink.reset(Source.MIC)

            transition = self._boundary.change(
                included, expected_generation=expected_generation, origin=origin, on_change=on_change
            )
            if not transition.changed:
                return transition
            current = transition.current
            self._emit(
                MicPrivacyStateEvent,
                MicPrivacyPayload(
                    code=MicPrivacyCode.MIC_INCLUDED if current.mic_included else MicPrivacyCode.MIC_EXCLUDED,
                    privacy_generation=current.generation,
                    at_ms=self._now_ms(),
                    discarded_pending_ms=discarded_ns // 1_000_000,
                ),
            )
            mic = self._sources[Source.MIC]
            if not current.mic_included:
                if mic.handle is not None:
                    self._close(Source.MIC, StateReason.MIC_EXCLUDED)
                self._end_pending_gap(mic)  # from here on the absence is intentional
            elif self._state in (MeetingState.CAPTURING, MeetingState.DEGRADED) and self._should_run(Source.MIC):
                if mic.state is not SourceState.RUNNING:
                    self._open(Source.MIC, StateReason.MIC_INCLUDED)
            self._refresh_meeting_state()
            return transition

    def pause(self) -> bool:
        """Pause all: close both streams. Already admitted audio is still processed."""
        with self._lock:
            if self._state not in (MeetingState.CAPTURING, MeetingState.DEGRADED):
                return False
            for source, rt in self._sources.items():
                if rt.handle is not None:
                    self._close(source, StateReason.PAUSED)
                self._end_pending_gap(rt)  # a lost source's gap ends where the pause begins
            self._set_state(MeetingState.PAUSED)
            return True

    def resume(self) -> bool:
        """Reopen selected sources in new epochs; an excluded mic stays excluded."""
        with self._lock:
            if self._state is not MeetingState.PAUSED:
                return False
            self._set_state(MeetingState.CAPTURING)
            for source in (Source.SYSTEM, Source.MIC):
                if self._should_run(source) and self._sources[source].state is not SourceState.RUNNING:
                    self._open(source, StateReason.RESUMED)
            self._refresh_meeting_state()
            return True

    def switch_source(self, source: Source, device_id: str, *, expected_epoch: int) -> SwitchResult:
        """Move one source to another endpoint (or reconnect it); the other keeps running.

        If the requested endpoint fails, the selection returns to the previous one,
        which is reopened once if it was capturing; otherwise the source is marked
        failed and the user must choose. Never falls back to a different device on its
        own. The result says which of these happened.
        """
        with self._lock:
            rt = self._sources[source]
            if expected_epoch != rt.epoch:
                raise StaleEpochError(f"expected epoch {expected_epoch}, current is {rt.epoch}")
            if self._state in (MeetingState.STOPPING, MeetingState.STOPPED):
                raise CaptureError("capture has stopped")
            previous = rt.device_id
            was_running = rt.handle is not None
            if was_running:
                self._close(source, StateReason.DEVICE_SWITCH)
            gap_start = rt.accounted_ns if rt.accounted_ns is not None else self._clock()
            rt.device_id = device_id

            def result(outcome: SwitchOutcome, **extra: Any) -> SwitchResult:
                return SwitchResult(source, device_id, previous, rt.device_id, outcome, rt.state, **extra)

            if self._state in (MeetingState.IDLE, MeetingState.PREFLIGHT):
                return result(SwitchOutcome.RECORDED, opens_on="start")
            if self._state is MeetingState.PAUSED:
                return result(SwitchOutcome.RECORDED, opens_on="resume")
            if not self._should_run(source):
                return result(SwitchOutcome.RECORDED, opens_on="mic_included")  # excluded mic stays closed
            # A lost source already carries its DEVICE_LOST gap from the moment of loss.
            if rt.pending_gap is None and (was_running or rt.state in (SourceState.LOST, SourceState.FAILED)):
                reason = GapReason.DEVICE_SWITCH if was_running or previous != device_id else GapReason.DEVICE_LOST
                rt.pending_gap = (gap_start, reason)
            state_reason = StateReason.DEVICE_SWITCH if previous != device_id else StateReason.RECONNECTED
            code = self._open(source, state_reason)
            if code is None:
                self._refresh_meeting_state()
                return result(SwitchOutcome.APPLIED)
            rollback_code = None
            if previous != device_id:
                rt.device_id = previous  # a device that failed to open never becomes the selection
                if previous is None:
                    rt.state = require_source_transition(rt.state, SourceState.CLOSED)  # nothing was added
                elif was_running:
                    rollback_code = self._open(source, StateReason.DEVICE_SWITCH)
            self._refresh_meeting_state()
            outcome = SwitchOutcome.ROLLED_BACK if rt.state is SourceState.RUNNING else SwitchOutcome.FAILED
            return result(outcome, error=code, rollback_error=rollback_code)

    def reconnect(self, source: Source) -> SwitchResult:
        rt = self._sources[source]
        if rt.device_id is None:
            raise CaptureError("no device selected for this source")
        return self.switch_source(source, rt.device_id, expected_epoch=rt.epoch)

    def stop(self, *, deadline_ns: int | None = None) -> StopResult:
        """Close capture, drain admitted audio within the budget, record the rest as gaps.

        `deadline_ns` (this controller's clock) can shorten the drain to fit a caller's
        overall stop budget. Idempotent. Pending mic audio never re-enters through
        draining: every frame still passes the privacy boundary.
        """
        with self._lock:
            if self._state in (MeetingState.STOPPED, MeetingState.STOPPING):
                return StopResult(0, 0, True)
            if self._state in (MeetingState.IDLE, MeetingState.PREFLIGHT):
                self._state = MeetingState.STOPPED  # nothing was ever opened
                return StopResult(0, 0, True)
            self._set_state(MeetingState.STOPPING)
            for source, rt in self._sources.items():
                if rt.handle is not None:
                    self._close(source, StateReason.STOPPED)
        deadline = self._clock() + self._config.drain_timeout_ns
        if deadline_ns is not None:
            deadline = min(deadline, deadline_ns)
        failed_before = sum(rt.frames_failed for rt in self._sources.values())
        taken = 0
        while self._clock() < deadline:
            moved = self.process_pending(max_frames=256)
            taken += moved
            if moved == 0:
                break
        failed = sum(rt.frames_failed for rt in self._sources.values()) - failed_before
        with self._lock:
            undrained = 0
            for rt in self._sources.values():
                while (frame := rt.queue.get()) is not None:
                    undrained += 1
                    self._losses.append(self._loss(_span_of(frame), GapReason.STOPPED_UNDRAINED))
                self._end_pending_gap(rt)  # lost, or reopened but never delivered
            self._flush_losses()
            end_ms = self._now_ms()
            for record in self.epochs:
                if record.end_ms is None:
                    record.end_ms = end_ms
            self._set_state(MeetingState.STOPPED)
            return StopResult(taken - failed, undrained, undrained == 0 and failed == 0, failed)

    def close(self) -> None:
        """Stop if needed and release the backend (PortAudio) completely."""
        self.stop()
        release = getattr(self._backend, "close", None)
        if callable(release):
            release()

    # -------------------------------------------------------------- worker steps

    def process_pending(self, max_frames: int | None = None) -> int:
        """Forward queued frames, oldest capture time first, through the privacy boundary.

        Returns how many frames were taken from the queues; a frame whose downstream
        processing failed counts as taken and is recorded as a processing_failed gap."""
        moved = 0
        while max_frames is None or moved < max_frames:
            heads = [
                (head, rt) for rt in self._sources.values() if (head := rt.queue.oldest_monotonic_ns) is not None
            ]
            if not heads:
                break
            rt = min(heads, key=lambda item: (item[0], item[1].source.value))[1]
            taken = self._boundary.forward(rt.queue.get, self._deliver)
            if taken is None:
                continue
            moved += 1
        self._flush_losses()
        return moved

    def check_health(self) -> None:
        """Detect lost endpoints: inactive streams, or a mic that stopped delivering."""
        with self._lock:
            if self._state not in (MeetingState.CAPTURING, MeetingState.DEGRADED):
                return
            now = self._clock()
            for source, rt in self._sources.items():
                if rt.state is not SourceState.RUNNING or rt.handle is None:
                    continue
                lost = not rt.handle.is_active()
                timeout = self._config.mic_stall_timeout_ns
                if not lost and source is Source.MIC and timeout is not None:
                    last = rt.last_admit_ns if rt.last_admit_ns is not None else rt.opened_at_ns
                    lost = now - last > timeout
                if lost:
                    self._close(source, StateReason.DEVICE_LOST, lost=True)
            self._refresh_meeting_state()

    def status(self) -> dict[str, Any]:
        """Content-free snapshot for the UI and diagnostics: states, counts, levels."""
        with self._lock:
            sources = {}
            for source, rt in self._sources.items():
                sources[source.value] = {
                    "state": rt.state.value,
                    "device_id": rt.device_id,
                    "epoch": rt.epoch,
                    "failure": rt.failure,
                    "frames_admitted": rt.frames_admitted,
                    "samples_admitted": rt.samples_admitted,
                    "frames_forwarded": rt.frames_forwarded,
                    "frames_rejected_at_gate": self._boundary.rejected_frames[source],
                    "queued_ms": rt.queue.queued_duration_ns // 1_000_000,
                    "level_dbfs": round(self.meter.level(source), 1),
                    "peak_dbfs": round(self.meter.peak(source), 1),
                    "lost_ms": {r.value: ns // 1_000_000 for r, ns in rt.lost_ns.items()},
                    "input_overflows": rt.overflow_reports,
                    "frames_failed_downstream": rt.frames_failed,
                }
            return {
                "meeting_state": self._state.value,
                "mic_included": self.mic_included,
                "privacy_generation": self.privacy_generation,
                "elapsed_ms": self._now_ms(),
                "sources": sources,
                "pcm_bytes": self.pcm_bytes(),
                "peak_pcm_bytes": self.peak_pcm_bytes,
                "pcm_budget_bytes": self._config.max_pcm_bytes_total,
                "gaps": len(self.gaps),
                "events": self._stream_seq,
                "rejected_at_forward": self._boundary.rejected_at_forward,
                "rejected_commits": self._boundary.rejected_commits,
                "sink_errors": self.sink_errors,
                "frames_after_close": self.frames_after_close,
            }

    # ----------------------------------------------------------------- internals

    def _should_run(self, source: Source) -> bool:
        rt = self._sources[source]
        return rt.device_id is not None and (source is Source.SYSTEM or self.mic_included)

    def _now_ms(self) -> int:
        if self._origin_ns is None:
            return 0
        return max(0, (self._clock() - self._origin_ns) // 1_000_000)

    def _ms(self, ns: int) -> int:
        return max(0, (ns - (self._origin_ns or 0)) // 1_000_000)

    def _set_state(self, target: MeetingState) -> None:
        self._state = require_meeting_transition(self._state, target)

    def _refresh_meeting_state(self) -> None:
        if self._state not in (MeetingState.CAPTURING, MeetingState.DEGRADED):
            return
        missing = any(
            self._should_run(s) and rt.state is not SourceState.RUNNING for s, rt in self._sources.items()
        )
        target = MeetingState.DEGRADED if missing else MeetingState.CAPTURING
        if target is not self._state:
            self._set_state(target)

    def _emit(self, event_type: type, payload: Any) -> None:
        with self._lock:
            event = event_type(event_id=uuid4(), meeting_id=self.meeting_id, stream_seq=self._stream_seq, payload=payload)
            self._stream_seq += 1
            self._events.append(event)

    def _emit_state(self, source: Source, reason: StateReason) -> None:
        rt = self._sources[source]
        self._emit(
            CaptureStateEvent,
            CaptureStatePayload(source=source, state=rt.state, epoch=max(rt.epoch, 1), reason=reason, at_ms=self._now_ms()),
        )

    def _describe(self, device_id: str) -> DeviceDescriptor | None:
        resolve = getattr(self._backend, "resolve", None)
        try:
            if callable(resolve):
                return resolve(device_id)
            return next((d for d in self._backend.enumerate() if d.device_id == device_id), None)
        except LookupError:
            return None

    def _open(self, source: Source, reason: StateReason) -> str | None:
        rt = self._sources[source]
        assert rt.device_id is not None
        rt.state = require_source_transition(rt.state, SourceState.OPENING)
        epoch = rt.epoch + 1
        try:
            handle = self._backend.open(
                DeviceSelection(source=source, device_id=rt.device_id),
                self._on_frame,
                meeting_id=self.meeting_id,
                epoch=epoch,
                privacy_generation=self._boundary.generation,
                clock=self._clock,
            )
        except (LookupError, ValueError, OSError) as exc:
            rt.failure = _error_code(exc)
            rt.state = require_source_transition(rt.state, SourceState.FAILED)
            self._emit_state(source, StateReason.OPEN_FAILED)
            return rt.failure
        # Taken once the stream is open and started: time spent opening belongs to a
        # switch/reconnect gap, not to the endpoint's own no-data wait.
        opened_at = self._clock()
        rt.failure = None
        rt.opened_at_ns = opened_at
        # The stall check starts from this stream, so unmuting after a long mute is
        # not mistaken for a lost mic.
        rt.last_admit_ns = None
        rt.accounted_ns = None
        if rt.pending_gap is None:
            # WASAPI loopback delivers nothing until something plays; until the first
            # buffer arrives, the epoch's start is an explicit no-data interval.
            rt.pending_gap = (opened_at, GapReason.NO_DATA_DELIVERED)
        rt.epoch = epoch
        rt.handle = handle  # last: the callback admits this epoch's buffers from here on
        rt.state = require_source_transition(rt.state, SourceState.RUNNING)
        descriptor = self._describe(rt.device_id)
        fmt = descriptor.probed_formats[0] if descriptor and descriptor.probed_formats else None
        self.epochs.append(
            SourceEpoch(
                source=source,
                epoch=epoch,
                device_id=rt.device_id,
                device_label=descriptor.display_name if descriptor else rt.device_id,
                native_rate=fmt.sample_rate if fmt else None,
                native_channels=fmt.channels if fmt else None,
                start_ms=self._now_ms(),
            )
        )
        if fmt is not None:
            self._emit(
                SourceChangedEvent,
                SourceChangedPayload(
                    source=source,
                    epoch=epoch,
                    device_id=rt.device_id,
                    sample_rate=fmt.sample_rate,
                    channels=fmt.channels,
                    at_ms=self._now_ms(),
                ),
            )
        self._emit_state(source, reason)
        return None

    def _end_pending_gap(self, rt: _SourceRuntime) -> None:
        if rt.pending_gap is not None:
            start, reason = rt.pending_gap
            rt.pending_gap = None
            end = self._clock()
            minimum = NO_DATA_MIN_NS if reason is GapReason.NO_DATA_DELIVERED else 1
            if end - start >= minimum:
                self._losses.append(LossRecord(rt.source, max(rt.epoch, 1), start, end, reason))
            self._flush_losses()

    def _close(self, source: Source, reason: StateReason, *, lost: bool = False) -> None:
        rt = self._sources[source]
        handle, rt.handle = rt.handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                pass
        delivered = rt.expected is not None and rt.expected[0] == rt.epoch
        now = self._clock()
        last = rt.last_end_ns if delivered and rt.last_end_ns is not None else rt.opened_at_ns
        rt.accounted_ns = last
        if lost:
            rt.pending_gap = (last, GapReason.DEVICE_LOST)
        elif rt.pending_gap is not None and rt.pending_gap[1] is GapReason.NO_DATA_DELIVERED:
            if now - rt.pending_gap[0] >= NO_DATA_MIN_NS:
                rt.accounted_ns = now
            self._end_pending_gap(rt)  # nothing arrived in this epoch at all
        elif delivered and now - last >= NO_DATA_MIN_NS:
            self._losses.append(LossRecord(source, rt.epoch, last, now, GapReason.NO_DATA_DELIVERED))
            rt.accounted_ns = now
        self._flush_losses()
        if rt.state is SourceState.RUNNING or rt.state is SourceState.SWITCHING:
            rt.state = require_source_transition(rt.state, SourceState.LOST if lost else SourceState.CLOSED)
        end_ms = self._now_ms()
        for record in reversed(self.epochs):
            if record.source is source and record.epoch == rt.epoch and record.end_ms is None:
                record.end_ms = end_ms
                break
        self._emit_state(source, reason)

    # Callback thread ------------------------------------------------------------

    def _on_frame(self, frame: AudioFrame) -> None:
        self._boundary.admit(frame, self._enqueue)

    def _enqueue(self, frame: AudioFrame) -> None:
        """Runs under the privacy boundary lock on the native callback thread."""
        rt = self._sources[frame.source]
        if rt.handle is None or frame.epoch != rt.epoch:
            # A callback that was in flight while its stream closed (stop, pause,
            # switch, loss, exclusion), or that ran before `_open` published the new
            # handle. Its interval lies inside a recorded gap or a closed period;
            # queuing it would leave PCM owned after release.
            self.frames_after_close += 1
            return
        if rt.pending_gap is not None and frame.epoch == rt.epoch:
            start, reason = rt.pending_gap
            rt.pending_gap = None
            first = frame.monotonic_ns
            if reason is GapReason.NO_DATA_DELIVERED:
                if first - start >= NO_DATA_MIN_NS:
                    self._losses.append(LossRecord(frame.source, frame.epoch, start, first, reason))
            elif first - rt.opened_at_ns >= NO_DATA_MIN_NS and rt.opened_at_ns > start:
                # The new endpoint was open but silent (idle loopback): the switch/loss
                # gap ends when it opened; the wait after that is no data, not loss.
                self._losses.append(LossRecord(frame.source, frame.epoch, start, rt.opened_at_ns, reason))
                self._losses.append(
                    LossRecord(frame.source, frame.epoch, rt.opened_at_ns, first, GapReason.NO_DATA_DELIVERED)
                )
            elif first > start:
                self._losses.append(LossRecord(frame.source, frame.epoch, start, first, reason))
        overflow = "input_overflow" in frame.flags
        jumped = rt.expected is not None and rt.expected[0] == frame.epoch and frame.first_sample_index > rt.expected[1]
        if jumped:
            missing = frame.first_sample_index - rt.expected[1]
            start = frame.monotonic_ns - missing * NS_PER_SECOND // frame.sample_rate
            # With a driver overflow report the missing time is discarded input, not idle.
            reason = GapReason.INPUT_OVERFLOW if overflow else GapReason.NO_DATA_DELIVERED
            self._losses.append(LossRecord(frame.source, frame.epoch, start, frame.monotonic_ns, reason))
        elif overflow:
            # The driver says input was discarded before this buffer, but the timing is
            # within jitter, so the amount is unknown: record a point marker, not a guess.
            self._losses.append(
                LossRecord(frame.source, frame.epoch, frame.monotonic_ns, frame.monotonic_ns,
                           GapReason.INPUT_OVERFLOW, duration_known=False)
            )
        if overflow:
            rt.overflow_reports += 1
        rt.expected = (frame.epoch, frame.end_sample_index)
        rt.last_end_ns = frame.monotonic_ns + frame.duration_ns
        rt.last_admit_ns = self._clock()
        rt.frames_admitted += 1
        rt.samples_admitted += frame.frame_count
        for span in rt.queue.put(frame):
            self._losses.append(self._loss(span, GapReason.QUEUE_OVERFLOW))
        self._enforce_budget()

    def _enforce_budget(self) -> None:
        total = self.pcm_bytes()
        while total > self._config.max_pcm_bytes_total:
            heads = [
                (head, rt) for rt in self._sources.values() if (head := rt.queue.oldest_monotonic_ns) is not None
            ]
            if not heads:
                break
            rt = min(heads, key=lambda item: (item[0], item[1].source.value))[1]
            span = rt.queue.drop_oldest()
            if span is not None:
                self._losses.append(self._loss(span, GapReason.PCM_BUDGET))
            total = self.pcm_bytes()
        self.peak_pcm_bytes = max(self.peak_pcm_bytes, total)

    # Worker thread --------------------------------------------------------------

    def _deliver(self, frame: AudioFrame) -> None:
        """Runs under the privacy boundary lock. A failing consumer does not stop
        capture, but the consumed frame's interval becomes an explicit gap: it is not
        retried (a sink may have partly accepted it) and not counted as forwarded."""
        rt = self._sources[frame.source]
        try:
            self._sink.accept(frame)
        except Exception:
            self.sink_errors += 1
            rt.frames_failed += 1
            self._losses.append(self._loss(_span_of(frame), GapReason.PROCESSING_FAILED))
            return
        rt.frames_forwarded += 1

    def _loss(self, span: DroppedSpan, reason: GapReason) -> LossRecord:
        end = span.monotonic_ns + (span.end_sample_index - span.first_sample_index) * NS_PER_SECOND // span.sample_rate
        return LossRecord(span.source, span.epoch, span.monotonic_ns, end, reason)

    def _flush_losses(self) -> None:
        with self._lock:
            while self._losses:
                loss = self._losses.popleft()
                payload = CaptureGapPayload(
                    source=loss.source,
                    epoch=loss.epoch,
                    start_ms=self._ms(loss.start_ns),
                    end_ms=max(self._ms(loss.start_ns), self._ms(loss.end_ns)),
                    reason=loss.reason,
                    duration_known=loss.duration_known,
                )
                rt = self._sources[loss.source]
                rt.lost_ns[loss.reason] = rt.lost_ns.get(loss.reason, 0) + max(0, loss.end_ns - loss.start_ns)
                self.gaps.append(payload)
                self._emit(CaptureGapEvent, payload)


def _span_of(frame: AudioFrame) -> DroppedSpan:
    return DroppedSpan(
        source=frame.source,
        epoch=frame.epoch,
        first_sample_index=frame.first_sample_index,
        end_sample_index=frame.end_sample_index,
        sample_rate=frame.sample_rate,
        monotonic_ns=frame.monotonic_ns,
    )


class CaptureWorker:
    """Background thread that forwards admitted audio and watches source health."""

    def __init__(self, controller: CaptureController, *, interval_s: float = 0.02) -> None:
        self._controller = controller
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="capture-worker", daemon=True)
        self.errors = 0

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval_s):
            try:
                self._controller.process_pending()
                self._controller.check_health()
            except Exception:
                self.errors += 1

    def stop(self, timeout_s: float = 2.0) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout_s)
