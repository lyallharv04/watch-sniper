"""The six DEALs on the live database on 2026-09-29, as regression cases.

Five were catalogue mismatches — both shadow models rejected every one — and
the sixth is the data gap the catalogue is meant to show. Titles verbatim.
"""

from __future__ import annotations

import unittest

from .catalogue import Catalogue
from .selftest import listing, valuer

CASES = [
    # (title, the reference it must now match, or None for unpriced)
    ("Tissot PRX Swiss Made Digital Watch Gold PVD Stainless 35mm  T1372633302000",
     "PRX-DIGITAL"),  # was priced as the 35mm Powermatic
    ("Tissot PRX Chronograph p475", None),  # a 1980s-90s PRX, not the modern one
    ("TISSOT PRX P480A VINTAGE DATE @3 AGED GOLD BEZEL White 35MM TWO TONE uk", None),
    ("Tissot PRX Men's Watch 40mm", "PRX-UNSPECIFIED"),  # the honest data gap
    ("Vintage *Near MINT* Seiko Alpinist S822-00A0 Green Solar Digital Men Watch JAPAN",
     None),  # a vintage solar digital, not the SPB line
    ("Seiko Alpinist - SPB339J1 ", None),  # an SPB reference the entry does not cover
]

STILL_MATCH = [
    ("Seiko Prospex Alpinist SPB121J1 green dial", "SPB121"),
    ("Prospex Alpinist SPB121", "SPB121"),
    ("Seiko Alpinist SPB117 boxed", "SPB121"),
    ("Seiko Alpinist automatic green dial", "SPB121"),
    ("Tissot PRX Powermatic 80 40mm blue", "T137.407.11.041.00"),
    ("Tissot PRX Quartz 35mm", "T137.210.11.041.00"),
    ("Tissot PRX Chronograph 42mm", "PRX-CHRONO"),
    ("Tissot PRX Digital 40mm", "PRX-DIGITAL"),
]


class TestLiveDealTitles(unittest.TestCase):
    def setUp(self):
        self.cat = Catalogue.load()

    def check(self, cases):
        for title, want in cases:
            m = self.cat.match(title)
            with self.subTest(title=title):
                self.assertEqual(m.reference.key if m else None, want)

    def test_the_six(self):
        self.check(CASES)

    def test_the_real_ones_still_match(self):
        self.check(STILL_MATCH)

    def test_none_of_the_mismatches_is_a_deal_any_more(self):
        v = valuer()
        for title, want in CASES:
            a = v.assess(listing(title=title, price=10_000))
            with self.subTest(title=title):
                if want is None:
                    self.assertEqual(a.verdict, "REJECT_CATALOGUE")
                else:
                    self.assertEqual(a.catalogue_key, want)


if __name__ == "__main__":
    unittest.main()
