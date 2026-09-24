"""Valuing a listing, and deciding what to say about it.

Two ideas run through this module.

**Every gate is evaluated, always.** A listing that fails the seller check is
still priced, and a listing with no FMV still records whether it would have
passed the blacklist. The verdict names the first failure in cost order, but
the full set is stored, because requirement 3 exists so that rejections can be
debugged and a rejection with one reason attached tells you nothing about the
other seven.

**Uncertainty is emitted, not resolved.** Most listings state neither scope of
delivery nor bracelet type, and those unknowns move the valuation further than
the entire fee stack does. Rather than assume a value and present a single
confident number, every listing gets two: a pessimistic valuation where each
unknown takes its worst plausible value, and an optimistic one where each takes
its best. The pass/fail gate uses the pessimistic figure — we only claim a deal
when it is a deal on the worst reading. The gap between the two is the size of
the data gap, and it is shown as such.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config as C
from .blacklist import Blacklist, Increments
from .catalogue import Catalogue, Match
from .fees import MaxBid, max_allowable_bid
from .models import Listing
from .money import Pence, compose_bp

# --------------------------------------------------------------------------
# Reading the listing
# --------------------------------------------------------------------------

# eBay conditionId -> our grade. Anything unmapped is treated as unknown, which
# widens the band rather than guessing.
_CONDITION_BY_ID = {
    "1000": "MINT",       # New
    "1500": "EXCELLENT",  # New other
    "1750": "EXCELLENT",  # New with defects
    "2000": "EXCELLENT",  # Certified refurbished
    "2010": "EXCELLENT",
    "2020": "EXCELLENT",
    "2030": "GOOD",       # Seller refurbished
    "2500": "GOOD",
    "3000": None,         # Pre-owned — the id alone does not settle the grade
    "4000": "GOOD",
    "5000": "FAIR",
    "6000": "FAIR",
    "7000": "FOR_PARTS",
}

# eBay UK now qualifies conditionId 3000 in the `condition` string —
# "Pre-owned - Excellent", "Pre-owned - Good", "Pre-owned - Fair". That is a
# seller-declared grade on a fixed scale, which is real information and better
# than assuming a band; it was going to waste until a live listing showed it.
# Order matters: "Very Good" must be tested before "Good".
_CONDITION_BY_TEXT = (
    ("for parts", "FOR_PARTS"),
    ("not working", "FOR_PARTS"),
    ("excellent", "EXCELLENT"),
    ("very good", "GOOD"),
    ("good", "GOOD"),
    ("fair", "FAIR"),
    ("acceptable", "FAIR"),
)

_SCOPE_PATTERNS = (
    ("FULL_SET", r"\b(full\s+set|box\s+(and|&|\+)\s+papers|papers\s+(and|&|\+)\s+box|complete\s+set)\b"),
    ("WATCH_PAPERS", r"\b(with\s+papers|warranty\s+card|papers\s+included|guarantee\s+card)\b"),
    ("WATCH_BOX", r"\b(boxed|with\s+box|inner\s+and\s+outer\s+box|original\s+box)\b"),
    ("WATCH_ONLY", r"\b(watch\s+only|no\s+box|without\s+box|head\s+only)\b"),
)

_BRACELET_PATTERNS = (
    ("AFTERMARKET", r"\b(aftermarket|third[\s-]party|replacement)\s+(strap|bracelet|band)\b"),
    ("OEM_BRACELET", r"\b(oem|original|genuine|factory)\s+(steel\s+)?bracelet\b"),
    ("OEM_STRAP", r"\b(oem|original|genuine|factory)\s+(leather\s+|rubber\s+|nato\s+)?strap\b"),
)


def read_condition(listing: Listing) -> str | None:
    """The condition grade, or None when the listing does not settle it.

    The condition *string* is consulted first, because eBay qualifies the
    generic "Pre-owned" id with a grade there and the id on its own throws that
    away. Falling back to the id keeps the mapping working where the string is
    absent or in an unexpected form.
    """
    text = listing.condition_raw.lower()
    for needle, grade in _CONDITION_BY_TEXT:
        if needle in text:
            return grade
    if text.startswith("new"):
        return "MINT"
    return _CONDITION_BY_ID.get(listing.condition_id)


def _first_match(text: str, patterns) -> str | None:
    for value, pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return value
    return None


def read_scope(title: str) -> str | None:
    return _first_match(title, _SCOPE_PATTERNS)


def read_bracelet(title: str) -> str | None:
    return _first_match(title, _BRACELET_PATTERNS)


# --------------------------------------------------------------------------
# Result shapes
# --------------------------------------------------------------------------


@dataclass
class Scenario:
    """One end of the valuation band."""

    label: str
    fmv_reference: Pence
    condition: str
    scope: str
    bracelet: str
    multiplier_bp: int
    effective_fmv: Pence
    bid: MaxBid

    @property
    def mab(self) -> Pence:
        return self.bid.amount


@dataclass
class Gate:
    name: str
    passed: bool
    detail: str


@dataclass
class Assessment:
    listing: Listing
    verdict: str
    primary_reason: str
    gates: list[Gate]
    caveats: list[str]
    unknown_fields: list[str]
    catalogue_key: str = ""
    catalogue_display: str = ""
    match_how: str = ""
    match_evidence: str = ""
    ambiguous_with: list[str] = field(default_factory=list)
    fmv_verified: bool = False
    fmv_low: Pence = 0
    fmv_high: Pence = 0
    pessimistic: Scenario | None = None
    optimistic: Scenario | None = None
    effective_price: Pence | None = None
    price_basis: str = ""
    config_fingerprint: str = ""

    @property
    def failed_gates(self) -> list[Gate]:
        return [g for g in self.gates if not g.passed]

    @property
    def headroom_pessimistic(self) -> Pence | None:
        if self.pessimistic is None or self.effective_price is None:
            return None
        return self.pessimistic.mab - self.effective_price

    @property
    def headroom_optimistic(self) -> Pence | None:
        if self.optimistic is None or self.effective_price is None:
            return None
        return self.optimistic.mab - self.effective_price

    @property
    def is_actionable(self) -> bool:
        return self.verdict == "PASS"


#: Ordered as the gates are reported. A listing failing several is named by the
#: first, because that is the one that explains the most about it.
GATE_ORDER = (
    "CATALOGUE",
    "BLACKLIST",
    "DOMESTIC",
    "CONDITION",
    "SELLER",
    "CURRENCY",
    "VIABLE",
    "PRICE",
)

REASON_TEXT = {
    "CATALOGUE": "No catalogue entry matched the title, so there is no FMV to value against.",
    "BLACKLIST": "A blacklist rule fired.",
    "DOMESTIC": "Not a UK-domestic listing.",
    "CONDITION": "Condition rules it out.",
    "SELLER": "Seller feedback below the floor.",
    "CURRENCY": "Priced in something other than GBP.",
    "VIABLE": "No bid at any price clears the profit floor.",
    "PRICE": "Priced above the maximum allowable bid on the optimistic reading.",
}


# --------------------------------------------------------------------------
# The engine
# --------------------------------------------------------------------------


class Valuer:
    def __init__(
        self,
        catalogue: Catalogue,
        blacklist: Blacklist,
        increments: Increments,
    ):
        self.catalogue = catalogue
        self.blacklist = blacklist
        self.increments = increments

    def assess(self, listing: Listing) -> Assessment:
        gates: list[Gate] = []
        caveats: list[str] = list(C.FEE_CAVEATS)
        unknown: list[str] = []

        # -- gates that need nothing from the catalogue --------------------

        hits = self.blacklist.check(listing.title)
        gates.append(
            Gate(
                "BLACKLIST",
                not hits,
                "; ".join(f"{h.rule_id}: {h.matched!r}" for h in hits) or "clean",
            )
        )

        domestic = listing.item_location_country == "GB"
        gates.append(
            Gate(
                "DOMESTIC",
                domestic,
                f"itemLocation.country={listing.item_location_country or 'absent'}",
            )
        )

        gbp = listing.currency == "GBP"
        gates.append(Gate("CURRENCY", gbp, listing.currency or "absent"))

        condition = read_condition(listing)
        gates.append(
            Gate(
                "CONDITION",
                condition != "FOR_PARTS",
                f"{listing.condition_raw or 'unstated'}"
                + ("" if condition else " (grade not settled)"),
            )
        )
        if condition is None:
            unknown.append("condition")

        gates.append(self._seller_gate(listing, caveats))

        # -- the catalogue -------------------------------------------------

        match = self.catalogue.match(listing.title)
        gates.append(
            Gate(
                "CATALOGUE",
                match is not None,
                f"{match.reference.key} via {match.how} {match.evidence!r}"
                if match
                else "no entry matched",
            )
        )

        if match is None:
            return self._finish(listing, gates, caveats, unknown, None)

        return self._value(listing, match, gates, caveats, unknown, condition)

    # -- helpers -----------------------------------------------------------

    def _seller_gate(self, listing: Listing, caveats: list[str]) -> Gate:
        pct = listing.seller_feedback_pct_x100
        score = listing.seller_feedback_score
        if pct is None and score is None:
            caveats.append("SELLER_DATA_MISSING")
            return Gate("SELLER", True, "no seller feedback returned by the API")
        problems = []
        if pct is not None and pct < C.MIN_SELLER_FEEDBACK_PCT_X100:
            problems.append(f"feedback {pct / 100:.2f}%")
        if score is not None and score < C.MIN_SELLER_FEEDBACK_SCORE:
            problems.append(f"{score} ratings")
        detail = ", ".join(problems) or (
            f"{(pct or 0) / 100:.2f}% over {score if score is not None else '?'} ratings"
        )
        return Gate("SELLER", not problems, detail)

    def _value(
        self,
        listing: Listing,
        match: Match,
        gates: list[Gate],
        caveats: list[str],
        unknown: list[str],
        condition: str | None,
    ) -> Assessment:
        ref = match.reference
        low, high = self.catalogue.band_for(match)

        if match.ambiguous_with:
            caveats.append("AMBIGUOUS_MATCH")
        if not ref.verified:
            caveats.append("FMV_UNVERIFIED")
        if ref.is_band or low != high:
            caveats.append("VARIANT_UNRESOLVED")
        if listing.is_auction and not self.increments.verified:
            caveats.append("BID_INCREMENTS_UNVERIFIED")

        scope = read_scope(listing.title)
        bracelet = read_bracelet(listing.title)
        if scope is None:
            unknown.append("scope")
        if bracelet is None:
            unknown.append("bracelet")

        inbound = listing.shipping
        if inbound is None:
            caveats.append("POSTAGE_UNKNOWN")

        pess = self._scenario(
            "pessimistic", low, condition, scope, bracelet, C.PESSIMISTIC_UNKNOWN,
            listing, inbound,
        )
        opt = self._scenario(
            "optimistic", high, condition, scope, bracelet, C.OPTIMISTIC_UNKNOWN,
            listing, inbound,
        )

        price, basis = self._effective_price(listing)

        gates.append(
            Gate(
                "VIABLE",
                opt.mab > 0,
                f"optimistic MAB {opt.mab}p, pessimistic {pess.mab}p",
            )
        )
        gates.append(
            Gate(
                "PRICE",
                price is not None and price <= opt.mab,
                f"{basis} {price}p vs optimistic MAB {opt.mab}p"
                if price is not None
                else "no price on the listing",
            )
        )

        assessment = self._finish(listing, gates, caveats, unknown, match)
        assessment.fmv_low = low
        assessment.fmv_high = high
        assessment.fmv_verified = ref.verified
        assessment.pessimistic = pess
        assessment.optimistic = opt
        assessment.effective_price = price
        assessment.price_basis = basis

        # A listing that clears every gate is either a deal on the worst
        # reading of its unknowns, or a deal only if those unknowns fall the
        # right way. Those are different things and the operator is told which.
        if assessment.verdict == "PASS" and price is not None and price > pess.mab:
            assessment.verdict = "DEPENDS_ON_UNKNOWNS"
            assessment.primary_reason = (
                "Clears the optimistic maximum bid but not the pessimistic one. "
                "Whether this is a deal depends on "
                + (", ".join(unknown) if unknown else "an unresolved variant")
                + "."
            )
        return assessment

    def _scenario(
        self,
        label: str,
        fmv_reference: Pence,
        condition: str | None,
        scope: str | None,
        bracelet: str | None,
        defaults: dict[str, str],
        listing: Listing,
        inbound: Pence | None,
    ) -> Scenario:
        cond = condition or defaults["condition"]
        scp = scope or defaults["scope"]
        brc = bracelet or defaults["bracelet"]
        mult = compose_bp(
            C.COND_MULT.get(cond, C.COND_MULT["GOOD"]),
            C.SCOPE_MULT.get(scp, C.SCOPE_MULT["WATCH_ONLY"]),
            C.BRACELET_MULT.get(brc, C.BRACELET_MULT["AFTERMARKET"]),
        )
        effective = (fmv_reference * mult) // 10_000
        bid = max_allowable_bid(
            effective,
            business_seller=listing.is_business_seller,
            inbound_postage=inbound,
        )
        return Scenario(
            label=label,
            fmv_reference=fmv_reference,
            condition=cond,
            scope=scp,
            bracelet=brc,
            multiplier_bp=mult,
            effective_fmv=effective,
            bid=bid,
        )

    def _effective_price(self, listing: Listing) -> tuple[Pence | None, str]:
        """What it would cost to be the winning party right now.

        For a Buy It Now that is the asking price. For an auction it is the
        next valid bid, which is a whole increment above the current one — a
        listing can sit under the maximum allowable bid and still be
        unreachable at the next step.

        Postage is not added here. It is subtracted inside the maximum
        allowable bid instead, so it is counted exactly once.
        """
        if listing.price is None:
            return None, "unpriced"
        if listing.is_auction:
            return (
                self.increments.next_bid(listing.price, listing.bid_count or 0),
                "next bid",
            )
        return listing.price, "buy it now"

    def _finish(
        self,
        listing: Listing,
        gates: list[Gate],
        caveats: list[str],
        unknown: list[str],
        match: Match | None,
    ) -> Assessment:
        by_name = {g.name: g for g in gates}
        failed = [n for n in GATE_ORDER if n in by_name and not by_name[n].passed]
        if failed:
            first = failed[0]
            verdict = f"REJECT_{first}"
            reason = f"{REASON_TEXT[first]} {by_name[first].detail}"
        else:
            verdict = "PASS"
            reason = "Clears every gate on the pessimistic valuation."

        return Assessment(
            listing=listing,
            verdict=verdict,
            primary_reason=reason,
            gates=[by_name[n] for n in GATE_ORDER if n in by_name],
            caveats=sorted(set(caveats)),
            unknown_fields=unknown,
            catalogue_key=match.reference.key if match else "",
            catalogue_display=match.reference.display if match else "",
            match_how=match.how if match else "",
            match_evidence=match.evidence if match else "",
            ambiguous_with=match.ambiguous_with if match else [],
            config_fingerprint=C.valuation_fingerprint(),
        )
