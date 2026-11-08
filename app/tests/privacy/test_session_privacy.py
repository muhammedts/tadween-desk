"""Mic mute/unmute carried through to the saved transcript, database and window events.

A canary word spoken while the mic is excluded must be absent from every saved row,
the search index, the database files and the events the window receives, while system
speech keeps being saved. Fakes only (fake devices/clock, tone recognizer)."""

from __future__ import annotations

import threading
from pathlib import Path

from session_rig import SessionRig
from speech_fakes import CANARY_WORD

from meeting_minutes.domain.contracts import Source


def everything_the_user_can_see(r: SessionRig) -> str:
    texts = [str(d.get("text", "")) for _, d in r.events]
    return " ".join(texts + r.saved())


def db_files(r: SessionRig) -> bytes:
    return b"".join(p.read_bytes() for p in r.db.parent.iterdir() if p.name.startswith(r.db.name))


def test_canary_spoken_while_muted_is_never_saved_shown_or_indexed(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.MIC, [(0.5, "hello"), (3.0, CANARY_WORD), (6.5, "budget")])
    r.say(Source.SYSTEM, [(1.5, "system"), (3.5, "again"), (5.0, "deadline")])
    r.run(2.5)
    assert r.session.set_mic_included(False)
    r.run(5.8)
    assert r.session.set_mic_included(True)  # a new mic epoch begins; the canary is not replayed
    r.say(Source.MIC, [(0.5, "budget")])
    r.run(8.0)
    r.session.stop()
    assert r.saved("mic") == ["hello", "budget"]
    assert r.saved("system") == ["system", "again", "deadline"]  # system continued while muted
    assert CANARY_WORD.lower() not in everything_the_user_can_see(r).lower()
    assert r.store.search(CANARY_WORD) == []
    codes = [code for kind, code, _ in r.timeline() if kind == "notice"]
    assert codes[:2] == ["mic_excluded", "mic_included"]
    assert [d["included"] for d in r.kinds("mic")] == [False, True]
    r.store.close()
    assert CANARY_WORD.encode() not in db_files(r) and CANARY_WORD.lower().encode() not in db_files(r)


def test_a_mic_decode_in_flight_when_muted_is_never_saved(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.MIC, [(0.5, CANARY_WORD)])
    r.say(Source.SYSTEM, [(3.0, "system")])
    r.run(1.0)
    # Cut the mic utterance but do not decode it yet.
    r.now += 700_000_000
    r.session.controller.process_pending()
    r.session.pipeline.ingest()
    r.recognizer.block = True
    worker = threading.Thread(target=r.session.pipeline.decode_next)
    worker.start()
    try:
        assert r.recognizer.entered.wait(5)  # the mic chunk is inside the recognizer
        r.session.set_mic_included(False)
    finally:
        r.recognizer.release.set()
        worker.join(5)
    r.run(5.0)
    r.session.stop()
    assert r.saved() == ["system"]
    assert CANARY_WORD.lower() not in everything_the_user_can_see(r).lower()
    assert r.session.pipeline.status()["sources"]["mic"]["rejected_at_commit"] == 1


def test_starting_with_the_mic_excluded_never_opens_it(tmp_path: Path) -> None:
    r = SessionRig(tmp_path)
    r.start(mic_included=False)
    assert r.handle(Source.MIC) is None
    r.say(Source.SYSTEM, [(0.5, "system")])
    r.run(2.0)
    r.session.stop()
    assert r.saved() == ["system"] and r.session.controller.status()["sources"]["mic"]["frames_admitted"] == 0


def test_a_canary_spoken_while_muted_never_reaches_an_exported_file(tmp_path: Path) -> None:
    """Save As writes the saved revision only, so excluded mic speech cannot
    reach a .txt or .md file either."""
    from meeting_minutes.export import render, write_file

    r = SessionRig(tmp_path)
    r.start()
    r.say(Source.MIC, [(0.5, "hello"), (3.0, CANARY_WORD)])
    r.say(Source.SYSTEM, [(3.5, "مرحبا")])
    r.run(2.5)
    assert r.session.set_mic_included(False)
    r.run(5.5)
    r.session.stop()
    snapshot = r.store.snapshot(r.session.meeting_id)
    for fmt in ("txt", "md"):
        path = tmp_path / f"export.{fmt}"
        write_file(path, render(snapshot, fmt))
        data = path.read_bytes()
        assert "hello".encode() in data and "مرحبا".encode("utf-8") in data
        assert CANARY_WORD.encode() not in data and CANARY_WORD.lower().encode() not in data
        assert b"mic excluded" in data  # the exclusion itself is recorded, without its speech
    r.store.close()
