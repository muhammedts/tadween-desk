"""Qwen3-ASR (Q8_0 GGUF) on a local llama.cpp `llama-server`: the app's default
recognizer. The model is the manifest's "stt" entry (Qwen3-ASR 0.6B); a calibrated profile
for the pinned 1.7B alternative is kept below. Speech recognition only; no text generation.

- The server is an owned child process started once from verified local files
  (`--host 127.0.0.1`, an ephemeral port) and reused by every meeting; nothing is
  fetched. It is placed in a Windows job object that kills it when the app's process
  ends, so it cannot outlive the app. Its output goes nowhere unless a log is asked for.
- `QwenRecognizer.transcribe(chunk)` implements the pipeline's Recognizer protocol: the
  bounded 16 kHz chunk is encoded as an in-memory 16-bit WAV, sent over loopback and not
  kept. The server writes no audio to disk. Prompt reuse is off (`cache_prompt` false,
  `--cache-reuse 0`, `--cache-ram 0`), so no earlier chunk's state can carry over.
- Language: "auto" lets the model say which language it heard ("language X<asr_text>
  ..."); "en"/"ar" prefill that header, which is how Qwen3-ASR forces a language.
- Transport: every request goes directly to 127.0.0.1:<port> through a private opener
  with proxies disabled and redirects refused; the server requires a random per-run key
  (passed by environment, never on the command line or in a log), and start-up proves
  that the listener refuses requests without it and accepts it. Errors are
  content-free codes.
- Bounds: `max_tokens` caps every decode; a reply cut off there raises `QwenTruncated`,
  which the pipeline records as a recognition failure, never as a complete final. Each
  request has a time limit, and a chunk's requests together share one budget.

Policy (snapshotted per meeting through the engine identity):
- "A": automatic, one request per chunk. The default.
- "R" (experimental, automatic only): the same first request also asks for the top
  log-probabilities. If its header names a language other than English or Arabic, the
  chunk is decoded once more with the header forced to whichever of English or Arabic
  the model rated higher at the language-name position of that same first request
  (`decide`). Every other outcome keeps the first result, marked when it is uncertain.
  F: a short third-language result is kept and flagged, never deleted.
"""

from __future__ import annotations

import base64
import ctypes
import dataclasses
import hashlib
import io
import json
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import wave
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from meeting_minutes.domain.contracts import TARGET_SAMPLE_RATE, AudioChunk
from meeting_minutes.speech.pipeline import DecodeGuard, RecognizedText

LANGUAGES = {"English": "en", "Arabic": "ar"}  # Qwen's names -> the app's codes
FORCED = {"en": "English", "ar": "Arabic"}
HEADER = re.compile(r"^\s*language\s+([A-Za-z]+|None)\s*<asr_text>", re.S)
POLICIES = ("A", "R")
TOP_K = 20  # candidates asked for at every position of an R first request
REQUEST_TIMEOUT_S = 20.0  # one request (measured decodes of 12 s chunks take 1-3 s)
CHUNK_BUDGET_S = 30.0  # the first request and an R retry together
MIN_RETRY_S = 2.0  # an R retry is not started with less time than this left
FILLER_MAX_MS = 1000  # F: a third-language result this short, of at most one word
CALL_RECORDS = 256  # content-free per-request records kept in memory (newest)

