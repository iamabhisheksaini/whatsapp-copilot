"""Weekday correction — the guard against the model's calendar arithmetic."""

import datetime as dt

import pytest

from shared.timeparse import correct_weekday, mentioned_weekday, snap_to_weekday


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Schedule a demo next Wed at 11", 2),
        ("let's meet Thursday", 3),
        ("call on tues", 1),
        ("sunday works", 6),
        ("no day mentioned here", None),
        ("Monday or Tuesday", None),   # ambiguous: leave it alone
        ("wednesday and wed", 2),      # same day named twice is still unambiguous
        ("", None),
    ],
)
def test_mentioned_weekday(text, expected):
    assert mentioned_weekday(text) == expected


def test_weekday_name_inside_another_word_is_not_matched():
    assert mentioned_weekday("we monitor satellites") is None


def test_snap_moves_to_nearest_matching_weekday():
    # 2026-10-06 is a Tuesday; Wednesday is one day forward.
    tuesday = dt.datetime(2026, 10, 6, 11, 0)
    got = snap_to_weekday(tuesday, 2, today=dt.date(2026, 9, 29))
    assert got == dt.datetime(2026, 10, 7, 11, 0)
    assert got.strftime("%A") == "Wednesday"


def test_snap_can_move_backwards_when_that_is_nearer():
    # Thursday 2026-10-08 -> Wednesday is one day back, not six forward.
    thursday = dt.datetime(2026, 10, 8, 9, 0)
    got = snap_to_weekday(thursday, 2, today=dt.date(2026, 9, 29))
    assert got == dt.datetime(2026, 10, 7, 9, 0)


def test_snap_never_lands_in_the_past():
    """Scheduling is always forward-looking."""
    # Correcting backwards would land before today, so it rolls a week on.
    thursday = dt.datetime(2026, 9, 24, 9, 0)
    got = snap_to_weekday(thursday, 2, today=dt.date(2026, 9, 29))
    assert got.date() >= dt.date(2026, 9, 29)
    assert got.strftime("%A") == "Wednesday"


def test_snap_preserves_time_of_day_and_offset():
    moment = dt.datetime(
        2026, 10, 6, 11, 30, tzinfo=dt.timezone(dt.timedelta(hours=5, minutes=30))
    )
    got = snap_to_weekday(moment, 2, today=dt.date(2026, 9, 29))
    assert (got.hour, got.minute) == (11, 30)
    assert got.utcoffset() == dt.timedelta(hours=5, minutes=30)


def test_correct_weekday_is_a_no_op_when_already_right():
    wednesday = dt.datetime(2026, 10, 7, 11, 0)
    got, corrected = correct_weekday(wednesday, "demo next Wed", today=dt.date(2026, 9, 29))
    assert got == wednesday
    assert corrected is False


def test_correct_weekday_is_a_no_op_without_a_named_day():
    moment = dt.datetime(2026, 10, 6, 11, 0)
    got, corrected = correct_weekday(moment, "schedule a demo", today=dt.date(2026, 9, 29))
    assert got == moment
    assert corrected is False


def test_correct_weekday_fixes_the_observed_live_failure():
    """The exact case seen from gpt-4o-mini: asked for Wed, returned a Tuesday."""
    model_output = dt.datetime(2026, 10, 6, 11, 0)  # a Tuesday
    got, corrected = correct_weekday(
        model_output, "Schedule a demo next Wed at 11.", today=dt.date(2026, 9, 29)
    )
    assert corrected is True
    assert got.strftime("%A") == "Wednesday"
    assert got == dt.datetime(2026, 10, 7, 11, 0)
