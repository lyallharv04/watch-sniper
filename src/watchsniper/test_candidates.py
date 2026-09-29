"""Catalogue.candidates — the references offered to the model (LLM_CONTRACT §4).

Hermetic: runs against the committed data/catalogue.toml and nothing else.
"""

from __future__ import annotations

import unittest

from .catalogue import Catalogue

PRX_AUTO_40 = "T137.407.11.041.00"
PRX_QUARTZ_40 = "T137.410.11.041.00"


class TestCandidates(unittest.TestCase):
    def setUp(self):
        self.cat = Catalogue.load()

    def keys(self, title: str, n: int = 3) -> list[str]:
        return [r.key for r in self.cat.candidates(title, n)]

    def test_prx_stating_neither_movement_offers_both(self):
        keys = self.keys("Tissot PRX 40mm blue dial integrated bracelet")
        self.assertIn(PRX_AUTO_40, keys)
        self.assertIn(PRX_QUARTZ_40, keys)

    def test_prx_stating_quartz_offers_quartz_first_and_still_the_powermatic(self):
        keys = self.keys("Tissot PRX Quartz 40mm")
        self.assertEqual(keys[0], PRX_QUARTZ_40)
        self.assertIn(PRX_AUTO_40, keys)

    def test_prx_stating_powermatic_still_offers_the_quartz(self):
        keys = self.keys("Tissot PRX Powermatic 80 40mm ice blue")
        self.assertEqual(keys[0], PRX_AUTO_40)
        self.assertIn(PRX_QUARTZ_40, keys)

    def test_rules_match_is_always_first(self):
        for title in (
            "Tissot PRX Quartz 40mm blue dial",
            "Tissot PRX 40mm steel integrated bracelet",
            "Christopher Ward C60 Trident Mk3 Pro 600 42mm black/red",
            "Seiko Prospex Alpinist SPB121",
            "Hamilton Khaki Field Mechanical H69439931",
            "Farer Lander GMT",
        ):
            m = self.cat.match(title)
            self.assertIsNotNone(m, title)
            self.assertEqual(self.keys(title)[0], m.reference.key, title)

    def test_no_brand_gives_nothing(self):
        self.assertEqual(self.keys("Rolex Submariner 40mm automatic"), [])
        self.assertEqual(self.keys(""), [])

    def test_brand_only_title_gives_nothing(self):
        self.assertEqual(self.keys("Tissot"), [])
        self.assertEqual(self.keys("Hamilton mens watch"), [])

    def test_generic_words_alone_make_no_candidate(self):
        self.assertEqual(self.keys("Hamilton 40mm automatic mens watch"), [])

    def test_n_is_respected(self):
        title = "Tissot PRX 40mm"
        self.assertEqual(len(self.keys(title, 1)), 1)
        self.assertEqual(len(self.keys(title, 2)), 2)
        self.assertLessEqual(len(self.keys(title, 3)), 3)
        self.assertEqual(self.keys(title, 0), [])
        self.assertGreater(len(self.keys(title, 10)), 3)

    def test_no_duplicates(self):
        for title in ("Tissot PRX 40mm", "Tissot PRX Quartz 40mm", "Hamilton Khaki Field"):
            keys = self.keys(title, 50)
            self.assertEqual(len(keys), len(set(keys)), title)

    def test_only_the_titles_brand_is_offered(self):
        refs = self.cat.candidates("Baltic Aquascaphe GMT", 50)
        self.assertTrue(refs)
        self.assertEqual({r.brand for r in refs}, {"Baltic"})

    def test_deterministic(self):
        title = "Tissot PRX 40mm blue dial"
        self.assertEqual(self.keys(title, 50), self.keys(title, 50))


if __name__ == "__main__":
    unittest.main()
