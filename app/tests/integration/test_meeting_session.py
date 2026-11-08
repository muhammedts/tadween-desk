"""Meeting session to saved transcript, with fakes only: fake devices and clock,
tone-word recognizer, energy VAD; real capture controller, speech pipeline and SQLite.

Covers the stop tail (short last utterance, late decode, overall stop budget), two-source
order and coverage, recovery after a restart, one-source loss and a full disk during a
meeting."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from capture_rig import SECOND
from session_rig import ENGINE, SessionRig
from speech_fakes import ToneRecognizer

from meeting_minutes.domain.contracts import Source
from meeting_minutes.domain.transcript import TextKind, TranscriptText
from meeting_minutes.session.meeting import SessionState
from meeting_minutes.storage.store import UNFINISHED, StorageError, TranscriptStore
from meeting_minutes.ui.wording import coverage_summary


class DiskFull:
    """The store refuses finals the way it reports a real SQLITE_FULL (that mapping and
    its full rollback are tested against SQLite in test_transcript_store.py)."""

    def __init__(self, store: TranscriptStore) -> None:
        self.full = False
        self._add_final = store.add_final
        store.add_final = self

    def __call__(self, *args, **kwargs):
        if self.full:
            raise StorageError("disk_full")
        return self._add_final(*args, **kwargs)


def test_two_sources_are_saved_in_time_order_with_lineage_and_a_complete_coverage_notice(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    assert r.start().started
    r.say(Source.MIC, [(0.5, "hello"), (3.0, "budget")])
    r.say(Source.SYSTEM, [(1.5, "مرحبا"), (4.5, "deadline")])
    r.run(6.0)
    r.session.stop()
    assert r.session.state is SessionState.STOPPED
    segments = r.store.segments(r.session.meeting_id)
    assert [(s.source, s.text) for s in segments] == [
        ("mic", "hello"), ("system", "مرحبا"), ("mic", "budget"), ("system", "deadline")]
    assert all(s.epoch == 1 and s.origin == "asr" and s.end_ms > s.start_ms for s in segments)
    assert [s.start_ms for s in segments] == sorted(s.start_ms for s in segments)
    meeting = r.store.meeting(r.session.meeting_id)
    assert meeting.status == "stopped" and meeting.ended_at is not None and meeting.transcript_revision == 4
    assert sorted(r.store.epochs(r.session.meeting_id)) == [("mic", 1), ("system", 1)]
    [notice] = [t for t in r.store.timeline(r.session.meeting_id) if t.code == "finalized"]
    assert notice.metadata["complete"] and notice.metadata["drain_complete"]
    assert [d["text"] for d in r.kinds("final")] == ["hello", "مرحبا", "budget", "deadline"]


def test_a_short_last_utterance_cut_off_by_stop_is_still_saved(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.SYSTEM, [(1.0, "hello"), (2.68, "again")])  # "again" ends exactly at stop
    r.run(3.0)
    assert r.saved() == ["hello"]
    r.session.stop()
    assert r.saved() == ["hello", "again"] and r.session.state is SessionState.STOPPED


def test_pause_and_resume_are_recorded_and_speech_continues_in_new_epochs(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.session.set_mic_included(False)
    r.say(Source.SYSTEM, [(0.5, "hello")])
    r.run(2.0)
    assert r.session.pause() and r.session.state is SessionState.PAUSED
    r.run(4.0)
    assert r.session.resume()
    assert r.handle(Source.MIC) is None  # the exclusion persists through pause/resume
    r.say(Source.SYSTEM, [(0.5, "again")])
    r.run(6.0)
    r.session.stop()
    assert [(s.source, s.epoch, s.text) for s in r.store.segments(r.session.meeting_id)] == [
        ("system", 1, "hello"), ("system", 2, "again")]
    timeline = r.timeline()
    assert ("pause", "paused_by_user", None) in timeline and ("resume", "resumed_by_user", None) in timeline
    assert ("notice", "mic_excluded", "mic") in timeline


def test_one_lost_source_is_recorded_while_the_other_keeps_saving(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.MIC, [(0.5, "hello"), (4.0, "budget")])
    r.say(Source.SYSTEM, [(0.5, "system")])
    r.run(2.0)
    r.handle(Source.SYSTEM).lose()  # unplugged
    r.run(6.0)
    assert r.session.state is SessionState.DEGRADED
    r.session.stop()
    assert r.saved("mic") == ["hello", "budget"] and r.saved("system") == ["system"]
    timeline = r.timeline()
    assert ("device_change", "device_lost", "system") in timeline
    assert any(kind == "gap" and source == "system" for kind, _, source in timeline)
    report = r.session.finish_report
    assert report["capture_loss_gaps"] >= 1 and not report["complete"]  # loss is never hidden
    assert r.kinds("source_lost") == [{"source": "system"}]


def test_a_full_disk_pauses_capture_keeps_text_and_saves_it_after_an_explicit_resume(tmp_path: Path) -> None:
    """No new audio is taken while text cannot be kept; nothing is lost
    once space is freed, and capture restarts only on the user's Resume."""
    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.SYSTEM, [(0.5, "hello"), (2.5, "budget"), (4.5, "deadline")])
    r.run(1.5)
    assert r.saved() == ["hello"]
    disk = DiskFull(r.store)
    disk.full = True
    r.run(4.0)
    assert [d["text"] for d in r.kinds("final_unsaved")] == ["budget"]
    assert r.session.state is SessionState.PAUSED and r.session.save_blocked == "disk_full"
    assert r.handle(Source.SYSTEM) is None  # capture closed: "deadline" is never taken in
    assert not r.session.resume()  # still full: stays paused
    assert r.session.state is SessionState.PAUSED
    disk.full = False  # space freed
    assert r.session.resume()
    assert r.saved() == ["hello", "budget"]  # the kept text was saved, in order
    assert [d["text"] for d in r.kinds("final")] == ["hello", "budget"]
    assert r.session.save_blocked is None and r.session.unsaved == []
    r.say(Source.SYSTEM, [(0.5, "again")])
    r.run(6.0)
    r.session.stop()
    assert r.saved() == ["hello", "budget", "again"]
    codes = [code for _, code, _ in r.timeline()]
    assert "paused_save_failed" in codes and "resumed_by_user" in codes and "text_not_saved" not in codes
    assert r.store.meeting(r.session.meeting_id).status == "stopped"


