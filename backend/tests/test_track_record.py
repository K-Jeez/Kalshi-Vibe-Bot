"""The settled-bet comparison is a report, not a new trading rule."""

from src.track_record import track_record_note, wilson_interval


def test_small_sample_does_not_invent_a_comparison():
    note = track_record_note([{"won": True, "ai_side_pct": 80, "entry": 0.6, "pnl": 1}])
    assert note["n"] == 1
    assert "Not enough" in note["note"]


def test_note_shows_claimed_chance_against_wins_and_an_interval():
    rows = [{"won": i < 7, "ai_side_pct": 80, "entry": 0.70, "pnl": 1 if i < 7 else -1} for i in range(10)]
    note = track_record_note(rows)
    assert note["n"] == 10
    assert note["avg_model_pct"] == 80.0
    assert "80%" in note["note"]
    assert "70%" in note["note"]
    lo, hi = wilson_interval(7, 10)
    assert lo < 0.7 < hi
