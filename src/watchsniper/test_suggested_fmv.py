"""Suggested FMV: the confident-only median, once there are enough sales.
Display only; nothing reads it back and no model is called. Hermetic."""

from __future__ import annotations

import unittest

from . import config as C
from . import web
from .catalogue import Catalogue
from .shadow import suggested_fmvs
from .test_observed_confident import GEMINI, HAIKU, PM, StorageCase


class TestRule(unittest.TestCase):
    def setUp(self):
        self.cat = Catalogue.load()
        self.fmv = self.cat.by_key[PM].point

    def test_needs_the_minimum_count(self):
        need = C.OBSERVED_MIN_AUCTIONS
        self.assertNotIn(PM, suggested_fmvs(self.cat, {PM: (self.fmv, need - 1, 0)}))
        self.assertEqual(suggested_fmvs(self.cat, {PM: (self.fmv, need, 0)})[PM], (self.fmv, need, 0))

    def test_signed_difference_in_basis_points_of_fmv(self):
        n = C.OBSERVED_MIN_AUCTIONS
        up = self.fmv + self.fmv // 10
        down = self.fmv - self.fmv // 4
        self.assertEqual(suggested_fmvs(self.cat, {PM: (up, n, 0)})[PM][2],
                         (up - self.fmv) * 10_000 // self.fmv)
        self.assertEqual(suggested_fmvs(self.cat, {PM: (down, n, 0)})[PM][2],
                         -((self.fmv - down) * 10_000 // self.fmv))

    def test_truncates_towards_zero(self):
        # One penny under: a floor would say -0.01%, this says 0.
        self.assertEqual(suggested_fmvs(self.cat, {PM: (self.fmv - 1, C.OBSERVED_MIN_AUCTIONS, 0)})[PM][2], 0)


class TestOnThePage(StorageCase):
    def test_shown_with_count_and_difference(self):
        both = [(HAIKU, PM, "high"), (GEMINI, PM, "high")]
        fmv = self.engine.catalogue.by_key[PM].point
        prices = [fmv - fmv // 5] * C.OBSERVED_MIN_AUCTIONS
        for i, p in enumerate(prices):
            self.closing(f"s{i}", p, both)
        html = web.render_catalogue(self.engine)
        self.assertIn("Suggested FMV", html)
        self.assertIn(f"{C.OBSERVED_MIN_AUCTIONS} sales · -20.0% vs FMV", html)

    def test_dash_until_enough(self):
        html = web.render_catalogue(self.engine)
        self.assertIn(f"needs {C.OBSERVED_MIN_AUCTIONS} confident sales", html)

    def test_catalogue_file_untouched(self):
        before = C.CATALOGUE_PATH.read_bytes()
        web.render_catalogue(self.engine)
        self.assertEqual(C.CATALOGUE_PATH.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
