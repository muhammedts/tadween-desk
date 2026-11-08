"""Minutes service, storage, versions, review, export, deletion/redaction and grounded
English/Arabic/code-switched synthetic meetings. Generation uses FakeMinutesGenerator
or the OpenAI adapter with a patched opener; no network."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from meeting_minutes.minutes.credentials import MemoryCredentialStore
from meeting_minutes.minutes.openai_provider import OpenAIMinutesGenerator
from meeting_minutes.minutes.provider import FakeMinutesGenerator, GenerationError, GenerationOptions
from meeting_minutes.minutes.schema import empty_content
from meeting_minutes.minutes.service import MinutesService, MinutesUnavailable
from meeting_minutes.storage.store import MinutesRejected, TranscriptStore

KEY = "sk-canary-key-a81c3f-NOT-REAL"
SECRET = "zebracanaryquartz"  # transcript text canary
GEN = "minutesgeneratedcanary"  # generated-text canary


def make_meeting(store: TranscriptStore, lines: list[tuple[str, str]], meeting_id: str = "m1",
                 title: str = "Weekly sync", *, status: str = "stopped", gap: bool = False) -> list[str]:
    store.create_meeting(meeting_id, title=title, timezone_name="EET (UTC+02:00)",  # as new meetings record it
                         started_at="2026-11-02T08:00:00.000+00:00")
    for source in ("mic", "system"):
        store.save_epoch(meeting_id, source=source, epoch=1, device_id=f"d-{source}", device_label=source,
                         native_rate=48_000, native_channels=1, start_ms=0, end_ms=None)
    ids = [store.add_final(meeting_id, source=source, epoch=1, start_ms=i * 2000, end_ms=i * 2000 + 1500, text=text,
                           language="ar" if any("؀" <= c <= "ۿ" for c in text) else "en",
                           engine_id="fake", model_hash="0" * 64)
           for i, (source, text) in enumerate(lines)]
    if gap:
        store.add_timeline(meeting_id, "gap", "device_lost", 3000, 4000, "mic", {"loss": True})
    if status != "capturing":
        store.set_status(meeting_id, status, ended=True)
        store.add_timeline(meeting_id, "notice", "finalized", len(lines) * 2000, metadata={"complete": not gap})
    else:
        store.set_status(meeting_id, "capturing")
    return ids


def service(store: TranscriptStore, generator=None, *, key: str | None = KEY) -> MinutesService:
    creds = MemoryCredentialStore()
    if key:
        creds.save(key)
    gen = generator or FakeMinutesGenerator()
    return MinutesService(store, creds, lambda c: gen)


def run(svc: MinutesService, meeting_id: str = "m1", revision: int | None = None, **kw):
    revision = svc.store.meeting(meeting_id).transcript_revision if revision is None else revision
    job = svc.start(meeting_id, revision, **kw)
    assert job.wait(10)
    return job


def db_bytes(tmp_path: Path) -> bytes:
    return b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.name.startswith("m.db"))


def ev(envelope: dict, i: int) -> list[dict]:
    seg = envelope["segments"][i]
    return [{"segment_id": seg["id"], "revision": seg["revision"]}]


def scripted(envelope: dict, repair=None) -> dict:
    """What a careful model would return for the canonical English meeting below."""
    out = empty_content()
    out["summary"] = [{"text": f"Budget review {GEN}", "evidence": ev(envelope, 0)}]
    out["topics"] = [{"title": "Budget", "discussion": "Numbers were reviewed.", "evidence": ev(envelope, 0)}]
    out["decisions"] = [{"text": "Launch on Monday", "status": "superseded", "evidence": ev(envelope, 1)},
                        {"text": "Launch on Tuesday instead", "status": "agreed", "evidence": ev(envelope, 3)}]
    out["open_questions"] = [{"text": "Who books the venue?", "owner": None, "evidence": ev(envelope, 2)}]
    out["action_items"] = [
        {"task": "Send the budget sheet", "owner": "Sara", "due_date": "2026-11-05", "due_date_text": "Thursday",
         "date_status": "resolved_relative", "commitment": "explicit", "evidence": ev(envelope, 0)},
        {"task": "Maybe draft a press note", "owner": None, "due_date": None, "due_date_text": None,
         "date_status": "unspecified", "commitment": "proposed", "evidence": ev(envelope, 2)},
    ]
    return out


ENGLISH = [("mic", f"Sara: I will send the budget sheet by Thursday {SECRET}"), ("system", "Let's launch on Monday"),
           ("mic", "Maybe someone could draft a press note? Who books the venue?"),
           ("system", "Actually no, we launch on Tuesday instead")]


@pytest.fixture()
def store(tmp_path: Path):
    s = TranscriptStore(tmp_path / "m.db")
    yield s
    s.close()


# ------------------------------------------------------------------ generation

def test_generation_saves_a_grounded_draft_version(store: TranscriptStore) -> None:
    ids = make_meeting(store, ENGLISH)
    gen = FakeMinutesGenerator(scripted)
    svc = service(store, gen)
    progress = []
    job = run(svc, on_progress=lambda j: progress.append(j.step[0]))
    assert job.state == "done", job.error
    assert progress[0] == "preparing" and "sending" in progress and "validating" in progress
    [info] = svc.versions("m1")
    v = info.row
    assert (v.version, v.state, v.provider, v.prompt_version, v.schema_version) == (1, "draft", "Test provider",
                                                                                   "mom-v1", 1)
    assert (v.input_tokens, v.output_tokens, v.requests, v.parts) == (100, 20, 1, 1)
    assert v.transcript_revision == store.meeting("m1").transcript_revision and not info.stale
    doc = svc.document(v.id)
    actions = doc.of("action")
    assert [a.commitment for a in actions] == ["explicit", "proposed"]
    assert actions[1].owner is None and actions[1].due_date is None and actions[1].date_status == "unspecified"
    assert actions[0].evidence[0] == {"segment_id": ids[0], "revision": 1, "start_ms": 0, "source": "mic"}
    assert [d.status for d in doc.of("decision")] == ["superseded", "agreed"]
    assert svc.badge("m1") == ("Draft", "2")
    assert store.search(GEN) == []  # generated minutes text is not indexed for search


def test_snapshot_sends_only_the_selected_saved_revision(store: TranscriptStore) -> None:
    ids = make_meeting(store, ENGLISH)
    make_meeting(store, [("mic", "othermeetingcanary text")], "m2", "Other")
    store.correct_segment(ids[1], "Let's launch on Monday (corrected)")
    store.add_timeline("m1", "gap", "mic_excluded", 7000, 8000, "mic", {"loss": False})
    gen = FakeMinutesGenerator()
    job = run(service(store, gen))
    assert job.state == "done"
    [env] = gen.envelopes
    texts = [s["text"] for s in env["segments"]]
    assert "Let's launch on Monday (corrected)" in texts and "Let's launch on Monday" not in texts
    assert next(s for s in env["segments"] if s["id"] == ids[1])["revision"] == 2
    flat = str(env)
    assert "othermeetingcanary" not in flat and KEY not in flat
    assert {g["reason"] for g in env["gaps"]} == {"mic_excluded"}
    assert env["meeting"]["local_date"] == "2026-11-02" and env["meeting"]["local_start_time"] == "10:00"
    assert env["meeting"]["utc_offset"] == "+02:00" and env["meeting"]["timezone"] == "EET (UTC+02:00)"
    assert set(env["segments"][0]) == {"id", "revision", "source", "start_ms", "end_ms", "text"}  # no audio


def test_pre_send_refusals_never_call_the_provider(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)
    make_meeting(store, [("mic", "live")], "live", status="capturing")
    gen = FakeMinutesGenerator()
    no_key = run(service(store, gen, key=None))
    assert (no_key.state, no_key.error.code, no_key.error.request_sent) == ("failed", "no_key", False)
    old = run(service(store, gen), revision=1)
    assert (old.error.code, old.error.request_sent) == ("transcript_changed", False)
    live = run(service(store, gen), "live")
    assert (live.error.code, live.error.request_sent) == ("not_saved", False)
    assert gen.envelopes == [] and store.minutes_versions("m1") == []


def test_one_job_at_a_time(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)
    gate = threading.Event()
    svc = service(store, FakeMinutesGenerator(gate=gate))
    job = svc.start("m1", store.meeting("m1").transcript_revision)
    with pytest.raises(GenerationError) as info:
        svc.start("m1", store.meeting("m1").transcript_revision)
    assert info.value.code == "busy" and info.value.request_sent is False
    gate.set()
    assert job.wait(5) and job.state == "done"


def test_cancel_discards_the_result(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)
    gate = threading.Event()
    svc = service(store, FakeMinutesGenerator(gate=gate))
    job = svc.start("m1", store.meeting("m1").transcript_revision)
    job.cancel()
    assert job.wait(5)
    assert job.state == "cancelled" and store.minutes_versions("m1") == []


def test_saving_the_key_makes_no_request_and_the_key_never_reaches_storage(store: TranscriptStore, tmp_path: Path,
                                                                           block_network) -> None:
    make_meeting(store, ENGLISH)

    class Opener:
        calls = 0

        def open(self, *a, **k):
            Opener.calls += 1
            raise AssertionError("no request expected")

    creds = MemoryCredentialStore()
    svc = MinutesService(store, creds, lambda c: OpenAIMinutesGenerator(c.get, opener=Opener()))
    svc.save_key(KEY)
    svc.set_model_id("gpt-test-model")
    svc.set_prices(2.0, 8.0)  # a custom model's price is unknown until entered
    svc.save_key(KEY + "2")  # replace
    svc.remove_key()
    svc.save_key(KEY)
    assert Opener.calls == 0 and block_network == []
    job = run(MinutesService(store, creds, lambda c: FakeMinutesGenerator(scripted)))
    text = svc.export_text(job.version_id, "md") + svc.export_text(job.version_id, "txt")
    store.close()
    raw = db_bytes(tmp_path)
    assert KEY.encode() not in raw and KEY not in text and KEY not in repr(svc) and KEY not in repr(creds)
    assert b"gpt-test-model" in raw  # the model id is a plain, non-secret setting


def test_key_canary_absent_from_error_text(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)

    class Opener:
        def open(self, request, timeout=None):
            raise ConnectionResetError(10054, "reset")

    creds = MemoryCredentialStore()
    creds.save(KEY)
    svc = MinutesService(store, creds, lambda c: OpenAIMinutesGenerator(c.get, opener=Opener()))
    job = run(svc)
    assert job.error.code == "offline" and job.error.request_sent is True
    assert all(KEY not in t for t in (str(job.error), repr(job.error), job.error.detail))


# ---------------------------------------------------------- versions and review

def test_edit_review_approve_and_new_draft(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)
    svc = service(store, FakeMinutesGenerator(scripted))
    v1 = run(svc).version_id
    action = svc.document(v1).of("action")[1]
    svc.edit_item(action.id, owner="  ", due_date="2026-11-20", commitment="explicit", reviewed=True)
    edited = svc.document(v1).of("action")[1]
    assert edited.owner is None and edited.due_date == "2026-11-20" and edited.date_status == "explicit"
    assert edited.commitment == "explicit" and edited.reviewed and svc.document(v1).version.user_edited
    svc.edit_item(action.id, due_date=None)
    assert svc.document(v1).of("action")[1].date_status == "unspecified"
    added = svc.add_item(v1, "decision", "Hire a designer")
    assert next(i for i in svc.document(v1).items if i.id == added).origin == "user"
    with pytest.raises(MinutesRejected):
        svc.approve(v1)  # draft -> approved skips review
    svc.start_review(v1)
    assert svc.unspecified_actions(v1) == 1
    svc.approve(v1)
    with pytest.raises(MinutesRejected) as info:
        svc.edit_item(action.id, owner="Ali")
    assert info.value.code == "read_only"
    v2 = svc.edit_as_new_draft(v1)
    copy = svc.document(v2)
    assert (copy.version.version, copy.version.state, copy.version.parent_version) == (2, "draft", 1)
    assert len(copy.items) == len(svc.document(v1).items) and {i.id for i in copy.items}.isdisjoint(
        {i.id for i in svc.document(v1).items})
    assert svc.document(v1).version.state == "approved"


def test_regenerate_creates_a_new_draft_and_keeps_edits(store: TranscriptStore) -> None:
    ids = make_meeting(store, ENGLISH)
    svc = service(store, FakeMinutesGenerator(scripted))
    v1 = run(svc).version_id
    summary = svc.document(v1).of("summary")[0]
    svc.edit_item(summary.id, text="My edited overview")
    store.correct_segment(ids[3], "Actually we launch on Wednesday")
    assert svc.versions("m1")[0].stale and svc.badge("m1")[0] == "Stale"
    v2 = run(svc).version_id
    infos = svc.versions("m1")
    assert [i.row.version for i in infos] == [1, 2] and infos[1].latest and not infos[1].stale
    assert svc.document(v1).of("summary")[0].text == "My edited overview"
    assert svc.document(v1).version.user_edited and not svc.document(v2).version.user_edited
    assert v1 != v2


def test_versions_survive_a_restart(tmp_path: Path) -> None:
    store = TranscriptStore(tmp_path / "m.db")
    make_meeting(store, ENGLISH)
    v1 = run(service(store, FakeMinutesGenerator(scripted))).version_id
    store.close()
    again = TranscriptStore(tmp_path / "m.db")
    svc = service(again)
    assert svc.document(v1).of("action")[0].owner == "Sara"
    again.close()


# ---------------------------------------------------------------------- export

def test_export_states_draft_version_revision_and_unspecified(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH, gap=True)
    svc = service(store, FakeMinutesGenerator(scripted))
    v1 = run(svc).version_id
    txt = svc.export_text(v1, "txt")
    for needle in ("Weekly sync — minutes", "DRAFT — check before sharing", "Draft — not reviewed · Version 1",
                   f"Transcript revision: {store.meeting('m1').transcript_revision}", "Owner: Not specified",
                   "Due: Not specified", "Suggested — not agreed", "[00:00 Mic]", "1 interval(s) without text",
                   "[Superseded] Launch on Monday", "Needs review", "Open questions"):
        assert needle.lower() in txt.lower(), needle
    md = svc.export_text(v1, "md")
    assert md.startswith("# Weekly sync — minutes") and "\\[00:00 Mic\\]" in md
    assert svc.suggested_filename(v1).endswith("— minutes (draft).md")
    svc.start_review(v1)
    svc.approve(v1)
    approved = svc.export_text(v1, "txt")
    assert "DRAFT" not in approved and "Approved · Version 1" in approved
    out = Path(store.path).parent / "minutes.md"
    svc.save_as(v1, out, "md")
    assert out.read_text(encoding="utf-8").startswith("# Weekly sync — minutes")


def test_markdown_escapes_generated_text(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)

    def hostile(envelope, repair=None):
        out = empty_content()
        out["summary"] = [{"text": "<script>x</script> [link](http://e.example) # heading",
                           "evidence": ev(envelope, 0)}]
        return out

    svc = service(store, FakeMinutesGenerator(hostile))
    md = svc.export_text(run(svc).version_id, "md")
    assert "<script>" not in md and "\\<script\\>" in md and "[link](" not in md and "\\[link\\](" in md
    assert "\\# heading" in md


# ------------------------------------------------------- deletion and redaction

def test_meeting_deletion_removes_all_minutes_data(store: TranscriptStore, tmp_path: Path) -> None:
    make_meeting(store, ENGLISH)
    svc = service(store, FakeMinutesGenerator(scripted))
    v1 = run(svc).version_id
    svc.start_review(v1)
    store.delete_meeting("m1")
    assert store.minutes_versions("m1") == [] and store.minutes_items(v1) == []
    with pytest.raises(MinutesUnavailable):
        svc.document(v1)
    store.close()
    raw = db_bytes(tmp_path)
    assert GEN.encode() not in raw and SECRET.encode() not in raw


def test_redaction_invalidates_every_version_and_deletes_derived_text(store: TranscriptStore,
                                                                      tmp_path: Path) -> None:
    ids = make_meeting(store, ENGLISH)
    svc = service(store, FakeMinutesGenerator(scripted))
    v1 = run(svc).version_id
    svc.start_review(v1)
    svc.approve(v1)
    v2 = svc.edit_as_new_draft(v1)
    svc.edit_item(svc.document(v2).of("summary")[0].id, text=f"edited copy {GEN}")
    store.redact_segment(ids[3])  # not cited by the summary: the safest policy still invalidates all
    infos = svc.versions("m1")
    assert all(i.row.invalidated and i.row.invalidated_reason == "transcript_redacted" for i in infos)
    for vid in (v1, v2):
        assert store.minutes_items(vid) == [] and store.minutes_snapshot_refs(vid) == []
        with pytest.raises(MinutesUnavailable) as info:
            svc.export_text(vid, "txt")
        assert info.value.code == "invalidated"
        with pytest.raises(MinutesRejected):
            svc.edit_as_new_draft(vid)
    assert svc.badge("m1") == ("Not generated", "")
    store.close()
    raw = db_bytes(tmp_path)
    assert GEN.encode() not in raw and b"Launch on Tuesday instead" not in raw
    reopened = TranscriptStore(tmp_path / "m.db")  # still invalidated after a restart
    assert all(v.invalidated for v in reopened.minutes_versions("m1"))
    reopened.close()


def test_redaction_by_a_build_without_minutes_support_is_detected(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)
    svc = service(store, FakeMinutesGenerator(scripted))
    v1 = run(svc).version_id
    # An older build redacts without touching the minutes tables: only its notice remains.
    store.add_timeline("m1", "notice", "segment_redacted", 0, 900, "mic")
    assert svc.versions("m1")[0].row.invalidated and store.minutes_items(v1) == []


@pytest.mark.parametrize("change", ["redact", "delete"])
def test_a_late_result_after_redaction_or_deletion_is_rejected(store: TranscriptStore, tmp_path: Path,
                                                               change: str) -> None:
    ids = make_meeting(store, ENGLISH)
    gate = threading.Event()
    svc = service(store, FakeMinutesGenerator(scripted, gate=gate))
    job = svc.start("m1", store.meeting("m1").transcript_revision)
    while not job.step[0] == "sending":
        threading.Event().wait(0.01)
    if change == "redact":
        store.redact_segment(ids[0])
    else:
        store.delete_meeting("m1")
    gate.set()
    assert job.wait(5)
    assert job.state == "failed" and job.error.code == "transcript_invalidated" and job.error.request_sent is True
    assert store.minutes_versions("m1") == []
    store.close()
    assert GEN.encode() not in db_bytes(tmp_path)


def test_a_correction_during_generation_saves_a_stale_draft(store: TranscriptStore) -> None:
    ids = make_meeting(store, ENGLISH)
    gate = threading.Event()
    svc = service(store, FakeMinutesGenerator(scripted, gate=gate))
    job = svc.start("m1", store.meeting("m1").transcript_revision)
    while not job.step[0] == "sending":
        threading.Event().wait(0.01)
    store.correct_segment(ids[1], "Corrected later")
    gate.set()
    assert job.wait(5) and job.state == "done"
    assert svc.versions("m1")[0].stale


# --------------------------------------------------- bilingual grounded meetings

BILINGUAL = [
    ("mic", "صباح الخير، نبدأ بمراجعة الميزانية"),
    ("system", "OK, so the vendor contract — العقد لازم يتراجع before we sign"),
    ("mic", "ممكن حد يجهز العرض؟ مش متأكدين مين"),
    ("system", "لن نوقّع العقد هذا الأسبوع"),  # negation: no signing
    ("mic", "خالد هيبعت الفاتورة يوم الأحد"),
]


def bilingual_model(envelope, repair=None):
    """A grounded answer in Arabic: unknown owner/date null, proposal kept as proposal,
    negated decision kept as a decision not to sign, relative date ambiguous."""
    out = empty_content("ar")
    out["output_language"] = "mixed"
    out["summary"] = [{"text": "مراجعة الميزانية وعقد المورد", "evidence": ev(envelope, 0) + ev(envelope, 1)}]
    out["decisions"] = [{"text": "لن يتم توقيع العقد هذا الأسبوع", "status": "agreed", "evidence": ev(envelope, 3)}]
    out["open_questions"] = [{"text": "من سيجهز العرض؟", "owner": None, "evidence": ev(envelope, 2)}]
    out["action_items"] = [
        {"task": "تجهيز العرض", "owner": None, "due_date": None, "due_date_text": None, "date_status": "unspecified",
         "commitment": "proposed", "evidence": ev(envelope, 2)},
        {"task": "إرسال الفاتورة", "owner": "خالد", "due_date": None, "due_date_text": "يوم الأحد",
         "date_status": "ambiguous", "commitment": "explicit", "evidence": ev(envelope, 4)},
    ]
    out["warnings"] = ["Mic and System are sources, not speakers."]
    return out


def test_bilingual_code_switched_meeting_keeps_script_unknowns_and_proposals(store: TranscriptStore) -> None:
    make_meeting(store, BILINGUAL, gap=True)
    gen = FakeMinutesGenerator(bilingual_model)
    svc = service(store, gen)
    svc.set_output_language("ar")
    job = run(svc)
    assert job.state == "done", job.error
    env = gen.envelopes[0]
    assert env["output_language"] == "ar" and env["segments"][1]["text"] == BILINGUAL[1][1]
    doc = svc.document(job.version_id)
    assert doc.version.incomplete and doc.version.output_language == "mixed"
    proposal, invoice = doc.of("action")
    assert proposal.owner is None and proposal.commitment == "proposed"
    assert invoice.owner == "خالد" and invoice.due_date is None and invoice.due_date_text == "يوم الأحد"
    txt = svc.export_text(job.version_id, "txt")
    assert "لن يتم توقيع العقد هذا الأسبوع" in txt and "Not specified (said “يوم الأحد”)" in txt
    assert "Owner: Not specified" in txt and "1 interval(s) without text" in txt


def test_a_normalized_ambiguous_date_is_repaired_once(store: TranscriptStore) -> None:
    make_meeting(store, BILINGUAL)

    def first_wrong(envelope, repair=None):
        out = bilingual_model(envelope)
        if repair is None:
            out["action_items"][1]["due_date"] = "2026-11-08"  # guessed a date for an ambiguous weekday
        return out

    gen = FakeMinutesGenerator(first_wrong)
    job = run(service(store, gen))
    assert job.state == "done" and len(gen.repairs) == 1
    assert "ambiguous date must not be normalized" in gen.repairs[0]["errors"][0]


def test_invented_evidence_is_never_saved(store: TranscriptStore) -> None:
    make_meeting(store, ENGLISH)

    def inventive(envelope, repair=None):
        out = scripted(envelope)
        out["action_items"][0]["evidence"] = [{"segment_id": "not-a-segment", "revision": 1}]
        return out

    job = run(service(store, FakeMinutesGenerator(inventive)))
    assert job.error.code == "invalid_output" and job.error.invalid_count == 1
    assert store.minutes_versions("m1") == []


# ------------------------------------------------------------- long transcripts

def test_long_transcript_map_reduce_keeps_late_actions(store: TranscriptStore) -> None:
    lines = [("mic" if i % 2 else "system", f"Line {i}: " + "discussion words " * 30) for i in range(60)]
    lines[-1] = ("mic", "Final point: Omar will archive the files by Friday")
    ids = make_meeting(store, lines)

    def per_part(envelope, repair=None):
        out = empty_content()
        new = envelope["segments"]
        assert all(s["id"] not in {c["id"] for c in envelope["context_segments"]} for s in new)
        out["summary"] = [{"text": "Same recurring point", "evidence": ev(envelope, 0)}]
        out["action_items"] = [{"task": f"Follow up part {envelope['part']['index']}", "owner": None,
                                "due_date": None, "due_date_text": None, "date_status": "unspecified",
                                "commitment": "uncertain", "evidence": ev(envelope, len(new) - 1)}]
        return out

    gen = FakeMinutesGenerator(per_part)
    svc = service(store, gen)
    options = GenerationOptions(model_id="m", window_tokens=1500, max_parts=12)
    job = run(svc, options=options)
    assert job.state == "done", job.error
    parts = len(gen.envelopes)
    assert parts > 3
    sent = [s["id"] for e in gen.envelopes for s in e["segments"]]
    assert sent == ids  # every segment exactly once, in order, never truncated
    assert all(len(e["context_segments"]) == (0 if e["part"]["index"] == 1 else 2) for e in gen.envelopes)
    doc = svc.document(job.version_id)
    actions = doc.of("action")
    assert len(actions) == parts and actions[-1].evidence[0]["segment_id"] == ids[-1]
    assert doc.version.parts == parts and doc.version.requests == parts
    assert any(f"processed in {parts} parts" in w for w in doc.version.warnings)
    assert len(doc.of("summary")) == parts  # different evidence: kept, not merged away


def test_too_long_is_refused_before_sending(store: TranscriptStore) -> None:
    make_meeting(store, [("mic", "words " * 200) for _ in range(30)])
    gen = FakeMinutesGenerator()
    job = run(service(store, gen), options=GenerationOptions(model_id="m", window_tokens=300, max_parts=3))
    assert (job.error.code, job.error.request_sent) == ("too_large", False) and gen.envelopes == []


def test_model_and_language_settings_persist(store: TranscriptStore) -> None:
    svc = service(store)
    assert svc.model_id() == "gpt-6.1-terra"
    svc.set_model_id(" my-model-2026 ")
    assert svc.model_id() == "my-model-2026" and svc.options().model_id == "my-model-2026"
    with pytest.raises(ValueError):
        svc.set_model_id("bad model id; drop")
    with pytest.raises(ValueError):
        svc.set_output_language("fr")
