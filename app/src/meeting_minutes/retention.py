"""History retention policy.

The user chooses how long History keeps transcripts: until they delete them, or 30, 90
or 365 days. 90 days is proposed at first run, but nothing expires until the user has
actually chosen: an unset or unreadable setting never deletes anything. The choice is
stored in the transcript database's `settings` table, so it survives restarts.

A meeting expires when it ended (or, if no end was recorded, started) more than the
chosen number of days ago. Meetings still running or still being saved are never
expired. Files the user saved with Save As are independent copies outside the app's
control; the app keeps no other copies of transcript text.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

SETTING_KEY = "retention"
CHOICES: tuple[tuple[int | None, str], ...] = (
    (None, "Keep until I delete them"),
    (30, "Delete after 30 days"),
    (90, "Delete after 90 days (proposed)"),
    (365, "Delete after 365 days"),
)
ALLOWED = frozenset(days for days, _ in CHOICES)
PROPOSED_DAYS = 90
NOT_CHOSEN = object()  # sentinel: the user has not chosen a policy yet


def label(days: int | None) -> str:
    return next(text for d, text in CHOICES if d == days)


def parse(value: object) -> int | None | object:
    """The stored setting as days (None = keep until deleted), or NOT_CHOSEN when it is
    missing or not one of the allowed choices (which must never delete anything)."""
    if not isinstance(value, dict) or "days" not in value:
        return NOT_CHOSEN
    days = value["days"]
    if days is not None and type(days) is not int:  # bool, float, text, list, object: not a choice
        return NOT_CHOSEN
    return days if days in ALLOWED else NOT_CHOSEN


def to_setting(days: int | None, now: datetime) -> dict[str, object]:
    if days not in ALLOWED:
        raise ValueError("unsupported retention choice")
    return {"days": days, "chosen_at": now.astimezone(timezone.utc).isoformat(timespec="milliseconds")}


def cutoff(days: int, now: datetime) -> str:
    """UTC timestamp before which a finished meeting has expired."""
    return (now.astimezone(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