def test_unsaved_text_kept_in_memory_is_bounded_and_the_rest_is_recorded_as_intervals(tmp_path: Path,
                                                                                       monkeypatch) -> None:
    import meeting_minutes.session.meeting as meeting_module

    monkeypatch.setattr(meeting_module, "UNSAVED_TEXT_MAX", 1)
    r = SessionRig(tmp_path)
    r.start(mic=None)
    disk = DiskFull(r.store)
    disk.full = True
    for i, word in enumerate(("first", "second", "third")):
        r.session._texts.put(TranscriptText(
            kind=TextKind.FINAL, meeting_id=r.session.meeting_uuid, source=Source.SYSTEM, epoch=1,
            privacy_generation=0, hypothesis_id=f"system:{i}", chunk_index=i, start_ms=1_000 * i,
            end_ms=1_000 * i + 500, text=word, language="en"))
    r.session._write_pending()
    assert [u.event.text if u.event else None for u in r.session.unsaved] == ["first", None, None]
    assert [d["text_kept"] for d in r.kinds("final_unsaved")] == [True, False, False]
    disk.full = False
    r.session.stop()
    assert r.saved() == ["first"]
    lost = [t for t in r.store.timeline(r.session.meeting_id) if t.code == "text_not_saved"]
    assert [(t.start_ms, t.end_ms) for t in lost] == [(1_000, 1_500), (2_000, 2_500)]
    assert r.store.meeting(r.session.meeting_id).status == "stopped"  # every interval accounted for
    # The queue drained, but two finals' text is gone for good.
    report = r.session.finish_report
    assert report["drain_complete"] and not report["complete"] and report["text_not_saved"] == 2
    assert r.session.text_lost == 2  # never reset by the successful retry
    [notice] = [t for t in r.store.timeline(r.session.meeting_id) if t.code == "finalized"]
    assert notice.metadata["complete"] is False and notice.metadata["text_not_saved"] == 2
    summary = coverage_summary("stopped", r.store.timeline(r.session.meeting_id))
    assert not summary.startswith("Complete") and "2 interval(s) without text" in summary


