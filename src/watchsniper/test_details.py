"""Item details: getItem mapping, description text, pictures, storage."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as C
from .details import (
    ItemDetails,
    download_image,
    from_get_item,
    html_to_text,
    sized_image_url,
)
from .ebay import BudgetExhausted, EbayError
from .models import utcnow
from .selftest import _FakeItemClient, listing

PIC = "https://i.ebayimg.com/images/g/AbCdEf/s-l1600.jpg"


def get_item_row(**kw) -> dict:
    row = {
        "itemId": "v1|123|0",
        "localizedAspects": [
            {"type": "STRING", "name": "Brand", "value": "Tissot"},
            {"type": "STRING", "name": "Movement", "value": "Automatic"},
        ],
        "description": "<p>Lovely watch.</p><p>Box &amp; papers.</p>",
        "shortDescription": "short",
        "image": {"imageUrl": PIC},
        "additionalImages": [
            {"imageUrl": "https://i.ebayimg.com/images/g/X/s-l1600.jpg"},
            {"imageUrl": PIC},
            {"imageUrl": "https://i.ebayimg.com/images/g/Y/s-l1600.jpg"},
        ],
    }
    row.update(kw)
    return row


class TestFromGetItem(unittest.TestCase):
    def test_maps_aspects_description_and_pictures(self):
        at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        row = get_item_row()
        d = from_get_item(row, fetched_at=at)
        self.assertEqual(d.item_id, "v1|123|0")
        self.assertEqual(d.fetched_at, at)
        self.assertEqual(d.aspects, [("Brand", "Tissot"), ("Movement", "Automatic")])
        self.assertEqual(d.description_text, "Lovely watch.\nBox & papers.")
        self.assertEqual(
            d.image_urls,
            [
                PIC,
                "https://i.ebayimg.com/images/g/X/s-l1600.jpg",
                "https://i.ebayimg.com/images/g/Y/s-l1600.jpg",
            ],
        )
        self.assertIs(d.raw, row)

    def test_absent_fields(self):
        d = from_get_item({"itemId": "v1|9|0"})
        self.assertEqual(d.aspects, [])
        self.assertEqual(d.description_text, "")
        self.assertEqual(d.image_urls, [])
        self.assertIsNotNone(d.fetched_at.tzinfo)

    def test_short_description_is_the_fallback(self):
        row = get_item_row()
        del row["description"]
        self.assertEqual(from_get_item(row).description_text, "short")

    def test_description_is_capped(self):
        row = get_item_row(description="x" * 500)
        self.assertEqual(len(from_get_item(row, max_chars=100).description_text), 100)

    def test_malformed_aspects_and_images_are_skipped(self):
        row = get_item_row(
            localizedAspects=["junk", {"name": "", "value": "x"}, {"name": "Case", "value": None}],
            image=None,
            additionalImages=[{}, "junk", {"imageUrl": PIC}],
        )
        d = from_get_item(row)
        self.assertEqual(d.aspects, [("Case", "")])
        self.assertEqual(d.image_urls, [PIC])


class TestHtmlToText(unittest.TestCase):
    def test_script_style_and_comments_dropped(self):
        html = (
            "<html><head><style>.a{color:red}</style></head><body>"
            "<script>alert('x')</script><!-- hidden -->Visible</body></html>"
        )
        self.assertEqual(html_to_text(html, 1000), "Visible")

    def test_entities_decoded_and_whitespace_collapsed(self):
        self.assertEqual(
            html_to_text("Box&nbsp;&amp;  papers &#163;5 \n\t ok", 1000),
            "Box & papers £5 ok",
        )

    def test_block_tags_break_lines(self):
        self.assertEqual(
            html_to_text("<div>one</div><div>two</div>three<br>four<li>five</li>", 1000),
            "one\ntwo\nthree\nfour\nfive",
        )

    def test_malformed_markup_does_not_raise(self):
        for html in ("<div><p>unclosed <b>bold", "</p></div>stray", "<<<>>>&&;", "<a href='x", "<!--"):
            self.assertIsInstance(html_to_text(html, 1000), str)
        self.assertIn("unclosed bold", html_to_text("<div><p>unclosed <b>bold", 1000))

    def test_long_input_is_cut(self):
        out = html_to_text("<p>" + "word " * 10000 + "</p>", 250)
        self.assertLessEqual(len(out), 250)
        self.assertTrue(out.startswith("word word"))

    def test_empty(self):
        self.assertEqual(html_to_text("", 100), "")


class TestSizedImageUrl(unittest.TestCase):
    def test_replaces_size_token(self):
        self.assertEqual(
            sized_image_url(PIC, 960),
            "https://i.ebayimg.com/images/g/AbCdEf/s-l960.jpg",
        )
        self.assertEqual(
            sized_image_url("https://i.ebayimg.com/images/g/A/s-l500.webp", 800),
            "https://i.ebayimg.com/images/g/A/s-l800.webp",
        )
        self.assertEqual(
            sized_image_url("https://i.ebayimg.com/images/g/A/s-l225.png"),
            f"https://i.ebayimg.com/images/g/A/s-l{C.LLM_IMAGE_EDGE_PX}.png",
        )

    def test_unchanged_without_token(self):
        for url in ("https://i.ebayimg.com/images/g/A/picture.jpg", "", "not a url"):
            self.assertEqual(sized_image_url(url, 960), url)


class TestDownloadImage(unittest.TestCase):
    def opener(self, content_type="image/jpeg", body=b"\xff\xd8jpeg"):
        calls = []

        def _open(url, timeout):
            calls.append(url)
            return content_type, body

        _open.calls = calls
        return _open

    def test_fetches_an_ebay_picture(self):
        op = self.opener(content_type="image/jpeg; charset=binary")
        img = download_image(PIC, opener=op)
        self.assertIsNotNone(img)
        self.assertEqual(img.media_type, "image/jpeg")
        self.assertEqual(img.data, b"\xff\xd8jpeg")
        self.assertEqual(img.source_url, PIC)

    def test_rejects_other_hosts_without_fetching(self):
        op = self.opener()
        for url in (
            "https://example.com/s-l1600.jpg",
            "https://i.ebayimg.com.evil.test/s-l1600.jpg",
            "https://evil.test/?u=i.ebayimg.com",
            "https://user@evil.test/i.ebayimg.com/x.jpg",
            "http://i.ebayimg.com/images/g/A/s-l1600.jpg",
            "file:///etc/passwd",
            "https://i.ebayimg.com:8443/images/g/A/s-l1600.jpg",
        ):
            self.assertIsNone(download_image(url, opener=op), url)
        self.assertEqual(op.calls, [])

    def test_rejects_wrong_content_type(self):
        for ct in ("text/html", "image/svg+xml", "", None, "application/octet-stream"):
            self.assertIsNone(download_image(PIC, opener=self.opener(content_type=ct)), ct)

    def test_keeps_the_supported_types(self):
        for ct in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            self.assertEqual(download_image(PIC, opener=self.opener(content_type=ct)).media_type, ct)

    def test_rejects_oversize_and_empty_bodies(self):
        self.assertIsNone(download_image(PIC, max_bytes=10, opener=self.opener(body=b"x" * 11)))
        self.assertIsNotNone(download_image(PIC, max_bytes=10, opener=self.opener(body=b"x" * 10)))
        self.assertIsNone(download_image(PIC, opener=self.opener(body=b"")))

    def test_opener_exceptions_become_none(self):
        for exc in (OSError("reset"), TimeoutError(), ValueError("bad"), RuntimeError("x")):
            def boom(url, timeout, exc=exc):
                raise exc

            self.assertIsNone(download_image(PIC, opener=boom))


class _DbCase(unittest.TestCase):
    def setUp(self):
        from .db import Database

        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()


class TestItemDetailsStorage(_DbCase):
    def test_round_trip(self):
        self.db.upsert_listing(listing(item_id="v1|123|0"))
        at = datetime(2026, 9, 1, 12, 30, tzinfo=timezone.utc)
        d = from_get_item(get_item_row(), fetched_at=at)
        self.db.save_item_details(d)
        got = self.db.item_details("v1|123|0")
        self.assertIsInstance(got, ItemDetails)
        self.assertEqual(got.item_id, d.item_id)
        self.assertEqual(got.fetched_at, at)
        self.assertEqual(got.aspects, d.aspects)
        self.assertEqual(got.description_text, d.description_text)
        self.assertEqual(got.image_urls, d.image_urls)
        self.assertEqual(got.raw, d.raw)

    def test_replace_and_missing(self):
        self.assertIsNone(self.db.item_details("nope"))
        self.db.upsert_listing(listing(item_id="v1|123|0"))
        self.db.save_item_details(from_get_item(get_item_row()))
        self.db.save_item_details(from_get_item(get_item_row(description="new")))
        self.assertEqual(self.db.item_details("v1|123|0").description_text, "new")


class TestEngineDetails(_DbCase):
    def engine(self, client):
        from .poller import Engine

        return Engine(self.db, client)

    def test_record_closings_stores_details_without_a_second_call(self):
        self.db.upsert_listing(
            listing(
                item_id="a1",
                is_auction=True,
                end_time_utc=utcnow() - C.CLOSING_CHECK_DELAY - timedelta(minutes=1),
            )
        )
        row = get_item_row(
            itemId="a1",
            buyingOptions=["AUCTION"],
            currentBidPrice={"value": "310.00", "currency": "GBP"},
            bidCount=4,
        )
        client = _FakeItemClient({"a1": row})
        engine = self.engine(client)
        self.assertEqual(engine.record_closings(), 1)
        self.assertEqual(client.fetched, ["a1"])
        stored = self.db.item_details("a1")
        self.assertIsNotNone(stored)
        self.assertEqual(stored.aspects[0], ("Brand", "Tissot"))
        # Asking for the details afterwards uses the stored copy.
        self.assertIsNotNone(engine.item_details("a1"))
        self.assertEqual(client.fetched, ["a1"])

    def test_item_details_fetches_once_then_uses_the_stored_copy(self):
        self.db.upsert_listing(listing(item_id="b1"))
        client = _FakeItemClient({"b1": get_item_row(itemId="b1")})
        engine = self.engine(client)
        first = engine.item_details("b1")
        second = engine.item_details("b1")
        self.assertEqual(client.fetched, ["b1"])
        self.assertEqual(first.image_urls, second.image_urls)
        self.assertEqual(second.item_id, "b1")

    def test_ebay_error_is_none_and_budget_propagates(self):
        self.db.upsert_listing(listing(item_id="e1"))
        self.db.upsert_listing(listing(item_id="e2"))
        client = _FakeItemClient(
            {"e1": EbayError("gone", 404, ""), "e2": BudgetExhausted("spent")}
        )
        engine = self.engine(client)
        self.assertIsNone(engine.item_details("e1"))
        self.assertIsNone(self.db.item_details("e1"))
        with self.assertRaises(BudgetExhausted):
            engine.item_details("e2")

    def test_unknown_listing_is_returned_but_not_stored(self):
        client = _FakeItemClient({"z9": get_item_row(itemId="z9")})
        d = self.engine(client).item_details("z9")
        self.assertIsNotNone(d)
        self.assertIsNone(self.db.item_details("z9"))


if __name__ == "__main__":
    unittest.main()
