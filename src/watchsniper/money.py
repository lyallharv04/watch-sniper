"""Money is an integer number of pence. There is no float anywhere in this file
and no float may enter the valuation path through it.

Rounding policy (requirement 14). Every rounding choice below is made in the
direction that lowers the maximum allowable bid, so that a rounding error can
only ever cost an opportunity, never money:

    income and value      -> floor   (assume we realise less)
    costs and fees        -> ceil    (assume we pay more)
    required profit       -> ceil    (assume we need more)
    the final bid ceiling -> floor   (bid no more than exactly affordable)

Rates are integers in basis points (1 bp = 0.01%), never floats. 14.9% is 1490.
Multipliers are integers per 10,000. 0.88 is 8800.
"""

from __future__ import annotations

import re

Pence = int

_MONEY_RE = re.compile(r"^\s*£?\s*(-?)(\d+)(?:\.(\d{1,2}))?\s*$")


def parse_gbp(text: str | int) -> Pence:
    """Parse a human-written GBP amount into pence.

    Accepts "380", "380.00", "£380.5", "£380". Rejects anything else loudly —
    a silently mis-parsed price is a wrong bid.
    """
    if isinstance(text, int):
        return text * 100
    m = _MONEY_RE.match(str(text))
    if not m:
        raise ValueError(f"not a GBP amount: {text!r}")
    sign, whole, frac = m.groups()
    pence = int(whole) * 100 + int((frac or "0").ljust(2, "0"))
    return -pence if sign else pence


def parse_api_amount(value: str | None) -> Pence | None:
    """Parse an amount as eBay returns it: a decimal *string*, e.g. "199.99".

    Returns None for a missing value. eBay occasionally sends more than two
    decimal places on converted amounts; those are floored, which is the
    conservative direction for a price we are about to pay.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    neg = s.startswith("-")
    s = s.lstrip("+-")
    if "." in s:
        whole, _, frac = s.partition(".")
    else:
        whole, frac = s, ""
    if not whole.isdigit() or (frac and not frac.isdigit()):
        raise ValueError(f"not an API amount: {value!r}")
    pence = int(whole or "0") * 100 + int((frac + "00")[:2])
    return -pence if neg else pence


def fmt(pence: Pence | None) -> str:
    """Render pence for display. The only place money becomes a string."""
    if pence is None:
        return "—"
    sign = "-" if pence < 0 else ""
    p = abs(pence)
    return f"{sign}£{p // 100:,}.{p % 100:02d}"


def mul_bp(pence: Pence, bp: int) -> Pence:
    """pence x (bp / 10_000), rounded *down*. Use for value and income."""
    return (pence * bp) // 10_000


def mul_bp_ceil(pence: Pence, bp: int) -> Pence:
    """pence x (bp / 10_000), rounded *up*. Use for costs, fees and profit."""
    return -((-pence * bp) // 10_000)


def compose_bp(*multipliers: int) -> int:
    """Compose multipliers-per-10,000 without leaving integer arithmetic.

    compose_bp(8800, 8800) == 7744, i.e. 0.88 * 0.88 = 0.7744. Rounds down at
    each step, which lowers effective FMV — the conservative direction.
    """
    out = 10_000
    for m in multipliers:
        out = (out * m) // 10_000
    return out
