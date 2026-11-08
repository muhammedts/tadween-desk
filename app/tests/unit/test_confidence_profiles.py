"""Per-model "uncertain recognition" settings: the standard 0.6B marks lines below a 0.70
token-probability floor and nothing else; the experimental 1.7B profile marks below 0.75
and also marks a line whose recognized language is neither English nor Arabic. The text
is never changed or dropped. Scripted local replies only; no server is started."""

from __future__ import annotations

import math

from test_qwen_engine import THIRD, Server, chunk, reply, tokens

from meeting_minutes.models.manifest import default_manifest_path, load_manifest, with_stt
from meeting_minutes.speech import qwen
from meeting_minutes.speech.qwen import LANG_RETRY, LOW_CONFIDENCE, QwenRecognizer, QwenService

LARGE = "qwen3-asr-1.7b-q8_0-gguf"


def p(value: float, n: int = 3) -> list[tuple[str, float]]:
    return [(f" w{i}", math.log(value)) for i in range(n)]


def test_each_model_gets_its_own_profile_and_0_6b_is_unchanged() -> None:
    path = default_manifest_path()
    standard = QwenService(load_manifest(path), path.parent)
    trial = QwenService(with_stt(load_manifest(path), LARGE), path.parent)
    assert (standard.confidence_floor, standard.mark_other_language) == (qwen.LOW_CONFIDENCE_P, False) == (0.70, False)
    assert (trial.confidence_floor, trial.mark_other_language) == (0.75, True)
    assert set(qwen.CONFIDENCE_PROFILES) == {LARGE}


def test_1_7b_profile_marks_below_0_75_and_any_third_language_line(monkeypatch) -> None:
    replies = [reply("language Arabic<asr_text>سنراجع الميزانية", logprobs=tokens("Arabic", p(0.80))),
               reply("language Arabic<asr_text>سنراجع الميزانية", logprobs=tokens("Arabic", p(0.74))),
               reply("language Chinese<asr_text>嗯。", logprobs=tokens("Chinese", p(0.99, 1))),
               reply("language Hindi<asr_text>बहुत अच्छी बात है", logprobs=tokens("Hindi", p(0.85))),
               reply("language English<asr_text>the budget", logprobs=tokens("English", p(0.76))),
               reply("language None<asr_text>", logprobs=tokens("None", []))]
    server = Server(monkeypatch, *replies)
    rec = QwenRecognizer(server, confidence_floor=0.75, mark_other_language=True)
    results = [rec.transcribe(chunk()) for _ in replies]
    assert [r.flags for r in results] == [(), (LOW_CONFIDENCE,), (LOW_CONFIDENCE,), (LOW_CONFIDENCE,), (), ()]
    assert [r.text for r in results][:4] == ["سنراجع الميزانية", "سنراجع الميزانية", "嗯。", "बहुत अच्छी बात है"]
    assert [r.language for r in results][:4] == ["ar", "ar", "chinese", "hindi"]  # kept as recognized


def test_the_standard_profile_still_leaves_a_confident_third_language_line_unmarked(monkeypatch) -> None:
    server = Server(monkeypatch, reply("language Chinese<asr_text>嗯。", logprobs=tokens("Chinese", p(0.99, 1))),
                    reply("language Arabic<asr_text>نعم", logprobs=tokens("Arabic", p(0.75))))
    rec = QwenRecognizer(server)  # defaults = the 0.6B profile
    assert [rec.transcribe(chunk()).flags for _ in range(2)] == [(), ()]


def test_forced_languages_are_never_third_language(monkeypatch) -> None:
    server = Server(monkeypatch, reply("the budget", logprobs=tokens(None, p(0.9))))
    rec = QwenRecognizer(server, language="en", confidence_floor=0.75, mark_other_language=True)
    assert rec.transcribe(chunk()).flags == ()


def test_r_judges_the_language_of_the_text_it_keeps(monkeypatch) -> None:
    sure = [{"token": " the", "logprob": math.log(0.95)}, {"token": " budget", "logprob": math.log(0.9)}]
    server = Server(monkeypatch, reply("language Portuguese<asr_text>o orcamento", logprobs=THIRD),
                    reply("the budget", logprobs=sure))
    rec = QwenRecognizer(server, policy="R", confidence_floor=0.75, mark_other_language=True)
    assert rec.transcribe(chunk()).flags == (LANG_RETRY,)  # re-decoded as English: not marked
