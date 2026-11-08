"""The app's Qwen3-ASR adapter (speech/qwen.py): the frozen R/F rules, the
bounded retry and its privacy/Stop guard, the shared owned server, and quality marks
through the real pipeline, meeting session, store and export. Synthetic replies and
fake processes only: no socket, model or network is used."""

from __future__ import annotations

import io
import json
import threading
import urllib.request
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.response import addinfourl
from uuid import uuid4

import numpy as np
import pytest
from session_rig import SessionRig
from speech_fakes import silence, utterance
from test_speech_pipeline import Harness

from meeting_minutes import export
from meeting_minutes.domain.contracts import AudioChunk, Source
from meeting_minutes.domain.events import GapReason
from meeting_minutes.models.manifest import ModelManifest
from meeting_minutes.speech import qwen
from meeting_minutes.speech.pipeline import DecodeGuard
from meeting_minutes.speech.qwen import (
    FILLER_SUSPECT,
    LANG_RETRY,
    LANG_UNCERTAIN,
    Decision,
    QwenRecognizer,
    Reply,
    decide,
    language_position,
)
from meeting_minutes.speech.sherpa import SpeechAssetsMissing
from meeting_minutes.ui.wording import engine_text


def position(chosen: str, top: list[tuple[str, float]]) -> list[dict]:
    """logprobs.content for 'language <chosen><asr_text>...'."""
    return [{"token": "language", "top_logprobs": [{"token": "language", "logprob": 0.0}]},
            {"token": f" {chosen}", "top_logprobs": [{"token": t, "logprob": lp} for t, lp in top]},
            {"token": "<asr_text>", "top_logprobs": [{"token": "<asr_text>", "logprob": 0.0}]}]


def reply(content: str, *, finish: str = "stop", logprobs: list | None = None, status: int = 200):
    choice: dict = {"message": {"content": content}, "finish_reason": finish}
    if logprobs is not None:
        choice["logprobs"] = {"content": logprobs}
    r = addinfourl(io.BytesIO(json.dumps({"choices": [choice]}).encode()), Message(),
                   "http://127.0.0.1:12345/v1/chat/completions", status)
    r.msg = "OK"
    return r


class Server:
    """Scripted replies in order; records every request's JSON body."""

    def __init__(self, monkeypatch, *replies) -> None:
        self.replies = list(replies)
        self.bodies: list[dict] = []
        self.port, self.api_key, self.process = 12345, "k", None
        self.on_request = None
        monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", self._open)

    def _open(self, request):
        self.bodies.append(json.loads(request.data))
        if self.on_request is not None:
            self.on_request(len(self.bodies))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def chunk(ms: int = 2000, source: Source = Source.SYSTEM) -> AudioChunk:
    return AudioChunk(uuid4(), source, 1, 0, 0, 0, ms, np.zeros(ms * 16, np.float32))


THIRD = position("Portuguese", [(" Portuguese", -0.2), (" English", -1.9), (" Arabic", -3.1)])


# ----------------------------------------------------------------- frozen rules


@pytest.mark.parametrize(("reply_", "expected"), [
    (Reply("text", None, False), Decision(None, "no_header", (LANG_UNCERTAIN,))),
    (Reply("", "None", True), Decision(None, "no_speech")),
    (Reply("", "Portuguese", True, ((" English", -1.0),)), Decision(None, "no_speech")),
    (Reply("hello", "English", True), Decision(None, "allowed")),
    (Reply("مرحبا", "Arabic", True), Decision(None, "allowed")),
    (Reply("ola", "Portuguese", True, None), Decision(None, "candidates_missing", (LANG_UNCERTAIN,))),
    (Reply("ola", "Portuguese", True, ((" Portuguese", -0.1), (" Spanish", -2.0))),
     Decision(None, "neither_listed", (LANG_UNCERTAIN,))),
    (Reply("ola", "Portuguese", True, ((" English", -1.5), (" Arabic", -1.5))),
     Decision(None, "tie", (LANG_UNCERTAIN,), -1.5, -1.5)),
    (Reply("ola", "Portuguese", True, ((" English", -1.0), ("English", -0.5), (" Arabic", -0.7))),
     Decision("English", "retry", (), -0.5, -0.7)),  # " English" and "English" are one name: best taken
    (Reply("ola", "Portuguese", True, ((" english", -0.1), (" Arab", -0.2), (" Arabic", -4.0))),
     Decision("Arabic", "retry", (), None, -4.0)),  # " english" and " Arab" are not the names
    (Reply("ola", "Swedish", True, ((" Swedish", -0.1), (" English", -6.0))),
     Decision("English", "retry", (), -6.0, None)),  # only one listed: it outranks the absent one
])
def test_decision_rule_is_frozen(reply_, expected) -> None:
    assert decide(reply_) == expected


