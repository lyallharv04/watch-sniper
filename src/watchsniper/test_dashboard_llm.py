"""The dashboard's shadow-mode pieces (LLM_CONTRACT.md §9). Hermetic."""

from __future__ import annotations

import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from . import web
from .test_shadow import GEMINI, HAIKU, SONNET, FakeLlm, ShadowCase


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TestLlmOnTheDashboard(ShadowCase):
    def setUp(self):
        super().setUp()
        self.llm = FakeLlm({
            HAIKU: {"confidence": "medium", "reason": "Waffle dial <script>x</script>",
                    "raw_response": '{"content": "<b>raw</b>"}'},
            GEMINI: {"catalogue_key": None, "confidence": "low"},
            SONNET: {"ok": False, "error": "HTTP 529: overloaded"},
        })
        self.shadow(self.llm).run(self.lst, "listing")
        self.rows_ = self.rows()

    def result_id(self, model: str) -> int:
        return next(r["id"] for r in self.rows_ if r["model"] == model)

    def test_item_page_shows_every_result_as_stored_and_escaped(self):
        html = web.render_item(self.engine, self.lst.item_id)
        for model in (HAIKU, GEMINI, SONNET):
            self.assertIn(model, html)
        self.assertIn("T137.407.11.041.00", html)
        self.assertIn("found in the listing", html)
        self.assertIn("none of the candidates", html)
        self.assertIn("HTTP 529: overloaded", html)
        self.assertIn("Live mode would say", html)
        self.assertIn("REJECT_LLM", html)
        self.assertIn("CHECK", html)
        self.assertNotIn("<script>x", html)
        self.assertIn("&lt;script&gt;x", html)
        self.assertIn("&lt;b&gt;raw&lt;/b&gt;", html)
        self.assertEqual(html.count('action="/llm-label"'), 3, "one pair of buttons per result")

    def test_no_results_no_section(self):
        from .selftest import listing

        other = listing(item_id="v1|9|0")
        self.db.upsert_listing(other)
        self.engine.score(other)
        self.assertNotIn("Verification models", web.render_item(self.engine, other.item_id))

    def test_labels_latest_wins_and_tally_per_model(self):
        rid = self.result_id(HAIKU)
        self.db.add_llm_label(rid, True)
        self.db.add_llm_label(rid, False)
        self.db.add_llm_label(self.result_id(GEMINI), True)
        self.assertEqual(self.db.llm_label_state(self.lst.item_id)[rid], False)
        tally = {r["model"]: (r["right_n"], r["wrong_n"]) for r in self.db.llm_label_tally()}
        self.assertEqual(tally, {HAIKU: (0, 1), GEMINI: (1, 0)})
        html = web.render_item(self.engine, self.lst.item_id)
        self.assertIn("✓ model wrong", html)

    def test_review_page_samples_would_be_rejections(self):
        html = web.render_llm_review(self.engine)
        self.assertIn(GEMINI, html)
        self.assertIn("none of the candidates", html)
        self.assertNotIn(f"<b>{HAIKU}</b>", html, "only would-be rejections are sampled")
        self.assertIn("No model answer labelled yet", html)

    def test_health_shows_mode_spend_calls_and_errors(self):
        html = web.render_health(self.engine)
        self.assertIn("Verification models", html)
        self.assertIn("shadow", html)
        self.assertIn("cap", html)
        self.assertIn(f"{SONNET}", html)
        self.assertIn("1 errors", html)
        self.assertIn("HTTP 529", html)

    def test_feed_knows_the_live_mode_verdicts(self):
        self.assertIn("CHECK", web.VERDICT_OPTIONS)
        self.assertIn("REJECT_LLM", web.VERDICT_OPTIONS)
        web.render_feed(self.engine, {"verdict": ["CHECK"]})

    def test_label_post_stores_and_returns_only_to_a_local_path(self):
        httpd = web.serve(self.engine, "127.0.0.1", 0)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
        port = httpd.server_address[1]
        try:
            def post(**form):
                data = urllib.parse.urlencode(form).encode()
                try:
                    opener.open(f"http://127.0.0.1:{port}/llm-label", data, timeout=5)
                except urllib.error.HTTPError as exc:
                    return exc.code, exc.headers.get("Location")
                return 200, None

            rid = self.result_id(HAIKU)
            self.assertEqual(post(result_id=rid, correct="1", next="/item/x"), (303, "/item/x"))
            self.assertEqual(post(result_id=rid, correct="0", next="//evil.example"), (303, "/"))
            self.assertEqual(post(result_id="nope", correct="1", next="/"), (303, "/"))
            self.assertEqual(self.db.llm_label_state(self.lst.item_id), {rid: False})
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
