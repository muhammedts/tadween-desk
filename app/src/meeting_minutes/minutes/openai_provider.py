"""OpenAI Responses API adapter for minutes, standard library only (no SDK).

Request (checked against developers.openai.com on 2026-11-02):
POST https://api.openai.com/v1/responses with `model`, `instructions` (mom-v1),
`input` (one user message whose input_text is the JSON envelope; a repair adds a second
user message with the validation errors), `text.format` = strict `json_schema`,
`store: false`, `max_output_tokens` and `truncation: "disabled"` (an over-long input is
refused instead of silently dropped). No tools, web search, files, background mode,
previous_response_id or conversation state are sent. Temperature/reasoning are
omitted because support differs between models and unsupported parameters are rejected.

The key comes from the credential store at call time and is sent only in the
Authorization header of this fixed HTTPS endpoint; it is never put in an exception,
log or repr, and redirects are refused so the header cannot be forwarded elsewhere.
The blocking HTTP call runs on a helper thread so Cancel returns at once; the helper
ends at its own bounded timeout. Retries: rate limit (not quota) and 5xx only, at most
`max_retries`, honouring Retry-After up to 20 s, interruptible by Cancel.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from meeting_minutes.minutes.provider import (
    GenerationError,
    GenerationOptions,
    GenerationSnapshot,
    MapReduceGenerator,
    Usage,
)
from meeting_minutes.minutes.schema import ENVELOPE_NOTES, SYSTEM_PROMPT, wire_schema

API_URL = "https://api.openai.com/v1/responses"
# Default model, user-configurable in Settings: listed on developers.openai.com/api/docs/models
# (2026-11-02) as "fast, efficient", with Structured Outputs and the Responses API.
DEFAULT_MODEL = "gpt-6.1-terra"
# USD per 1M (input, output) tokens, from the same official model page (2026-11-02). Only
# for the local pre-send estimate and limit; the user can change it in Settings,
# and any other model's price is unknown until the user enters it.
KNOWN_PRICES = {DEFAULT_MODEL: (1.00, 5.00)}
PROVIDER_NAME = "OpenAI"
SCHEMA_NAME = "meeting_minutes_v1"
MAX_RESPONSE_BYTES = 4_000_000
MAX_RETRY_WAIT_S = 20.0
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
QUOTA_CODES = {"insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
               "project_spend_limit_exceeded", "organization_usage_limit_exceeded", "billing_hard_limit_reached"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib hook
        return None  # a redirect becomes an HTTPError; the key is never sent to another URL


def default_opener() -> urllib.request.OpenerDirector:
    """HTTPS with certificate verification and the system/user proxy settings."""
    return urllib.request.build_opener(_NoRedirect())


def build_request_body(envelope: dict[str, Any], options: GenerationOptions,
                       repair: dict[str, Any] | None = None) -> dict[str, Any]:
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": [{"type": "input_text", "text": json.dumps(envelope, ensure_ascii=False)}]}]
    if repair is not None:
        note = {"task": "repair_minutes",
                "instruction": "Your previous output failed validation. Return corrected JSON for the same task, "
                               "citing only supplied segment IDs and revisions.",
                "validation_errors": repair["errors"],
                "previous_output": repair.get("previous_output")}
        messages.append({"role": "user",
                         "content": [{"type": "input_text", "text": json.dumps(note, ensure_ascii=False)}]})
    return {
        "model": options.model_id,
        "instructions": f"{SYSTEM_PROMPT}\n\n{ENVELOPE_NOTES}",
        "input": messages,
        "text": {"format": {"type": "json_schema", "name": SCHEMA_NAME, "schema": wire_schema(), "strict": True}},
        "store": False,
        "max_output_tokens": options.max_output_tokens,
        "truncation": "disabled",
    }


def _safe(value: Any) -> str:
    return value if isinstance(value, str) and _SAFE_CODE.match(value) else ""


def _error_fields(body: bytes) -> tuple[str, str, str]:
    """(type, code, param) of an OpenAI error body, only if they are plain identifiers.
    The error message is never read: it may echo request content."""
    try:
        err = json.loads(body.decode("utf-8")).get("error") or {}
    except (ValueError, UnicodeDecodeError, AttributeError):
        return "", "", ""
    if not isinstance(err, dict):
        return "", "", ""
    return _safe(err.get("type")), _safe(err.get("code")), _safe(err.get("param"))


def classify_http(status: int, body: bytes) -> GenerationError:
    kind, code, param = _error_fields(body)
    detail = " ".join(x for x in (f"HTTP {status}", kind, code, param and f"param={param}") if x)
    if status == 401:
        return GenerationError("auth", request_sent=True, detail=detail)
    if status == 404 or code in ("model_not_found", "unsupported_model") or param == "model":
        return GenerationError("model_unavailable", request_sent=True, detail=detail)
    if status == 429:
        quota = code in QUOTA_CODES or kind == "insufficient_quota"
        return GenerationError("quota" if quota else "rate_limit", request_sent=True, detail=detail)
    if status == 400 and code in ("context_length_exceeded", "string_above_max_length"):
        return GenerationError("too_large", request_sent=True, detail=detail)
    if status >= 500:
        return GenerationError("server_error", request_sent=True, detail=detail)
    return GenerationError("request_rejected", request_sent=True, detail=detail)


def classify_transport(exc: BaseException) -> GenerationError:
    """Map a transport failure to a code and a truthful `request_sent`.

    urllib wraps errors raised while connecting/sending in URLError; errors raised
    after the request was fully written (waiting for or reading the reply) arrive
    unwrapped. Only failures that provably happen before any request byte reaches
    OpenAI — name resolution, refused connection, certificate rejection — are
    request_sent=False. Anything else during connect/send is unknown (None)."""
    if isinstance(exc, urllib.error.URLError) and not isinstance(exc, urllib.error.HTTPError):
        reason = exc.reason
        if isinstance(reason, (socket.gaierror, ConnectionRefusedError, ssl.SSLCertVerificationError)):
            return GenerationError("offline", request_sent=False, detail=type(reason).__name__)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return GenerationError("timeout", request_sent=None, detail="connect/send timeout")
        return GenerationError("offline", request_sent=None, detail=type(reason).__name__)
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return GenerationError("timeout", request_sent=True, detail="no reply in time")
    if isinstance(exc, (http.client.HTTPException, OSError)):
        return GenerationError("offline", request_sent=True, detail=type(exc).__name__)
    return GenerationError("offline", request_sent=None, detail=type(exc).__name__)


def parse_response(body: bytes) -> tuple[str, Usage]:
    """Output text and usage of a Responses API reply; refusals and incomplete or
    failed responses raise content-free errors."""
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise GenerationError("invalid_output", request_sent=True, detail="reply is not JSON") from None
    if not isinstance(data, dict):
        raise GenerationError("invalid_output", request_sent=True, detail="reply is not an object")
    usage_data = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    usage = Usage(_int(usage_data.get("input_tokens")), _int(usage_data.get("output_tokens")), 1)
    status = data.get("status")
    texts: list[str] = []
    refused = False
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("type") == "refusal":
                refused = True
            elif isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
    if refused:
        raise GenerationError("refusal", request_sent=True, detail="refusal")
    if status == "incomplete":
        reason = _safe((data.get("incomplete_details") or {}).get("reason"))
        if reason == "content_filter":
            raise GenerationError("refusal", request_sent=True, detail="incomplete content_filter")
        raise GenerationError("too_large" if reason == "max_output_tokens" else "invalid_output",
                              request_sent=True, detail=f"incomplete {reason}".strip())
    if status != "completed":
        code = _safe((data.get("error") or {}).get("code")) if isinstance(data.get("error"), dict) else ""
        raise GenerationError("server_error", request_sent=True, detail=f"status {_safe(status)} {code}".strip())
    if not texts:
        raise GenerationError("invalid_output", request_sent=True, detail="no output text")
    return "".join(texts), usage


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


class OpenAIMinutesGenerator(MapReduceGenerator):
    provider_name = PROVIDER_NAME

    def __init__(self, key_provider: Callable[[], str | None], *, opener: Any = None,
                 sleep: Callable[[float], None] | None = None) -> None:
        self._key_provider = key_provider
        self._opener = opener if opener is not None else default_opener()
        self._sleep = sleep  # injectable for tests; default waits on the cancel event

    def __repr__(self) -> str:
        return "OpenAIMinutesGenerator()"

    def _before(self, snapshot: GenerationSnapshot, options: GenerationOptions) -> None:
        if not self._key_provider():
            raise GenerationError("no_key", request_sent=False)

    def _request(self, envelope, repair, options, cancel):
        body = json.dumps(build_request_body(envelope, options, repair), ensure_ascii=False).encode("utf-8")
        attempt = 0
        while True:
            if cancel.is_set():
                raise GenerationError("cancelled", request_sent=attempt > 0)
            self._reserve_attempt()  # local cost limit: refused before sending
            status, reply, retry_after = self._post(body, options, cancel)
            if status == 200:
                text, usage = parse_response(reply)
                return text, Usage(usage.input_tokens, usage.output_tokens, attempt + 1)
            error = classify_http(status, reply)
            if error.code not in ("rate_limit", "server_error") or attempt >= options.max_retries:
                raise error
            attempt += 1
            delay = min(MAX_RETRY_WAIT_S, retry_after if retry_after is not None else 2.0 * 2 ** (attempt - 1))
            if self._sleep is not None:
                self._sleep(delay)
            elif cancel.wait(delay):
                raise GenerationError("cancelled", request_sent=True)

    def _post(self, body: bytes, options: GenerationOptions, cancel: threading.Event
              ) -> tuple[int, bytes, float | None]:
        key = self._key_provider()
        if not key:
            raise GenerationError("no_key", request_sent=False)
        request = urllib.request.Request(API_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        request.add_unredirected_header("Authorization", f"Bearer {key}")
        del key
        box: dict[str, Any] = {}

        def run() -> None:
            try:
                with self._opener.open(request, timeout=options.timeout_s) as reply:
                    box["status"] = getattr(reply, "status", None) or reply.getcode()
                    box["body"] = reply.read(MAX_RESPONSE_BYTES + 1)
            except urllib.error.HTTPError as exc:
                box["status"] = exc.code
                try:
                    box["body"] = exc.read(65_536)
                except Exception:  # noqa: BLE001 - an unreadable error body is just empty
                    box["body"] = b""
                box["retry_after"] = exc.headers.get("Retry-After") if exc.headers is not None else None
                exc.close()
            except BaseException as exc:  # noqa: BLE001 - classified below, never re-raised raw
                box["error"] = exc

        worker = threading.Thread(target=run, name="openai-minutes-request", daemon=True)
        worker.start()
        deadline = time.monotonic() + options.timeout_s + 5.0
        while worker.is_alive():
            worker.join(0.05)
            if cancel.is_set():
                raise GenerationError("cancelled", request_sent=None)
            if time.monotonic() > deadline:
                raise GenerationError("timeout", request_sent=None, detail="overall deadline")
        if "error" in box:
            raise classify_transport(box.pop("error")) from None
        payload = box.get("body") or b""
        if len(payload) > MAX_RESPONSE_BYTES:
            raise GenerationError("invalid_output", request_sent=True, detail="reply larger than limit")
        retry_after = None
        try:
            retry_after = float(box["retry_after"]) if box.get("retry_after") else None
        except ValueError:
            retry_after = None
        return int(box.get("status") or 0), payload, retry_after