# Quality flags (saved with the segment; they never change `complete`).
LANG_RETRY = "lang_retry"  # R replaced a third-language result by a forced English/Arabic decode
LANG_UNCERTAIN = "lang_uncertain"  # R kept a third-language (or unlabelled) result: no evidence to choose
FILLER_SUSPECT = "filler_suspect"  # F: a short third-language result, kept as recognized
LOW_CONFIDENCE = "low_confidence"  # the model's own mean token probability for the line is low
# Measured on a real meeting: lines Qwen invented for side talk/unclear speech averaged
# p 0.26-0.69 per token, well-recognized lines 0.84-1.0. Below this the line is kept and marked.
LOW_CONFIDENCE_P = 0.70
# Per speech model (manifest id): (mark below this mean token probability, also mark a line
# whose recognized language is neither English nor Arabic). Calibrated for the experimental
# 1.7B model with the same method: 1.7B is more confident while wrong, and in automatic
# mode writes confident Chinese/Hindi for side talk. No floor up to 0.77 marks clean
# public/synthetic speech or a well-recognized meeting line (lowest 0.79); 0.75 marks exactly
# the same lines as 0.77 on every measured set while staying >= 0.05 below every clean chunk,
# so 0.75 is used.
# Any other model (the standard 0.6B) keeps (LOW_CONFIDENCE_P, False).
CONFIDENCE_PROFILES: dict[str, tuple[float, bool]] = {"qwen3-asr-1.7b-q8_0-gguf": (0.75, True)}


class QwenTruncated(RuntimeError):
    """The server stopped at `max_tokens` (finish_reason "length"): the text is known to
    be cut off. Raised so the pipeline records the chunk as a recognition failure
    (`stt_failed` gap, content-free fault) instead of saving it as a complete final."""


class QwenServerError(RuntimeError):
    """A content-free failure talking to the owned server (no body, key or URL)."""


class QwenStartCancelled(RuntimeError):
    """Start-up was cancelled (the app is closing); the child has been stopped."""


def wav_bytes(samples: np.ndarray) -> bytes:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_SAMPLE_RATE)
        w.writeframes(pcm)
    return out.getvalue()


def split_header(raw: str) -> tuple[str, str | None, bool]:
    """'language Arabic<asr_text>...' -> (text, 'Arabic', True). Output without a
    header is returned stripped with (None, False)."""
    match = HEADER.match(raw)
    if not match:
        return raw.strip(), None, False
    return raw[match.end():].strip(), match.group(1), True


def language_code(name: str | None) -> str | None:
    if name is None or name == "None":
        return None
    return LANGUAGES.get(name, name.lower()[:16])


def parse(text: str) -> tuple[str, str | None]:
    """'language Arabic<asr_text>...' -> (text, 'ar'); an unknown header keeps the raw
    language name; output without a header is returned as is."""
    body, name, _ = split_header(text)
    return body, language_code(name)


# --------------------------------------------------------------------- transport


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001 - urllib API
        raise QwenServerError(f"qwen_server_redirect_{code}")


