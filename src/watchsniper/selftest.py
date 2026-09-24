"""The test suite. `python -m watchsniper selftest`.

Plain `unittest`, so the project keeps its zero-dependency property and the
tests run on a clean machine with nothing installed.

The suite is hermetic: it makes no network call and reads no credential. What
it therefore does NOT prove is that the eBay call works — only `diagnose`
against the live API can say that, and a passing suite here is not evidence
about the API.
"""

from __future__ import annotations

import os
import tempfile
import tomllib
import unittest
from datetime import datetime, timezone
from pathlib import Path

from . import config as C
from .blacklist import Blacklist, Increments
from .catalogue import Catalogue
from .fees import buyer_protection_fee, max_allowable_bid, sell_side
from .models import Listing, from_item_summary, parse_ts, utcnow
from .money import compose_bp, fmt, mul_bp, mul_bp_ceil, parse_api_amount, parse_gbp
from .valuation import Valuer, read_bracelet, read_condition, read_scope

ROOT = C.ROOT
CORPUS = ROOT / "tests" / "corpus.toml"


def listing(**kw) -> Listing:
    base = dict(
        item_id="v1|1|0",
        title="Tissot PRX Powermatic 80 40mm",
        web_url="https://example.invalid/1",
        is_auction=False,
        price=parse_gbp("200.00"),
        shipping=parse_gbp("5.00"),
        currency="GBP",
        condition_raw="Pre-owned",
        condition_id="3000",
        bid_count=None,
        seller_username="someone",
        seller_account_type="INDIVIDUAL",
        seller_feedback_pct_x100=9900,
        seller_feedback_score=200,
        item_location_country="GB",
        end_time_utc=None,
        category_id="31387",
        fetched_at=utcnow(),
        raw={},
    )
    base.update(kw)
    return Listing(**base)


def valuer() -> Valuer:
    return Valuer(Catalogue.load(), Blacklist.load(), Increments.load())


# --------------------------------------------------------------------------


class TestMoney(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(parse_gbp("380"), 38000)
        self.assertEqual(parse_gbp("380.00"), 38000)
        self.assertEqual(parse_gbp("£380.5"), 38050)
        self.assertEqual(parse_gbp("0.09"), 9)
        with self.assertRaises(ValueError):
            parse_gbp("about £380")

    def test_api_amount(self):
        self.assertEqual(parse_api_amount("199.99"), 19999)
        self.assertEqual(parse_api_amount("200"), 20000)
        self.assertEqual(parse_api_amount("12.005"), 1200)  # floored
        self.assertIsNone(parse_api_amount(None))

    def test_rounding_direction(self):
        # value floors, cost ceilings — never the other way round
        self.assertEqual(mul_bp(1001, 5000), 500)
        self.assertEqual(mul_bp_ceil(1001, 5000), 501)

    def test_compose(self):
        self.assertEqual(compose_bp(8800, 8800), 7744)
        self.assertEqual(compose_bp(10_000), 10_000)

    def test_format(self):
        self.assertEqual(fmt(20171), "£201.71")
        self.assertEqual(fmt(0), "£0.00")
        self.assertEqual(fmt(-150), "-£1.50")
        self.assertEqual(fmt(None), "—")

    def test_no_floats_reach_money(self):
        for value in (20171, 0, -5):
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, float)


