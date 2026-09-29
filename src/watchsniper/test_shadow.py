"""Shadow mode (LLM_CONTRACT.md §8): asked once, stored, and nothing changed.

Hermetic. The LLM client is a fake that records what it was asked; item
details are stored up front so no eBay call is made.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from . import config as C
from .details import ItemDetails
from .llm import Image, LlmResult
from .models import utcnow
from .selftest import _FakeItemClient, listing

HAIKU, GEMINI = C.LLM_SHADOW_MODELS
SONNET = C.LLM_ESCALATION_MODEL
POWERMATIC = "T137.407.11.041.00"
TITLE = "Tissot PRX Powermatic 80 T137.407.11.041.00 40mm blue"


class FakeLlm:
    """Answers from a script keyed by model; counts calls."""

    def __init__(self, answers: dict | None = None, *, raises: bool = False):
        self.answers = answers or {}
        self.calls: list[str] = []
        self.raises = raises

    def available(self, model: str) -> bool:
        return True

    def identify(self, model: str, inp) -> LlmResult:
        if self.raises:
            raise RuntimeError("boom")
        self.calls.append(model)
        a = self.answers.get(model, {})
        if callable(a):
            a = a()
        base = dict(
            provider="anthropic" if model.startswith("claude") else "gemini",
            model=model,
            ok=True,
            catalogue_key=POWERMATIC,
            confidence="high",
            condition="EXCELLENT",
            evidence=[{"source": "title", "text": "T137.407.11.041.00"}],
            evidence_verified=True,
            input_tokens=5000,
            output_tokens=200,
            cost_micro_usd=10,
        )
        base.update(a)
        return LlmResult(**base)


class ShadowCase(unittest.TestCase):
    def setUp(self):
        from .db import Database
        from .poller import Engine

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")
        self.engine = Engine(self.db)
        self.images: list[str] = []
        self.lst = listing(item_id="v1|42|0", title=TITLE, price=10_000)
        self.db.upsert_listing(self.lst)
        self.engine.score(self.lst)
        self.db.save_item_details(ItemDetails(
            item_id=self.lst.item_id, fetched_at=utcnow(),
            aspects=[("Brand", "Tissot")], description_text="Runs well.",
            image_urls=["https://i.ebayimg.com/images/g/a/s-l1600.jpg"],
        ))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def fetch(self, url):
        self.images.append(url)
        return Image("image/jpeg", b"jpeg", url)

    def shadow(self, llm):
        from .shadow import Shadow

        s = Shadow(self.engine, llm, fetch_image=self.fetch)
        self.engine.shadow = s
        return s

    def rows(self):
        return self.db.query("SELECT * FROM llm_results ORDER BY id")


class TestAskingOnce(ShadowCase):
    def test_both_models_once_then_cached(self):
        llm = FakeLlm()
        s = self.shadow(llm)
        self.assertEqual(len(s.run(self.lst, "listing")), 2)
        self.assertEqual(llm.calls, [HAIKU, GEMINI])
        self.assertEqual(self.images, ["https://i.ebayimg.com/images/g/a/s-l960.jpg"])
        self.assertEqual(s.run(self.lst, "listing"), [])
        self.assertEqual(llm.calls, [HAIKU, GEMINI])
        self.assertEqual(len(self.images), 1, "no picture fetched on a cache hit")

    def test_low_confidence_escalates_once(self):
        llm = FakeLlm({GEMINI: {"confidence": "low"}, HAIKU: {"confidence": "medium"}})
        s = self.shadow(llm)
        s.run(self.lst, "listing")
        s.run(self.lst, "listing")
        self.assertEqual(llm.calls, [HAIKU, GEMINI, SONNET])
        rows = self.rows()
        gemini = next(r for r in rows if r["model"] == GEMINI)
        sonnet = next(r for r in rows if r["model"] == SONNET)
        self.assertEqual(sonnet["escalated_from"], gemini["id"])

    def test_confident_answers_do_not_escalate(self):
        llm = FakeLlm()
        self.shadow(llm).run(self.lst, "listing")
        self.assertNotIn(SONNET, llm.calls)

    def test_a_changed_candidate_set_is_asked_again(self):
        llm = FakeLlm()
        s = self.shadow(llm)
        s.run(self.lst, "listing")
        cat = self.engine.valuer.catalogue
        real = cat.candidates
        cat.candidates = lambda title, n=3: real(title, n)[:1]
        try:
            s.run(self.lst, "listing")
        finally:
            cat.candidates = real
        self.assertEqual(llm.calls, [HAIKU, GEMINI, HAIKU, GEMINI])

    def test_failures_retry_up_to_the_limit(self):
        llm = FakeLlm({m: {"ok": False, "error": "HTTP 500"} for m in (HAIKU, GEMINI)})
        s = self.shadow(llm)
        for _ in range(C.LLM_MAX_ATTEMPTS + 2):
            s.run(self.lst, "listing")
        self.assertEqual(llm.calls.count(HAIKU), C.LLM_MAX_ATTEMPTS)
        self.assertIn("HTTP 500", s.last_error)

    def test_skipped_is_not_stored(self):
        llm = FakeLlm({m: {"ok": False, "skipped": "spend_cap"} for m in (HAIKU, GEMINI)})
        self.shadow(llm).run(self.lst, "listing")
        self.assertEqual(self.rows(), [])

    def test_at_the_cap_nothing_is_fetched_or_asked(self):
        llm = FakeLlm({HAIKU: {"cost_micro_usd": C.LLM_DAILY_SPEND_CAP_MICRO_USD}})
        s = self.shadow(llm)
        s.run(self.lst, "listing")
        before = (list(llm.calls), list(self.images))
        other = listing(item_id="v1|43|0", title=TITLE, price=10_000)
        self.db.upsert_listing(other)
        s.run(other, "listing")
        self.assertEqual((llm.calls, self.images), before)

    def test_a_failure_inside_never_raises(self):
        s = self.shadow(FakeLlm(raises=True))
        self.assertEqual(s.run(self.lst, "listing"), [])
        self.assertIn("boom", s.last_error)


class TestWouldVerdict(ShadowCase):
    def would(self, lst=None, **facts):
        base = dict(ok=True, catalogue_key=POWERMATIC, confidence="high",
                    condition="EXCELLENT", evidence_verified=True, red_flags=[])
        base.update(facts)
        return self.engine.valuer.would_verdict(lst or self.lst, **base)

    def test_deal_needs_everything(self):
        self.assertEqual(self.would()[0], "DEAL")
        self.assertEqual(self.would(confidence="medium")[0], "CHECK")
        self.assertEqual(self.would(evidence_verified=False)[0], "CHECK")
        self.assertEqual(self.would(red_flags=["hands look aftermarket"])[0], "CHECK")

    def test_blacklist_flag_caps_at_check(self):
        flagged = listing(title=TITLE + " custom dial", price=10_000)
        verdict, _, reason = self.would(flagged)
        self.assertEqual(verdict, "CHECK")
        self.assertIn("custom_dial", reason)

    def test_rejections(self):
        self.assertEqual(self.would(catalogue_key=None)[0], "REJECT_LLM")
        self.assertEqual(self.would(condition="FOR_PARTS")[0], "REJECT_LLM")
        self.assertEqual(self.would(ok=False)[0], None)

    def test_model_condition_only_lowers_the_stated_grade(self):
        fair = listing(title=TITLE, price=10_000, condition_raw="Pre-owned - Fair")
        v = self.engine.valuer
        ref = v.catalogue.by_key[POWERMATIC]
        _, mab, _ = self.would(fair, condition="MINT")
        self.assertEqual(mab, v.max_bid_for(fair, ref, "FAIR").amount)
        _, mab, _ = self.would(self.lst, condition="FAIR")  # stated grade unknown
        self.assertEqual(mab, v.max_bid_for(self.lst, ref, "FAIR").amount)

    def test_over_the_max_bid(self):
        v = self.engine.valuer
        mab = v.max_bid_for(self.lst, v.catalogue.by_key[POWERMATIC], "EXCELLENT").amount
        within = listing(title=TITLE, price=mab + 1)
        beyond = listing(title=TITLE, price=mab * 2)
        self.assertEqual(self.would(within)[0], "CHECK")
        self.assertEqual(self.would(beyond)[0], "REJECT_LLM")

    def test_stored_with_the_result_and_recomputed_by_rescore(self):
        llm = FakeLlm()
        self.shadow(llm).run(self.lst, "listing")
        self.assertEqual({r["would_verdict"] for r in self.rows()}, {"DEAL"})
        self.db.execute("UPDATE llm_results SET would_verdict=NULL")
        self.engine.rescore_all()
        self.assertEqual({r["would_verdict"] for r in self.rows()}, {"DEAL"})
        self.assertEqual(len(llm.calls), 2, "rescore calls no model")


class TestNothingChanges(ShadowCase):
    def test_off_by_default(self):
        # The built-in default, not whatever this host's .env says.
        registered = next(v for v in C.ENV_VARS if v["name"] == "LLM_MODE")
        self.assertEqual(registered["default"], "off")
        from unittest import mock

        from .poller import Engine

        for mode in ("off", "live", ""):
            with mock.patch.object(C, "LLM_MODE", mode):
                self.assertIsNone(Engine(self.db).shadow, mode)
        with mock.patch.object(C, "LLM_MODE", "shadow"):
            self.assertIsNotNone(Engine(self.db).shadow)

    def test_sweep_runs_shadow_without_touching_verdict_or_alerts(self):
        before = self.db.one("SELECT verdict FROM verdicts WHERE item_id=?", (self.lst.item_id,))
        llm = FakeLlm()
        self.shadow(llm)

        class Search:
            class budget:
                used = 0
                remaining = 100
                ceiling = 100

            def search(self, **_kw):
                return {"itemSummaries": [{
                    "itemId": "v1|42|0", "title": TITLE, "buyingOptions": ["FIXED_PRICE"],
                    "price": {"value": "100.00", "currency": "GBP"},
                    "condition": "Pre-owned", "conditionId": "3000",
                    "seller": {"username": "s", "sellerAccountType": "INDIVIDUAL",
                               "feedbackPercentage": "99.5", "feedbackScore": 300},
                    "itemLocation": {"country": "GB"},
                    "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "GBP"}}],
                }]}

        self.engine.client = Search()
        result = self.engine.poll_once("bin", notify=False)
        self.assertIsNone(result.error)
        self.assertEqual(llm.calls, [HAIKU, GEMINI])
        after = self.db.one("SELECT verdict FROM verdicts WHERE item_id=?", (self.lst.item_id,))
        self.assertEqual(after["verdict"], before["verdict"])
        self.assertEqual(self.db.query("SELECT * FROM notifications"), [])

    def test_closing_stage_asks_about_a_matched_auction(self):
        auction = listing(item_id="v1|77|0", title=TITLE, is_auction=True,
                          end_time_utc=utcnow() - C.CLOSING_CHECK_DELAY - timedelta(minutes=1))
        self.db.upsert_listing(auction)
        self.engine.score(auction)
        llm = FakeLlm()
        self.shadow(llm)
        self.engine.client = _FakeItemClient({"v1|77|0": {
            "itemId": "v1|77|0", "title": TITLE, "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "150.00", "currency": "GBP"}, "bidCount": 3,
            "estimatedAvailabilities": [{"estimatedSoldQuantity": 1}],
            "image": {"imageUrl": "https://i.ebayimg.com/images/g/b/s-l1600.jpg"},
        }})
        self.engine.record_closings()
        self.assertEqual(self.engine.client.fetched, ["v1|77|0"], "one getItem only")
        rows = [r for r in self.rows() if r["item_id"] == "v1|77|0"]
        self.assertEqual({r["stage"] for r in rows}, {"closing"})
        self.assertEqual({r["would_verdict"] for r in rows}, {None})


if __name__ == "__main__":
    unittest.main()
