"""Bounded local speech pipeline: admitted PCM -> 16 kHz -> VAD chunks -> recognizer -> text.

It is the capture controller's downstream `FrameSink` (capture/sinks.py) and keeps
the mic privacy contract of capture/gate.py all the way to the text consumer:

- `accept` runs under the privacy boundary lock, so it only copies the frame into a
  bounded per-source inbox (`inbox_ms_per_source`); the oldest audio is released as
  an explicit `stt_backlog` gap if the worker falls behind.
- One worker (`step`) resamples, runs VAD and cuts chunks, with independent
  resampler/VAD/segmenter state per source and per contiguous run (a new epoch,
  privacy generation, format or sample discontinuity starts a new run).
- Cut chunks wait in a bounded per-source backlog (`max_pending_ms_per_source`);
  overflow releases the oldest whole chunks as `stt_backlog` gaps, never a spool.
- The single recognizer is shared fairly: finals are taken round-robin across
  sources; a partial is decoded only when no final is waiting anywhere. One decoder
  lock admits one caller at a time, whether the worker or `finish`.
- Mic results pass two checks: a chunk enters the recognizer only after a claim
  made under the privacy boundary lock (`PrivacyBoundary.dispatch`) finds its
  generation admitted and its source not reset, and its text reaches the consumer
  only through `PrivacyBoundary.commit` and only if the source was not reset since.
  `reset(MIC)` (called by the controller inside the exclusion and inclusion
  transitions) discards the inbox, the open utterance, pre-roll and waiting chunks
  and voids shown partials, so excluded speech is never decoded or replayed later.
  System audio is unaffected.

A decode whose claim succeeded before the mic was excluded finishes on pre-exclusion
audio (the recognizer cannot be interrupted) and its result is rejected at commit.
The claim is the last step before the adapter call, so a job still waiting, or
picked but not yet claimed, when exclusion completes never reaches the recognizer.

Loss accounting: admitted audio that yields no text for any reason other than VAD
silence, mic exclusion or an empty transcription becomes a `SpeechGap`: released by
a full backlog, failed in the resampler, VAD or recognizer, refused by the text
consumer, or still waiting or decoding when `finish` ends. Each failure is also a
content-free fault record (stage, source, exception type). `finish` reports
complete coverage only if nothing was left and no gap or fault ever occurred.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import UUID

import numpy as np

from meeting_minutes.capture.base import Clock
from meeting_minutes.capture.gate import PrivacyBoundary
from meeting_minutes.domain.contracts import NS_PER_SECOND, TARGET_SAMPLE_RATE, AudioChunk, AudioFrame, Source
from meeting_minutes.domain.events import GapReason
from meeting_minutes.domain.transcript import PartialsCleared, SpeechGap, TextEvent, TextKind, TranscriptText
from meeting_minutes.speech.config import SAMPLES_PER_MS, SpeechConfig
from meeting_minutes.speech.dedup import trim_overlap
from meeting_minutes.speech.resample import Resampler, ResamplerFactory, downmix, soxr_factory
from meeting_minutes.speech.segmenter import Cut, FrameVad, Segmenter

LATENCY_SAMPLES = 4096
FAULT_LOG = 64
# Front-end failures this close together extend one gap rather than one gap per
# buffer; a still-failing interval is reported once it spans FAULT_GAP_MAX_NS.
FAULT_JOIN_NS = NS_PER_SECOND
FAULT_GAP_MAX_NS = 5 * NS_PER_SECOND


@dataclass(frozen=True, slots=True)
class RecognizedText:
    text: str = field(repr=False)
    language: str | None = None
    # Optional quality marks (e.g. an uncertain language); saved with the segment and
    # shown with it, never a change to coverage or `complete`.
    flags: tuple[str, ...] = ()


class Recognizer(Protocol):
    def transcribe(self, chunk: AudioChunk) -> RecognizedText:
        """Decode one bounded 16 kHz mono chunk; must not keep the samples."""
        ...


@dataclass(frozen=True, slots=True)
class DecodeGuard:
    """Given to a recognizer that declares `accepts_guard = True` and may make more than
    one request for a chunk: before another request it checks that the chunk is still
    admitted (not excluded, reset or abandoned; serialized like the dispatch claim)
    and how many seconds of the Stop budget are left (None while not stopping)."""

    admitted: Callable[[], bool]
    seconds_left: Callable[[], float | None]


TextConsumer = Callable[[TextEvent], None]


@dataclass(eq=False)
class _Job:
    kind: TextKind
    chunk: AudioChunk
    token: int
    cut_ns: int  # worker clock when the chunk was cut (end of utterance detected)
    abandoned: bool = False  # `finish` has accounted for it: it may never publish


@dataclass(eq=False)
class _Run:
    meeting_id: UUID
    epoch: int
    generation: int
    sample_rate: int
    channels: int
    anchor_ns: int  # capture time of the run's first native sample
    first_index: int  # first_sample_index of the run's first frame
    next_index: int  # expected first_sample_index of the next frame
    resampler: Resampler
    segmenter: Segmenter


@dataclass(eq=False)
class _SourceState:
    source: Source
    inbox: deque[AudioFrame] = field(default_factory=deque)
    inbox_ns: int = 0
    run: _Run | None = None
    vad: FrameVad | None = None
    pending: deque[_Job] = field(default_factory=deque)
    pending_samples: int = 0
    last_frame_ns: int = 0
    last_partial_ns: int | None = None
    token: int = 0  # advanced by reset: invalidates dispatched work
    chunk_counter: int = 0
    last_final: tuple[int, int, str] | None = None  # (epoch, chunk_index, text) for dedup
    fault_gap: list[int] | None = None  # [epoch, start_ns, end_ns] of failing front-end input
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(
        ("chunks_cut", "short_discarded", "finals", "empty_finals", "partials", "rejected_at_input",
         "rejected_at_dispatch", "rejected_at_commit", "rejected_after_finish", "decode_failures",
         "processing_failures", "delivery_failures", "backlog_dropped_ms", "resets"), 0))


class SpeechStats:
    """Content-free counters and timings (no text, no audio)."""

    def __init__(self) -> None:
        self.decode_ns = 0
        self.decoded_audio_ms = 0
        self.decodes = 0
        self.partial_decode_ns = 0
        self.first_frame_ns: int | None = None
        self.decode_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self.rtf: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self.final_latency_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)
        self.queue_wait_ms: deque[float] = deque(maxlen=LATENCY_SAMPLES)


class SpeechPipeline:
    def __init__(
        self,
        recognizer: Recognizer,
        vad_factory: Callable[[], FrameVad],
        *,
        on_text: TextConsumer,
        resampler_factory: ResamplerFactory = soxr_factory,
        config: SpeechConfig = SpeechConfig(),
        clock: Clock = time.monotonic_ns,
    ) -> None:
        self._recognizer = recognizer
        self._vad_factory = vad_factory
        self._on_text = on_text
        self._resampler_factory = resampler_factory
        self._config = config
        self._clock = clock
        # Lock order: privacy boundary lock -> self._lock. self._decoder is taken
        # before either and held from picking a job until its commit.
        self._lock = threading.Lock()
        self._decoder = threading.Lock()
        self._finishing = threading.Lock()  # one finalizer at a time
        self._wake = threading.Event()
        self._sources = {s: _SourceState(s) for s in Source}
        self._order = list(Source)
        self._boundary: PrivacyBoundary | None = None
        self._origin: Callable[[], int | None] = lambda: None
        self._dispatched: _Job | None = None  # picked for decoding and not yet committed
        self._finished: dict[str, Any] | None = None  # the finish report, once finished
        self._finish_deadline: int | None = None  # set once `finish` starts
        self._speech_gaps = 0
        self._faults: deque[dict[str, str | None]] = deque(maxlen=FAULT_LOG)
        self.fault_count = 0
        self.stats = SpeechStats()

    # ------------------------------------------------------------------ wiring

    def bind(self, boundary: PrivacyBoundary, origin_ns: Callable[[], int | None]) -> None:
        self._boundary = boundary
        self._origin = origin_ns

    def attach(self, controller: Any) -> None:
        """Use a CaptureController's privacy boundary and meeting time origin."""
        self.bind(controller.boundary, lambda: controller.origin_ns)

    @property
    def wake(self) -> threading.Event:
        return self._wake

    # --------------------------------------------- FrameSink (boundary lock held)

    def accept(self, frame: AudioFrame) -> None:
        events: list[TextEvent] = []
        limit = self._config.inbox_ms_per_source * 1_000_000
        with self._lock:
            st = self._sources[frame.source]
            if self._finished is not None:
                # Capture outlived `finish`: this audio will never be decoded.
                st.counts["rejected_after_finish"] += 1
                events.append(self._gap(frame.source, frame.epoch, frame.monotonic_ns,
                                        frame.monotonic_ns + frame.duration_ns, GapReason.STOPPED_UNDRAINED))
            else:
                st.inbox.append(frame)
                st.inbox_ns += frame.duration_ns
                st.last_frame_ns = self._clock()
                if self.stats.first_frame_ns is None:
                    self.stats.first_frame_ns = st.last_frame_ns
                while st.inbox_ns > limit and len(st.inbox) > 1:
                    old = st.inbox.popleft()
                    st.inbox_ns -= old.duration_ns
                    st.counts["backlog_dropped_ms"] += old.duration_ns // 1_000_000
                    events.append(self._gap(old.source, old.epoch, old.monotonic_ns,
                                            old.monotonic_ns + old.duration_ns, GapReason.STT_BACKLOG))
        self._emit(events)
        self._wake.set()

    def reset(self, source: Source) -> int:
        """Discard everything not yet committed for `source`; returns discarded ns."""
        events: list[TextEvent] = []
        with self._lock:
            st = self._sources[source]
            self._close_fault_gap(st, events)  # audio lost to a failure before the reset
            samples = sum(j.chunk.data.shape[0] - j.chunk.overlap_ms * SAMPLES_PER_MS for j in st.pending)
            if st.run is not None:
                samples += st.run.segmenter.active_samples - st.run.segmenter.open_overlap_samples
            discarded = st.inbox_ns + samples * NS_PER_SECOND // TARGET_SAMPLE_RATE
            st.inbox.clear()
            st.inbox_ns = 0
            st.pending.clear()
            st.pending_samples = 0
            st.run = None
            st.last_final = None
            st.last_partial_ns = None
            st.token += 1
            st.counts["resets"] += 1
        self._emit([*events, PartialsCleared(source=source)])
        return discarded

    # ------------------------------------------------------------ worker side

    def step(self, *, partials: bool | None = None) -> bool:
        """Ingest admitted audio and decode at most one job. Returns whether work was done."""
        ingested = self.ingest()
        return self.decode_next(partials=partials) or ingested

    def ingest(self) -> bool:
        """Resample, run VAD and cut chunks for everything in the inboxes; no decoding."""
        if self._boundary is None:
            raise RuntimeError("bind the speech pipeline to a privacy boundary first")
        events: list[TextEvent] = []
        with self._lock:
            ingested = self._ingest(self._clock(), events)
        self._emit(events)
        return ingested

    def decode_next(self, *, partials: bool | None = None) -> bool:
        """Decode and commit at most one job (finals round-robin, then a partial)."""
        if self._boundary is None:
            raise RuntimeError("bind the speech pipeline to a privacy boundary first")
        with self._decoder:
            return self._decode_one(self._config.partials if partials is None else partials)

    def finish(self, deadline_ns: int) -> bool:
        """End of capture: close every open utterance, decode what is waiting until
        `deadline_ns`, and record the rest as `stopped_undrained`.

        Returns whether transcript coverage is complete: everything drained, nothing
        left decoding, and no speech gap or fault at any point in this pipeline's life.
        It returns by the deadline whatever the recognizer does: the drain decodes on
        one helper thread, and a decode still running at the deadline (on that thread
        or on a worker whose stop timed out) is recorded as undrained and its result
        can never be published afterwards. `finish_report` separates drain from
        coverage. Concurrent and later calls return the same answer.
        """
        if self._boundary is None:
            raise RuntimeError("bind the speech pipeline to a privacy boundary first")
        with self._finishing:
            events: list[TextEvent] = []
            with self._lock:
                if self._finished is not None:
                    return bool(self._finished["complete"])
                self._finish_deadline = deadline_ns
                now = self._clock()
                self._ingest(now, events)
                for st in self._sources.values():
                    self._end_run(st, now, events)
            self._emit(events)
            remaining = deadline_ns - self._clock()
            if remaining <= 0:  # no time to decode anything: decide now
                drained = self._decoder.acquire(blocking=False)
                try:
                    report = self._boundary.serialized(lambda: self._close(drained))
                finally:
                    if drained:
                        self._decoder.release()
                return bool(report["complete"])
            done, acquired = threading.Event(), threading.Event()

            def drain() -> None:
                # Native recognition cannot be interrupted, so it never runs on the
                # finalizer's thread; this thread ends once its decode returns.
                try:
                    if not self._decoder.acquire(timeout=max(0, deadline_ns - self._clock()) / NS_PER_SECOND):
                        return
                    acquired.set()
                    try:
                        while self._clock() < deadline_ns and (self.ingest() | self._decode_one(False)):
                            pass
                    finally:
                        self._decoder.release()
                except Exception as exc:
                    self.record_fault("finish", exc)
                finally:
                    done.set()

            threading.Thread(target=drain, name="speech-drain", daemon=True).start()
            done.wait(max(0, deadline_ns - self._clock()) / NS_PER_SECOND)
            drained = done.is_set() and acquired.is_set()
            report = self._boundary.serialized(lambda: self._close(drained))
            return bool(report["complete"])

    @property
    def finish_report(self) -> dict[str, Any] | None:
        with self._lock:
            return None if self._finished is None else dict(self._finished)

    def record_fault(self, stage: str, exc: BaseException) -> None:
        """A failure outside the pipeline's own stages (e.g. the worker loop)."""
        with self._lock:
            self._fault(stage, None, exc)

    def status(self) -> dict[str, Any]:
        """Content-free snapshot: backlog, owned PCM, counters and timing summaries."""
        with self._lock:
            now = self._clock()
            sources = {}
            owned = 0
            for source, st in self._sources.items():
                inbox_bytes = sum(f.nbytes for f in st.inbox)
                pending_bytes = sum(j.chunk.data.nbytes for j in st.pending)
                active_bytes = st.run.segmenter.owned_bytes if st.run else 0
                owned += inbox_bytes + pending_bytes + active_bytes
                sources[source.value] = {
                    "inbox_ms": st.inbox_ns // 1_000_000,
                    "pending_chunks": len(st.pending),
                    "pending_ms": st.pending_samples // SAMPLES_PER_MS,
                    "oldest_pending_age_ms": (now - st.pending[0].cut_ns) // 1_000_000 if st.pending else 0,
                    "open_utterance_ms": st.run.segmenter.active_samples // SAMPLES_PER_MS if st.run else 0,
                    **st.counts,
                }
            job = self._dispatched
            if job is not None:
                owned += job.chunk.data.nbytes  # the recognizer holds it until it returns
            s = self.stats
            wall = now - s.first_frame_ns if s.first_frame_ns is not None else 0
            return {
                "sources": sources,
                "owned_pcm_bytes": owned,
                "in_flight": None if job is None else {
                    "source": job.chunk.source.value, "kind": job.kind.value,
                    "audio_ms": job.chunk.end_ms - job.chunk.start_ms, "abandoned": job.abandoned,
                },
                "speech_gaps": self._speech_gaps,
                "faults": self.fault_count,
                "fault_log": list(self._faults),
                "finished": self._finished is not None,
                "decodes": s.decodes,
                "decoded_audio_ms": s.decoded_audio_ms,
                "decode_ms_total": s.decode_ns // 1_000_000,
                "duty": round((s.decode_ns + s.partial_decode_ns) / wall, 4) if wall > 0 else 0.0,
            }

    # --------------------------------------------------------------- internals

    def _ingest(self, now: int, events: list[TextEvent]) -> bool:
        if self._finished is not None:
            return False
        did = False
        idle = self._config.idle_finalize_ms * 1_000_000
        for st in self._sources.values():
            while st.inbox:
                frame = st.inbox.popleft()
                st.inbox_ns -= frame.duration_ns
                self._front(st, frame, now, events)
                did = True
            if now - st.last_frame_ns >= idle:
                if st.run is not None and st.run.segmenter.active_samples:
                    self._end_run(st, now, events)
                    did = True
                self._close_fault_gap(st, events)
        return did

    def _front(self, st: _SourceState, frame: AudioFrame, now: int, events: list[TextEvent]) -> None:
        assert self._boundary is not None
        if not self._boundary.state.admits(frame.source, frame.privacy_generation):
            st.counts["rejected_at_input"] += 1  # reset clears the inbox first; defensive only
            return
        run = st.run
        if run is not None and not (
            frame.epoch == run.epoch and frame.privacy_generation == run.generation
            and frame.first_sample_index == run.next_index
            and frame.sample_rate == run.sample_rate and frame.channels == run.channels
        ):
            self._end_run(st, now, events)
            run = None
        lost_from = frame.monotonic_ns
        try:
            if run is None:
                if st.vad is None:
                    st.vad = self._vad_factory()
                else:
                    st.vad.reset()
                run = st.run = _Run(
                    meeting_id=frame.meeting_id, epoch=frame.epoch, generation=frame.privacy_generation,
                    sample_rate=frame.sample_rate, channels=frame.channels, anchor_ns=frame.monotonic_ns,
                    first_index=frame.first_sample_index, next_index=frame.first_sample_index,
                    resampler=self._resampler_factory(frame.sample_rate),
                    segmenter=Segmenter(st.vad, self._config),
                )
            # If this frame fails, everything not yet cut is lost, including chunks
            # the failing feed would have cut.
            lost_from = min(lost_from, self._run_ns(run, run.segmenter.uncommitted_start))
            run.next_index = frame.end_sample_index
            cuts = run.segmenter.feed(run.resampler.process(downmix(frame.data)))
        except Exception as exc:
            self._front_failed(st, frame.epoch, lost_from, frame.monotonic_ns + frame.duration_ns, exc, events)
            return
        self._queue_cuts(st, run, cuts, now, events)
        if st.fault_gap is not None and frame.monotonic_ns > st.fault_gap[2] + FAULT_JOIN_NS:
            self._close_fault_gap(st, events)  # processing has worked again for a while

    def _end_run(self, st: _SourceState, now: int, events: list[TextEvent]) -> None:
        run = st.run
        st.run = None
        if run is None:
            return
        lost_from = self._run_ns(run, run.segmenter.uncommitted_start)
        try:
            cuts = run.segmenter.feed(run.resampler.flush()) + run.segmenter.flush()
        except Exception as exc:
            end = run.anchor_ns + (run.next_index - run.first_index) * NS_PER_SECOND // run.sample_rate
            self._front_failed(st, run.epoch, lost_from, end, exc, events)
            return
        self._queue_cuts(st, run, cuts, now, events)
        st.counts["short_discarded"] += run.segmenter.short_discarded

    def _front_failed(self, st: _SourceState, epoch: int, start_ns: int, end_ns: int, exc: Exception,
                      events: list[TextEvent]) -> None:
        """The resampler or VAD raised: the run's audio not yet cut is lost. The next
        frame starts a fresh run (new resampler and VAD) anchored at its own capture
        time, so later timestamps are not shifted. The other source is unaffected."""
        st.run = None
        st.vad = None
        st.last_final = None  # the next chunk does not continue the last final
        st.last_partial_ns = None
        st.counts["processing_failures"] += 1
        self._fault("front_end", st.source, exc)
        gap = st.fault_gap
        if gap is not None and gap[0] == epoch and start_ns <= gap[2] + FAULT_JOIN_NS:
            gap[1], gap[2] = min(gap[1], start_ns), max(gap[2], end_ns)
        else:
            self._close_fault_gap(st, events)
            gap = st.fault_gap = [epoch, start_ns, end_ns]
        if gap[2] - gap[1] >= FAULT_GAP_MAX_NS:
            self._close_fault_gap(st, events)

    def _close_fault_gap(self, st: _SourceState, events: list[TextEvent]) -> None:
        if st.fault_gap is not None:
            epoch, start_ns, end_ns = st.fault_gap
            st.fault_gap = None
            events.append(self._gap(st.source, epoch, start_ns, end_ns, GapReason.PROCESSING_FAILED))

    @staticmethod
    def _run_ns(run: _Run, sample: int) -> int:
        """Capture time of a 16 kHz run sample index."""
        return run.anchor_ns + sample * NS_PER_SECOND // TARGET_SAMPLE_RATE

    def _queue_cuts(self, st: _SourceState, run: _Run, cuts: list[Cut], now: int, events: list[TextEvent]) -> None:
        for cut in cuts:
            chunk = self._chunk(st, run, cut.start, cut.samples, cut.overlap, cut.forced, st.chunk_counter)
            st.chunk_counter += 1
            st.counts["chunks_cut"] += 1
            st.pending.append(_Job(TextKind.FINAL, chunk, st.token, now))
            st.pending_samples += chunk.data.shape[0]
        limit = self._config.max_pending_ms_per_source * SAMPLES_PER_MS
        while st.pending_samples > limit and st.pending:
            job = st.pending.popleft()
            st.pending_samples -= job.chunk.data.shape[0]
            st.counts["backlog_dropped_ms"] += job.chunk.end_ms - job.chunk.start_ms
            events.append(self._chunk_gap(job.chunk, GapReason.STT_BACKLOG))

    def _chunk(self, st: _SourceState, run: _Run, start: int, samples: np.ndarray, overlap: int,
               forced: bool, index: int) -> AudioChunk:
        start_ms = self._ms(self._run_ns(run, start))
        return AudioChunk(
            meeting_id=run.meeting_id, source=st.source, epoch=run.epoch, privacy_generation=run.generation,
            chunk_index=index, start_ms=start_ms, end_ms=start_ms + max(1, samples.shape[0] // SAMPLES_PER_MS),
            data=samples, overlap_ms=overlap // SAMPLES_PER_MS, forced_cut=forced,
        )

    def _next_job(self, now: int, partials: bool) -> _Job | None:
        assert self._boundary is not None
        state = self._boundary.state
        for source in list(self._order):
            st = self._sources[source]
            while st.pending:
                job = st.pending.popleft()
                st.pending_samples -= job.chunk.data.shape[0]
                if state.admits(source, job.chunk.privacy_generation):
                    self._order.remove(source)
                    self._order.append(source)  # round-robin: this source goes last next time
                    return job
                st.counts["rejected_at_input"] += 1
        if not partials:
            return None
        interval = self._config.partial_interval_ms * 1_000_000
        minimum = self._config.partial_min_ms * SAMPLES_PER_MS
        for source in list(self._order):
            st = self._sources[source]
            run = st.run
            if run is None or run.segmenter.active_samples < minimum:
                continue
            if st.last_partial_ns is not None and now - st.last_partial_ns < interval:
                continue
            if not state.admits(source, run.generation):
                continue
            snap = run.segmenter.snapshot()
            assert snap is not None
            start, overlap, samples = snap
            st.last_partial_ns = now
            self._order.remove(source)
            self._order.append(source)
            chunk = self._chunk(st, run, start, samples, overlap, False, st.chunk_counter)
            return _Job(TextKind.PARTIAL, chunk, st.token, now)
        return None

    def _decode_one(self, partials: bool) -> bool:
        """With the decoder lock held: pick, decode and commit at most one job."""
        with self._lock:
            job = None if self._finished is not None else self._next_job(self._clock(), partials)
            self._dispatched = job  # visible to `finish` and `status` until committed
        if job is None:
            return False
        self._decode(job)
        return True

    def _release(self, job: _Job) -> None:
        if self._dispatched is job:
            self._dispatched = None

    def _claim(self, job: _Job) -> bool:
        """The last check before recognition, serialized with exclusion and `finish`."""
        assert self._boundary is not None
        st = self._sources[job.chunk.source]

        def start() -> bool:
            with self._lock:
                return not job.abandoned and job.token == st.token

        if self._boundary.dispatch(job.chunk.source, job.chunk.privacy_generation, start):
            return True
        with self._lock:
            self._release(job)
            if not job.abandoned:
                st.counts["rejected_at_dispatch"] += 1
        return False

    def _guard(self, job: _Job) -> DecodeGuard:
        boundary = self._boundary
        assert boundary is not None
        st = self._sources[job.chunk.source]

        def admitted() -> bool:
            def check() -> bool:
                with self._lock:
                    current = not job.abandoned and job.token == st.token and self._finished is None
                return current and boundary.state.admits(job.chunk.source, job.chunk.privacy_generation)

            return boundary.serialized(check)

        def seconds_left() -> float | None:
            deadline = self._finish_deadline
            return None if deadline is None else (deadline - self._clock()) / NS_PER_SECOND

        return DecodeGuard(admitted, seconds_left)

    def _decode(self, job: _Job) -> None:
        assert self._boundary is not None
        if not self._claim(job):
            return
        started = self._clock()
        try:
            if getattr(self._recognizer, "accepts_guard", False):
                result = self._recognizer.transcribe(job.chunk, guard=self._guard(job))
            else:
                result = self._recognizer.transcribe(job.chunk)
        except Exception as exc:
            def failed() -> None:
                events: list[TextEvent] = []
                with self._lock:
                    self._release(job)
                    st = self._sources[job.chunk.source]
                    st.counts["decode_failures"] += 1
                    self._fault("recognizer", job.chunk.source, exc)
                    if job.kind is TextKind.FINAL:
                        st.last_final = None
                        if not job.abandoned:  # else `finish` already reported it
                            events.append(self._chunk_gap(job.chunk, GapReason.STT_FAILED))
                self._emit(events)

            self._boundary.serialized(failed)
            return
        finished = self._clock()
        audio_ms = job.chunk.end_ms - job.chunk.start_ms
        s = self.stats
        if job.kind is TextKind.FINAL:
            s.decodes += 1
            s.decode_ns += finished - started
            s.decoded_audio_ms += audio_ms
            s.decode_ms.append((finished - started) / 1e6)
            s.rtf.append((finished - started) / 1e6 / audio_ms)
            s.queue_wait_ms.append((started - job.cut_ns) / 1e6)
        else:
            s.partial_decode_ns += finished - started
        self._commit(job, result)

    def _commit(self, job: _Job, result: RecognizedText) -> None:
        assert self._boundary is not None
        chunk = job.chunk
        st = self._sources[chunk.source]

        def prepare() -> TextEvent | None:
            # self._lock held.
            if job.abandoned or self._finished is not None:
                st.counts["rejected_after_finish"] += 1
                return None
            if job.token != st.token:
                st.counts["rejected_at_commit"] += 1
                return None
            if job.kind is TextKind.PARTIAL and st.chunk_counter > chunk.chunk_index:
                return None  # its chunk was already cut; the final supersedes it
            text, trimmed, ambiguous = result.text.strip(), 0, False
            if chunk.overlap_ms and st.last_final is not None and st.last_final[:2] == (
                chunk.epoch, chunk.chunk_index - 1
            ):
                text, trimmed, ambiguous = trim_overlap(
                    st.last_final[2], text, max_tokens=self._config.dedup_max_tokens
                )
            if not text:
                if job.kind is TextKind.FINAL:
                    return PartialsCleared(source=chunk.source, through_chunk_index=chunk.chunk_index)
                return None
            return TranscriptText(
                kind=job.kind, meeting_id=chunk.meeting_id, source=chunk.source, epoch=chunk.epoch,
                privacy_generation=chunk.privacy_generation, hypothesis_id=f"{chunk.source.value}:{chunk.chunk_index}",
                chunk_index=chunk.chunk_index, start_ms=chunk.start_ms, end_ms=chunk.end_ms, text=text,
                language=result.language, forced_cut=chunk.forced_cut, overlap_trimmed_words=trimmed,
                boundary_ambiguous=ambiguous, quality_flags=tuple(result.flags),
            )

        def deliver() -> None:
            # Under the boundary lock from the check until the consumer returns: no
            # exclusion or `finish` slips in between, and a refused delivery is
            # accounted for before either can run.
            events: list[TextEvent] = []
            with self._lock:
                self._release(job)
                event = prepare()
            if event is None:
                return
            try:
                self._on_text(event)
            except Exception as exc:
                with self._lock:
                    st.counts["delivery_failures"] += 1
                    self._fault("delivery", chunk.source, exc)
                    if job.kind is TextKind.FINAL:
                        st.last_final = None  # its words were not shown; never trim against them
                        if isinstance(event, TranscriptText):
                            events.append(self._chunk_gap(chunk, GapReason.PROCESSING_FAILED))
                self._emit(events)
                return
            with self._lock:  # counted as delivered only once the consumer took it
                if job.kind is TextKind.PARTIAL:
                    st.counts["partials"] += 1
                    return
                st.last_final = (chunk.epoch, chunk.chunk_index, result.text.strip())
                if isinstance(event, TranscriptText):
                    st.counts["finals"] += 1
                    self.stats.final_latency_ms.append((self._clock() - job.cut_ns) / 1e6)
                else:
                    st.counts["empty_finals"] += 1

        accepted, _ = self._boundary.commit(chunk.source, chunk.privacy_generation, deliver)
        if not accepted:
            with self._lock:
                self._release(job)
                st.counts["rejected_at_commit"] += 1

    def _close(self, drained: bool) -> dict[str, Any]:
        """Terminal step of `finish`, under the boundary lock: whatever is not committed
        by now becomes a `stopped_undrained` gap and can never be published."""
        assert self._boundary is not None
        events: list[TextEvent] = []
        with self._lock:
            now = self._clock()
            self._ingest(now, events)  # audio accepted while draining
            undrained = 0
            for st in self._sources.values():
                self._end_run(st, now, events)
                self._close_fault_gap(st, events)
                for job in st.pending:
                    events.append(self._chunk_gap(job.chunk, GapReason.STOPPED_UNDRAINED))
                    undrained += 1
                st.pending.clear()
                st.pending_samples = 0
            job = self._dispatched
            if job is not None:
                job.abandoned = True
                chunk = job.chunk
                # A mic job voided by exclusion is discarded on purpose, not lost.
                if (job.kind is TextKind.FINAL and job.token == self._sources[chunk.source].token
                        and self._boundary.state.admits(chunk.source, chunk.privacy_generation)):
                    events.append(self._chunk_gap(chunk, GapReason.STOPPED_UNDRAINED))
            self._finished = {
                "drain_complete": drained and undrained == 0 and job is None,
                "decoder_acquired": drained,
                "undrained_chunks": undrained,
                "abandoned_in_flight": job is not None,
            }
        self._emit(events)
        with self._lock:
            report = self._finished
            report.update(speech_gaps=self._speech_gaps, faults=self.fault_count)
            report["complete"] = report["drain_complete"] and not self._speech_gaps and not self.fault_count
            return dict(report)

    def _ms(self, ns: int) -> int:
        origin = self._origin()
        if origin is None:
            origin = self.stats.first_frame_ns or 0
        return max(0, (ns - origin) // 1_000_000)

    # Gap constructors run with self._lock held; every gap is a known loss.

    def _gap(self, source: Source, epoch: int, start_ns: int, end_ns: int, reason: GapReason) -> SpeechGap:
        self._speech_gaps += 1
        start = self._ms(start_ns)
        return SpeechGap(source=source, epoch=epoch, start_ms=start, end_ms=max(start, self._ms(end_ns)), reason=reason)

    def _chunk_gap(self, chunk: AudioChunk, reason: GapReason) -> SpeechGap:
        self._speech_gaps += 1
        # The overlap was already covered by the previous chunk of the run.
        start = min(chunk.start_ms + chunk.overlap_ms, chunk.end_ms)
        return SpeechGap(source=chunk.source, epoch=chunk.epoch, start_ms=start, end_ms=chunk.end_ms, reason=reason)

    def _fault(self, stage: str, source: Source | None, exc: BaseException) -> None:
        """Content-free fault record: stage, source and exception type only (self._lock held)."""
        self.fault_count += 1
        self._faults.append({"stage": stage, "source": source.value if source else None,
                             "error": type(exc).__name__})

    def _emit(self, events: list[TextEvent]) -> None:
        """Never called with self._lock held. A consumer that refuses a gap or clear
        event is recorded as a fault, so `finish` cannot report full coverage."""
        for event in events:
            try:
                self._on_text(event)
            except Exception as exc:
                with self._lock:
                    self._fault("consumer", event.source, exc)


class SpeechWorker:
    """Two threads over one pipeline.

    - `speech-ingest` drains the 2 s inboxes into resampling/VAD and cuts chunks
      (`ingest`, short and under the pipeline lock), so admitted audio keeps moving
      into the byte-bounded pending queue (`max_pending_ms_per_source`) while a
      decode waits on the recognizer.
    - `speech-worker` decodes and commits one job at a time (`decode_next`, under the
      decoder lock), finals round-robin across sources, then partials.

    Splitting ingest from decoding adds no queue or buffer: a slow or stuck decode
    delays text instead of overflowing the inbox; only a backlog beyond the pending
    cap is an explicit `stt_backlog` gap. Lock order: decoder -> boundary -> pipeline."""

    def __init__(self, pipeline: SpeechPipeline, *, interval_s: float = 0.05) -> None:
        self._pipeline = pipeline
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._cut = threading.Event()  # set when ingest may have queued work for decoding
        self._ingest_thread = threading.Thread(target=self._ingest_loop, name="speech-ingest", daemon=True)
        self._thread = threading.Thread(target=self._run, name="speech-worker", daemon=True)

    @property
    def errors(self) -> int:
        """Faults the pipeline recorded: stage failures and unexpected loop errors."""
        return self._pipeline.fault_count

    def start(self) -> None:
        self._ingest_thread.start()
        self._thread.start()

    def _ingest_loop(self) -> None:
        wake = self._pipeline.wake
        while not self._stop.is_set():
            try:
                if self._pipeline.ingest():
                    self._cut.set()
            except Exception as exc:
                self._pipeline.record_fault("worker", exc)
            wake.wait(self._interval_s)
            wake.clear()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                busy = self._pipeline.decode_next()
            except Exception as exc:
                self._pipeline.record_fault("worker", exc)
                busy = False
            if not busy:
                self._cut.wait(self._interval_s)
                self._cut.clear()

    def stop(self, timeout_s: float = 30.0) -> bool:
        """Waits up to `timeout_s` for a running decode (it cannot be interrupted).
        Returns whether both threads stopped; if not, `SpeechPipeline.finish` records
        the decode as undrained and voids its result."""
        self._stop.set()
        self._pipeline.wake.set()
        self._cut.set()
        deadline = time.monotonic() + max(0.0, timeout_s)
        for thread in (self._ingest_thread, self._thread):
            if thread.is_alive():
                thread.join(max(0.0, deadline - time.monotonic()))
        return not (self._ingest_thread.is_alive() or self._thread.is_alive())
