"""The one-shot live diagnostic (requirement 21).

Run this first, before anything else, the moment credentials are in place. Its
job is to find out how the live API differs from what this code assumes, using
the smallest number of real calls that can answer the question — currently
four, plus one OAuth token request.

It answers, in order:

  1. Do the credentials produce an application token at all?
  2. Does the candidate category resolve on EBAY_GB, or is it silently ignored?
  3. Which seller fields actually come back populated? The fee branch and the
     seller guard both depend on `sellerAccountType`, and nothing verifies it
     is served on this marketplace until it is asked for.
  4. Does the published rate limit appear in a response header?
  5. What does one whole item summary look like, unedited?

It writes nothing to the database and sends no notifications.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from . import config as C
from .ebay import CallBudget, EbayClient, EbayError
from .models import from_item_summary

SEP = "─" * 72


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def run(verbose: bool = True) -> dict:
    out: dict = {"ok": False, "findings": []}

    def say(*a):
        if verbose:
            print(*a)

    def finding(text: str):
        out["findings"].append(text)
        say(f"  ! {text}")

    missing = C.missing_credentials()
    if missing:
        say(f"MISSING CREDENTIALS: {', '.join(missing)}")
        say("Fill them in .env — see .env.example for where each one comes from.")
        out["error"] = f"missing {missing}"
        return out

    client = EbayClient(
        C.EBAY_CLIENT_ID,
        C.EBAY_CLIENT_SECRET,
        env=C.EBAY_ENV or "production",
        marketplace=C.EBAY_MARKETPLACE_ID or "EBAY_GB",
        budget=CallBudget(50),
    )
    day = _today()

    # 1 -- token ------------------------------------------------------------
    say(SEP)
    say("1. Application OAuth token (client credentials)")
    try:
        token = client.token()
    except EbayError as exc:
        say(f"  FAILED: {exc}")
        say(f"  body: {getattr(exc, 'body', '')[:600]}")
        out["error"] = str(exc)
        return out
    say(f"  ok — {len(token)} characters, environment {C.EBAY_ENV}")
    out["token_ok"] = True

    # 2 -- category resolution ---------------------------------------------
    say(SEP)
    say(f"2. Does category {C.EBAY_CATEGORY_IDS} resolve on {C.EBAY_MARKETPLACE_ID}?")
    common = dict(
        q="Hamilton",
        sort="newlyListed",
        buying_options="FIXED_PRICE",
        min_price=C.SEARCH_MIN_PRICE,
        max_price=C.SEARCH_MAX_PRICE,
        limit=50,
        day=day,
    )
    try:
        with_cat = client.search(category_ids=C.EBAY_CATEGORY_IDS or "", **common)
        without_cat = client.search(category_ids="", **common)
    except EbayError as exc:
        say(f"  FAILED: {exc} {getattr(exc, 'body', '')[:400]}")
        out["error"] = str(exc)
        return out

    n_with = int(with_cat.get("total", 0))
    n_without = int(without_cat.get("total", 0))
    say(f"  with category:    total={n_with}")
    say(f"  without category: total={n_without}")
    out["category_total"] = n_with
    out["uncategorised_total"] = n_without
    if n_with == 0:
        finding(
            f"Category {C.EBAY_CATEGORY_IDS} returned nothing on "
            f"{C.EBAY_MARKETPLACE_ID}. Either it does not resolve here or the "
            "band is empty. Do not trust the filter until this is non-zero."
        )
    elif n_with == n_without:
        finding(
            "The category filter changed nothing. It may be being ignored — "
            "check that leaf ids, not parent ids, are configured."
        )
    else:
        say(f"  category filter removes {n_without - n_with} results — it is applied")

    cats = Counter(
        str((row.get("categories") or [{}])[0].get("categoryId", "?"))
        for row in with_cat.get("itemSummaries") or []
    )
    say(f"  leaf categories seen: {dict(cats)}")
    out["leaf_categories"] = dict(cats)

    # 3 -- which fields actually arrive ------------------------------------
    say(SEP)
    say("3. Field coverage across the returned summaries")
    rows = with_cat.get("itemSummaries") or without_cat.get("itemSummaries") or []
    if not rows:
        finding("No item summaries returned at all; nothing further can be checked.")
        return out

    def coverage(path: str) -> tuple[int, int]:
        got = 0
        for row in rows:
            cur: object = row
            for part in path.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                    break
            if cur not in (None, "", [], {}):
                got += 1
        return got, len(rows)

    paths = [
        "seller.sellerAccountType",
        "seller.feedbackPercentage",
        "seller.feedbackScore",
        "seller.username",
        "itemLocation.country",
        "condition",
        "conditionId",
        "price.value",
        "currentBidPrice.value",
        "bidCount",
        "shippingOptions",
        "itemEndDate",
        "buyingOptions",
        "itemWebUrl",
    ]
    out["coverage"] = {}
    for p in paths:
        got, total = coverage(p)
        out["coverage"][p] = [got, total]
        flag = "" if got else "   <-- absent"
        say(f"  {p:<32} {got:>3}/{total}{flag}")

    if out["coverage"]["seller.sellerAccountType"][0] == 0:
        finding(
            "sellerAccountType is NOT returned. Buyer protection is charged on "
            "private-seller purchases only, so without this field every "
            "valuation must assume the private (more expensive) branch. The "
            "code does that already — assessments will carry a "
            "SELLER_DATA_MISSING style caveat rather than silently guessing."
        )
    if out["coverage"]["seller.feedbackPercentage"][0] == 0:
        finding(
            "feedbackPercentage is NOT returned, so the seller guard cannot "
            "fire. Listings will pass the seller gate with a caveat instead."
        )

    # 4 -- rate limit -------------------------------------------------------
    say(SEP)
    say("4. Rate limit headers")
    if client.last_rate_limit_headers:
        for k, v in client.last_rate_limit_headers.items():
            say(f"  {k}: {v}")
        out["rate_headers"] = dict(client.last_rate_limit_headers)
    else:
        say("  none exposed on the response.")
        finding(
            "eBay exposes no rate-limit header here, so EBAY_DAILY_CALL_CEILING "
            "cannot be checked from a response. Confirm the published figure at "
            "developer.ebay.com/develop/get-started/api-call-limits and set it "
            "in .env."
        )

    # 5 -- one raw listing, and what we make of it -------------------------
    say(SEP)
    say("5. One raw item summary, unedited")
    sample = rows[0]
    say(json.dumps(sample, indent=2)[:2600])
    out["sample"] = sample

    say(SEP)
    say("   Parsed through this codebase:")
    listing = from_item_summary(sample)
    for field in (
        "item_id", "title", "is_auction", "price", "shipping", "currency",
        "condition_raw", "condition_id", "seller_account_type",
        "seller_feedback_pct_x100", "seller_feedback_score",
        "item_location_country", "end_time_utc", "category_id",
    ):
        say(f"  {field:<28} {getattr(listing, field)!r}")

    # 6 -- what the auction sweep sees -------------------------------------
    say(SEP)
    say("6. Auction sweep (endingSoonest)")
    try:
        auctions = client.search(
            q="Hamilton",
            sort="endingSoonest",
            buying_options="AUCTION",
            category_ids=C.EBAY_CATEGORY_IDS or "",
            min_price=C.SEARCH_MIN_PRICE,
            max_price=C.SEARCH_MAX_PRICE,
            limit=20,
            day=day,
        )
        a_rows = auctions.get("itemSummaries") or []
        say(f"  total={auctions.get('total', 0)}, returned={len(a_rows)}")
        no_price = sum(1 for r in a_rows if not (r.get("price") or {}).get("value"))
        say(f"  auctions with no `price` field: {no_price}/{len(a_rows)}")
        if no_price:
            say("  (handled — currentBidPrice is used instead)")
        out["auction_total"] = auctions.get("total", 0)
    except EbayError as exc:
        finding(f"auction sweep failed: {exc}")

    say(SEP)
    say(f"Calls used: {client.budget.used}")
    say(f"Findings: {len(out['findings'])}")
    out["ok"] = True
    out["calls_used"] = client.budget.used
    return out
