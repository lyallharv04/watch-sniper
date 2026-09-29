"""A sweep's call count survives the budget's midnight reset."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from .ebay import CallBudget


class TestCallBudget(unittest.TestCase):
    def test_total_never_resets(self):
        b = CallBudget(ceiling=10)
        for _ in range(5):
            b.take("2026-09-28")
        b.take("2026-09-29")
        self.assertEqual((b.used, b.total), (1, 6))

    def test_a_sweep_across_midnight_records_its_real_calls(self):
        """The live database recorded one sweep as -483 calls: the day's
        count reset to zero mid-sweep and the sweep subtracted across it."""
        from .db import Database
        from .poller import Engine

        class Client:
            budget = CallBudget(ceiling=10_000)

            def __init__(self):
                self.budget.day, self.budget.used = "2026-09-28", 483

            def search(self, **_kw):
                self.budget.take("2026-09-29")  # midnight passed mid-sweep
                return {"itemSummaries": []}

        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "t.db")
            try:
                result = Engine(db, Client()).poll_once("bin", notify=False)
                self.assertIsNone(result.error)
                self.assertEqual(result.http_calls, 1)
                self.assertEqual(db.one("SELECT http_calls FROM poll_runs")[0], 1)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
