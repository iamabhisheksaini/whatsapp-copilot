"""Weekday correction for LLM-extracted datetimes.

LLMs are unreliable at calendar arithmetic: asked for "next Wed" on a Tuesday,
gpt-4o-mini returns a date that is a Tuesday about as often as not. Booking a
meeting on the wrong day is the kind of error a user only notices after the
fact, so when the message names a weekday we treat that name as authoritative
and snap the model's date onto it.

The model still does what it is good at — reading the time of day and the
intent — and this only corrects the date component.
"""

import datetime as dt
import re
from typing import Optional

WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2, "weds": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

_WEEKDAY_RE = re.compile(
    r"\b(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b", re.IGNORECASE
)


def mentioned_weekday(text: str) -> Optional[int]:
    """Return the weekday index named in the text, or None.

    Only acts on an unambiguous single mention — "Monday or Tuesday" is left
    for a human to sort out rather than silently picking one.
    """
    names = {m.group(1).lower() for m in _WEEKDAY_RE.finditer(text or "")}
    indices = {WEEKDAYS[n] for n in names}
    return indices.pop() if len(indices) == 1 else None


def snap_to_weekday(
    moment: dt.datetime, target: int, today: Optional[dt.date] = None
) -> dt.datetime:
    """Shift `moment` to the nearest date matching `target`, keeping the time.

    The shift is the smallest one in [-3, +3] days, so an off-by-one from the
    model is corrected without jumping a whole week. If the corrected result has
    already passed, it moves a week forward — a request to schedule something is
    always about the future.
    """
    delta = (target - moment.weekday()) % 7
    if delta > 3:
        delta -= 7
    corrected = moment + dt.timedelta(days=delta)

    reference = today or dt.date.today()
    if corrected.date() < reference:
        corrected += dt.timedelta(days=7)
    return corrected


def correct_weekday(
    start: dt.datetime, text: str, today: Optional[dt.date] = None
) -> tuple[dt.datetime, bool]:
    """Apply weekday correction if the message names one.

    Returns (datetime, corrected?) so callers can log when the model was wrong.
    """
    target = mentioned_weekday(text)
    if target is None or start.weekday() == target:
        return start, False
    return snap_to_weekday(start, target, today), True