def test_failed_history_writes_are_retried_in_order_and_overflow_makes_coverage_incomplete(
        tmp_path: Path, monkeypatch) -> None:
    """A refused timeline write is never silently dropped."""
    import meeting_minutes.session.meeting as meeting_module

    r = SessionRig(tmp_path)
    r.start()
    add_timeline = r.store.add_timeline
    refusing = {"on": True}

    def flaky(*args, **kwargs):
        if refusing["on"]:
            raise StorageError("io_error")
        return add_timeline(*args, **kwargs)

    r.store.add_timeline = flaky
    r.session.set_mic_included(False)
    r.session.pause()
    r.session._write_pending()
    assert r.session.status()["unsaved_timeline"] == 2 and r.session.storage_error == "io_error"
    refusing["on"] = False
    r.session._write_pending()
    assert sorted(code for _, code, _ in r.timeline()) == ["mic_excluded", "paused_by_user"]  # same instant
    assert r.session.storage_error is None

    monkeypatch.setattr(meeting_module, "PENDING_META_MAX", 1)
    refusing["on"] = True
    r.session.resume()
    r.session.pause()
    r.session._write_pending()
    assert r.session.metadata_lost == 1  # the retry list was full
    refusing["on"] = False
    r.session.stop()
    report = r.session.finish_report
    assert report["unsaved_timeline"] == 1 and not report["complete"] and not report["drain_complete"]
    assert r.store.meeting(r.session.meeting_id).status == "interrupted"


