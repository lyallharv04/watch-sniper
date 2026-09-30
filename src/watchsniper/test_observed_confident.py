"""Observed, confident only: closings the shadow models identified with high
confidence. Reads stored answers; no model is called. Hermetic."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from . import config as C
from . import web
from .llm import Candidate, LlmInput, LlmResult
from .money import median_pence
from .selftest import listing
from .shadow import confident_key, confident_observed

HAIKU, GEMINI = C.LLM_SHADOW_MODELS
SONNET = C.LLM_ESCALATION_MODEL
PM = "T137.407.11.041.00"
QZ = "T137.410.11.041.00"
TITLE = "Tissot PRX Powermatic 80 40mm"


def answer(model, key, confidence="high"):
    return {"model": model, "catalogue_key": key, "confidence": confidence}


class TestRule(unittest.TestCase):
    M = (HAIKU, GEMINI)

    def test_both_agree_high(self):
        self.assertEqual(confident_key([answer(HAIKU, PM), answer(GEMINI, PM)], self.M), PM)

    def test_the_one_that_answered(self):
        self.assertEqual(confident_key([answer(GEMINI, QZ)], self.M), QZ)
        self.assertIsNone(confident_key([answer(GEMINI, QZ, "medium")], self.M))

    def test_any_doubt_is_no_identification(self):
        self.assertIsNone(confident_key([answer(HAIKU, PM), answer(GEMINI, QZ)], self.M))
        self.assertIsNone(confident_key([answer(HAIKU, PM), answer(GEMINI, PM, "low")], self.M))
        self.assertIsNone(confident_key([answer(HAIKU, None), answer(GEMINI, None)], self.M))
        self.assertIsNone(confident_key([], self.M))

    def test_escalation_has_no_say_and_latest_answer_counts(self):
        self.assertIsNone(confident_key([answer(SONNET, PM)], self.M))
        self.assertEqual(
            confident_key([answer(HAIKU, QZ, "low"), answer(HAIKU, PM), answer(GEMINI, PM)], self.M),
            PM,
        )

    def test_median_rounds_down(self):
        self.assertEqual(median_pence([100, 301]), 200)
        self.assertEqual(median_pence([3, 1, 2]), 2)
        self.assertIsNone(median_pence([]))


class TestFromStorage(unittest.TestCase):
    def setUp(self):
        from .db import Database
        from .poller import Engine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.engine = Engine(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def closing(self, item_id, price, answers, *, title=TITLE, sold=True):
        lst = listing(item_id=item_id, title=title, is_auction=True)
        self.db.upsert_listing(lst)
        self.engine.score(lst)
        self.db.save_closing(item_id, price, 5, sold)
        inp = LlmInput(item_id, title, "", (), "", (), (Candidate(PM, "Tissot", "PRX"),))
        for model, key, confidence in answers:
            self.db.save_llm_result(
                item_id=item_id, stage="closing", inp=inp,
                result=LlmResult(provider="x", model=model, ok=True,
                                 catalogue_key=key, confidence=confidence),
                escalated_from=None, would_verdict=None, would_mab=None, would_reason="",
            )

    def test_median_count_and_excluded(self):
        both = [(HAIKU, PM, "high"), (GEMINI, PM, "high")]
        self.closing("a", 30_000, both)
        self.closing("b", 32_000, both)
        self.closing("c", 34_000, [(GEMINI, PM, "high")])
        self.closing("d", 99_000, [(HAIKU, PM, "high"), (GEMINI, QZ, "high")])  # disagree
        self.closing("e", 99_000, [])                                          # never asked
        self.closing("f", 10_000, both, sold=False)                            # not a sale
        self.closing("g", 15_000, [(HAIKU, QZ, "high"), (GEMINI, QZ, "high")])  # rules PM, really QZ
        out = confident_observed(self.db)
        self.assertEqual(out[PM], (32_000, 3, 3))
        self.assertEqual(out[QZ], (15_000, 1, 0))
        self.assertEqual(self.db.observed_closings()[PM][1], 6, "the rules-based count is unchanged")

    def test_catalogue_page_shows_it(self):
        both = [(HAIKU, PM, "high"), (GEMINI, PM, "high")]
        for i, p in enumerate((30_000, 32_000, 34_000)):
            self.closing(f"s{i}", p, both)
        self.closing("x", 99_000, [])
        html = web.render_catalogue(self.engine)
        self.assertIn("Observed, confident only", html)
        self.assertIn("£320.00", html)
        self.assertIn("3 sold · 1 excluded", html)
        self.assertIn("(3; 1 excl.)", html)


if __name__ == "__main__":
    unittest.main()
