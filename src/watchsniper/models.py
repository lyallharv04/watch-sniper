"""The one shape a listing takes, and the mapping from eBay's JSON onto it.

Every datetime is timezone-aware UTC (requirement 15). Every money field is
pence (requirement 14). Both are enforced here, at the boundary, so nothing
downstream has to remember.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .money import Pence, parse_api_amount


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(value: str | None) -> datetime | None:
    """eBay sends RFC3339 with a trailing Z. Never returns a naive datetime."""
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_pct_x100(value: str | float | None) -> int | None:
    """Feedback percentage as hundredths of a percent. "99.3" -> 9930."""
    if value is None or value == "":
        return None
    s = str(value)
    whole, _, frac = s.partition(".")
    try:
        return int(whole) * 100 + int((frac + "00")[:2])
    except ValueError:
        return None


@dataclass
class Listing:
    item_id: str
    title: str
    web_url: str
    is_auction: bool
    price: Pence | None
    shipping: Pence | None
    currency: str
    condition_raw: str
    condition_id: str
    bid_count: int | None
    seller_username: str
    seller_account_type: str
    seller_feedback_pct_x100: int | None
    seller_feedback_score: int | None
    item_location_country: str
    end_time_utc: datetime | None
    category_id: str
    fetched_at: datetime
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def is_business_seller(self) -> bool:
        return self.seller_account_type.upper() == "BUSINESS"

    @property
    def raw_json(self) -> str:
        return json.dumps(self.raw, separators=(",", ":"), sort_keys=True)


def from_item_summary(row: dict, *, fetched_at: datetime | None = None) -> Listing:
    """Map one `itemSummaries` element.

    Two field-level traps are handled here because both silently discarded
    real listings in the previous attempt:

      * `price` is absent on many auctions, which supply only
        `currentBidPrice`. Requiring `price` dropped every such listing.
      * `shippingOptions` may be missing entirely rather than zero. That is
        recorded as unknown, not as free.
    """
    buying = [b.upper() for b in row.get("buyingOptions", [])]
    is_auction = "AUCTION" in buying

    price = parse_api_amount((row.get("price") or {}).get("value"))
    bid = parse_api_amount((row.get("currentBidPrice") or {}).get("value"))
    if is_auction:
        effective = bid if bid is not None else price
    else:
        effective = price if price is not None else bid

    shipping = None
    for opt in row.get("shippingOptions") or []:
        cost = parse_api_amount((opt.get("shippingCost") or {}).get("value"))
        if cost is not None:
            shipping = cost if shipping is None else min(shipping, cost)

    seller = row.get("seller") or {}
    loc = row.get("itemLocation") or {}
    cats = row.get("categories") or []
    # Absent stays absent: a price with no stated currency is not a GBP price,
    # and the valuation rejects it rather than assuming.
    currency = (row.get("price") or row.get("currentBidPrice") or {}).get(
        "currency"
    ) or ""

    return Listing(
        item_id=row.get("itemId", ""),
        title=row.get("title", ""),
        web_url=row.get("itemWebUrl", ""),
        is_auction=is_auction,
        price=effective,
        shipping=shipping,
        currency=currency,
        condition_raw=row.get("condition", "") or "",
        condition_id=str(row.get("conditionId", "") or ""),
        bid_count=row.get("bidCount"),
        seller_username=seller.get("username", "") or "",
        seller_account_type=(seller.get("sellerAccountType") or "").upper(),
        seller_feedback_pct_x100=parse_pct_x100(seller.get("feedbackPercentage")),
        seller_feedback_score=(
            int(seller["feedbackScore"]) if seller.get("feedbackScore") is not None
            else None
        ),
        item_location_country=(loc.get("country") or "").upper(),
        end_time_utc=parse_ts(row.get("itemEndDate")),
        category_id=str(cats[0].get("categoryId", "")) if cats else "",
        fetched_at=fetched_at or utcnow(),
        raw=row,
    )