def test_a_stalled_store_cannot_hold_stop_past_its_budget_and_a_late_save_stays_interrupted(
        tmp_path: Path) -> None:
    """A stalled store, with the real writer thread and a real clock."""
    from capture_rig import DEVICES
    from speech_fakes import EnergyVad

    from meeting_minutes.capture.fake import RealtimeFakeBackend
    from meeting_minutes.session.meeting import MeetingSession
    from meeting_minutes.speech.config import SpeechConfig

    store = TranscriptStore(tmp_path / "meetings.db")
    entered, release = threading.Event(), threading.Event()
    add_final = store.add_final

    def stalled(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return add_final(*args, **kwargs)

    store.add_final = stalled
    session = MeetingSession(store, RealtimeFakeBackend(devices=DEVICES), ToneRecognizer(), EnergyVad,
                             engine=ENGINE, speech_config=SpeechConfig(partials=False), stop_budget_s=0.5)
    try:
        assert session.start(mic=None, system="fake:system-44k1-stereo").started
        handle = [h for h in session.controller._backend.opened if not h.closed][-1]
        from speech_fakes import scripted
        handle.signal = scripted(44_100, [(handle._next_index / 44_100 + 0.2, "hello")])
        assert entered.wait(10)  # the writer is stuck saving "hello"
        started = time.monotonic()
        session.stop()
        assert session.finished.wait(3.0)
        assert time.monotonic() - started < 1.5  # 0.5 s budget, generous tolerance
        assert session.state is SessionState.INTERRUPTED
        report = session.finish_report
        assert report["unsaved_finals"] >= 1 and not report["complete"] and report["save_timed_out"]
        # Nothing terminal is claimed yet (the stuck writer may not even have recorded
        # 'stopping'); an unfinished status is recovered as interrupted at the next start.
        assert store.meeting(session.meeting_id).status in UNFINISHED
    finally:
        release.set()
    deadline = time.monotonic() + 5
    while store.meeting(session.meeting_id).status in UNFINISHED and time.monotonic() < deadline:
        time.sleep(0.05)
    assert store.meeting(session.meeting_id).status == "interrupted"  # the late save never says "stopped"
    [notice] = [t for t in store.timeline(session.meeting_id) if t.code == "finalized"]
    assert notice.metadata["completed_after_stop_deadline"] and not notice.metadata["complete"]
    assert [s.text for s in store.segments(session.meeting_id)] == ["hello"]  # its text was not lost


def test_a_decode_running_at_the_stop_deadline_is_recorded_and_never_saved_later(tmp_path: Path) -> None:
    recognizer = ToneRecognizer()
    r = SessionRig(tmp_path, recognizer=recognizer, stop_budget_s=0.3)
    r.start()
    r.say(Source.SYSTEM, [(0.5, "hello"), (2.4, "budget")])
    r.run(3.0)
    assert r.saved() == ["hello"]  # "budget" is still an open utterance
    recognizer.block = True  # stop's drain will decode it, and that decode hangs
    started = time.monotonic()
    try:
        r.session.stop()
        assert time.monotonic() - started < 3.0  # bounded by the stop budget
        assert recognizer.entered.is_set()
    finally:
        recognizer.release.set()
    drain = next((t for t in threading.enumerate() if t.name == "speech-drain"), None)
    if drain is not None:
        drain.join(5)
    assert r.saved() == ["hello"]  # the late result never reaches the transcript
    assert r.session.state is SessionState.INTERRUPTED
    assert r.store.meeting(r.session.meeting_id).status == "interrupted"
    gaps = [t for t in r.store.timeline(r.session.meeting_id) if t.code == "stopped_undrained"]
    assert gaps and gaps[0].source == "system" and gaps[0].end_ms > gaps[0].start_ms
    assert r.session.finish_report["abandoned_in_flight"]


def test_a_restart_shows_saved_text_and_the_interruption_without_reopening_capture(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.SYSTEM, [(0.5, "hello"), (2.0, "budget")])
    r.run(3.0)
    meeting_id = r.session.meeting_id
    opened = len(r.backend.opened)
    r.store.close()  # the app ends without Stop (crash or forced exit)

    store = TranscriptStore(r.db)
    assert store.recover_unfinished() == [meeting_id]
    assert store.meeting(meeting_id).status == "interrupted"
    assert [s.text for s in store.segments(meeting_id)] == ["hello", "budget"]
    assert ("error", "app_ended_during_meeting") in [(t.kind, t.code) for t in store.timeline(meeting_id)]
    assert len(r.backend.opened) == opened  # nothing reopened capture


def test_a_start_that_opens_nothing_leaves_no_meeting_behind(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.backend.fail_open.update({"fake:mic-48k-mono", "fake:system-44k1-stereo"})
    outcome = r.start()
    assert not outcome.started and set(outcome.failed) == {"mic", "system"}
    assert r.store.list_meetings() == []


def test_a_failed_start_still_reports_cleanly_when_index_maintenance_fails(tmp_path: Path) -> None:
    import sqlite3

    r = SessionRig(tmp_path)
    r.backend.fail_open.update({"fake:mic-48k-mono", "fake:system-44k1-stereo"})
    denied: list[str] = []

    def refuse_index_maintenance(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_INSERT and table == "search_fts" and trigger is None:
            denied.append(table)
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    r.store._conn.set_authorizer(refuse_index_maintenance)
    outcome = r.start()  # the empty record is deleted; only the tidy-up after it fails
    r.store._conn.set_authorizer(None)
    assert denied and not outcome.started and set(outcome.failed) == {"mic", "system"}
    assert r.store.list_meetings() == []


def test_stop_finishes_within_the_budget_even_when_capture_never_delivered(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.session.stop()
    assert r.session.state is SessionState.STOPPED and r.saved() == []
    assert r.now < SECOND  # no fake time needed
