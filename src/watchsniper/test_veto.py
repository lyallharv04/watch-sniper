"""The shadow veto (DECISIONS.md A20): alerts only, verdicts unchanged."""

from __future__ import annotations

import unittest
from datetime import timedelta
from unittest import mock

from . import config as C
from . import web
from .models import utcnow
from .shadow import veto_from
from .test_shadow import GEMINI, HAIKU, TITLE, FakeLlm, ShadowCase

NEW_ID = "v1|50|0"
REJECT = {"catalogue_key": None, "confidence": "high",
          "reason": "A vintage PRX, not the modern one."}


class Client:
    """One auction in every search; its getItem for the models."""

    class budget:
        used = 0
        total = 0
        remaining = 1000
        ceiling = 1000

    def search(self, **_kw):
        end = (utcnow() + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        return {"itemSummaries": [{
            "itemId": NEW_ID, "title": TITLE, "buyingOptions": ["AUCTION"],
            "currentBidPrice": {"value": "100.00", "currency": "GBP"}, "bidCount": 0,
            "itemEndDate": end, "condition": "Pre-owned", "conditionId": "3000",
            "seller": {"username": "s", "sellerAccountType": "INDIVIDUAL",
                       "feedbackPercentage": "99.5", "feedbackScore": 300},
            "itemLocation": {"country": "GB"},
            "shippingOptions": [{"shippingCost": {"value": "5.00", "currency": "GBP"}}],
        }]}

    def get_item(self, item_id, *, day):
        return {"itemId": item_id, "description": "Runs well.",
                "image": {"imageUrl": "https://i.ebayimg.com/images/g/z/s-l1600.jpg"}}


class TestVeto(ShadowCase):
    def sweep(self, answers):
        self.shadow(FakeLlm(answers))
        self.engine.client = Client()
        self.sent = []
        self.engine.notifier.send = lambda note: (self.sent.append(note), (True, "ok"))[1]
        result = self.engine.poll_once("auction")
        self.assertIsNone(result.error)
        return result

    def kinds(self):
        return [r["kind"] for r in self.db.query(
            "SELECT kind FROM notifications WHERE item_id=? ORDER BY id", (NEW_ID,))]

    def verdict(self):
        return self.db.one("SELECT verdict FROM verdicts WHERE item_id=?", (NEW_ID,))[0]

    def test_both_models_high_confidence_reject_suppresses_the_alert(self):
        result = self.sweep({HAIKU: REJECT, GEMINI: REJECT})
        self.assertEqual(self.verdict(), "DEAL", "verdict unchanged")
        self.assertEqual(self.sent, [])
        self.assertEqual(self.kinds(), ["vetoed"])
        self.assertEqual(result.alerts, 0)
        reasons = self.db.veto(NEW_ID)
        self.assertEqual([r["model"] for r in reasons], [HAIKU, GEMINI])

    def test_vetoed_once_not_every_sweep(self):
        self.sweep({HAIKU: REJECT, GEMINI: REJECT})
        self.engine.poll_once("auction")
        self.assertEqual(self.kinds(), ["vetoed"])

    def test_shown_as_vetoed_with_both_reasons(self):
        self.sweep({HAIKU: REJECT, GEMINI: REJECT})
        feed = web.render_feed(self.engine, {"view": ["auctions"]})
        self.assertIn("VETOED", feed)
        self.assertIn("A vintage PRX, not the modern one.", feed)
        item = web.render_item(self.engine, NEW_ID)
        self.assertIn("VETOED", item)
        self.assertIn(HAIKU, item)
        self.assertIn("Vetoed:", web.render_health(self.engine))

    def test_any_doubt_and_the_alert_goes_out(self):
        for answers in (
            {HAIKU: REJECT, GEMINI: {**REJECT, "confidence": "medium"}},
            {HAIKU: REJECT, GEMINI: {"confidence": "high"}},  # identified: a DEAL
            {HAIKU: REJECT, GEMINI: {"ok": False, "error": "HTTP 500"}},
        ):
            with self.subTest(answers=answers):
                self.db.execute("DELETE FROM notifications")
                self.db.execute("DELETE FROM llm_results")
                self.db.execute("DELETE FROM vetoes")
                self.sweep(answers)
                self.assertEqual(self.kinds(), ["alert"])
                self.assertEqual(len(self.sent), 1)

    def test_the_flag_turns_it_off(self):
        with mock.patch.object(C, "LLM_VETO", False):
            self.sweep({HAIKU: REJECT, GEMINI: REJECT})
        self.assertEqual(self.kinds(), ["alert"])

    def test_flag_defaults_on(self):
        registered = next(v for v in C.ENV_VARS if v["name"] == "LLM_VETO")
        self.assertEqual(registered["default"], "on")

    def test_rescore_rebuilds_it_without_a_call(self):
        self.sweep({HAIKU: REJECT, GEMINI: REJECT})
        self.db.execute("DELETE FROM vetoes")
        self.engine.rescore_all()
        self.assertIsNotNone(self.db.veto(NEW_ID))


class TestRule(unittest.TestCase):
    def row(self, model, confidence="high", would="REJECT_LLM"):
        return {"model": model, "confidence": confidence, "would_verdict": would,
                "reason": "r", "would_reason": "w"}

    def test_needs_both_models(self):
        models = (HAIKU, GEMINI)
        self.assertIsNotNone(veto_from([self.row(HAIKU), self.row(GEMINI)], models))
        self.assertIsNone(veto_from([self.row(HAIKU)], models))
        self.assertIsNone(veto_from([self.row(HAIKU), self.row(GEMINI, "medium")], models))
        self.assertIsNone(veto_from([self.row(HAIKU), self.row(GEMINI, would="CHECK")], models))
        # The escalation model has no vote.
        self.assertIsNone(veto_from([self.row(HAIKU), self.row(C.LLM_ESCALATION_MODEL)], models))


if __name__ == "__main__":
    unittest.main()