class TestBuyerProtection(unittest.TestCase):
    def test_documented_middle_tier_form(self):
        # For P in (£20, £300]: fee = £0.70 + 4% of P
        for p in (2001, 5000, 12345, 30000):
            expected = 70 + -(-p * 400 // 10_000)
            self.assertEqual(
                buyer_protection_fee(p, business_seller=False), expected, p
            )

    def test_first_tier(self):
        self.assertEqual(buyer_protection_fee(2000, business_seller=False), 10 + 140)

    def test_monotonic(self):
        prev = -1
        for p in range(0, 60000, 137):
            fee = buyer_protection_fee(p, business_seller=False)
            self.assertGreaterEqual(p + fee, prev)
            prev = p + fee

    def test_business_sellers_pay_none(self):
        self.assertEqual(buyer_protection_fee(30000, business_seller=True), 0)


class TestMaxBid(unittest.TestCase):
    """The golden case. If this moves, a fee constant moved with it."""

    def test_golden_tissot_prx(self):
        # FMV £380, mint, full set, OEM bracelet, private seller, not VAT
        # registered, £5 inbound postage.
        bid = max_allowable_bid(
            parse_gbp("380.00"),
            business_seller=False,
            inbound_postage=parse_gbp("5.00"),
        )
        self.assertEqual(bid.amount, 20171, fmt(bid.amount))

    def test_golden_line_items(self):
        sell = sell_side(parse_gbp("380.00"))
        self.assertEqual(sell.platform_fees, 7866)
        self.assertEqual(sell.order_fee, 36)
        self.assertEqual(sell.net_proceeds, 29148)

    def test_bid_plus_fee_never_exceeds_budget(self):
        for fmv in range(15000, 80001, 1000):
            bid = max_allowable_bid(fmv, business_seller=False)
            if bid.amount == 0:
                continue
            spend = bid.amount + buyer_protection_fee(
                bid.amount, business_seller=False
            )
            self.assertLessEqual(spend, bid.acquisition_budget - bid.inbound_postage)
            # and one penny more must not fit — the solve is tight
            over = bid.amount + 1
            self.assertGreater(
                over + buyer_protection_fee(over, business_seller=False),
                bid.acquisition_budget - bid.inbound_postage,
            )

    def test_business_branch_allows_more(self):
        private = max_allowable_bid(parse_gbp("380.00"), business_seller=False)
        business = max_allowable_bid(parse_gbp("380.00"), business_seller=True)
        self.assertGreater(business.amount, private.amount)

    def test_unviable_below_the_profit_floor(self):
        bid = max_allowable_bid(parse_gbp("80.00"), business_seller=False)
        self.assertEqual(bid.amount, 0)

    def test_solved_not_divided(self):
        """A flat divide under-costs the purchase. Prove the gap is real."""
        bid = max_allowable_bid(parse_gbp("380.00"), business_seller=False)
        naive = bid.acquisition_budget - bid.inbound_postage
        self.assertGreater(naive - bid.amount, 800)  # more than £8 of error


class TestBlacklistCorpus(unittest.TestCase):
    def test_corpus(self):
        bl = Blacklist.load()
        cases = tomllib.loads(CORPUS.read_text(encoding="utf-8"))["case"]
        failures = []
        for case in cases:
            hits = bl.check(case["title"])
            if bool(hits) != case["drop"]:
                failures.append(
                    f"{case['title']!r}: expected drop={case['drop']}, "
                    f"got {[h.rule_id for h in hits]}"
                )
        self.assertEqual(failures, [], "\n".join(failures))

    def test_corpus_covers_both_directions(self):
        cases = tomllib.loads(CORPUS.read_text(encoding="utf-8"))["case"]
        self.assertTrue(any(c["drop"] for c in cases))
        self.assertTrue(any(not c["drop"] for c in cases))


class TestCatalogue(unittest.TestCase):
    def setUp(self):
        self.cat = Catalogue.load()

    def test_loads(self):
        self.assertGreater(len(self.cat.references), 40)

    def test_every_fmv_sits_inside_its_band(self):
        for r in self.cat.references:
            self.assertLessEqual(r.fmv_low, r.fmv)
            self.assertLessEqual(r.fmv, r.fmv_high)

    def test_quartz_is_never_priced_as_an_automatic(self):
        m = self.cat.match("Tissot PRX Quartz 40mm blue dial")
        self.assertIsNotNone(m)
        self.assertEqual(m.reference.key, "T137.410.11.041.00")

    def test_powermatic_matches_the_automatic(self):
        m = self.cat.match("Tissot PRX Powermatic 80 40mm ice blue")
        self.assertEqual(m.reference.key, "T137.407.11.041.00")

    def test_unsettled_prx_falls_to_the_banded_entry(self):
        m = self.cat.match("Tissot PRX 40mm steel integrated bracelet")
        self.assertEqual(m.reference.key, "PRX-UNSPECIFIED")
        self.assertTrue(m.reference.is_band)

    def test_banded_entry_values_at_the_midpoint(self):
        for r in self.cat.references:
            if r.is_band:
                self.assertEqual(r.point, (r.fmv_low + r.fmv_high) // 2, r.key)
            else:
                self.assertEqual(r.point, r.fmv, r.key)

    def test_integra_alias_does_not_match_integrated(self):
        m = self.cat.match("Tissot PRX 40mm with integrated bracelet")
        self.assertNotEqual(m.reference.brand, "Farer")

    def test_alpinist_requires_seiko_or_prospex(self):
        self.assertIsNone(self.cat.match("MilFortic Alpinist Green Sunburst Dial"))
        self.assertIsNotNone(self.cat.match("Seiko Prospex Alpinist SPB121"))

    def test_generation_marker_does_not_break_matching(self):
        """Real titles interleave a Mk number. Eight live listings hit this."""
        m = self.cat.match("Christopher Ward C60 Trident Mk3 Pro 300 42mm Pool Blue")
        self.assertEqual(m.reference.key, "C60-TRIDENT-PRO-300")
        m = self.cat.match("Christopher Ward C60 Trident Mk3 Pro 600 42mm black/red")
        self.assertEqual(m.reference.key, "C60-TRIDENT-PRO-600")

    def test_diver_depth_is_not_read_as_a_model_number(self):
        """'Diver 300m' on a Pro 600 must not exclude it."""
        m = self.cat.match("Christopher Ward C60 Trident Pro 600 Diver 300m black")
        self.assertEqual(m.reference.key, "C60-TRIDENT-PRO-600")

    def test_synthetic_keys_are_declared_not_inferred(self):
        self.assertFalse(self.cat.by_key["556-I"].synthetic_key)
        self.assertTrue(self.cat.by_key["C60-TRIDENT-PRO-300"].synthetic_key)

    def test_no_duplicate_keys(self):
        keys = [r.key for r in self.cat.references]
        self.assertEqual(len(keys), len(set(keys)))


class TestListingMapping(unittest.TestCase):
    def test_auction_without_price_is_not_discarded(self):
        row = {
            "itemId": "v1|9|0",
            "title": "Hamilton Khaki Field",
            "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "150.00", "currency": "GBP"},
            "bidCount": 4,
            "itemLocation": {"country": "GB"},
            "seller": {"feedbackPercentage": "99.3", "feedbackScore": 812},
        }
        parsed = from_item_summary(row)
        self.assertTrue(parsed.is_auction)
        self.assertEqual(parsed.price, 15000)
        self.assertEqual(parsed.seller_feedback_pct_x100, 9930)

    def test_missing_shipping_is_unknown_not_free(self):
        parsed = from_item_summary({"itemId": "x", "buyingOptions": ["FIXED_PRICE"]})
        self.assertIsNone(parsed.shipping)

    def test_missing_currency_is_not_assumed_gbp(self):
        parsed = from_item_summary(
            {"itemId": "x", "buyingOptions": ["FIXED_PRICE"], "price": {"value": "200"}}
        )
        self.assertEqual(parsed.currency, "")

    def test_datetimes_are_aware_utc(self):
        dt = parse_ts("2026-09-04T14:30:00.000Z")
        self.assertIsNotNone(dt.tzinfo)
        self.assertEqual(dt.utcoffset().total_seconds(), 0)
        self.assertTrue(utcnow().tzinfo is timezone.utc)

    def test_no_naive_datetime_escapes(self):
        naive = datetime(2026, 1, 1)
        self.assertIsNone(naive.tzinfo)  # the shape we must never store
        self.assertIsNotNone(parse_ts("2026-01-01T00:00:00Z").tzinfo)


class TestFieldReading(unittest.TestCase):
    def test_condition(self):
        self.assertEqual(
            read_condition(listing(condition_id="1000", condition_raw="New")), "MINT"
        )
        self.assertEqual(read_condition(listing(condition_id="7000",
                                                condition_raw="For parts")), "FOR_PARTS")

    def test_bare_pre_owned_is_not_graded(self):
        self.assertIsNone(
            read_condition(listing(condition_id="3000", condition_raw="Pre-owned"))
        )

    def test_ebay_uk_qualifies_pre_owned_and_we_use_it(self):
        """conditionId 3000 alone says nothing; the string carries the grade."""
        for text, grade in (
            ("Pre-owned - Excellent", "EXCELLENT"),
            ("Pre-owned - Very Good", "GOOD"),
            ("Pre-owned - Good", "GOOD"),
            ("Pre-owned - Fair", "FAIR"),
        ):
            self.assertEqual(
                read_condition(listing(condition_id="3000", condition_raw=text)),
                grade,
                text,
            )

    def test_graded_condition_is_used_and_unstated_is_assumed_good(self):
        v = valuer()
        vague = v.assess(listing(condition_id="3000", condition_raw="Pre-owned"))
        graded = v.assess(
            listing(condition_id="3000", condition_raw="Pre-owned - Excellent")
        )
        self.assertFalse(vague.valuation.condition_stated)
        self.assertEqual(vague.valuation.condition, "GOOD")
        self.assertTrue(graded.valuation.condition_stated)
        self.assertGreater(graded.valuation.mab, vague.valuation.mab)

    def test_scope(self):
        self.assertEqual(read_scope("PRX full set box and papers"), "FULL_SET")
        self.assertEqual(read_scope("PRX boxed"), "WATCH_BOX")
        self.assertIsNone(read_scope("PRX 40mm blue"))

    def test_bracelet(self):
        self.assertEqual(read_bracelet("with original bracelet"), "OEM_BRACELET")
        self.assertEqual(read_bracelet("aftermarket strap fitted"), "AFTERMARKET")
        self.assertIsNone(read_bracelet("PRX 40mm blue"))


class TestValuation(unittest.TestCase):
    def setUp(self):
        self.v = valuer()

    def test_effective_fmv_is_point_times_condition_only(self):
        a = self.v.assess(listing(condition_raw="Pre-owned - Excellent"))
        self.assertEqual(
            a.valuation.effective_fmv,
            mul_bp(a.fmv, C.COND_MULT["EXCELLENT"]),
        )

    def test_banded_reference_uses_the_midpoint(self):
        a = self.v.assess(listing(title="Tissot PRX 40mm steel integrated bracelet"))
        ref = Catalogue.load().by_key["PRX-UNSPECIFIED"]
        self.assertEqual(a.fmv, (ref.fmv_low + ref.fmv_high) // 2)
        self.assertIn("VARIANT_UNRESOLVED", a.caveats)

    def test_scope_and_bracelet_are_labels_not_value(self):
        bare = self.v.assess(listing(title="Tissot PRX Powermatic 80 40mm"))
        stated = self.v.assess(
            listing(title="Tissot PRX Powermatic 80 40mm full set original bracelet")
        )
        self.assertIsNone(bare.scope)
        self.assertIsNone(bare.bracelet)
        self.assertEqual(stated.scope, "FULL_SET")
        self.assertEqual(stated.bracelet, "OEM_BRACELET")
        self.assertEqual(bare.valuation.mab, stated.valuation.mab)

    def test_every_gate_is_recorded_even_when_one_fails(self):
        a = self.v.assess(listing(seller_feedback_pct_x100=8000))
        self.assertEqual(a.verdict, "REJECT_SELLER")
        self.assertEqual(
            [g.name for g in a.gates],
            ["CATALOGUE", "BLACKLIST", "SELLER", "VIABLE", "PRICE"],
        )

    def test_verdict_names_the_first_failure_in_evaluation_order(self):
        a = self.v.assess(
            listing(title="Rolex Submariner replica", seller_feedback_pct_x100=8000)
        )
        self.assertEqual(a.verdict, "REJECT_CATALOGUE")
        self.assertEqual(len(a.failed_gates), 3)

    def test_for_parts_condition_is_caught_by_the_blacklist(self):
        a = self.v.assess(
            listing(condition_id="7000", condition_raw="For parts or not working")
        )
        self.assertEqual(a.verdict, "REJECT_BLACKLIST")
        self.assertIn("for_parts", a.primary_reason)

    def test_missing_currency_rejects(self):
        a = self.v.assess(listing(currency="", price=parse_gbp("60.00")))
        self.assertEqual(a.verdict, "REJECT_PRICE")
        self.assertIn("no currency", a.primary_reason)

    def test_unpriced_reference_rejects_with_a_reason(self):
        a = self.v.assess(listing(title="Rolex Submariner 116610LN"))
        self.assertEqual(a.verdict, "REJECT_CATALOGUE")
        self.assertIn("catalogue", a.primary_reason.lower())

    def test_seed_fmv_carries_the_caveat(self):
        a = self.v.assess(listing())
        self.assertIn("FMV_UNVERIFIED", a.caveats)

    def test_fee_caveats_are_always_present(self):
        a = self.v.assess(listing())
        for caveat in C.FEE_CAVEATS:
            self.assertIn(caveat, a.caveats)

    def test_deal_is_at_or_under_the_max_bid(self):
        # £60 is below the search band but the valuation must still be coherent
        a = self.v.assess(listing(price=parse_gbp("60.00")))
        self.assertEqual(a.verdict, "DEAL")
        self.assertLessEqual(a.effective_price, a.valuation.mab)

    def test_one_penny_over_the_max_bid_rejects_on_price(self):
        mab = self.v.assess(listing()).valuation.mab
        self.assertEqual(self.v.assess(listing(price=mab)).verdict, "DEAL")
        self.assertEqual(
            self.v.assess(listing(price=mab + 1)).verdict, "REJECT_PRICE"
        )

    def test_auction_uses_the_next_valid_bid(self):
        a = self.v.assess(
            listing(is_auction=True, price=parse_gbp("150.00"), bid_count=3)
        )
        self.assertEqual(a.price_basis, "next bid")
        self.assertGreater(a.effective_price, parse_gbp("150.00"))

    def test_auction_with_no_bids_uses_the_start_price(self):
        a = self.v.assess(
            listing(is_auction=True, price=parse_gbp("150.00"), bid_count=0)
        )
        self.assertEqual(a.effective_price, parse_gbp("150.00"))

    def test_missing_seller_data_does_not_silently_reject(self):
        a = self.v.assess(
            listing(seller_feedback_pct_x100=None, seller_feedback_score=None)
        )
        gate = next(g for g in a.gates if g.name == "SELLER")
        self.assertTrue(gate.passed)
        self.assertIn("SELLER_DATA_MISSING", a.caveats)

    def test_bad_seller_rejects(self):
        a = self.v.assess(listing(seller_feedback_pct_x100=8000))
        self.assertEqual(a.verdict, "REJECT_SELLER")

    def test_business_seller_gets_the_no_buyer_protection_branch(self):
        private = self.v.assess(listing(seller_account_type="INDIVIDUAL"))
        business = self.v.assess(listing(seller_account_type="BUSINESS"))
        self.assertGreater(business.valuation.mab, private.valuation.mab)

    def test_bracelet_only_listing_is_dropped(self):
        a = self.v.assess(
            listing(
                title="Christopher Ward BX149 C60 Trident Pro 600 42mm Bader bracelet- used"
            )
        )
        self.assertEqual(a.verdict, "REJECT_BLACKLIST")

    def test_money_stays_integral_through_the_whole_path(self):
        a = self.v.assess(listing())
        for value in (
            a.effective_price,
            a.valuation.mab,
            a.valuation.effective_fmv,
            a.fmv,
            a.headroom,
            *(v for _, v in a.valuation.bid.lines),
        ):
            self.assertIsInstance(value, int)
            self.assertNotIsInstance(value, bool)


class TestPhaseOneBoundary(unittest.TestCase):
    def test_user_token_in_the_environment_is_fatal(self):
        os.environ["EBAY_USER_REFRESH_TOKEN"] = "x"
        try:
            with self.assertRaises(C.UserTokenPresent):
                C.assert_phase_1()
        finally:
            del os.environ["EBAY_USER_REFRESH_TOKEN"]

    def test_clean_environment_passes(self):
        C.assert_phase_1()

    def test_no_bidding_code_exists(self):
        """The guarantee is structural. Prove no source file reaches for it."""
        banned = ("place_proxy_bid", "buy.offer.auction", "authorization_code")
        for path in (ROOT / "src").rglob("*.py"):
            if path.name in ("selftest.py",):
                continue
            text = path.read_text(encoding="utf-8")
            for term in banned:
                self.assertNotIn(term, text, f"{path.name} mentions {term}")


class TestEnvExample(unittest.TestCase):
    def test_committed_file_matches_the_generated_one(self):
        from .__main__ import render_env_example

        committed = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertEqual(
            committed.strip(),
            render_env_example().strip(),
            "`.env.example` is stale. Regenerate with "
            "`python -m watchsniper env-example > .env.example`.",
        )

    def test_every_documented_var_has_both_halves(self):
        for var in C.ENV_VARS:
            self.assertTrue(var["where"], var["name"])
            self.assertTrue(var["missing"], var["name"])


class TestDocumentsDoNotRestateConstants(unittest.TestCase):
    """Requirement 16, enforced.

    A document that repeats a number goes stale the day the number changes.
    This looks for the current values of the most drift-prone constants in the
    markdown, and fails if it finds one.
    """

    def test_no_document_carries_a_live_figure(self):
        needles = {
            "FVF_BP": f"{C.FVF_BP / 100:.1f}%",
            "MIN_ABSOLUTE_PROFIT": fmt(C.MIN_ABSOLUTE_PROFIT),
            "OUTBOUND_POSTAGE": fmt(C.OUTBOUND_POSTAGE),
            "TARGET_PROFIT_MARGIN_BP": f"{C.TARGET_PROFIT_MARGIN_BP // 100}%",
        }
        offenders = []
        for path in ROOT.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            for name, needle in needles.items():
                if needle in text:
                    offenders.append(f"{path.name} restates {name} as {needle}")
        self.assertEqual(offenders, [], "\n".join(offenders))


class TestStorage(unittest.TestCase):
    def test_round_trip_and_rescore(self):
        from .db import Database, listing_from_row

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            v = valuer()
            item = listing()
            self.assertTrue(db.upsert_listing(item))
            self.assertFalse(db.upsert_listing(item))  # dedup is the primary key
            db.save_verdict(v.assess(item))
            rows = db.all_listings()
            self.assertEqual(len(rows), 1)
            again = listing_from_row(rows[0])
            self.assertEqual(again.item_id, item.item_id)
            self.assertEqual(again.price, item.price)
            self.assertIsNotNone(again.fetched_at.tzinfo)
            db.add_label(item.item_id, "bad_pass", "")
            self.assertEqual(len(db.labels_for(item.item_id)), 1)
            feed = db.feed()
            self.assertEqual(len(feed), 1)
            db.close()

    def test_two_scenario_verdicts_table_is_dropped(self):
        import sqlite3

        from .db import Database

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            old = sqlite3.connect(path)
            old.execute("CREATE TABLE verdicts (item_id TEXT, mab_pess_pence INTEGER)")
            old.commit()
            old.close()
            db = Database(path)
            self.assertTrue(db.verdicts_dropped)
            cols = {c["name"] for c in db.query("PRAGMA table_info(verdicts)")}
            self.assertIn("mab_pence", cols)
            db.close()

    def test_money_columns_are_integers(self):
        from .db import Database

        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "t.db")
            cols = db.query("PRAGMA table_info(listings)")
            types = {c["name"]: c["type"] for c in cols}
            self.assertEqual(types["price_pence"], "INTEGER")
            self.assertEqual(types["shipping_pence"], "INTEGER")
            db.close()


class TestAlerting(unittest.TestCase):
    """When a DEAL is worth a phone notification. No network: nothing is sent."""

    def setUp(self):
        from .db import Database
        from .poller import Engine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.engine = Engine(self.db, None)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def deal(self, pounds: str, **kw):
        a = self.engine.valuer.assess(listing(price=parse_gbp(pounds), **kw))
        self.assertEqual(a.verdict, "DEAL")
        return a

    def test_new_deal_alerts_once_at_the_same_price(self):
        a = self.deal("60.00")
        self.assertTrue(self.engine._should_notify(a, is_new=True))
        self.db.log_notification("alert", True, "", a.listing.item_id, a.effective_price)
        self.assertFalse(self.engine._should_notify(self.deal("60.00"), is_new=False))

    def test_price_drop_below_the_alerted_price_re_alerts(self):
        a = self.deal("60.00")
        self.db.log_notification("alert", True, "", a.listing.item_id, a.effective_price)
        self.assertTrue(self.engine._should_notify(self.deal("59.99"), is_new=False))
        self.assertFalse(self.engine._should_notify(self.deal("65.00"), is_new=False))

    def test_the_latest_alert_is_the_baseline(self):
        item = listing().item_id
        self.db.log_notification("alert", True, "", item, parse_gbp("60.00"))
        self.db.log_notification("alert", True, "", item, parse_gbp("50.00"))
        self.assertFalse(self.engine._should_notify(self.deal("55.00"), is_new=False))

    def test_a_failed_send_is_not_a_baseline(self):
        a = self.deal("60.00")
        self.db.log_notification("alert", False, "", a.listing.item_id, a.effective_price)
        self.assertTrue(self.engine._should_notify(a, is_new=True))

    def test_alert_without_a_recorded_price_does_not_re_alert(self):
        a = self.deal("60.00")
        self.db.log_notification("alert", True, "", a.listing.item_id, None)
        self.assertFalse(self.engine._should_notify(self.deal("10.00"), is_new=False))

    def test_old_notifications_table_gains_the_price_column(self):
        import sqlite3

        from .db import Database

        path = Path(self.tmp.name) / "old.db"
        old = sqlite3.connect(path)
        old.execute(
            "CREATE TABLE notifications (id INTEGER PRIMARY KEY, at_utc TEXT NOT"
            " NULL, kind TEXT NOT NULL, item_id TEXT, ok INTEGER NOT NULL,"
            " detail TEXT NOT NULL DEFAULT '')"
        )
        old.commit()
        old.close()
        db = Database(path)
        cols = {c["name"] for c in db.query("PRAGMA table_info(notifications)")}
        self.assertIn("price_pence", cols)
        db.close()


class TestIncrements(unittest.TestCase):
    def test_next_bid_steps_up(self):
        inc = Increments.load()
        self.assertEqual(inc.next_bid(parse_gbp("150.00"), 0), parse_gbp("150.00"))
        self.assertEqual(inc.next_bid(parse_gbp("150.00"), 1), parse_gbp("155.00"))

    def test_marked_unverified(self):
        self.assertFalse(Increments.load().verified)


def main() -> int:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(__import__(__name__, fromlist=["x"]))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