def _opener() -> urllib.request.OpenerDirector:
    """Direct loopback only: an empty ProxyHandler disables environment/system proxies
    for these requests (global settings are untouched), and redirects are refused."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _call(server: Any, path: str, body: bytes | None = None, *, key: str | None = None,
          timeout: float = 2.0) -> tuple[int, bytes]:
    """One request to the owned server at 127.0.0.1:<port>; (status, body). HTTP errors
    are returned by status; transport errors raise QwenServerError without details."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=body, headers=headers)
    try:
        with _opener().open(request, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as exc:
        exc.close()
        return exc.code, b""
    except QwenServerError:
        raise
    except (OSError, ValueError) as exc:
        raise QwenServerError(f"qwen_server_unreachable_{type(exc).__name__}") from None


# ------------------------------------------------------------------ owned server


class _KillOnCloseJob:
    """A Windows job object that terminates its processes when its last handle closes,
    which happens when this process exits for any reason (including a crash)."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes

        class _Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                       "OtherOperationCount", "ReadTransferCount",
                                                       "WriteTransferCount", "OtherTransferCount")]

        class _Extended(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _Basic), ("IoInfo", _Io),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return
        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(handle)
            return
        self._kernel32 = kernel32
        self.handle = handle

    def assign(self, process: subprocess.Popen) -> bool:
        if self.handle is None:
            return False
        handle = getattr(process, "_handle", None)
        if handle is None:
            return False
        return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(handle)))


_JOB: _KillOnCloseJob | None = None
_JOB_LOCK = threading.Lock()


def _job() -> _KillOnCloseJob:
    global _JOB
    with _JOB_LOCK:
        if _JOB is None:
            _JOB = _KillOnCloseJob()
        return _JOB


@dataclass
class QwenServer:
    """The owned llama-server. `server_exe`, `model` and `mmproj` are verified paths."""

    server_exe: Path | None = None
    model: Path | None = None
    mmproj: Path | None = None
    threads: int = 4
    ctx: int = 4096
    port: int = 0
    startup_s: float = 180.0
    process: subprocess.Popen | None = None
    load_s: float | None = None
    log_path: Path | None = None
    tied_to_app: bool = False  # in the kill-on-exit job object
    api_key: str | None = field(default=None, repr=False)
    _log: object | None = field(default=None, repr=False)

    def start(self, log_path: Path | None = None, *, cancel: threading.Event | None = None) -> "QwenServer":
        """Start the owned server and return once it is ready, proven to be ours (it
        accepts this run's key and refuses requests without it) and placed in the
        kill-on-close job (it cannot outlive the app). Any failure after the spawn, or
        `cancel` being set, stops and reaps the child and closes the log before raising."""
        if self.port == 0:
            with socket.socket() as s:  # a free loopback port for this run
                s.bind(("127.0.0.1", 0))
                self.port = s.getsockname()[1]
        self.api_key = secrets.token_urlsafe(32)
        self.log_path = log_path
        self._log = log_path.open("w", encoding="utf-8") if log_path is not None else None
        args = [str(self.server_exe), "-m", str(self.model), "--mmproj", str(self.mmproj), "--host", "127.0.0.1",
                "--port", str(self.port), "-t", str(self.threads), "-c", str(self.ctx), "-np", "1",
                "--no-webui", "--temp", "0", "--cache-reuse", "0", "--cache-ram", "0", "--no-slots"]
        # The key goes by environment, not argv, so it is not in the command line.
        env = {**os.environ, "LLAMA_ARG_HF_REPO": "", "HF_HUB_OFFLINE": "1", "LLAMA_API_KEY": self.api_key}
        out = self._log if self._log is not None else subprocess.DEVNULL
        started = time.monotonic()
        try:
            self.process = subprocess.Popen(args, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                            env=env, cwd=str(Path(str(self.server_exe)).parent),
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            self._close_log()
            raise RuntimeError(f"qwen_server_spawn_failed_{type(exc).__name__}") from None
        try:
            self.tied_to_app = _job().assign(self.process)
            while time.monotonic() - started < self.startup_s:
                if cancel is not None and cancel.is_set():
                    raise QwenStartCancelled("qwen_server_start_cancelled")
                if self.process.poll() is not None:
                    raise RuntimeError(f"qwen_server_exited_{self.process.returncode}")
                try:
                    if _call(self, "/health")[0] == 200:
                        self._verify_owned()
                        if not self.tied_to_app:  # no crash-cleanup guarantee: not ready
                            raise RuntimeError("qwen_server_not_owned_by_app")
                        self.load_s = round(time.monotonic() - started, 2)
                        return self
                except QwenServerError:
                    pass  # not listening yet
                if cancel is not None:
                    cancel.wait(0.25)
                else:
                    time.sleep(0.25)
            raise RuntimeError(f"qwen_server_not_ready_within_{self.startup_s:g}_s")
        except BaseException:
            self.stop()
            raise

    def _verify_owned(self) -> None:
        """The server on this port must refuse an unauthenticated request and accept this
        run's key; otherwise it is not the process this run started."""
        if _call(self, "/v1/models")[0] != 401:
            raise RuntimeError("qwen_server_identity_unverified_no_auth")
        if _call(self, "/v1/models", key=self.api_key)[0] != 200:
            raise RuntimeError("qwen_server_identity_unverified_key")

    def alive(self) -> bool:
        process = self.process
        return process is not None and process.poll() is None

    def stop(self, timeout_s: float = 10.0) -> None:
        """Idempotent and bounded: terminate, wait; if it does not exit, kill and wait
        again. Only the child this object spawned is ever signalled."""
        process, self.process = self.process, None
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout_s)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout_s)
        finally:
            self._close_log()

    def _close_log(self) -> None:
        log, self._log = self._log, None
        if log is not None:
            log.close()