def test_language_position_is_verified_or_absent() -> None:
    assert language_position(THIRD, "Portuguese")[1] == (" English", -1.9)
    split = position("Portug", [(" Portug", -0.1), (" English", -2.0)])  # a name split over tokens
    assert language_position(split, "Portuguese") == ((" Portug", -0.1), (" English", -2.0))
    assert language_position(THIRD, "Swedish") is None  # chosen token does not start the header
    assert language_position([{"token": "language"}], "Portuguese") is None  # no next position
    assert language_position([{"token": "x", "top_logprobs": []}], "Portuguese") is None
    assert language_position(position("Polish", []), "Polish") is None  # empty candidates
    assert language_position(None, "Polish") is None
    assert language_position([{"token": "language"}, {"token": " Polish", "top_logprobs": [{"token": 1}]}],
                             "Polish") is None  # malformed


# ------------------------------------------------------------------- recognizer


def test_policy_a_is_one_request_with_token_probabilities_only(monkeypatch) -> None:
    """Policy A asks for token log-probabilities (for the low-confidence mark) but no
    language candidates; with no probabilities returned there is no mark."""
    server = Server(monkeypatch, reply("language Portuguese<asr_text>ola"))
    result = QwenRecognizer(server).transcribe(chunk())
    assert (result.text, result.language, result.flags) == ("ola", "portuguese", ())
    body = server.bodies[0]
    assert len(server.bodies) == 1 and body["logprobs"] is True and "top_logprobs" not in body
    assert not body["cache_prompt"]


def test_r_retries_once_as_the_higher_allowed_language_and_marks_the_line(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Portuguese<asr_text>o orcamento", logprobs=THIRD),
                    reply("the budget"))
    paired: list = []
    recognizer = QwenRecognizer(server, policy="R", paired=paired)
    result = recognizer.transcribe(chunk())
    assert (result.text, result.language, result.flags) == ("the budget", "en", (LANG_RETRY,))
    first, second = server.bodies
    assert first["logprobs"] is True and first["top_logprobs"] == 20 and len(first["messages"]) == 1
    assert second["messages"][-1] == {"role": "assistant", "content": "language English<asr_text>"}
    assert "top_logprobs" not in second and second["max_tokens"] == first["max_tokens"]
    assert paired[0]["first_text"] == "o orcamento" and paired[0]["text"] == "the budget"  # A kept beside R
    decision = [c for c in recognizer.calls if c["kind"] == "decision"][0]
    assert decision["outcome"] == "retried" and decision["english_logprob"] == -1.9
    assert all("text" not in c for c in recognizer.calls)  # app records are content-free


def test_r_leaves_english_and_arabic_results_identical_to_a(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Arabic<asr_text>مرحبا",
                                       logprobs=position("Arabic", [(" Arabic", -0.01)])))
    result = QwenRecognizer(server, policy="R").transcribe(chunk())
    assert (result.text, result.language, result.flags) == ("مرحبا", "ar", ()) and len(server.bodies) == 1


@pytest.mark.parametrize(("second", "outcome"), [
    (reply("language English<asr_text>the bud", finish="length"), "retry_truncated"),
    (reply("", status=500), "retry_failed"),
    (OSError("synthetic reset"), "retry_failed"),
    (reply("   "), "retry_empty"),
])
def test_a_failed_cut_off_or_empty_retry_keeps_the_first_text_marked_uncertain(monkeypatch, second, outcome) -> None:
    server = Server(monkeypatch, reply("language Portuguese<asr_text>o orcamento", logprobs=THIRD), second)
    recognizer = QwenRecognizer(server, policy="R")
    result = recognizer.transcribe(chunk())
    assert (result.text, result.language, result.flags) == ("o orcamento", "portuguese", (LANG_UNCERTAIN,))
    assert recognizer.calls[-1]["outcome"] == outcome and len(server.bodies) == 2


