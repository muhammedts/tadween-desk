"""Forced-cut boundary de-duplication: original spelling kept, only the boundary trimmed."""

from __future__ import annotations

from meeting_minutes.speech.dedup import normalize_token, trim_overlap


def test_repeated_leading_words_are_removed() -> None:
    assert trim_overlap("we will review the budget", "the budget tomorrow", max_tokens=8) == ("tomorrow", 2, False)


def test_matching_ignores_case_punctuation_and_arabic_marks_but_output_keeps_them() -> None:
    assert trim_overlap("Review the Budget.", "budget, then Deadline!", max_tokens=8) == ("then Deadline!", 1, False)
    # Shadda/tatweel/hamza variants match for alignment; the kept words are untouched.
    text, removed, ambiguous = trim_overlap("سنراجع الميزانيّة", "الميزانـية غداً إن شاء الله", max_tokens=8)
    assert (text, removed, ambiguous) == ("غداً إن شاء الله", 1, False)
    assert normalize_token("أحمد") == normalize_token("احمد")


def test_mixed_script_boundary() -> None:
    assert trim_overlap("نراجع the budget", "the budget يوم الأحد", max_tokens=8) == ("يوم الأحد", 2, False)


def test_unaligned_boundary_keeps_both_and_is_flagged() -> None:
    assert trim_overlap("review the budget", "tomorrow morning", max_tokens=8) == ("tomorrow morning", 0, True)


def test_repeats_inside_the_new_text_are_not_touched() -> None:
    assert trim_overlap("x y", "hello hello world", max_tokens=8) == ("hello hello world", 0, True)


def test_longest_alignment_wins_within_the_token_limit() -> None:
    assert trim_overlap("a b a b", "a b a b c", max_tokens=8) == ("c", 4, False)
    assert trim_overlap("a b a b", "a b a b c", max_tokens=2) == ("a b c", 2, False)


def test_empty_sides() -> None:
    assert trim_overlap("", "hello", max_tokens=8) == ("hello", 0, False)
    assert trim_overlap("hello", "", max_tokens=8) == ("", 0, False)
