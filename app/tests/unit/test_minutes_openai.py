"""The OpenAI Responses adapter with a patched urllib opener. No real
network (the suite blocks sockets), no real key; synthetic canary values only."""

from __future__ import annotations

import io
import json
import socket
import ssl
import threading
import time
import urllib.error

import pytest

from meeting_minutes.minutes.openai_provider import (
    API_URL,
    DEFAULT_MODEL,
    OpenAIMinutesGenerator,
    _NoRedirect,
    classify_transport,
)
from meeting_minutes.minutes.provider import (
    GenerationError,
    GenerationOptions,
    GenerationSnapshot,
    SnapshotGap,
    SnapshotSegment,
)
from meeting_minutes.minutes.schema import empty_content

KEY = "sk-canary-key-5d1e7a90-NOT-REAL"


def snapshot(n: int = 3) -> GenerationSnapshot:
    segs = tuple(SnapshotSegment(f"s-{i}", 2 if i == 0 else 1, "mic" if i % 2 else "system", i * 1000, i * 1000 + 900,
                                 f"line {i} text", "en") for i in range(n))
    return GenerationSnapshot("m1", "Weekly sync", "2026-11-02", "10:00", "+02:00", "EET", 7, segs,
                              (SnapshotGap(5000, 6000, "mic", "mic_excluded", False),), 0)


def good_content(snap: GenerationSnapshot | None = None) -> dict:
    snap = snap or snapshot()
    out = empty_content()
    out["summary"] = [{"text": "Weekly status", "evidence": [{"segment_id": snap.segments[0].id,
                                                              "revision": snap.segments[0].revision}]}]
    return out


def reply(content=None, *, status="completed", refusal=False, usage=(120, 30), incomplete=None) -> dict:
    parts = [{"type": "refusal", "refusal": "no"}] if refusal else [
        {"type": "output_text", "text": content if isinstance(content, str) else json.dumps(content),
         "annotations": []}]
    body = {"id": "resp_test", "object": "response", "status": status,
            "output": [{"type": "reasoning", "id": "rs_1", "summary": []},
                       {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed",
                        "content": parts}],
            "usage": {"input_tokens": usage[0], "output_tokens": usage[1], "total_tokens": sum(usage)}}
    if incomplete:
        body["incomplete_details"] = {"reason": incomplete}
    return body


class Reply:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = io.BytesIO(body)

    def read(self, n: int = -1) -> bytes:
        return self._body.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status: int, error: dict | None = None, headers: dict | None = None) -> urllib.error.HTTPError:
    body = json.dumps({"error": error or {}}).encode()
    return urllib.error.HTTPError(API_URL, status, "error", headers or {}, io.BytesIO(body))


class FakeOpener:
    """Scripted replies: a dict (200 JSON body), bytes, an exception, or a callable."""

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        step = self.script.pop(0) if self.script else reply(good_content())
        if callable(step) and not isinstance(step, BaseException):
            step = step()
        if isinstance(step, BaseException):
            raise step
        if isinstance(step, bytes):
            return Reply(200, step)
        return Reply(200, json.dumps(step).encode())

    def body(self, i: int = 0) -> dict:
        return json.loads(self.requests[i].data.decode())

    def envelope(self, i: int = 0) -> dict:
        return json.loads(self.body(i)["input"][0]["content"][0]["text"])


def generate(opener: FakeOpener, snap: GenerationSnapshot | None = None, *, key: str | None = KEY,
             cancel: threading.Event | None = None, **opts):
    gen = OpenAIMinutesGenerator(lambda: key, opener=opener, sleep=lambda s: None)
    steps = []
    options = GenerationOptions(model_id=DEFAULT_MODEL, **opts)
    result = gen.generate(snap or snapshot(), options, cancel or threading.Event(),
                          lambda *a: steps.append(a))
    return result, steps


def failure(opener: FakeOpener, **kw) -> GenerationError:
    with pytest.raises(GenerationError) as info:
        generate(opener, **kw)
    err = info.value
    for text in (str(err), repr(err), err.detail):
        assert KEY not in text
    return err


def test_request_shape(block_network) -> None:
    opener = FakeOpener()
    result, steps = generate(opener)
    assert len(opener.requests) == 1 and block_network == []
    req = opener.requests[0]
    assert req.full_url == API_URL == "https://api.openai.com/v1/responses" and req.get_method() == "POST"
    assert req.unredirected_hdrs["Authorization"] == f"Bearer {KEY}"
    assert "Authorization" not in req.headers  # never forwarded on a redirect
    body = opener.body()
    assert KEY not in req.data.decode()
    assert set(body) == {"model", "instructions", "input", "text", "store", "max_output_tokens", "truncation"}
    assert body["store"] is False and body["model"] == DEFAULT_MODEL and body["truncation"] == "disabled"
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "meeting_minutes_v1"
    assert body["instructions"].startswith("Create draft meeting minutes")
    env = opener.envelope()
    assert env["task"] == "extract_minutes" and env["schema_version"] == 1
    assert [s["id"] for s in env["segments"]] == ["s-0", "s-1", "s-2"] and env["segments"][0]["revision"] == 2
    assert env["gaps"][0]["reason"] == "mic_excluded" and env["confirmed_participants"] == []
    assert result.usage.input_tokens == 120 and result.usage.output_tokens == 30 and result.usage.requests == 1
    assert result.content["summary"][0]["text"] == "Weekly status"
    assert ("sending", 1, 1) in steps and ("validating", 1, 1) in steps


def test_no_key_is_refused_before_any_request() -> None:
    opener = FakeOpener()
    err = failure(opener, key=None)
    assert err.code == "no_key" and err.request_sent is False and opener.requests == []


