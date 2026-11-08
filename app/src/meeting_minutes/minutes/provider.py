"""Provider boundary for minutes generation.

`MinutesGenerator.generate(snapshot, options, cancel, progress)` returns the canonical
content of `schema.py` or raises `GenerationError`. The UI and service only see this
result and a provider display name, so a later local provider plugs in unchanged.

`MapReduceGenerator` holds the shared, provider-neutral part: bounded windows split on
segment boundaries (never truncating), the mom-v1 envelope, local validation with at
most one repair request per job, and a structured merge that keeps every item's
evidence (no late action is dropped). A concrete provider implements `_request` only.

`GenerationError.request_sent` is True (a request reached or was fully sent to the
provider), False (proven: stopped before anything was sent) or None (unknown, e.g. a
connection that failed mid-way). Only False may be described as "no request was made".
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from meeting_minutes.minutes.schema import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    empty_content,
    normalize_content,
    parse_output,
    validate_content,
)

Progress = Callable[[str, int, int], None]  # step ('preparing'|'sending'|'validating'), part i, of n

# Content-free failure codes (provider errors plus app-side checks).
ERROR_CODES = (
    "no_key", "vault_unavailable", "offline", "auth", "model_unavailable", "quota", "rate_limit", "timeout",
    "refusal", "invalid_output", "cancelled", "too_large", "server_error", "request_rejected",
    "transcript_changed", "transcript_invalidated", "not_saved", "empty_transcript", "busy", "storage",
    "budget", "price_unknown",
)

# Recognition-quality flags of a saved line that are passed to the minutes model.
# UNCERTAIN_FLAGS are the ones that mean the words themselves may be wrong.
RECOGNITION_FLAGS = ("low_confidence", "lang_uncertain", "filler_suspect", "lang_retry")
UNCERTAIN_FLAGS = ("low_confidence", "lang_uncertain", "filler_suspect")


class GenerationError(Exception):
    def __init__(self, code: str, *, request_sent: bool | None, detail: str = "", invalid_count: int = 0) -> None:
        super().__init__(f"minutes generation failed: {code}")
        self.code = code
        self.request_sent = request_sent
        self.detail = detail  # content-free technical detail (HTTP status, error code)
        self.invalid_count = invalid_count

    def __repr__(self) -> str:
        return f"GenerationError({self.code!r}, request_sent={self.request_sent!r}, detail={self.detail!r})"


@dataclass(frozen=True, slots=True)
class SnapshotSegment:
    id: str
    revision: int
    source: str  # 'mic' | 'system' — an audio source, not a person
    start_ms: int
    end_ms: int
    text: str = field(repr=False)
    language: str = "unknown"
    flags: tuple[str, ...] = ()  # quality flags of the original speech recognition
    corrected: bool = False  # the current text is the user's correction, not the recognized text

    @property
    def uncertain(self) -> bool:
        """Recognized text marked doubtful and not since corrected by the user."""
        return not self.corrected and any(f in UNCERTAIN_FLAGS for f in self.flags)


@dataclass(frozen=True, slots=True)
class SnapshotGap:
    start_ms: int
    end_ms: int | None
    source: str | None
    reason: str  # content-free timeline code
    loss: bool


@dataclass(frozen=True, slots=True)
class GenerationSnapshot:
    """One explicitly selected saved transcript revision: current segment text (with the
    user's corrections), ids/revisions, source labels, times, meeting metadata and
    coverage gaps. Never audio, live partials or other meetings."""

    meeting_id: str
    title: str = field(repr=False)
    local_date: str
    local_start_time: str
    utc_offset: str
    timezone: str
    transcript_revision: int
    segments: tuple[SnapshotSegment, ...]
    gaps: tuple[SnapshotGap, ...]
    redactions: int
    source_complete: bool = True

    def refs(self) -> dict[str, int]:
        return {s.id: s.revision for s in self.segments}

    def snapshot_hash(self) -> str:
        h = hashlib.sha256()
        h.update(json.dumps([self.meeting_id, self.transcript_revision,
                             [[s.id, s.revision, s.text] for s in self.segments]], ensure_ascii=False).encode())
        return h.hexdigest()


@dataclass(frozen=True, slots=True)
class GenerationOptions:
    model_id: str
    output_language: str = "en"  # 'en' | 'ar'
    window_tokens: int = 20_000  # estimated input tokens of transcript per request
    context_segments: int = 2  # preceding segments sent as context with each later part
    max_parts: int = 12  # more parts than this: refused before sending (too_large)
    max_output_tokens: int = 32_000
    timeout_s: float = 300.0
    max_retries: int = 2  # rate limit / server errors only
    max_repairs: int = 1  # per job
    # Local cost control: USD per 1M tokens (None = unknown) and the most one
    # generation may cost by this app's own estimate (None = no local limit).
    price_in_per_m: float | None = None
    price_out_per_m: float | None = None
    budget_usd: float | None = None


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    requests: int = 0

    def __add__(self, other: Usage) -> Usage:
        def add(a: int | None, b: int | None) -> int | None:
            return None if a is None and b is None else (a or 0) + (b or 0)
        return Usage(add(self.input_tokens, other.input_tokens), add(self.output_tokens, other.output_tokens),
                     self.requests + other.requests)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    content: dict[str, Any] = field(repr=False)
    usage: Usage
    parts: int
    provider: str
    model_id: str
    prompt_version: str = PROMPT_VERSION
    schema_version: int = SCHEMA_VERSION


class MinutesGenerator(Protocol):
    provider_name: str

    def generate(self, snapshot: GenerationSnapshot, options: GenerationOptions, cancel: threading.Event,
                 progress: Progress) -> GenerationResult: ...


# ---------------------------------------------------------------- windows

def estimate_tokens(text: str) -> int:
    """Conservative estimate (about 3 UTF-8 bytes per token, plus per-segment overhead);
    Arabic text uses more bytes per character, so it is counted heavier."""
    return math.ceil(len(text.encode("utf-8")) / 3) + 24


def plan_windows(snapshot: GenerationSnapshot, options: GenerationOptions) -> list[tuple[list[SnapshotSegment],
                                                                                       list[SnapshotSegment]]]:
    """(context, new segments) per part, split on segment boundaries. Every segment is
    in exactly one part; a part is never cut inside a segment."""
    windows: list[list[SnapshotSegment]] = []
    current: list[SnapshotSegment] = []
    used = 0
    for seg in snapshot.segments:
        cost = estimate_tokens(seg.text)
        if current and used + cost > options.window_tokens:
            windows.append(current)
            current, used = [], 0
        current.append(seg)
        used += cost
    if current:
        windows.append(current)
    out = []
    index = 0
    for window in windows:
        start = index
        out.append((list(snapshot.segments[max(0, start - options.context_segments):start]), window))
        index += len(window)
    return out


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """Pre-send estimate for one generation. Token counts are this app's
    conservative estimates, not OpenAI's billing; costs are None when a price is unknown."""

    parts: int
    input_tokens: int  # every part sent once
    output_tokens_max: int  # every part once, each at the output limit
    requests_max: int  # including every allowed retry and repair
    first_pass_usd: float | None  # every part once, each at the output limit
    worst_case_usd: float | None  # every allowed retry and repair as well
    budget_usd: float | None


def request_cost(options: GenerationOptions, input_tokens: int) -> float | None:
    """The most one request can cost: its input plus the full output limit."""
    if options.price_in_per_m is None or options.price_out_per_m is None:
        return None
    return (input_tokens * options.price_in_per_m + options.max_output_tokens * options.price_out_per_m) / 1e6


def request_input_tokens(envelope: dict[str, Any], repair: dict[str, Any] | None = None) -> int:
    """Estimated input tokens of one request: instructions, schema and envelope (and,
    for a repair, the validation errors and the previous output)."""
    from meeting_minutes.minutes.schema import ENVELOPE_NOTES, SYSTEM_PROMPT, wire_schema

    tokens = estimate_tokens(SYSTEM_PROMPT + ENVELOPE_NOTES + json.dumps(wire_schema()))
    tokens += estimate_tokens(json.dumps(envelope, ensure_ascii=False))
    if repair is not None:
        tokens += estimate_tokens(json.dumps(repair, ensure_ascii=False))
    return tokens


def estimate_cost(snapshot: GenerationSnapshot, options: GenerationOptions) -> CostEstimate:
    windows = plan_windows(snapshot, options)
    n = len(windows)
    inputs = [request_input_tokens(build_envelope(snapshot, options, segs, ctx, (i, n)))
              for i, (ctx, segs) in enumerate(windows, start=1)]
    attempts = 1 + options.max_retries
    first_pass = worst = None
    if request_cost(options, 0) is not None:
        first_pass = sum(request_cost(options, t) for t in inputs)
        # a repair resends the part plus the previous output (up to the output limit) and the errors
        repair = request_cost(options, max(inputs, default=0) + options.max_output_tokens + 2_000)
        worst = attempts * (first_pass + options.max_repairs * repair)
    return CostEstimate(n, sum(inputs), n * options.max_output_tokens, attempts * (n + options.max_repairs),
                        first_pass, worst, options.budget_usd)


def check_budget(estimate: CostEstimate) -> None:
    """Refuse before sending when the local limit can't be honoured."""
    if estimate.budget_usd is None:
        return
    if estimate.first_pass_usd is None:
        raise GenerationError("price_unknown", request_sent=False)
    if estimate.first_pass_usd > estimate.budget_usd:
        raise GenerationError("budget", request_sent=False,
                              detail=f"estimate ${estimate.first_pass_usd:.2f} > limit ${estimate.budget_usd:.2f}")


def build_envelope(snapshot: GenerationSnapshot, options: GenerationOptions, segments: Sequence[SnapshotSegment],
                   context: Sequence[SnapshotSegment] = (), part: tuple[int, int] = (1, 1)) -> dict[str, Any]:
    """The mom-v1 user envelope (serialized as JSON, never string-concatenated)."""

    def seg(s: SnapshotSegment) -> dict[str, Any]:
        out = {"id": s.id, "revision": s.revision, "source": s.source, "start_ms": s.start_ms,
               "end_ms": s.end_ms, "text": s.text}
        if s.flags:
            out["recognition_flags"] = list(s.flags)
        if s.corrected:
            out["corrected_by_user"] = True
        return out

    envelope: dict[str, Any] = {
        "task": "extract_minutes",
        "meeting": {"title": snapshot.title, "local_date": snapshot.local_date,
                    "local_start_time": snapshot.local_start_time, "utc_offset": snapshot.utc_offset,
                    "timezone": snapshot.timezone},
        "output_language": options.output_language,
        "confirmed_participants": [],
        "part": {"index": part[0], "count": part[1]},
        "context_segments": [seg(s) for s in context],
        "segments": [seg(s) for s in segments],
        "gaps": [{"start_ms": g.start_ms, "end_ms": g.end_ms, "source": g.source, "reason": g.reason}
                 for g in snapshot.gaps],
        "schema_version": SCHEMA_VERSION,
    }
    return envelope


# ------------------------------------------------------------------ merge

def _norm(text: str | None) -> str:
    return " ".join(unicodedata.normalize("NFKC", text or "").casefold().split())


def _evidence_key(item: dict[str, Any]) -> tuple:
    return tuple(sorted((e["segment_id"], e["revision"]) for e in item["evidence"]))


def merge_contents(parts: Sequence[dict[str, Any]], output_language: str) -> dict[str, Any]:
    """Reduce structured part results: keep every item with its evidence, in part
    (chronological) order; drop only exact duplicates (same text and same evidence)."""
    if len(parts) == 1:
        return parts[0]
    merged = empty_content(output_language)
    langs = {p["output_language"] for p in parts}
    merged["output_language"] = langs.pop() if len(langs) == 1 else "mixed"
    text_field = {"summary": "text", "topics": "title", "decisions": "text", "open_questions": "text",
                  "action_items": "task"}
    for section, name in text_field.items():
        seen: set[tuple] = set()
        for part in parts:
            for item in part[section]:
                key = (_norm(item[name]), _evidence_key(item))
                if key not in seen:
                    seen.add(key)
                    merged[section].append(item)
    seen_w: set[str] = set()
    for part in parts:
        for w in part["warnings"]:
            if _norm(w) not in seen_w:
                seen_w.add(_norm(w))
                merged["warnings"].append(w)
    merged["warnings"].append(
        f"The meeting was processed in {len(parts)} parts and the results were combined without a final "
        "rewrite, so similar points may appear more than once and a later reversal may be listed beside "
        "the earlier decision.")
    return merged


# ----------------------------------------------------------------- driver

class _Budget:
    """Repairs left, usage so far and the local cost limit. Each request attempt
    reserves its worst-case cost before it is sent and is refused if that would pass
    the limit; a reply with reported usage replaces its own reservation by the actual
    cost, higher or lower (failed attempts and replies without usage keep theirs)."""

    def __init__(self, options: GenerationOptions) -> None:
        self.options = options
        self.repairs = options.max_repairs
        self.usage = Usage()
        self.spent_usd = 0.0
        self.input_tokens = 0  # of the request about to be sent
        self.last_cost = 0.0
        self.attempts = 0  # request attempts allowed (and so sent or being sent)

    def reserve(self) -> None:
        cost = request_cost(self.options, self.input_tokens)
        self.last_cost = 0.0
        if self.options.budget_usd is not None and cost is not None:
            if self.spent_usd + cost > self.options.budget_usd + 1e-9:
                raise GenerationError("budget", request_sent=self.attempts > 0,
                                      detail=f"limit ${self.options.budget_usd:.2f} reached")
            self.spent_usd += cost
            self.last_cost = cost
        self.attempts += 1

    def settle(self, usage: Usage) -> None:
        o = self.options
        if self.last_cost and usage.input_tokens is not None and usage.output_tokens is not None:
            actual = (usage.input_tokens * o.price_in_per_m + usage.output_tokens * o.price_out_per_m) / 1e6
            # the full reported cost, also when above the estimate: later attempts must fit what is left
            self.spent_usd += actual - self.last_cost
        self.last_cost = 0.0


class MapReduceGenerator:
    """Shared generation driver. Subclasses implement `_request(envelope, repair,
    options, cancel) -> (raw_json_text, Usage)`; `repair` is None or
    {"errors": [...], "previous_output": str}."""

    provider_name = "provider"

    def _request(self, envelope: dict[str, Any], repair: dict[str, Any] | None, options: GenerationOptions,
                 cancel: threading.Event) -> tuple[str, Usage]:
        raise NotImplementedError

    def _before(self, snapshot: GenerationSnapshot, options: GenerationOptions) -> None:
        """Pre-send checks (e.g. the key is available). Raise with request_sent=False."""

    _budget: _Budget | None = None

    def _reserve_attempt(self) -> None:
        """Subclasses call this right before sending each attempt (first try and every
        retry): it raises `budget` instead when the local limit would be passed."""
        if self._budget is not None:
            self._budget.reserve()

    def generate(self, snapshot: GenerationSnapshot, options: GenerationOptions, cancel: threading.Event,
                 progress: Progress) -> GenerationResult:
        if not snapshot.segments:
            raise GenerationError("empty_transcript", request_sent=False)
        windows = plan_windows(snapshot, options)
        if len(windows) > options.max_parts:
            raise GenerationError("too_large", request_sent=False, detail=f"{len(windows)} parts")
        check_budget(estimate_cost(snapshot, options))
        self._before(snapshot, options)
        budget = self._budget = _Budget(options)
        results = []
        refs = snapshot.refs()
        for i, (context, segments) in enumerate(windows, start=1):
            if cancel.is_set():
                raise GenerationError("cancelled", request_sent=budget.usage.requests > 0)
            envelope = build_envelope(snapshot, options, segments, context, (i, len(windows)))
            progress("sending", i, len(windows))
            results.append(self._part(envelope, refs, options, cancel, budget, progress, i, len(windows)))
        if cancel.is_set():
            raise GenerationError("cancelled", request_sent=True)
        progress("validating", len(windows), len(windows))
        content = merge_contents(results, options.output_language)
        return GenerationResult(content, budget.usage, len(windows), self.provider_name, options.model_id)

    def _part(self, envelope: dict[str, Any], refs: dict[str, int], options: GenerationOptions,
              cancel: threading.Event, budget: _Budget, progress: Progress, i: int, n: int) -> dict[str, Any]:
        repair = None
        while True:
            budget.input_tokens = request_input_tokens(envelope, repair)
            try:
                raw, usage = self._request(envelope, repair, options, cancel)
            except GenerationError as exc:
                if budget.usage.requests:  # an earlier request of this job was already sent
                    exc.request_sent = True
                raise
            budget.settle(usage)
            budget.usage = budget.usage + usage
            progress("validating", i, n)
            content, errors = parse_output(raw)
            if not errors:
                errors = validate_content(content, refs)
            if not errors:
                return normalize_content(content)
            if budget.repairs <= 0:
                raise GenerationError("invalid_output", request_sent=True, invalid_count=len(errors),
                                      detail="; ".join(errors[:5]))
            budget.repairs -= 1
            repair = {"errors": errors, "previous_output": raw if len(raw) <= 100_000 else None}
            progress("sending", i, n)


class FakeMinutesGenerator(MapReduceGenerator):
    """Test provider: `responder(envelope, repair)` returns content (dict or raw JSON
    text) or raises GenerationError. Records every envelope it received. `gate`, when
    given, is waited on before answering (to test late results)."""

    provider_name = "Test provider"

    def __init__(self, responder: Callable[[dict[str, Any], dict[str, Any] | None], Any] | None = None, *,
                 gate: threading.Event | None = None, usage: Usage = Usage(100, 20, 1)) -> None:
        self.responder = responder or grounded_echo
        self.gate = gate
        self.usage = usage
        self.envelopes: list[dict[str, Any]] = []
        self.repairs: list[dict[str, Any]] = []

    def _request(self, envelope, repair, options, cancel):
        self._reserve_attempt()
        self.envelopes.append(envelope)
        if repair is not None:
            self.repairs.append(repair)
        if self.gate is not None:
            while not self.gate.wait(0.02):
                if cancel.is_set():
                    raise GenerationError("cancelled", request_sent=True)
        answer = self.responder(envelope, repair)
        return (answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)), self.usage


def grounded_echo(envelope: dict[str, Any], repair: dict[str, Any] | None = None) -> dict[str, Any]:
    """A deterministic grounded result for tests and demos: one summary statement per
    part citing its first segment, and nothing invented."""
    content = empty_content(envelope["output_language"])
    segs = envelope["segments"]
    if segs:
        first = {"segment_id": segs[0]["id"], "revision": segs[0]["revision"]}
        content["summary"].append({"text": f"Part {envelope['part']['index']} discussed the saved transcript.",
                                   "evidence": [first]})
    return content