# -------------------------------------------------------------------- recognizer


@dataclass(frozen=True)
class Reply:
    """One decoded request. `top` is the language-name position's candidates as
    (token, logprob), or None when they were not asked for or are unusable."""

    text: str
    header: str | None
    has_header: bool
    top: tuple[tuple[str, float], ...] | None = field(default=None, repr=False)
    mean_p: float | None = None  # geometric-mean probability of the text tokens, if known


def text_mean_p(content: Any) -> float | None:
    """exp(mean logprob) of the tokens after the "<asr_text>" header token (all tokens
    when the header was prefilled), or None when log-probabilities are absent/malformed."""
    if not isinstance(content, list) or not content:
        return None
    try:
        start = next((i + 1 for i, c in enumerate(content) if "asr_text" in str(c["token"])), 0)
        values = [float(c["logprob"]) for c in content[start:]]
    except (KeyError, TypeError, ValueError):
        return None
    values = [v for v in values if not math.isnan(v)]
    return round(math.exp(sum(values) / len(values)), 3) if values else None


@dataclass(frozen=True)
class Decision:
    """R's frozen decision for one first reply (see `decide`)."""

    retry_as: str | None  # "English"/"Arabic" to re-decode as, else None
    reason: str
    flags: tuple[str, ...] = ()
    english: float | None = None  # best logprob of an English candidate, if listed
    arabic: float | None = None


def language_position(content: Any, header: str | None) -> tuple[tuple[str, float], ...] | None:
    """Candidates at the language-name position: the token right after the first
    token that is "language" (ignoring surrounding whitespace). The chosen token there
    must start the header's language name; otherwise the position is not verified and
    None is returned (as for absent or malformed log-probabilities)."""
    if not isinstance(content, list) or not header:
        return None
    try:
        start = next(i for i, c in enumerate(content) if str(c["token"]).strip() == "language")
        entry = content[start + 1]
        chosen = str(entry["token"]).strip()
        if not chosen or not header.startswith(chosen):
            return None
        top = tuple((str(t["token"]), float(t["logprob"])) for t in entry["top_logprobs"])
    except (StopIteration, IndexError, KeyError, TypeError, ValueError):
        return None
    if not top or any(math.isnan(lp) for _, lp in top):
        return None
    return top


def _best(top: tuple[tuple[str, float], ...], name: str) -> float | None:
    """Best logprob among candidates that are exactly `name` once whitespace is
    stripped (" English" and "English" are the same name; " english" and " Arab" are
    not). None if the name is not listed."""
    values = [lp for token, lp in top if token.strip() == name]
    return max(values) if values else None


def decide(first: Reply) -> Decision:
    """R's rule, fixed before it was scored:

    - no header at all: keep, flag uncertain (nothing to compare);
    - header "None" or empty text (no speech): keep as is, no retry, no flag;
    - header English or Arabic: keep as is (identical to A);
    - another language, candidates missing or unverified: keep, flag uncertain;
    - another language, neither English nor Arabic listed: keep, flag uncertain;
    - both listed with equal logprob: keep, flag uncertain (no guess);
    - otherwise retry once as the higher of the two; if only one is listed it is the
      higher (the absent one ranks below every listed candidate).
    Whole-transcript likelihoods across languages are never compared.
    """
    if not first.has_header:
        return Decision(None, "no_header", (LANG_UNCERTAIN,))
    if first.header == "None" or not first.text:
        return Decision(None, "no_speech")
    if first.header in LANGUAGES:
        return Decision(None, "allowed")
    if first.top is None:
        return Decision(None, "candidates_missing", (LANG_UNCERTAIN,))
    english, arabic = _best(first.top, "English"), _best(first.top, "Arabic")
    if english is None and arabic is None:
        return Decision(None, "neither_listed", (LANG_UNCERTAIN,))
    if english is not None and arabic is not None and english == arabic:
        return Decision(None, "tie", (LANG_UNCERTAIN,), english, arabic)
    if arabic is None or (english is not None and english > arabic):
        return Decision("English", "retry", (), english, arabic)
    return Decision("Arabic", "retry", (), english, arabic)


