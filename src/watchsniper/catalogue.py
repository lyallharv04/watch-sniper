"""Loading and matching the FMV catalogue.

The catalogue is one human-editable TOML file (requirement 22). It is read at
start-up and on demand; nothing copies it into the database, so the file stays
the single owner of every FMV and editing it is the whole workflow.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C
from .money import Pence, parse_gbp


@dataclass(frozen=True)
class Reference:
    key: str
    brand: str
    model: str
    fmv: Pence
    fmv_low: Pence
    fmv_high: Pence
    verified: bool
    priority: int
    synthetic_key: bool
    aliases: tuple[str, ...]
    requires_any: tuple[str, ...]
    excludes: tuple[str, ...]
    notes: str

    @property
    def is_band(self) -> bool:
        return self.fmv_low != self.fmv_high

    @property
    def display(self) -> str:
        return f"{self.brand} {self.model}"


@dataclass
class Match:
    reference: Reference
    how: str  # "key" | "alias"
    evidence: str
    ambiguous_with: list[str] = field(default_factory=list)


def _norm(text: str) -> str:
    """Lowercase, strip punctuation to spaces, collapse runs of whitespace.

    Matching happens on this form, so an alias containing punctuation must be
    normalised the same way or it can never match.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9.]+", " ", text.lower())).strip()


class Catalogue:
    def __init__(self, references: list[Reference], as_of: str = "", source: str = ""):
        self.references = references
        self.as_of = as_of
        self.source = source
        self.by_key = {r.key: r for r in references}

    @classmethod
    def load(cls, path: Path | None = None) -> Catalogue:
        path = path or C.CATALOGUE_PATH
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        refs: list[Reference] = []
        seen: set[str] = set()
        for row in raw.get("reference", []):
            key = row["key"]
            if key in seen:
                raise ValueError(f"duplicate catalogue key: {key}")
            seen.add(key)
            fmv = parse_gbp(row["fmv"])
            low = parse_gbp(row["fmv_low"]) if "fmv_low" in row else fmv
            high = parse_gbp(row["fmv_high"]) if "fmv_high" in row else fmv
            if not (low <= fmv <= high):
                raise ValueError(
                    f"{key}: fmv must sit inside fmv_low..fmv_high "
                    f"({low} <= {fmv} <= {high} is false)"
                )
            refs.append(
                Reference(
                    key=key,
                    brand=row["brand"],
                    model=row["model"],
                    fmv=fmv,
                    fmv_low=low,
                    fmv_high=high,
                    verified=bool(row.get("verified", False)),
                    priority=int(row.get("priority", 10)),
                    synthetic_key=bool(row.get("synthetic_key", False)),
                    aliases=tuple(_norm(a) for a in row.get("aliases", ())),
                    requires_any=tuple(_norm(a) for a in row.get("requires_any", ())),
                    excludes=tuple(_norm(a) for a in row.get("excludes", ())),
                    notes=row.get("notes", ""),
                )
            )
        return cls(refs, raw.get("as_of", ""), raw.get("source", ""))

    @property
    def unverified_count(self) -> int:
        return sum(1 for r in self.references if not r.verified)

    def match(self, title: str) -> Match | None:
        """Resolve a listing title to a catalogue reference.

        A candidate must clear three tests: no `excludes` term present, at
        least one `requires_any` term present when the list is non-empty, and
        either its key or one of its aliases present.

        Scoring is `priority` first, then how the match was made, then the
        length of the longest alias that matched — a longer alias is a more
        specific claim. Where two entries tie exactly, the result is reported
        as ambiguous and the caller widens the valuation band to cover both
        rather than silently picking one.
        """
        hay = _norm(title)
        scored: list[tuple[tuple[int, int, int], Reference, str, str]] = []

        for ref in self.references:
            if any(x and x in hay for x in ref.excludes):
                continue
            if ref.requires_any and not any(x in hay for x in ref.requires_any):
                continue

            key_norm = _norm(ref.key)
            if not ref.synthetic_key and key_norm and key_norm in hay:
                scored.append(((ref.priority, 2, len(key_norm)), ref, "key", ref.key))
                continue

            hits = [a for a in ref.aliases if a and a in hay]
            if hits:
                best = max(hits, key=len)
                scored.append(((ref.priority, 1, len(best)), ref, "alias", best))

        if not scored:
            return None

        scored.sort(key=lambda t: t[0], reverse=True)
        top = scored[0]
        tied = [s for s in scored if s[0] == top[0]]
        return Match(
            reference=top[1],
            how=top[2],
            evidence=top[3],
            ambiguous_with=[s[1].key for s in tied[1:]],
        )

    def missing_worklist(
        self, titles: list[str], limit: int = 40
    ) -> list[tuple[str, int, list[str]]]:
        """Group unpriced listing titles into "references you have not added".

        Requirement 22 asks for curation to be a short sitting rather than a
        chore, and the hardest part of curating is not filling a number in —
        it is working out which twenty rows are worth the effort. This groups
        the listings that matched nothing by brand and the model word that
        follows it, so the worklist is ordered by real UK traffic.

        The grouping is a crude heuristic on purpose. It is a suggestion for
        where to look next, never an input to a valuation, so being wrong
        about a group costs a glance.
        """
        brands = sorted(
            {r.brand.lower() for r in self.references}, key=len, reverse=True
        )
        stop = {
            "mens", "men", "s", "gents", "gent", "ladies", "lady", "watch",
            "watches", "automatic", "auto", "quartz", "swiss", "made", "vintage",
            "rare", "new", "used", "boxed", "the", "and", "with", "for",
        }
        groups: dict[str, list[str]] = {}
        for title in titles:
            hay = _norm(title)
            brand = next((b for b in brands if b in hay), None)
            if brand is None:
                key = "(brand not recognised)"
            else:
                tail = hay.split(brand, 1)[1].split()
                words = [w for w in tail if w not in stop and not w.isdigit()][:2]
                key = f"{brand.title()} {' '.join(words)}".strip() or brand.title()
            groups.setdefault(key, []).append(title)
        ranked = sorted(groups.items(), key=lambda kv: -len(kv[1]))
        return [(k, len(v), v[:3]) for k, v in ranked[:limit]]

    def band_for(self, match: Match) -> tuple[Pence, Pence]:
        """The FMV band to value against, widened across an ambiguous match.

        An ambiguous match is a genuine data gap, not a coin toss. Widening
        makes it visible in the dashboard as a wide band instead of hiding it
        behind an arbitrary pick.
        """
        low, high = match.reference.fmv_low, match.reference.fmv_high
        for key in match.ambiguous_with:
            other = self.by_key[key]
            low = min(low, other.fmv_low)
            high = max(high, other.fmv_high)
        return low, high
