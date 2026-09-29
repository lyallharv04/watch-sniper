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
    def point(self) -> Pence:
        """The single FMV a valuation uses: the band's midpoint, rounded down."""
        return (self.fmv_low + self.fmv_high) // 2 if self.is_band else self.fmv

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


# Words that say nothing about which reference a listing is. A model string
# like "PRX (movement not stated)" is prose, and its prose words must not
# score at all.
_FILLER = frozenset({
    "the", "and", "with", "for", "not", "non", "stated", "variant", "movement",
    "modern", "line",
})
# Words that score but never qualify, whatever field they came from.
_WEAK = frozenset({
    "watch", "watches", "mens", "men", "gents", "ladies", "automatic", "auto",
    "jdm", "diver", "steel",
})
_MM = re.compile(r"(?<![a-z0-9.])\d+(?:\.\d+)? ?mm(?![a-z0-9])")
_SIZE = re.compile(r"^(\d{1,2}|\d+(\.\d+)? ?mm)$")  # "80", "38", "40mm", "35 mm"


def _has(hay: str, phrase: str) -> bool:
    """Whole-word containment on normalised text.

    Unlike `match`, which uses bare substrings, this refuses "integra" inside
    "integrated": candidates are scored on many short model words, and a
    substring rule would let every one of them misfire.
    """
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", hay) is not None


def _tokens(ref: Reference) -> tuple[frozenset[str], frozenset[str]]:
    """A reference's distinctive tokens, split into (strong, weak).

    Strong tokens identify the entry: its aliases, its key (unless synthetic —
    an invented key is never written in a title) and model-number words such
    as "c60" or "556". Weak tokens only separate siblings: `requires_any`
    qualifiers ("quartz", "titanium"), descriptive model words ("mechanical",
    "field"), sizes and short numbers. The brand's own words are removed from
    each, so "farer lander" contributes "lander" and a `requires_any` of
    "seiko" contributes nothing.
    """
    brand = set(_norm(ref.brand).split())
    ids = list(ref.aliases) + ([] if ref.synthetic_key else [_norm(ref.key)])
    words = _norm(ref.model).split()
    tagged = [(p, True) for p in ids] + [(p, False) for p in ref.requires_any]
    tagged += [(w, any(ch.isdigit() for ch in w)) for w in words]
    strong: set[str] = set()
    weak: set[str] = set()
    for phrase, identifies in tagged:
        kept = [w for w in phrase.split() if w not in brand]
        tok = " ".join(kept)
        if not tok or tok in _FILLER or (len(tok) < 3 and not tok.isdigit()):
            continue
        if identifies and tok not in _WEAK and not _SIZE.match(tok):
            strong.add(tok)
        else:
            weak.add(tok)
    return frozenset(strong), frozenset(weak - strong)


class Catalogue:
    def __init__(self, references: list[Reference], as_of: str = "", source: str = ""):
        self.references = references
        self.as_of = as_of
        self.source = source
        self.by_key = {r.key: r for r in references}
        self._tokens = {r.key: _tokens(r) for r in references}

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
        specific claim. Where two entries tie exactly, the first is used and
        the others are reported as ambiguous so the dashboard can say so.
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

    def candidates(self, title: str, n: int = 3) -> list[Reference]:
        """The up-to-n references a title is closest to, for a model to choose from.

        The rules match, if any, comes first. The rest are entries of a brand
        named in the title (or of the matched entry's brand), scored by how
        many of their distinctive tokens occur in it. `excludes` and
        `requires_any` do not disqualify: the point is to offer the siblings
        the rules ruled out — the Quartz when the title says Powermatic, and
        the reverse.

        Tokens are strong or weak (see `_tokens`). Every token that occurs
        scores one, but an entry is offered only if at least one strong token
        occurs: aliases and model numbers say which family a listing is,
        while qualifiers, sizes and descriptive words ("quartz", "40mm",
        "mechanical") only say which sibling. So "Hamilton Jazzmaster Quartz"
        offers nothing, and "Tissot PRX 40mm" offers the 40mm Powermatic and
        Quartz ahead of the other PRX entries. A family alias shared by every
        entry of a brand, such as "prx", is strong on purpose: it is how a
        title names the family, and the model is there to pick the variant.

        An entry whose case size contradicts the one the title states sorts
        after every entry that does not, so "PRX Powermatic 80 40mm" offers
        the 40mm Quartz before the 35mm Powermatic. Ties go to catalogue
        order. Pure; no I/O.
        """
        hay = _norm(title)
        m = self.match(title)
        brands = {r.brand for r in self.references if _has(hay, _norm(r.brand))}
        if m is not None:
            brands.add(m.reference.brand)
        if not brands:
            return []

        stated = {x.replace(" ", "") for x in _MM.findall(hay)}
        out: list[Reference] = [m.reference] if m is not None else []
        scored: list[tuple[bool, int, int, Reference]] = []
        for i, ref in enumerate(self.references):
            if ref.brand not in brands or (out and ref.key == out[0].key):
                continue
            strong, weak = self._tokens[ref.key]
            hits = sum(1 for t in strong if _has(hay, t))
            if not hits:
                continue
            hits += sum(1 for t in weak if _has(hay, t))
            sizes = {t.replace(" ", "") for t in weak if t.endswith("mm")}
            clash = bool(stated and sizes and not stated & sizes)
            scored.append((clash, -hits, i, ref))
        scored.sort(key=lambda t: t[:3])
        out += [t[3] for t in scored]
        return out[:n]

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
