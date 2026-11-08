"""Minutes generation: recognition uncertainty reaches the minutes, the original meeting
date/offset or an explicit unknown is sent, and the pre-send estimate and local cost
limit are enforced. Fake providers and a patched opener only; no network, no real key,
no paid request."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_minutes_openai import FakeOpener, http_error, reply  # noqa: F401 - shared helpers
from test_minutes_service import ENGLISH, ev, make_meeting, run, service

from meeting_minutes.minutes.openai_provider import DEFAULT_MODEL, KNOWN_PRICES, OpenAIMinutesGenerator
from meeting_minutes.minutes.provider import (
    FakeMinutesGenerator,
    GenerationError,
    GenerationOptions,
    Usage,
    estimate_cost,
)
from meeting_minutes.minutes.schema import empty_content
from meeting_minutes.minutes.service import (
    DEFAULT_BUDGET_USD,
    UNKNOWN_DATE_WARNING,
    original_local_start,
    snapshot_for,
)
from meeting_minutes.storage.store import TranscriptStore, local_zone_label, zone_offset


@pytest.fixture()
def store(tmp_path: Path):
    s = TranscriptStore(tmp_path / "m.db")
    yield s
    s.close()


def flagged_meeting(store: TranscriptStore) -> list[str]:
    """ENGLISH, with line 0 marked low_confidence and line 1 lang_uncertain then corrected."""
    store.create_meeting("m1", title="Weekly sync", timezone_name="EET (UTC+02:00)",
                         started_at="2026-11-02T08:00:00.000+00:00")
    store.save_epoch("m1", source="system", epoch=1, device_id="d", device_label="d", native_rate=16_000,
                     native_channels=1, start_ms=0, end_ms=None)
    flags = [["low_confidence"], ["lang_uncertain"], [], ["filler_suspect", "lang_retry", "not_a_flag"]]
    ids = [store.add_final("m1", source="system", epoch=1, start_ms=i * 2000, end_ms=i * 2000 + 1500, text=text,
                           language="en", engine_id="fake", model_hash="0" * 64, flags=flags[i])
           for i, (_, text) in enumerate(ENGLISH)]
    store.correct_segment(ids[1], "Let's launch on Monday (checked)")
    store.set_status("m1", "stopped", ended=True)
    store.add_timeline("m1", "notice", "finalized", 8000, metadata={"complete": True})
    return ids


def cite_all(envelope: dict, repair=None) -> dict:
    """A model that returns no warnings of its own and cites lines 0 and 2."""
    out = empty_content()
    out["summary"] = [{"text": "Budget", "evidence": ev(envelope, 0)},
                      {"text": "Press note", "evidence": ev(envelope, 2)}]
    return out


# ------------------------------------------------------------------ recognition uncertainty

def test_flags_reach_every_envelope_record_and_corrections_are_distinguished(store) -> None:
    ids = flagged_meeting(store)
    gen = FakeMinutesGenerator(cite_all)
    job = run(service(store, gen))
    assert job.state == "done"
    segs = {s["id"]: s for s in gen.envelopes[0]["segments"]}
    assert segs[ids[0]]["recognition_flags"] == ["low_confidence"] and "corrected_by_user" not in segs[ids[0]]
    assert segs[ids[1]]["recognition_flags"] == ["lang_uncertain"] and segs[ids[1]]["corrected_by_user"] is True
    assert "recognition_flags" not in segs[ids[2]]
    assert segs[ids[3]]["recognition_flags"] == ["filler_suspect", "lang_retry"]  # unknown flags are dropped
    assert [s["text"] for s in gen.envelopes[0]["segments"]][1] == "Let's launch on Monday (checked)"
    snap = snapshot_for(store, "m1", store.meeting("m1").transcript_revision)
    assert [s.uncertain for s in snap.segments] == [True, False, False, True]  # the correction is reviewed text


def test_context_segments_carry_flags_too(store) -> None:
    flagged_meeting(store)
    gen = FakeMinutesGenerator()
    snap = snapshot_for(store, "m1", store.meeting("m1").transcript_revision)
    gen.generate(snap, GenerationOptions("m", window_tokens=60, context_segments=1), threading.Event(),
                 lambda *a: None)
    assert len(gen.envelopes) == 4
    assert gen.envelopes[1]["context_segments"][0]["recognition_flags"] == ["low_confidence"]


def test_uncertainty_warning_is_kept_in_review_export_and_new_drafts(store) -> None:
    ids = flagged_meeting(store)
    svc = service(store, FakeMinutesGenerator(cite_all))
    version = run(svc).version_id
    doc = svc.document(version)
    [warning] = doc.version.warnings  # the provider returned none: this one is the app's own
    assert warning.startswith("2 transcript line(s) sent were marked") and "1 generated item(s) cite them" in warning
    evidence = {e["segment_id"]: e for item in doc.items for e in item.evidence}
    assert evidence[ids[0]]["uncertain"] is True and "uncertain" not in evidence[ids[2]]
    for fmt in ("md", "txt"):
        text = svc.export_text(version, fmt)
        assert "uncertain recognition" in text and "00:00 System (uncertain)" in text
        assert "00:04 System (uncertain)" not in text
    copy = svc.edit_as_new_draft(version)
    assert svc.document(copy).version.warnings == doc.version.warnings
    assert any(e.get("uncertain") for i in svc.document(copy).items for e in i.evidence)


def test_no_uncertainty_warning_without_flags(store) -> None:
    make_meeting(store, ENGLISH)
    svc = service(store, FakeMinutesGenerator(cite_all))
    assert svc.document(run(svc).version_id).version.warnings == ()


# ------------------------------------------------------------------ original meeting date

def test_new_meetings_record_their_offset_and_it_survives_a_timezone_change() -> None:
    cairo = timezone(timedelta(hours=2), "Egypt Standard Time")
    label = local_zone_label(datetime(2026, 11, 2, 0, 30, tzinfo=cairo))
    assert label == "Egypt Standard Time (UTC+02:00)" and zone_offset(label) == "+02:00"
    assert original_local_start("2026-11-01T22:30:00.000+00:00", label) == ("2026-11-02", "00:30", "+02:00")
    assert original_local_start("2026-11-02T03:00:00+00:00", "X (UTC-05:30)") == ("2026-11-01", "21:30", "-05:30")
    assert zone_offset("UTC") == "+00:00"
    for old in ("Egypt Standard Time", "Africa/Cairo", "EET", "local", ""):  # older meetings: unknown
        assert zone_offset(old) is None
        assert original_local_start("2026-11-01T22:30:00+00:00", old) == ("", "", "")


def test_session_start_records_the_zone_with_its_offset(tmp_path: Path) -> None:
    from capture_rig import DEVICES
    from session_rig import ENGINE
    from speech_fakes import EnergyVad, ToneRecognizer

    from meeting_minutes.capture.fake import RealtimeFakeBackend
    from meeting_minutes.session.meeting import MeetingSession
    from meeting_minutes.speech.config import SpeechConfig

    store = TranscriptStore(tmp_path / "s.db")
    session = MeetingSession(store, RealtimeFakeBackend(devices=DEVICES), ToneRecognizer(), EnergyVad,
                             engine=ENGINE, speech_config=SpeechConfig(partials=False))
    try:
        outcome = session.start(mic=None, system="fake:system-44k1-stereo", title="Zone")
        assert outcome.started
        zone = store.meeting(outcome.meeting_id).timezone
        assert zone_offset(zone) is not None and zone == local_zone_label()
    finally:
        session.stop()
        session.finished.wait(10)
        store.close(wait_s=5.0)


def test_unknown_original_date_leaves_relative_deadlines_unresolved(store) -> None:
    store.create_meeting("m1", title="Old", timezone_name="Egypt Standard Time",
                         started_at="2026-11-01T22:30:00.000+00:00")
    store.save_epoch("m1", source="mic", epoch=1, device_id="d", device_label="d", native_rate=16_000,
                     native_channels=1, start_ms=0, end_ms=None)
    store.add_final("m1", source="mic", epoch=1, start_ms=0, end_ms=1000, text="Sara sends it tomorrow",
                    language="en", engine_id="fake", model_hash="0" * 64)
    store.set_status("m1", "stopped", ended=True)

    def resolves_anyway(envelope: dict, repair=None) -> dict:
        assert envelope["meeting"]["local_date"] == "" and envelope["meeting"]["utc_offset"] == ""
        out = empty_content()
        out["action_items"] = [{"task": "Send it", "owner": "Sara", "due_date": "2026-11-02",
                                "due_date_text": "tomorrow", "date_status": "resolved_relative",
                                "commitment": "explicit", "evidence": ev(envelope, 0)}]
        return out

    svc = service(store, FakeMinutesGenerator(resolves_anyway))
    doc = svc.document(run(svc).version_id)
    [action] = doc.of("action")
    assert action.due_date is None and action.date_status == "ambiguous" and action.due_date_text == "tomorrow"
    assert UNKNOWN_DATE_WARNING in doc.version.warnings


# ------------------------------------------------------------------ estimate and cost limit

def test_estimate_counts_parts_retries_and_repair(store) -> None:
    make_meeting(store, ENGLISH)
    snap = snapshot_for(store, "m1", store.meeting("m1").transcript_revision)
    options = GenerationOptions(DEFAULT_MODEL, price_in_per_m=1.0, price_out_per_m=5.0, budget_usd=1.0,
                                window_tokens=60)
    est = estimate_cost(snap, options)
    assert est.parts == 4 and est.requests_max == 3 * (4 + 1)
    assert est.first_pass_usd == pytest.approx((est.input_tokens * 1.0 + 4 * 32_000 * 5.0) / 1e6)
    assert est.worst_case_usd > 3 * est.first_pass_usd
    unknown = estimate_cost(snap, GenerationOptions("custom-model"))
    assert unknown.first_pass_usd is None and unknown.worst_case_usd is None


def test_default_model_has_a_visible_price_and_a_default_limit(store) -> None:
    make_meeting(store, ENGLISH)
    svc = service(store)
    assert svc.prices() == KNOWN_PRICES[DEFAULT_MODEL] == (1.0, 5.0)
    opts = svc.options()
    assert (opts.price_in_per_m, opts.price_out_per_m, opts.budget_usd) == (1.0, 5.0, DEFAULT_BUDGET_USD)
    est = svc.estimate("m1", store.meeting("m1").transcript_revision)
    assert est is not None and est.parts == 1 and est.first_pass_usd < DEFAULT_BUDGET_USD
    svc.set_budget_usd(None)
    assert svc.budget_usd() is None and svc.options().budget_usd is None
    svc.set_budget_usd(2.5)
    assert svc.budget_usd() == 2.5
    with pytest.raises(ValueError):
        svc.set_budget_usd(0)
    with pytest.raises(ValueError):
        svc.set_prices(-1, 5)


def test_over_budget_and_unknown_price_are_refused_before_sending(store) -> None:
    make_meeting(store, ENGLISH)
    gen = FakeMinutesGenerator()
    svc = service(store, gen)
    svc.set_budget_usd(0.01)  # one part at the output limit already costs more
    job = run(svc)
    assert job.error.code == "budget" and job.error.request_sent is False and gen.envelopes == []
    svc.set_budget_usd(1.0)
    svc.set_model_id("custom-model")
    job = run(svc)
    assert job.error.code == "price_unknown" and job.error.request_sent is False and gen.envelopes == []
    svc.set_prices(1.0, 5.0)
    assert run(svc).state == "done" and len(gen.envelopes) == 1
    svc.set_prices(None, None)
    svc.set_budget_usd(None)  # no local limit: an unknown price no longer blocks
    assert run(svc).state == "done"


def test_the_limit_stops_retries_before_a_request_that_could_pass_it(block_network) -> None:
    from test_minutes_openai import snapshot

    snap = snapshot()
    base = GenerationOptions(DEFAULT_MODEL, price_in_per_m=1.0, price_out_per_m=5.0)
    one = estimate_cost(snap, base).first_pass_usd
    opener = FakeOpener(http_error(500), http_error(500), reply(empty_content()))
    gen = OpenAIMinutesGenerator(lambda: "sk-test-NOT-REAL", opener=opener, sleep=lambda s: None)
    with pytest.raises(GenerationError) as info:
        gen.generate(snap, GenerationOptions(DEFAULT_MODEL, price_in_per_m=1.0, price_out_per_m=5.0,
                                             budget_usd=one * 2.5), threading.Event(), lambda *a: None)
    assert info.value.code == "budget" and info.value.request_sent is True
    assert len(opener.requests) == 2  # the third attempt was never sent
    ok = FakeOpener(http_error(500), reply(empty_content(), usage=(100, 10)))
    gen = OpenAIMinutesGenerator(lambda: "sk-test-NOT-REAL", opener=ok, sleep=lambda s: None)
    result = gen.generate(snap, GenerationOptions(DEFAULT_MODEL, price_in_per_m=1.0, price_out_per_m=5.0,
                                                  budget_usd=one * 2.5), threading.Event(), lambda *a: None)
    assert result.usage.requests == 2 and gen._budget.spent_usd < one * 2  # actual usage replaced its reservation


# ------------------------------------------------------------------ reported usage against the limit

def _input_only(budget_factor: float, **kw) -> tuple:
    """Two-part synthetic snapshot, $1 per 1M input tokens and free output, so the
    reported input count alone decides what fits."""
    from test_minutes_openai import snapshot

    snap = snapshot(2)
    base = GenerationOptions(DEFAULT_MODEL, window_tokens=1, price_in_per_m=1.0, price_out_per_m=0.0, **kw)
    est = estimate_cost(snap, base)
    assert est.parts == 2
    budget = est.first_pass_usd * budget_factor
    return snap, GenerationOptions(DEFAULT_MODEL, window_tokens=1, price_in_per_m=1.0, price_out_per_m=0.0,
                                   budget_usd=budget, **kw), est


def test_reported_cost_above_the_estimate_is_counted_in_full_and_blocks_the_next_part(block_network) -> None:
    snap, options, est = _input_only(1.1)
    heavy = est.input_tokens  # one part reports as much as both parts were estimated
    opener = FakeOpener(reply(empty_content(), usage=(heavy, 0)), reply(empty_content(), usage=(10, 0)))
    gen = OpenAIMinutesGenerator(lambda: "sk-test-NOT-REAL", opener=opener, sleep=lambda s: None)
    with pytest.raises(GenerationError) as info:
        gen.generate(snap, options, threading.Event(), lambda *a: None)
    assert info.value.code == "budget" and info.value.request_sent is True
    assert len(opener.requests) == 1  # part 2 never sent
    assert gen._budget.spent_usd == pytest.approx(heavy / 1e6)  # not clipped to the reservation


def test_reported_overspend_blocks_the_repair_request() -> None:
    snap, options, est = _input_only(1.1, max_repairs=1)
    gen = FakeMinutesGenerator(lambda envelope, repair: "not json", usage=Usage(est.input_tokens, 0, 1))
    with pytest.raises(GenerationError) as info:
        gen.generate(snap, options, threading.Event(), lambda *a: None)
    assert info.value.code == "budget" and info.value.request_sent is True
    assert len(gen.envelopes) == 1 and gen.repairs == []


def test_a_retry_after_a_failed_attempt_keeps_that_reservation_and_reported_cost_adds_in_full(block_network) -> None:
    snap, options, est = _input_only(10.0)
    opener = FakeOpener(http_error(500), reply(empty_content(), usage=(est.input_tokens * 3, 0)),
                        reply(empty_content(), usage=(10, 0)))
    gen = OpenAIMinutesGenerator(lambda: "sk-test-NOT-REAL", opener=opener, sleep=lambda s: None)
    result = gen.generate(snap, options, threading.Event(), lambda *a: None)
    assert result.parts == 2 and len(opener.requests) == 3
    first_reservation = est.first_pass_usd / 2  # roughly one part's reservation, kept for the failed attempt
    assert gen._budget.spent_usd > (est.input_tokens * 3 + 10) / 1e6 + first_reservation * 0.5
