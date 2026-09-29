"""Which listings the verification models would see (LLM_CONTRACT.md §5).

Hermetic. Prices are derived from the live constants rather than written in,
so the tests say what the rule is and survive a fee correction.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from . import config as C
from .selftest import listing, valuer

QUARTZ = "T137.410.11.041.00"
POWERMATIC = "T137.407.11.041.00"


def ceiling_for(v, lst) -> int:
    refs = v.catalogue.candidates(lst.title, C.LLM_CANDIDATES)
    return max(v.max_bid_for(lst, r, None).amount for r in refs)


def at_margin(ceiling: int) -> int:
    """The highest price the margin admits."""
    return ceiling * (10_000 + C.CANDIDATE_MARGIN_BP) // 10_000


class TestCandidateSelection(unittest.TestCase):
    def setUp(self):
        self.v = valuer()

    def assess(self, **kw):
        return self.v.assess(listing(**kw))

    def test_quartz_title_reaches_the_models_on_the_powermatic_ceiling(self):
        # The rules price it as a Quartz and reject it; a sibling would clear.
        title = "Tissot PRX Quartz 40mm blue dial"
        probe = listing(title=title)
        quartz = self.v.max_bid_for(probe, self.v.catalogue.by_key[QUARTZ], None).amount
        ceiling = ceiling_for(self.v, probe)
        self.assertGreater(ceiling, quartz)
        a = self.assess(title=title, price=quartz + 100)
        self.assertEqual(a.verdict, "REJECT_PRICE")
        self.assertEqual(a.candidate_keys[0], QUARTZ)
        self.assertIn(POWERMATIC, a.candidate_keys)
        self.assertTrue(a.llm_candidate)

    def test_margin_is_the_boundary(self):
        probe = listing()
        top = at_margin(ceiling_for(self.v, probe))
        self.assertTrue(self.assess(price=top).llm_candidate)
        self.assertFalse(self.assess(price=top + 1).llm_candidate)

    def test_rules_verdict_is_never_changed(self):
        probe = listing()
        top = at_margin(ceiling_for(self.v, probe))
        a = self.assess(price=top)
        self.assertTrue(a.llm_candidate)
        self.assertNotEqual(a.verdict, "DEAL")

    def test_seller_below_the_floor_is_not_sent(self):
        a = self.assess(seller_feedback_pct_x100=9000, price=10_000)
        self.assertFalse(a.llm_candidate)

    def test_hard_blacklist_hit_is_not_sent(self):
        a = self.assess(title="Tissot PRX Powermatic 80 40mm spares or repairs", price=10_000)
        self.assertEqual(a.verdict, "REJECT_BLACKLIST")
        self.assertFalse(a.llm_candidate)

    def test_flag_hit_is_sent_and_still_rejects(self):
        a = self.assess(title="Tissot PRX Powermatic 80 40mm custom dial", price=10_000)
        self.assertEqual(a.verdict, "REJECT_BLACKLIST")
        self.assertTrue(a.llm_candidate)

    def test_no_candidates_is_not_sent(self):
        a = self.assess(title="Omega Seamaster 300", price=10_000)
        self.assertEqual(a.candidate_keys, [])
        self.assertFalse(a.llm_candidate)

    def test_no_usable_price_is_not_sent(self):
        self.assertFalse(self.assess(price=None).llm_candidate)
        self.assertFalse(self.assess(currency="", price=10_000).llm_candidate)


class TestStored(unittest.TestCase):
    def test_saved_and_rebuilt_from_an_older_verdicts_table(self):
        from .db import Database
        from .poller import Engine

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "t.db"
            db = Database(path)
            engine = Engine(db)
            db.upsert_listing(listing(price=10_000))
            engine.score(listing(price=10_000))
            self.assertEqual(db.one("SELECT llm_candidate FROM verdicts")[0], 1)
            db.close()

            # A file written before the column existed is re-scored on open.
            con = sqlite3.connect(path)
            con.execute("DROP TABLE verdicts")
            con.execute("CREATE TABLE verdicts (item_id TEXT PRIMARY KEY, below_fmv_bp INTEGER)")
            con.commit()
            con.close()
            db = Database(path)
            self.assertTrue(db.verdicts_dropped)
            Engine(db)
            self.assertEqual(db.one("SELECT llm_candidate FROM verdicts")[0], 1)
            db.close()


if __name__ == "__main__":
    unittest.main()
