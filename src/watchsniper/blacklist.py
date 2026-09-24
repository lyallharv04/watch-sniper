"""Negation-aware phrase matching, and the eBay bid increment table.

Both live here because both are small lookup tables loaded from TOML that the
valuation consults; neither owns a number that appears anywhere else.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config as C
from .money import Pence, parse_gbp


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    negations: tuple[re.Pattern[str], ...]
    notes: str


@dataclass(frozen=True)
class Hit:
    rule_id: str
    matched: str
    context: str


class Blacklist:
    def __init__(self, rules: list[Rule], window: int):
        self.rules = rules
        self.window = window

    @classmethod
    def load(cls, path: Path | None = None) -> Blacklist:
        raw = tomllib.loads((path or C.BLACKLIST_PATH).read_text(encoding="utf-8"))
        flags = re.IGNORECASE | re.MULTILINE
        rules = [
            Rule(
                id=r["id"],
                pattern=re.compile(r["pattern"], flags),
                negations=tuple(re.compile(n, flags) for n in r.get("negations", ())),
                notes=r.get("notes", ""),
            )
            for r in raw.get("rule", [])
        ]
        return cls(rules, int(raw.get("window_chars", 40)))

    def check(self, text: str) -> list[Hit]:
        """Every rule that fires on `text`, negations respected.

        A rule fires when its pattern matches somewhere that is NOT covered by
        one of its negations. The negation is searched in a window either side
        of the match rather than in the whole text, so a listing that says
        "not a replica" in one sentence and "replica dial fitted" in another
        still drops on the second.
        """
        hits: list[Hit] = []
        for rule in self.rules:
            for m in rule.pattern.finditer(text):
                lo = max(0, m.start() - self.window)
                hi = min(len(text), m.end() + self.window)
                window = text[lo:hi]
                if any(n.search(window) for n in rule.negations):
                    continue
                hits.append(
                    Hit(rule_id=rule.id, matched=m.group(0), context=window.strip())
                )
                break
        return hits


# --------------------------------------------------------------------------
# Bid increments
# --------------------------------------------------------------------------


class Increments:
    def __init__(self, tiers: list[tuple[Pence, Pence | None, Pence]], verified: bool):
        self.tiers = tiers
        self.verified = verified

    @classmethod
    def load(cls, path: Path | None = None) -> Increments:
        raw = tomllib.loads((path or C.INCREMENTS_PATH).read_text(encoding="utf-8"))
        tiers = []
        for t in raw.get("tier", []):
            upper = parse_gbp(t["to"]) if t.get("to") else None
            tiers.append((parse_gbp(t["from"]), upper, parse_gbp(t["increment"])))
        return cls(tiers, bool(raw.get("verified", False)))

    def increment_for(self, current: Pence) -> Pence:
        for lo, hi, inc in self.tiers:
            if current >= lo and (hi is None or current <= hi):
                return inc
        return self.tiers[-1][2] if self.tiers else 0

    def next_bid(self, current: Pence, bid_count: int) -> Pence:
        """What it would cost to be the leading bidder right now.

        With no bids yet the starting price itself is the bid; once there is a
        bid, the next valid one is a whole increment above.
        """
        if bid_count <= 0:
            return current
        return current + self.increment_for(current)