@pytest.mark.parametrize("status,error,code", [
    (401, {"type": "invalid_request_error", "code": "invalid_api_key"}, "auth"),
    (404, {"code": "model_not_found"}, "model_unavailable"),
    (400, {"param": "model", "code": "invalid_value"}, "model_unavailable"),
    (429, {"type": "insufficient_quota", "code": "insufficient_quota"}, "quota"),
    (429, {"code": "project_spend_limit_exceeded"}, "quota"),
    (400, {"code": "context_length_exceeded"}, "too_large"),
    (400, {"code": "something_else"}, "request_rejected"),
])
def test_http_failures_are_classified_and_not_retried(status, error, code) -> None:
    opener = FakeOpener(http_error(status, error))
    err = failure(opener)
    assert err.code == code and err.request_sent is True and len(opener.requests) == 1
    assert f"HTTP {status}" in err.detail


def test_error_message_text_is_never_read() -> None:
    opener = FakeOpener(http_error(400, {"code": "x", "message": "echo of line 1 text zebracanary"}))
    err = failure(opener)
    assert "zebracanary" not in err.detail and "line 1" not in err.detail


def test_rate_limit_retries_are_bounded() -> None:
    opener = FakeOpener(*[http_error(429, {"code": "rate_limit_exceeded"}, {"Retry-After": "1"})] * 5)
    err = failure(opener, max_retries=2)
    assert err.code == "rate_limit" and err.request_sent is True and len(opener.requests) == 3


def test_server_error_then_success_counts_both_requests() -> None:
    opener = FakeOpener(http_error(503, {"code": "server_is_overloaded"}), reply(good_content()))
    result, _ = generate(opener)
    assert len(opener.requests) == 2 and result.usage.requests == 2


def test_refusal_incomplete_and_malformed_replies() -> None:
    assert failure(FakeOpener(reply(refusal=True))).code == "refusal"
    assert failure(FakeOpener(reply(good_content(), status="incomplete", incomplete="max_output_tokens"))).code \
        == "too_large"
    assert failure(FakeOpener(reply(good_content(), status="incomplete", incomplete="content_filter"))).code \
        == "refusal"
    assert failure(FakeOpener(b"<html>not json</html>")).code == "invalid_output"
    assert failure(FakeOpener(reply(good_content(), status="failed"))).code == "server_error"


def test_invalid_output_gets_exactly_one_repair() -> None:
    invented = good_content()
    invented["summary"][0]["evidence"] = [{"segment_id": "s-invented", "revision": 1}]
    opener = FakeOpener(reply(invented), reply(good_content()))
    result, _ = generate(opener)
    assert len(opener.requests) == 2 and result.usage.requests == 2
    repair = json.loads(opener.body(1)["input"][1]["content"][0]["text"])
    assert repair["task"] == "repair_minutes" and "not supplied" in repair["validation_errors"][0]
    # Still invalid after the one repair: fails, no third request.
    opener = FakeOpener(reply(invented), reply(invented), reply(good_content()))
    err = failure(opener)
    assert err.code == "invalid_output" and err.invalid_count == 1 and len(opener.requests) == 2


def test_oversized_output_is_rejected() -> None:
    huge = json.dumps(good_content())[:-1] + ', "x": "' + "a" * 1_100_000 + '"}'
    err = failure(FakeOpener(reply(huge), reply(huge)))
    assert err.code == "invalid_output"


@pytest.mark.parametrize("exc,code,sent", [
    (urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed")), "offline", False),
    (urllib.error.URLError(ConnectionRefusedError(10061, "refused")), "offline", False),
    (urllib.error.URLError(ssl.SSLCertVerificationError("cert")), "offline", False),
    (urllib.error.URLError(ConnectionResetError(10054, "reset")), "offline", None),
    (urllib.error.URLError(TimeoutError("timed out")), "timeout", None),
    (TimeoutError("read timed out"), "timeout", True),
    (ConnectionResetError(10054, "reset after send"), "offline", True),
])
def test_transport_failures_report_request_sent_truthfully(exc, code, sent) -> None:
    err = failure(FakeOpener(exc))
    assert (err.code, err.request_sent) == (code, sent)
    assert classify_transport(exc).request_sent is sent


def test_cancel_during_a_request_returns_promptly_as_unknown() -> None:
    release = threading.Event()
    cancel = threading.Event()

    def slow():
        release.wait(5)
        return reply(good_content())

    opener = FakeOpener(slow)
    threading.Timer(0.1, cancel.set).start()
    started = time.monotonic()
    err = failure(opener, cancel=cancel)
    release.set()
    assert err.code == "cancelled" and err.request_sent is None and time.monotonic() - started < 2


def test_cancel_before_sending_is_pre_send() -> None:
    cancel = threading.Event()
    cancel.set()
    opener = FakeOpener()
    err = failure(opener, cancel=cancel)
    assert err.code == "cancelled" and err.request_sent is False and opener.requests == []


def test_overall_timeout_is_bounded() -> None:
    release = threading.Event()
    opener = FakeOpener(lambda: release.wait(10) and reply(good_content()))
    started = time.monotonic()
    err = failure(opener, timeout_s=0.1)
    release.set()
    assert err.code == "timeout" and err.request_sent is None and time.monotonic() - started < 8


def test_redirects_are_refused() -> None:
    handler = _NoRedirect()
    assert handler.redirect_request(None, None, 307, "moved", {}, "https://elsewhere.example/") is None


def test_too_many_parts_is_refused_before_sending() -> None:
    opener = FakeOpener()
    err = failure(opener, snap=snapshot(40), window_tokens=10, max_parts=5)
    assert err.code == "too_large" and err.request_sent is False and opener.requests == []