def test_a_cut_off_first_reply_is_a_failure_and_never_retried(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Portuguese<asr_text>o orc", finish="length", logprobs=THIRD))
    with pytest.raises(qwen.QwenTruncated):
        QwenRecognizer(server, policy="R").transcribe(chunk())
    assert len(server.bodies) == 1


def test_no_retry_once_the_chunk_is_no_longer_admitted(monkeypatch) -> None:
    """Excluded (or reset, or abandoned) audio is never sent again."""
    server = Server(monkeypatch, reply("language Portuguese<asr_text>ola", logprobs=THIRD))
    guard = DecodeGuard(admitted=lambda: False, seconds_left=lambda: None)
    recognizer = QwenRecognizer(server, policy="R")
    result = recognizer.transcribe(chunk(), guard=guard)
    assert len(server.bodies) == 1 and result.flags == (LANG_UNCERTAIN,)
    assert recognizer.calls[-1]["outcome"] == "not_admitted"


def test_no_retry_without_enough_of_the_stop_budget_and_the_retry_fits_the_rest(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Portuguese<asr_text>ola", logprobs=THIRD))
    recognizer = QwenRecognizer(server, policy="R")
    result = recognizer.transcribe(chunk(), guard=DecodeGuard(lambda: True, lambda: 1.5))
    assert len(server.bodies) == 1 and recognizer.calls[-1]["outcome"] == "no_time" and result.text == "ola"
    timeouts = []
    real_call = qwen._call
    monkeypatch.setattr(qwen, "_call", lambda *a, **k: timeouts.append(k["timeout"]) or real_call(*a, **k))
    server.replies += [reply("language Portuguese<asr_text>ola", logprobs=THIRD), reply("hello")]
    QwenRecognizer(server, policy="R").transcribe(chunk(), guard=DecodeGuard(lambda: True, lambda: 5.0))
    assert timeouts[0] == qwen.REQUEST_TIMEOUT_S and timeouts[1] <= 5.0  # bounded by Stop's remaining time


def test_f_keeps_and_flags_a_short_third_language_word(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Chinese<asr_text>嗯",
                                       logprobs=position("Chinese", [(" Chinese", -0.3), (" Japanese", -1.0)])))
    result = QwenRecognizer(server, policy="R").transcribe(chunk(ms=800))
    assert result.text == "嗯" and result.flags == (LANG_UNCERTAIN, FILLER_SUSPECT)  # kept, never deleted
    server.replies.append(reply("language English<asr_text>okay", logprobs=position("English", [(" English", 0.0)])))
    assert QwenRecognizer(server, policy="R").transcribe(chunk(ms=800)).flags == ()  # a real short "okay"


def test_r_is_refused_for_forced_languages_and_unknown_policies() -> None:
    with pytest.raises(ValueError):
        QwenRecognizer(SimpleNamespace(port=1), language="en", policy="R")
    with pytest.raises(ValueError):
        QwenRecognizer(SimpleNamespace(port=1), policy="B")


def test_a_dead_server_fails_fast_without_sending(monkeypatch) -> None:
    server = Server(monkeypatch)
    server.process = SimpleNamespace(poll=lambda: 1)
    with pytest.raises(qwen.QwenServerError, match="^qwen_server_exited$"):
        QwenRecognizer(server).transcribe(chunk())
    assert server.bodies == []


def test_app_call_records_are_bounded() -> None:
    recognizer = QwenRecognizer(SimpleNamespace(port=1))
    for i in range(qwen.CALL_RECORDS + 10):
        recognizer.calls.append({"i": i})
    assert len(recognizer.calls) == qwen.CALL_RECORDS


# --------------------------------------------------------- through the pipeline


def test_mic_exclusion_between_the_two_requests_stops_the_retry_and_rejects_the_result(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Portuguese<asr_text>ola", logprobs=THIRD), reply("hello"))
    recognizer = QwenRecognizer(server, policy="R")
    h = Harness(recognizer=recognizer)
    server.on_request = lambda n: h.boundary.change(False, expected_generation=h.boundary.generation()) \
        if n == 1 else None
    h.play(mic=np.concatenate((utterance(["hello"]), silence(800))))
    h.settle()
    assert len(server.bodies) == 1  # the excluded chunk was not sent a second time
    assert h.finals(Source.MIC) == [] and h.pipeline.status()["sources"]["mic"]["rejected_at_commit"] == 1


def test_server_death_mid_meeting_is_an_honest_recognition_gap(monkeypatch) -> None:
    server = Server(monkeypatch)
    server.process = SimpleNamespace(poll=lambda: 1)
    h = Harness(recognizer=QwenRecognizer(server))
    h.play(system=np.concatenate((utterance(["hello"]), silence(800))))
    h.settle()
    assert h.finals(Source.SYSTEM) == [] and [g.reason for g in h.text.gaps] == [GapReason.STT_FAILED]
    assert h.pipeline.status()["fault_log"][-1]["error"] == "QwenServerError"


class FlaggingRecognizer:
    boundary = None

    def transcribe(self, chunk: AudioChunk):
        from meeting_minutes.speech.pipeline import RecognizedText

        return RecognizedText("ola amigos", "portuguese", (LANG_UNCERTAIN,))


def test_quality_marks_are_saved_shown_and_exported_without_changing_completeness(tmp_path: Path) -> None:
    rig = SessionRig(tmp_path, recognizer=FlaggingRecognizer())
    rig.session.engine = rig.session.engine.__class__(qwen.engine_id("llama.cpp-b11960/qwen3-asr-0.6b-q8_0-gguf",
                                                                     "auto", "R"), "0" * 64)
    rig.start()
    rig.say(Source.SYSTEM, [(0.3, "hello")])
    rig.run(3)
    rig.session.stop()
    rig.run(4)
    segments = rig.store.segments(rig.session.meeting_id)
    assert [s.flags for s in segments] == [("lang_uncertain",)]
    assert rig.kinds("final")[0]["flags"] == ["lang_uncertain"]
    snapshot = rig.store.snapshot(rig.session.meeting_id)
    text = export.render(snapshot, "txt")
    assert "System · language uncertain" in text and "ola amigos" in text
    assert "Speech model: Qwen3-ASR 0.6B on this PC (automatic language, experimental retry (R))" in text
    assert rig.session.finish_report["complete"]  # a quality mark is not a loss


def test_engine_identity_wording() -> None:
    assert engine_text("llama.cpp-b11960/qwen3-asr-0.6b-q8_0-gguf;language=auto;policy=A") == \
        "Qwen3-ASR 0.6B on this PC (automatic language)"
    assert engine_text("sherpa-onnx-1.13.8/whisper-small-int8-sherpa") == "sherpa-onnx-1.13.8/whisper-small-int8-sherpa"
    assert engine_text(None) == "not recorded"


# ---------------------------------------------------------- shared owned server


def service_manifest(tmp_path: Path, *, tamper: bool = False) -> ModelManifest:
    import hashlib

    def entry(role: str, files: dict[str, bytes]) -> dict:
        folder = tmp_path / role
        folder.mkdir()
        for name, data in files.items():
            (folder / f"{name}.bin").write_bytes(data + (b"!" if tamper and role == "stt" else b""))
        return {"id": f"{role}-x", "role": role, "engine": "llama.cpp", "engine_version": "b0",
                "upstream_checkpoint": "x", "source_revision": "r", "conversion_revision": "r", "languages": [],
                "task": "t", "install_subdir": role, "status": "unresolved-not-shippable",
                "files": [{"role": n, "basename": f"{n}.bin", "source_url": "https://example.invalid/x",
                           "revision": "r", "sha256": hashlib.sha256(d).hexdigest(), "bytes": len(d)}
                          for n, d in files.items()]}

    return ModelManifest.model_validate({"manifest_version": 1, "models": [
        entry("stt", {"model": b"m", "mmproj": b"p"}), entry("stt-runtime", {"server": b"s"}),
        entry("vad", {"model": b"v"})]})


class Child:
    def __init__(self) -> None:
        self.returncode = None
        self.signals: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("terminate")

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


def owned_http(server_holder):
    def http(handler, request):
        if request.selector == "/v1/models":
            ok = request.get_header("Authorization") == f"Bearer {server_holder[0].api_key}"
            return reply("", status=200 if ok else 401)
        return reply("", status=200)
    return http


def test_tampered_assets_never_start_a_server(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(qwen.subprocess, "Popen", lambda *a, **k: pytest.fail("server started"))
    service = qwen.QwenService(service_manifest(tmp_path, tamper=True), tmp_path, port=12345)
    with pytest.raises(SpeechAssetsMissing) as caught:
        service.server()
    assert "stt:model:size_mismatch" in str(caught.value) and str(tmp_path) not in str(caught.value)


def test_one_server_is_shared_restarted_after_exit_and_stopped_on_close(monkeypatch, tmp_path: Path) -> None:
    children: list[Child] = []
    started: list = []

    def spawn(args, **k):
        assert k["stdout"] is qwen.subprocess.DEVNULL and k["env"]["LLAMA_API_KEY"] not in " ".join(args)
        children.append(Child())
        return children[-1]

    original_start = qwen.QwenServer.start

    def start(self, log_path=None, *, cancel=None):
        started.append(self)
        return original_start(self, log_path, cancel=cancel)

    monkeypatch.setattr(qwen.subprocess, "Popen", spawn)
    monkeypatch.setattr(qwen, "_job", lambda: SimpleNamespace(assign=lambda process: True))  # fake child: owned
    monkeypatch.setattr(qwen.QwenServer, "start", start)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", lambda h, r: owned_http(started[-1:])(h, r))
    service = qwen.QwenService(service_manifest(tmp_path), tmp_path, port=12345)
    a, r = service.recognizer("auto"), service.recognizer("auto", "R")
    assert a.server is r.server and len(children) == 1  # every language choice shares one server
    children[0].returncode = 1  # it died
    assert service.server() is not a.server and len(children) == 2  # restarted on the next use
    service.close()
    assert children[1].signals == ["terminate"] and children[1].poll() is not None
    with pytest.raises(qwen.QwenStartCancelled):
        service.server()  # closed for good: the app is exiting


def test_closing_while_starting_cancels_and_reaps_the_child(monkeypatch, tmp_path: Path) -> None:
    child, spawned = Child(), threading.Event()

    def spawn(*a, **k):
        spawned.set()
        return child

    monkeypatch.setattr(qwen.subprocess, "Popen", spawn)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open",
                        lambda h, r: (_ for _ in ()).throw(ConnectionRefusedError()))  # never ready
    service = qwen.QwenService(service_manifest(tmp_path), tmp_path, port=12345)
    errors: list[BaseException] = []

    def load() -> None:
        try:
            service.server()
        except BaseException as exc:  # noqa: BLE001 - collected for the assertion
            errors.append(exc)

    worker = threading.Thread(target=load)
    worker.start()
    assert spawned.wait(5)
    service.close(timeout_s=1.0)
    worker.join(5)
    assert not worker.is_alive() and isinstance(errors[0], qwen.QwenStartCancelled)
    assert child.signals == ["terminate"] and child.poll() is not None


def test_the_default_desktop_loader_uses_qwen_automatic_and_records_policy(monkeypatch) -> None:
    from meeting_minutes.ui import desktop

    class FakeService:
        base_id, model_hash = "llama.cpp-b11960/qwen3-asr-0.6b-q8_0-gguf", "f" * 64

        def __init__(self) -> None:
            from meeting_minutes.models.manifest import default_manifest_path, load_manifest

            self._manifest = load_manifest(default_manifest_path())
            self._models_dir = default_manifest_path().parent

        def recognizer(self, language, policy="A"):
            return QwenRecognizer(SimpleNamespace(port=1), language=language, policy=policy)

    monkeypatch.setattr(desktop, "_qwen_service", FakeService)
    engine = desktop.load_local_engine("mixed")
    assert engine.info.engine_id.endswith(";language=auto;policy=A") and engine.recognizer.policy == "A"
    assert desktop.load_local_engine("mixed_r").recognizer.policy == "R"
    assert desktop.load_local_engine("ar").info.engine_id.endswith(";language=ar;policy=A")
    assert desktop.LANGUAGES[0][0] == "mixed"  # automatic A stays the first, default choice


def test_a_start_finishing_just_as_the_app_closes_is_never_published(monkeypatch, tmp_path: Path) -> None:
    """No late publication or resurrection after close; the child is stopped."""
    child = Child()
    service = qwen.QwenService(service_manifest(tmp_path), tmp_path, port=12345)

    def start(self, *args, cancel=None, **kwargs):
        self.process, self.api_key, self.tied_to_app = child, "k", True
        service._cancel.set()  # close() ran while the server was becoming ready
        return self

    monkeypatch.setattr(qwen.QwenServer, "start", start)
    with pytest.raises(qwen.QwenStartCancelled):
        service.server()
    assert service._server is None and child.signals == ["terminate"] and child.poll() is not None


def test_a_job_object_that_could_not_be_created_refuses_readiness_and_reaps(monkeypatch, tmp_path: Path) -> None:
    """Without kill-on-close ownership (job creation/configuration failed, so
    there is no handle) the owned child is stopped and start fails content-free."""
    child = Child()
    unavailable = qwen._KillOnCloseJob.__new__(qwen._KillOnCloseJob)
    unavailable.handle = None
    monkeypatch.setattr(qwen, "_job", lambda: unavailable)
    monkeypatch.setattr(qwen.subprocess, "Popen", lambda *a, **k: child)
    server = qwen.QwenServer(Path("s.exe"), Path("m.gguf"), Path("p.gguf"), port=12345)
    monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", owned_http([server]))
    with pytest.raises(RuntimeError, match="^qwen_server_not_owned_by_app$"):
        server.start()
    assert child.signals == ["terminate"] and child.poll() is not None and server.process is None


def test_a_failed_request_is_timed_and_classified_without_content(monkeypatch) -> None:
    server = Server(monkeypatch, OSError("synthetic timeout"))
    recognizer = QwenRecognizer(server)
    with pytest.raises(qwen.QwenServerError):
        recognizer.transcribe(chunk())
    record = recognizer.calls[-1]
    assert record["error"] == "qwen_server_unreachable_OSError" and record["wall_ms"] >= 0
    assert set(record) == {"kind", "audio_ms", "wall_ms", "error"}


def tokens(header: str | None, pieces: list[tuple[str, float]]) -> list[dict]:
    """logprobs.content: an optional automatic header, then the text tokens."""
    head = [{"token": "language", "logprob": 0.0}, {"token": f" {header}", "logprob": -0.01},
            {"token": "<asr_text>", "logprob": 0.0}] if header else []
    return head + [{"token": t, "logprob": lp} for t, lp in pieces]


def test_a_line_the_model_was_unsure_of_is_kept_and_marked_low_confidence(monkeypatch) -> None:
    """In a real meeting, invented lines for side talk averaged p < 0.70 per token,
    well-recognized lines >= 0.84. The text is never changed or dropped."""
    import math

    unsure = tokens("English", [(" I", math.log(0.5)), (" forgot", math.log(0.4)), (" the", math.log(0.6))])
    sure = tokens("English", [(" the", math.log(0.97)), (" budget", math.log(0.95))])
    server = Server(monkeypatch, reply("language English<asr_text>I forgot the", logprobs=unsure),
                    reply("language English<asr_text>the budget", logprobs=sure))
    recognizer = QwenRecognizer(server)
    low = recognizer.transcribe(chunk())
    assert (low.text, low.flags) == ("I forgot the", (qwen.LOW_CONFIDENCE,))
    assert recognizer.calls[-1]["mean_p"] == 0.493 and "text" not in recognizer.calls[-1]
    assert recognizer.transcribe(chunk()).flags == ()


def test_forced_language_lines_are_scored_too_and_the_floor_can_be_disabled(monkeypatch) -> None:
    import math

    forced = tokens(None, [(" invented", math.log(0.3)), (" words", math.log(0.4))])  # prefilled header
    server = Server(monkeypatch, reply("invented words", logprobs=forced), reply("invented words", logprobs=forced))
    assert QwenRecognizer(server, language="en").transcribe(chunk()).flags == (qwen.LOW_CONFIDENCE,)
    unmarked = QwenRecognizer(server, language="en", confidence_floor=None)
    assert unmarked.transcribe(chunk()).flags == () and "logprobs" not in server.bodies[-1]


def test_r_marks_the_confidence_of_the_text_it_keeps(monkeypatch) -> None:
    import math

    retry_sure = [{"token": " the", "logprob": math.log(0.95)}, {"token": " budget", "logprob": math.log(0.9)}]
    server = Server(monkeypatch, reply("language Portuguese<asr_text>o orcamento", logprobs=THIRD),
                    reply("the budget", logprobs=retry_sure))
    result = QwenRecognizer(server, policy="R").transcribe(chunk())
    assert result.flags == (LANG_RETRY,)  # the kept retry text is confident


def test_text_mean_p_ignores_header_tokens_and_bad_input() -> None:
    import math

    content = tokens("Arabic", [("x", math.log(0.5)), ("y", math.log(0.5))])
    assert qwen.text_mean_p(content) == 0.5
    assert qwen.text_mean_p(None) is None and qwen.text_mean_p([]) is None
    assert qwen.text_mean_p([{"token": "x"}]) is None