@dataclass
class QwenRecognizer:
    server: Any
    language: str = "auto"  # "auto", "en" or "ar"
    policy: str = "A"
    max_tokens: int = 256
    timeout_s: float = REQUEST_TIMEOUT_S
    budget_s: float = CHUNK_BUDGET_S
    # Content-free per-request records (durations, token counts, labels, R decision).
    calls: Any = field(default_factory=lambda: deque(maxlen=CALL_RECORDS))
    # Controlled evaluation only (never set by the app): R's first and final text per
    # chunk, kept in memory for the caller.
    paired: list | None = None
    # Mark a line "low_confidence" below this mean token probability (None: never).
    confidence_floor: float | None = LOW_CONFIDENCE_P
    # Also mark it when the recognized language is neither English nor Arabic (1.7B profile).
    mark_other_language: bool = False
    accepts_guard = True  # transcribe takes the pipeline's DecodeGuard

    def __post_init__(self) -> None:
        if self.policy not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}")
        if self.policy == "R" and self.language != "auto":
            raise ValueError("policy R applies to automatic language only")

    def transcribe(self, chunk: AudioChunk, guard: DecodeGuard | None = None) -> RecognizedText:
        started = time.monotonic()
        budget_end = started + self.budget_s
        audio_ms = chunk.end_ms - chunk.start_ms
        audio = base64.b64encode(wav_bytes(chunk.data)).decode("ascii")
        r_policy = self.policy == "R"
        first = self._request(audio, audio_ms, FORCED.get(self.language), self.timeout_s,
                              top_k=TOP_K if r_policy else 0, kind="first")
        if not r_policy:
            return RecognizedText(first.text, language_code(first.header), self._confidence(first, first.header))
        decision = decide(first)
        text, header, flags, outcome = first.text, first.header, decision.flags, decision.reason
        if decision.retry_as is not None:
            left = budget_end - time.monotonic()
            stop_left = guard.seconds_left() if guard is not None else None
            if stop_left is not None:
                left = min(left, stop_left)
            if guard is not None and not guard.admitted():
                outcome, flags = "not_admitted", (LANG_UNCERTAIN,)  # never send it again
            elif left < MIN_RETRY_S:
                outcome, flags = "no_time", (LANG_UNCERTAIN,)
            else:
                try:
                    second = self._request(audio, audio_ms, decision.retry_as, min(self.timeout_s, left),
                                           top_k=0, kind="retry")
                except QwenTruncated:
                    outcome, flags = "retry_truncated", (LANG_UNCERTAIN,)
                except QwenServerError:
                    outcome, flags = "retry_failed", (LANG_UNCERTAIN,)
                else:
                    if second.text:
                        text, header, outcome, flags = second.text, decision.retry_as, "retried", (LANG_RETRY,)
                        first = dataclasses.replace(first, mean_p=second.mean_p)  # confidence of the kept text
                    else:  # an empty retry must not delete what the first decode heard
                        outcome, flags = "retry_empty", (LANG_UNCERTAIN,)
        third = first.has_header and first.header not in LANGUAGES and first.header != "None"
        if third and audio_ms < FILLER_MAX_MS and text and len(text.split()) <= 1:
            flags = (*flags, FILLER_SUSPECT)
        flags = (*flags, *self._confidence(first, header))  # the language of the text it keeps
        self.calls.append({"kind": "decision", "audio_ms": audio_ms, "first_language": first.header,
                           "outcome": outcome, "english_logprob": decision.english,
                           "arabic_logprob": decision.arabic, "flags": list(flags),
                           "chunk_ms": round((time.monotonic() - started) * 1000)})
        if self.paired is not None:
            self.paired.append({"start_ms": chunk.start_ms, "end_ms": chunk.end_ms, "source": chunk.source.value,
                                "overlap_ms": chunk.overlap_ms,
                                "first_language": first.header, "first_text": first.text,
                                "language": header, "text": text, "outcome": outcome, "flags": list(flags),
                                "english_logprob": decision.english, "arabic_logprob": decision.arabic})
        return RecognizedText(text, language_code(header), flags)

    def _confidence(self, reply: Reply, header: str | None) -> tuple[str, ...]:
        if not reply.text:
            return ()
        if self.mark_other_language and header not in (None, "None") and header not in LANGUAGES:
            return (LOW_CONFIDENCE,)
        floor = self.confidence_floor
        if floor is None or reply.mean_p is None:
            return ()
        return (LOW_CONFIDENCE,) if reply.mean_p < floor else ()

    def _request(self, audio: str, audio_ms: int, forced: str | None, timeout: float, *, top_k: int,
                 kind: str) -> Reply:
        messages: list[dict] = [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": audio, "format": "wav"}}]}]
        prefix = ""
        if forced is not None:
            prefix = f"language {forced}<asr_text>"
            messages.append({"role": "assistant", "content": prefix})  # prefill: forces the language
        payload: dict[str, Any] = {"messages": messages, "temperature": 0, "max_tokens": self.max_tokens,
                                   "stream": False, "cache_prompt": False}
        if top_k:
            payload.update(logprobs=True, top_logprobs=top_k)
        elif self.confidence_floor is not None:
            payload["logprobs"] = True  # token probabilities only; the decoded text is unchanged
        server = self.server
        process = getattr(server, "process", None)
        if process is not None and process.poll() is not None:
            raise QwenServerError("qwen_server_exited")
        started = time.perf_counter()
        try:
            status, data = _call(server, "/v1/chat/completions", json.dumps(payload).encode(),
                                 key=getattr(server, "api_key", None), timeout=timeout)
        except QwenServerError as exc:  # failed attempts are timed too (content-free code)
            self.calls.append({"kind": kind, "audio_ms": audio_ms,
                               "wall_ms": round((time.perf_counter() - started) * 1000), "error": str(exc)})
            raise
        wall_ms = round((time.perf_counter() - started) * 1000)
        if status != 200:
            self.calls.append({"kind": kind, "audio_ms": audio_ms, "wall_ms": wall_ms, "http_status": status})
            raise QwenServerError(f"qwen_server_http_{status}")
        try:
            reply = json.loads(data)
            choice = reply["choices"][0]
            raw = choice["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError):
            raise QwenServerError("qwen_server_malformed_reply") from None
        text, header, has_header = split_header(raw if raw.lstrip().startswith("language") else prefix + raw)
        timings = reply.get("timings", {}) if isinstance(reply, dict) else {}
        truncated = choice.get("finish_reason") == "length"
        top = None
        content = (choice.get("logprobs") or {}).get("content")
        if top_k:
            top = language_position(content, header)
        mean_p = text_mean_p(content)
        self.calls.append({
            "kind": kind, "audio_ms": audio_ms, "wall_ms": wall_ms,
            "prompt_ms": round(timings.get("prompt_ms", 0)), "predicted_ms": round(timings.get("predicted_ms", 0)),
            "prompt_tokens": timings.get("prompt_n"), "predicted_tokens": timings.get("predicted_n"),
            "hit_max_tokens": truncated, "language": language_code(header),
            "language_candidates": None if not top_k else (len(top) if top is not None else 0),
            "mean_p": mean_p,
        })
        if truncated:  # known cut-off: never an ordinary final
            raise QwenTruncated("qwen_truncated_at_max_tokens")
        return Reply(text, header, has_header, top, mean_p)


def engine_id(base: str, language: str, policy: str) -> str:
    """The identity saved with every segment: engine/model, language and policy."""
    return f"{base};language={language};policy={policy}"


class QwenService:
    """The app's one owned server: started (after a SHA-256 check of the model and
    runtime) on first use, shared by every meeting and language choice, restarted on
    the next use if it has exited, and stopped by `close`. Never downloads anything."""

    def __init__(self, manifest: Any, models_dir: Path, *, threads: int = 4, log_path: Path | None = None,
                 port: int = 0) -> None:
        from meeting_minutes.models.assets import check_speech_assets, entry_dir, file_path

        self._manifest = manifest
        self._models_dir = models_dir
        self._threads = threads
        self._log_path = log_path
        self._port = port  # 0: a free loopback port per start
        self._check = check_speech_assets
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._server: QwenServer | None = None
        self._hashes_verified = False
        stt = next(m for m in manifest.models if m.role == "stt")
        runtime = next(m for m in manifest.models if m.role == "stt-runtime")
        self.base_id = f"{stt.engine}-{stt.engine_version}/{stt.id}"
        from meeting_minutes.models.manifest import stt_label

        self.label = stt_label(stt)  # e.g. "Qwen3-ASR 0.6B"
        self.confidence_floor, self.mark_other_language = CONFIDENCE_PROFILES.get(stt.id, (LOW_CONFIDENCE_P, False))
        self.model_hash = hashlib.sha256("".join(f.sha256 for f in stt.files).encode()).hexdigest()
        self._paths = (file_path(models_dir, runtime, "server"), file_path(models_dir, stt, "model"),
                       file_path(models_dir, stt, "mmproj"))
        self.runtime_dir = entry_dir(models_dir, runtime)

    def server(self) -> QwenServer:
        """The running server, starting it if needed (blocks; raises SpeechAssetsMissing,
        QwenStartCancelled or a content-free RuntimeError)."""
        from meeting_minutes.speech.sherpa import SpeechAssetsMissing

        with self._lock:
            if self._cancel.is_set():
                raise QwenStartCancelled("qwen_server_start_cancelled")
            if self._server is not None and self._server.alive():
                return self._server
            if self._server is not None:  # it exited: stop/reap what is left, start again
                self._server.stop(timeout_s=5.0)
                self._server = None
            readiness = self._check(self._manifest, self._models_dir, verify_hashes=not self._hashes_verified)
            if not readiness["ready"]:
                raise SpeechAssetsMissing(readiness)
            self._hashes_verified = True
            if self._cancel.is_set():  # closed while the files were being checked
                raise QwenStartCancelled("qwen_server_start_cancelled")
            server_exe, model, mmproj = self._paths
            server = QwenServer(server_exe, model, mmproj, threads=self._threads, port=self._port)
            self._server = server.start(self._log_path, cancel=self._cancel)
            if self._cancel.is_set():  # closed just as it became ready: never published
                self._server = None
                server.stop(timeout_s=5.0)
                raise QwenStartCancelled("qwen_server_start_cancelled")
            return server

    def recognizer(self, language: str, policy: str = "A") -> QwenRecognizer:
        return QwenRecognizer(self.server(), language=language, policy=policy,
                              confidence_floor=self.confidence_floor, mark_other_language=self.mark_other_language)

    def close(self, timeout_s: float = 5.0) -> None:
        """Cancel a start in progress and stop the server, within `timeout_s` even while
        another thread is checking files or starting; idempotent. A start that
        is still running sees the cancellation and stops its own child itself."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        self._cancel.set()
        locked = self._lock.acquire(timeout=max(0.0, timeout_s))
        try:
            server, self._server = self._server, None
        finally:
            if locked:
                self._lock.release()
        if server is not None:
            server.stop(timeout_s=max(0.1, deadline - time.monotonic()))
