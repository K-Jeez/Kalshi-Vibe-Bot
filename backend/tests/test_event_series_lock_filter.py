import unittest

from src.bot.loop import _apply_event_series_locks, _event_series_lock_blocks_market


class TestEventSeriesLockFilter(unittest.TestCase):
    def test_allows_only_locked_market_in_series(self):
        tradeable = [
            {"id": "M1", "event_ticker": "EVT1"},
            {"id": "M2", "event_ticker": "EVT1"},
            {"id": "M3", "event_ticker": "EVT2"},
        ]
        out = _apply_event_series_locks(
            tradeable,
            locked_event_tickers={"EVT1"},
            allowed_market_id_by_event={"EVT1": "M2"},
        )
        self.assertEqual([m["id"] for m in out], ["M2", "M3"])

    def test_unlocked_events_unchanged(self):
        tradeable = [
            {"id": "M1", "event_ticker": "EVT1"},
            {"id": "M2", "event_ticker": "EVT2"},
        ]
        out = _apply_event_series_locks(
            tradeable,
            locked_event_tickers=set(),
            allowed_market_id_by_event={},
        )
        self.assertEqual(out, tradeable)

    def test_lock_blocks_non_chosen_sibling(self):
        locked = {"EVT1"}
        allowed = {"EVT1": "M1"}
        self.assertFalse(_event_series_lock_blocks_market("M1", "EVT1", locked_event_tickers=locked, allowed_market_id_by_event=allowed))
        self.assertTrue(_event_series_lock_blocks_market("M2", "EVT1", locked_event_tickers=locked, allowed_market_id_by_event=allowed))
        self.assertFalse(_event_series_lock_blocks_market("M3", "EVT2", locked_event_tickers=locked, allowed_market_id_by_event=allowed))


if __name__ == "__main__":
    unittest.main()

