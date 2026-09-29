"""DEAL auctions appear on the auction view whatever their end time."""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from . import config as C
from . import web
from .models import utcnow
from .selftest import listing


class TestDealSection(unittest.TestCase):
    def setUp(self):
        from .db import Database
        from .poller import Engine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.engine = Engine(self.db)
        now = utcnow()
        soon = now + C.AUCTION_ENDING_SOON / 2
        later = now + timedelta(days=3)
        self.add("deal_later", end=later, price=10_000)
        self.add("deal_soon", end=soon, price=10_000)
        self.add("dear_soon", end=soon, price=100_000)
        self.add("dear_later", end=later, price=100_000)
        self.add("deal_ended", end=now - timedelta(minutes=5), price=10_000)
        self.assertEqual(self.verdict("deal_later"), "DEAL")
        self.assertNotEqual(self.verdict("dear_soon"), "DEAL")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, item_id, *, end, price):
        item = listing(item_id=item_id, is_auction=True, end_time_utc=end, price=price)
        self.db.upsert_listing(item)
        self.engine.score(item)

    def verdict(self, item_id):
        return self.db.one("SELECT verdict FROM verdicts WHERE item_id=?", (item_id,))[0]

    def page(self, **params):
        return web.render_feed(self.engine, {"view": ["auctions"], **{k: [v] for k, v in params.items()}})

    def test_deals_any_end_time_above_the_ending_soon_list(self):
        html = self.page()
        self.assertIn("2 DEAL auctions", html)
        for shown in ("deal_later", "deal_soon", "dear_soon"):
            self.assertIn(f'/item/{shown}"', html)
        self.assertNotIn("/item/dear_later", html)
        self.assertNotIn("/item/deal_ended", html)
        # Deals come first and are not repeated in the ending-soon list.
        self.assertLess(html.index("/item/deal_later"), html.index("/item/dear_soon"))
        self.assertEqual(html.count('/item/deal_soon"'), 1)

    def test_query_helpers(self):
        deals = {r["item_id"] for r in self.db.feed(view="auctions", verdict="DEAL", ending_within=None)}
        self.assertEqual(deals, {"deal_later", "deal_soon"})
        rest = {r["item_id"] for r in self.db.feed(
            view="auctions", ending_within=C.AUCTION_ENDING_SOON, exclude_verdict="DEAL")}
        self.assertEqual(rest, {"dear_soon"})

    def test_another_verdict_filter_has_no_deal_section(self):
        html = self.page(verdict="REJECT_PRICE")
        self.assertNotIn("DEAL auction", html)
        self.assertNotIn("deal_later", html)

    def test_buy_it_now_view_is_unchanged(self):
        self.assertNotIn("DEAL auction", web.render_feed(self.engine, {}))


if __name__ == "__main__":
    unittest.main()
