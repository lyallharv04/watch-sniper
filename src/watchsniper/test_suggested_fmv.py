"""Suggested FMV: the median FMV that confident sales imply, grade by grade.

Each sale implies FMV = price / COND_MULT[grade], the grade being the lower of
eBay's stated grade and the models' reading. Display only; no model is
called. Hermetic; expected values are derived from the live constants.
"""

from __future__ import annotations

import unittest

from . import config as C
from . import web
from .catalogue import Catalogue
from .money import div_bp, median_pence
from .shadow import ConfidentClosing, confident_answer, confident_closings, suggested_fmvs
from .test_observed_confident import GEMINI, HAIKU, PM, StorageCase
from .valuation import lower_grade

N = C.OBSERVED_MIN_AUCTIONS


def cc(price, grade="GOOD", assumed=False, key=PM):
    return ConfidentClosing(key, price, grade, assumed)


class TestArithmetic(unittest.TestCase):
    def test_div_bp_rounds_down(self):
        self.assertEqual(div_bp(8_400, 8_400), 10_000)
        self.assertEqual(div_bp(10_000, 3_000), 33_333)  # 33333.3 down

    def test_lower_grade_is_the_valuation_rule(self):
        self.assertEqual(lower_grade("FAIR", "MINT"), "FAIR")
        self.assertEqual(lower_grade("EXCELLENT", "GOOD"), "GOOD")
        self.assertEqual(lower_grade(None, "EXCELLENT"), "EXCELLENT")
        self.assertIsNone(lower_grade(None, None))

    def test_the_counting_answers_lower_reading(self):
        answers = [
            {"model": HAIKU, "catalogue_key": PM, "confidence": "high", "condition": "MINT"},
            {"model": GEMINI, "catalogue_key": PM, "confidence": "high", "condition": "GOOD"},
        ]
        self.assertEqual(confident_answer(answers, (HAIKU, GEMINI)), (PM, "GOOD"))


class TestSuggestion(unittest.TestCase):
    def setUp(self):
        self.cat = Catalogue.load()
        self.fmv = self.cat.by_key[PM].point

    def test_median_of_implied_fmvs(self):
        grades = ("GOOD", "EXCELLENT", "FAIR")
        closings = [cc(30_000 + 1_000 * i, grades[i % 3]) for i in range(max(N, 3))]
        want = median_pence([div_bp(c.price, C.COND_MULT[c.grade]) for c in closings])
        s = suggested_fmvs(self.cat, closings)[PM]
        self.assertEqual((s.value, s.n), (want, len(closings)))

    def test_signed_difference_truncated_towards_zero(self):
        mult = C.COND_MULT["MINT"]
        at = [cc(self.fmv - 1, "MINT")] * N  # one penny under
        self.assertEqual(suggested_fmvs(self.cat, at)[PM].diff_bp, 0)
        up = self.fmv + self.fmv // 10
        self.assertEqual(
            suggested_fmvs(self.cat, [cc(up, "MINT")] * N)[PM].diff_bp,
            (div_bp(up, mult) - self.fmv) * 10_000 // self.fmv,
        )

    def test_below_the_minimum_no_value_but_the_spread(self):
        s = suggested_fmvs(self.cat, [cc(30_000)] * (N - 1))[PM]
        self.assertIsNone(s.value)
        self.assertIsNone(s.diff_bp)
        self.assertEqual(s.by_grade[0].n, N - 1)

    def test_spread_by_grade_best_first_with_assumed_counts(self):
        closings = [cc(30_000, "GOOD", True), cc(32_000, "GOOD"), cc(33_000, "EXCELLENT")]
        s = suggested_fmvs(self.cat, closings)[PM]
        self.assertEqual([(g.grade, g.n, g.assumed) for g in s.by_grade],
                         [("EXCELLENT", 1, 0), ("GOOD", 2, 1)])
        self.assertEqual(s.by_grade[1].median, median_pence(
            [div_bp(30_000, C.COND_MULT["GOOD"]), div_bp(32_000, C.COND_MULT["GOOD"])]))

    def test_for_parts_implies_nothing(self):
        self.assertNotIn(PM, suggested_fmvs(self.cat, [cc(5_000, "FOR_PARTS")] * N))


class TestFromStorage(StorageCase):
    def test_grade_is_the_lower_of_stated_and_model(self):
        both = lambda cond: [(HAIKU, PM, "high", cond), (GEMINI, PM, "high", cond)]  # noqa: E731
        self.closing("fair", 20_000, both("MINT"), condition_raw="Pre-owned - Fair")
        self.closing("good", 30_000, both("GOOD"), condition_raw="Pre-owned - Excellent")
        self.closing("unk", 31_000, both(None))
        grades = {c.price: (c.grade, c.assumed) for c in confident_closings(self.db)[0]}
        self.assertEqual(grades, {20_000: ("FAIR", False), 30_000: ("GOOD", False),
                                  31_000: ("GOOD", True)})

    def test_card_shows_suggestion_difference_and_spread(self):
        fmv = self.engine.catalogue.by_key[PM].point
        price = fmv * C.COND_MULT["GOOD"] // 10_000 * 4 // 5  # implies 80% of FMV at GOOD
        for i in range(N):
            self.closing(f"s{i}", price, [(HAIKU, PM, "high", "GOOD"), (GEMINI, PM, "high", "GOOD")])
        s = suggested_fmvs(self.engine.catalogue, confident_closings(self.db)[0])[PM]
        html = web.render_catalogue(self.engine)
        self.assertIn(f"{N} sales · {web.pct(s.diff_bp)} vs FMV", html)
        self.assertIn(f"good {web.fmt(s.value)} ({N})", html)
        self.assertIn("Implied FMV by grade", html)

    def test_dash_until_enough(self):
        self.assertIn(f"needs {N} confident sales", web.render_catalogue(self.engine))

    def test_catalogue_file_untouched(self):
        before = C.CATALOGUE_PATH.read_bytes()
        web.render_catalogue(self.engine)
        self.assertEqual(C.CATALOGUE_PATH.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
