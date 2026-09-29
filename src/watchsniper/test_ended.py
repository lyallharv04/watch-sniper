"""Ended listings: found by getItem, hidden from the feed views, marked in All.

Hermetic. getItem shapes are the ones measured on 2026-09-29: a live Buy It
Now has no itemEndDate; an ended one has it in the past, with
estimatedSoldQuantity saying whether it sold.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from . import config as C
from . import web
from .details import end_state, from_get_item
from .ebay import BudgetExhausted, EbayError
from .models import utcnow
from .selftest import listing

MATCHED = "Tissot PRX Powermatic 80 40mm"
UNMATCHED = "Rolex Submariner 116610LN"


def live_row(item_id: str) -> dict:
    return {
        "itemId": item_id, "buyingOptions": ["FIXED_PRICE"],
        "estimatedAvailabilities": [{"estimatedAvailabilityStatus": "IN_STOCK",
                                     "estimatedSoldQuantity": 0}],
    }


def ended_row(item_id: str, ago: timedelta, sold: int) -> dict:
    row = live_row(item_id)
    row["itemEndDate"] = (utcnow() - ago).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    row["estimatedAvailabilities"] = [{
        "estimatedAvailabilityStatus": "OUT_OF_STOCK" if sold else "IN_STOCK",
        "estimatedSoldQuantity": sold,
    }]
    return row


class TestEndState(unittest.TestCase):
    def test_measured_shapes(self):
        now = utcnow()
        self.assertEqual(end_state(live_row("a"), now), (None, None))
        at, state = end_state(ended_row("a", timedelta(hours=3), 1), now)
        self.assertEqual(state, "sold")
        self.assertLess(at, now)
        self.assertEqual(end_state(ended_row("a", timedelta(hours=3), 0), now)[1], "unsold")

    def test_future_end_is_live_and_sold_out_without_a_date_is_sold(self):
        now = utcnow()
        self.assertEqual(end_state(ended_row("a", -timedelta(days=2), 0), now), (None, None))
        row = live_row("a")
        row["estimatedAvailabilities"] = [{"estimatedAvailabilityStatus": "OUT_OF_STOCK",
                                           "estimatedSoldQuantity": 2}]
        self.assertEqual(end_state(row, now), (None, "sold"))


class _Items:
    """getItem from a dict; a value that is an exception is raised."""

    class budget:
        used = 0
        total = 0
        remaining = 100
        ceiling = 100

    def __init__(self, items: dict):
        self.items = items
        self.fetched: list[str] = []

    def get_item(self, item_id: str, *, day: str) -> dict:
        self.fetched.append(item_id)
        value = self.items[item_id]
        if isinstance(value, Exception):
            raise value
        return value


class EndedCase(unittest.TestCase):
    def setUp(self):
        from .db import Database
        from .poller import Engine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.engine = Engine(self.db)
        self.sweep = utcnow()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def add(self, item_id, *, title=MATCHED, seen_ago=timedelta(hours=1), **kw):
        item = listing(item_id=item_id, title=title, fetched_at=self.sweep - seen_ago, **kw)
        self.db.upsert_listing(item)
        self.engine.score(item)
        return item

    def state(self, item_id):
        return self.db.one(
            "SELECT ended_at_utc, end_state, end_checked_at_utc FROM listings WHERE item_id=?",
            (item_id,),
        )


class TestCheck(EndedCase):
    def test_what_is_checked(self):
        self.add("in_sweep", seen_ago=-timedelta(seconds=1))
        self.add("dropped")
        self.add("unmatched", title=UNMATCHED)
        self.add("fresh")
        self.db.mark_checked_live("fresh", utcnow() - timedelta(hours=1))
        self.add("stale")
        self.db.mark_checked_live("stale", utcnow() - C.ENDED_RECHECK - timedelta(hours=1))
        self.add("done")
        self.db.mark_ended("done", None, "sold", utcnow())
        self.add("auction", is_auction=True, end_time_utc=utcnow() + timedelta(days=1))
        items = _Items({"dropped": live_row("dropped"), "stale": live_row("stale")})
        self.engine.client = items
        self.assertEqual(self.engine.check_ended(self.sweep), 2)
        self.assertEqual(items.fetched, ["dropped", "stale"], "never-checked first")

    def test_outcomes_are_stored(self):
        for i in ("live", "sold", "unsold", "gone", "flaky"):
            self.add(i)
        self.engine.client = _Items({
            "live": live_row("live"),
            "sold": ended_row("sold", timedelta(days=1), 1),
            "unsold": ended_row("unsold", timedelta(hours=2), 0),
            "gone": EbayError("not found", 404),
            "flaky": EbayError("server", 500),
        })
        self.engine.check_ended(self.sweep)
        self.assertIsNone(self.state("live")["ended_at_utc"])
        self.assertIsNotNone(self.state("live")["end_checked_at_utc"])
        self.assertEqual(self.state("sold")["end_state"], "sold")
        self.assertEqual(self.state("unsold")["end_state"], "unsold")
        self.assertEqual(self.state("gone")["end_state"], "gone")
        self.assertIsNone(self.state("flaky")["end_checked_at_utc"], "retried next sweep")
        self.assertIsNotNone(self.db.item_details("sold"), "details refreshed")

    def test_a_sweep_limit(self):
        for i in range(C.ENDED_CHECKS_PER_SWEEP + 5):
            self.add(f"x{i}")
        items = _Items({f"x{i}": live_row(f"x{i}") for i in range(C.ENDED_CHECKS_PER_SWEEP + 5)})
        self.engine.client = items
        self.engine.check_ended(self.sweep)
        self.assertEqual(len(items.fetched), C.ENDED_CHECKS_PER_SWEEP)

    def test_budget_exhausted_propagates(self):
        self.add("a")
        self.engine.client = _Items({"a": BudgetExhausted("out")})
        with self.assertRaises(BudgetExhausted):
            self.engine.check_ended(self.sweep)

    def test_seen_again_in_a_sweep_is_live_again(self):
        item = self.add("back")
        self.db.mark_ended("back", None, "gone", utcnow())
        self.db.upsert_listing(item)
        self.assertIsNone(self.state("back")["ended_at_utc"])

    def test_the_bin_sweep_runs_the_check(self):
        self.add("dropped")
        items = _Items({"dropped": ended_row("dropped", timedelta(hours=5), 1)})
        items.search = lambda **_kw: {"itemSummaries": []}
        self.engine.client = items
        result = self.engine.poll_once("bin", notify=False)
        self.assertIsNone(result.error)
        self.assertEqual(self.state("dropped")["end_state"], "sold")


class TestSeedAndClosing(EndedCase):
    def test_seeded_from_stored_details_without_a_call(self):
        from .poller import Engine

        self.add("was_sold")
        self.add("still_live")
        self.add("auc", is_auction=True, end_time_utc=utcnow() - timedelta(hours=1))
        self.db.save_item_details(from_get_item(ended_row("was_sold", timedelta(days=1), 1)))
        self.db.save_item_details(from_get_item(live_row("still_live")))
        self.db.save_item_details(from_get_item(ended_row("auc", timedelta(hours=1), 0)))
        Engine(self.db)  # start-up
        self.assertEqual(self.state("was_sold")["end_state"], "sold")
        self.assertIsNone(self.state("still_live")["ended_at_utc"])
        self.assertIsNone(self.state("auc")["ended_at_utc"], "auctions wait for the closing check")

    def test_closing_marks_the_auction_ended_with_its_sale(self):
        end = utcnow() - C.CLOSING_CHECK_DELAY - timedelta(minutes=1)
        self.add("auc", is_auction=True, end_time_utc=end)
        row = ended_row("auc", C.CLOSING_CHECK_DELAY + timedelta(minutes=1), 1)
        row.update(buyingOptions=["AUCTION"], currentBidPrice={"value": "200.00", "currency": "GBP"}, bidCount=5)
        self.engine.client = _Items({"auc": row})
        self.engine.record_closings()
        self.assertEqual(self.state("auc")["end_state"], "sold")


class TestFeed(EndedCase):
    def setUp(self):
        super().setUp()
        self.add("live")
        self.add("ended")
        self.db.mark_ended("ended", utcnow() - timedelta(days=1), "sold", utcnow())
        self.add("unmatched", title=UNMATCHED)
        self.add("auc_live", is_auction=True, end_time_utc=utcnow() + timedelta(hours=1))
        self.add("auc_early", is_auction=True, end_time_utc=utcnow() + timedelta(hours=1))
        self.db.mark_ended("auc_early", utcnow(), "gone", utcnow())

    def ids(self, **kw):
        return {r["item_id"] for r in self.db.feed(ending_within=C.AUCTION_ENDING_SOON, **kw)}

    def test_views_hide_ended_and_all_marks_it(self):
        self.assertEqual(self.ids(), {"live"})
        self.assertNotIn("ended", self.ids(verdict="DEAL") | self.ids(verdict="REJECT_PRICE"))
        self.assertEqual(self.ids(view="auctions"), {"auc_live"})
        everything = {r["item_id"]: r for r in self.db.feed(verdict="all")}
        self.assertEqual(set(everything), {"live", "ended", "unmatched", "auc_live", "auc_early"})
        self.assertEqual(everything["ended"]["is_ended"], 1)
        self.assertEqual(everything["live"]["is_ended"], 0)
        self.assertEqual(everything["unmatched"]["end_not_checked"], 1)
        html = web.render_feed(self.engine, {"verdict": ["all"]})
        self.assertIn("ENDED", html)
        self.assertIn("sold", html)
        self.assertIn("end not checked", html)

    def test_item_page_marks_it(self):
        self.assertIn("ENDED", web.render_item(self.engine, "ended"))
        self.assertNotIn("ENDED", web.render_item(self.engine, "live"))

    def test_an_auction_past_its_end_is_ended_before_its_closing(self):
        self.add("auc_past", is_auction=True, end_time_utc=utcnow() - timedelta(minutes=1))
        row = self.db.item("auc_past")
        self.assertEqual(row["is_ended"], 1)
        self.assertIsNone(json.loads(json.dumps(row["end_state"])))


if __name__ == "__main__":
    unittest.main()
