"""The UK fee stack, and the solver for the maximum allowable bid.

The important structural fact: on a private-seller purchase eBay charges the
*buyer* a tiered Buyer Protection fee that is a function of the purchase price.
The cost of bidding therefore depends on the bid, so the maximum allowable bid
must be **solved for**. Dividing a budget by a flat rate under-costs the
purchase, and under-costing is the direction that loses money.

Every rate used here is unverified. See config.UNVERIFIED.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config as C
from .money import Pence, mul_bp, mul_bp_ceil


def buyer_protection_fee(price: Pence, *, business_seller: bool) -> Pence:
    """eBay UK Buyer Protection fee on a purchase at `price`.

    Business sellers do not attract it: their price is VAT-inclusive and no
    buyer protection applies. Private sellers pay a fixed component plus a
    marginal rate that steps down through bands.

    Rounded up — a fee we underestimate is money we do not have.
    """
    if business_seller or price <= 0:
        return 0
    weighted = 0
    lower = 0
    for upper, bp in C.BUYER_PROTECTION_TIERS:
        band = max(0, min(price, upper) - lower)
        weighted += band * bp
        lower = upper
        if price <= upper:
            break
    return C.BUYER_PROTECTION_FIXED + -(-weighted // 10_000)


@dataclass(frozen=True)
class SellSide:
    """What is left after selling at `gross`, before paying for the watch."""

    gross: Pence
    platform_fees: Pence
    order_fee: Pence
    outbound_postage: Pence
    service_buffer: Pence
    net_proceeds: Pence
    lines: list[tuple[str, Pence]] = field(default_factory=list)


def sell_side(gross: Pence, *, service_buffer: Pence = 0) -> SellSide:
    """Net proceeds of selling one watch at `gross`.

    The final value fee, the regulatory operating fee and the promoted-listing
    rate are all percentages of the sale; the order fee is flat. VAT on eBay
    fees is a real cost because the operator is not VAT registered, so every
    fee is grossed up by FEE_VAT_MULT_BP rather than netted off.

    Costs round up.
    """
    rate_bp = C.FVF_BP + C.REG_OP_FEE_BP + C.AD_RATE_BP
    vatted_bp = (rate_bp * C.FEE_VAT_MULT_BP) // 10_000
    platform = mul_bp_ceil(gross, vatted_bp)
    order = mul_bp_ceil(C.ORDER_FEE, C.FEE_VAT_MULT_BP)
    net = gross - platform - order - C.OUTBOUND_POSTAGE - service_buffer
    return SellSide(
        gross=gross,
        platform_fees=platform,
        order_fee=order,
        outbound_postage=C.OUTBOUND_POSTAGE,
        service_buffer=service_buffer,
        net_proceeds=net,
        lines=[
            ("Sale price (effective FMV)", gross),
            (f"eBay fees @ {rate_bp / 100:.2f}% x VAT", -platform),
            ("Per-order fee inc. VAT", -order),
            ("Outbound postage", -C.OUTBOUND_POSTAGE),
            ("Service buffer", -service_buffer),
            ("Net proceeds", net),
        ],
    )


def required_profit(gross: Pence) -> Pence:
    """The profit floor. Rounds up: we would rather miss than under-earn."""
    return max(mul_bp_ceil(gross, C.TARGET_PROFIT_MARGIN_BP), C.MIN_ABSOLUTE_PROFIT)


@dataclass(frozen=True)
class MaxBid:
    amount: Pence
    acquisition_budget: Pence
    buyer_protection: Pence
    inbound_postage: Pence
    required_profit: Pence
    sell: SellSide
    lines: list[tuple[str, Pence]] = field(default_factory=list)


def max_allowable_bid(
    effective_fmv: Pence,
    *,
    business_seller: bool,
    inbound_postage: Pence | None = None,
    service_buffer: Pence = 0,
) -> MaxBid:
    """The most that can be paid for the watch and still clear the profit floor.

    Solved, not divided. `bid + inbound_postage + buyer_protection(bid)` must
    fit inside the acquisition budget, and buyer protection depends on the bid.

    `inbound_postage` is the listing's own stated postage where it has one;
    INBOUND_POSTAGE_ESTIMATE is the fallback when it does not. Passing the real
    figure keeps the postage from being counted once in the price the operator
    sees and again in the budget.

    The solve is an integer bisection rather than a closed form per fee tier.
    `bid + buyer_protection(bid)` is strictly increasing in `bid`, so bisection
    is exact to the penny; a closed form would be marginally faster and would
    have three tier boundaries to get wrong. Nothing here is hot enough to
    justify that trade.
    """
    inbound = C.INBOUND_POSTAGE_ESTIMATE if inbound_postage is None else inbound_postage
    sell = sell_side(effective_fmv, service_buffer=service_buffer)
    profit = required_profit(effective_fmv)
    budget = sell.net_proceeds - profit
    spendable = budget - inbound

    if spendable <= 0:
        return MaxBid(
            amount=0,
            acquisition_budget=budget,
            buyer_protection=0,
            inbound_postage=inbound,
            required_profit=profit,
            sell=sell,
            lines=_lines(sell, profit, budget, inbound, 0, 0),
        )

    lo, hi = 0, spendable
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if mid + buyer_protection_fee(mid, business_seller=business_seller) <= spendable:
            lo = mid
        else:
            hi = mid - 1

    bp = buyer_protection_fee(lo, business_seller=business_seller)
    return MaxBid(
        amount=lo,
        acquisition_budget=budget,
        buyer_protection=bp,
        inbound_postage=inbound,
        required_profit=profit,
        sell=sell,
        lines=_lines(sell, profit, budget, inbound, bp, lo),
    )


def _lines(
    sell: SellSide,
    profit: Pence,
    budget: Pence,
    inbound: Pence,
    bp: Pence,
    bid: Pence,
) -> list[tuple[str, Pence]]:
    return sell.lines + [
        ("Required profit", -profit),
        ("Acquisition budget", budget),
        ("Inbound postage", -inbound),
        ("Buyer protection at this bid", -bp),
        ("Maximum allowable bid", bid),
    ]


def effective_fmv(fmv_reference: Pence, *multipliers_bp: int) -> Pence:
    """Apply condition / scope / bracelet multipliers to a reference FMV.

    Rounds down at each step, which lowers the bid ceiling.
    """
    out = fmv_reference
    for m in multipliers_bp:
        out = mul_bp(out, m)
    return out
