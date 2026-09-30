"""Shadow mode: ask the verification models, store what they said, change nothing.

DECISIONS.md A19 and CLAUDE.md §6a say why; docs/LLM_CONTRACT.md §8 is the
contract. This module decides *when* to ask and records the answer. It never
writes a verdict, never alerts and never feeds the Observed column — the
rules pipeline is still what the operator acts on, so the outcome log still
measures the rules.

Every answer is bought once. The cache key is the item, the stage, the model,
the prompt version and a hash of the candidate set, so a sweep that sees the
same listing every ninety seconds asks nothing new, and a catalogue edit that
changes the candidates makes the old answer visibly stale rather than reused.
"""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable
from dataclasses import dataclass

from . import config as C
from .details import ItemDetails, download_image, sized_image_url
from .llm import (
    CONFIDENCE,
    PROMPT_VERSION,
    Candidate,
    LlmClient,
    LlmInput,
    LlmResult,
    candidate_hash,
)
from .models import Listing
from .money import div_bp, median_pence
from .valuation import grade_of, lower_grade


def llm_input(
    listing: Listing,
    details: ItemDetails,
    candidates: tuple[Candidate, ...],
    fetch_image: Callable[[str], object] = download_image,
) -> LlmInput:
    """Everything a model sees. Pictures are downloaded here, by this
    process, at the size LLM_IMAGE_EDGE_PX asks for; a picture that fails is
    skipped rather than failing the call."""
    images = []
    for url in details.image_urls:
        if len(images) >= C.LLM_MAX_IMAGES:
            break
        img = fetch_image(sized_image_url(url)) or fetch_image(url)
        if img is not None:
            images.append(img)
    return LlmInput(
        item_id=listing.item_id,
        title=listing.title,
        condition_raw=listing.condition_raw,
        aspects=tuple(details.aspects),
        description=details.description_text,
        images=tuple(images),
        candidates=candidates,
    )


class Shadow:
    """Built by the Engine when LLM_MODE is "shadow". `engine` supplies the
    database, the current Valuer (it changes when the catalogue is reloaded)
    and `item_details`."""

    def __init__(
        self,
        engine,
        client: LlmClient,
        *,
        models: tuple[str, ...] = C.LLM_SHADOW_MODELS,
        escalation: str | None = C.LLM_ESCALATION_MODEL,
        fetch_image: Callable[[str], object] = download_image,
    ):
        self.engine = engine
        self.client = client
        self.models = models
        self.escalation = escalation
        self.fetch_image = fetch_image
        self.last_error: str | None = None

    @property
    def db(self):
        return self.engine.db

    # -- entry points ------------------------------------------------------

    def run(
        self, listing: Listing, stage: str, details: ItemDetails | None = None
    ) -> list[int]:
        """Ask whatever has not been asked. Returns the ids of stored rows.

        Never raises: this runs inline in a sweep, and a failure here must
        leave the sweep — and the rules verdict — exactly as they were.
        """
        try:
            return self._run(listing, stage, details)
        except Exception:
            self.last_error = traceback.format_exc(limit=3)
            return []

    # -- the work ----------------------------------------------------------

    def _run(
        self, listing: Listing, stage: str, details: ItemDetails | None
    ) -> list[int]:
        valuer = self.engine.valuer
        refs = valuer.catalogue.candidates(listing.title, C.LLM_CANDIDATES)
        if not refs:
            return []
        candidates = tuple(Candidate(r.key, r.brand, r.model, r.notes) for r in refs)
        chash = candidate_hash(candidates)

        todo = [
            m for m in self.models
            if self.client.available(m) and self._wanted(listing.item_id, stage, m, chash)
        ]
        want_escalation = bool(
            self.escalation
            and self.client.available(self.escalation)
            and self._wanted(listing.item_id, stage, self.escalation, chash)
        )
        if not todo and not (want_escalation and self._lowest(listing, stage, chash)):
            return []
        # Checked before anything is downloaded: at the cap the answer would
        # be "skipped" anyway, and the pictures would be fetched for nothing.
        if self.db.llm_spent_today() >= C.LLM_DAILY_SPEND_CAP_MICRO_USD:
            return []

        details = details or self.engine.item_details(listing.item_id)
        if details is None:
            return []
        inp = llm_input(listing, details, candidates, self.fetch_image)

        stored = []
        for model in todo:
            row_id = self._ask(listing, stage, model, inp, None)
            if row_id is not None:
                stored.append(row_id)

        lowest = self._lowest(listing, stage, chash) if want_escalation else None
        if lowest is not None:
            row_id = self._ask(listing, stage, self.escalation, inp, lowest["id"])
            if row_id is not None:
                stored.append(row_id)
        if stage == "listing":
            self.db.set_veto(listing.item_id, self.veto_reasons(listing.item_id, chash))
        return stored

    def veto_reasons(self, item_id: str, chash: str) -> list[dict] | None:
        """Both shadow models' reasons, when both current answers would be
        REJECT_LLM with high confidence; otherwise None (DECISIONS.md A20).
        Only answers under the current prompt and candidate set count, and
        the escalation model has no vote."""
        rows = self.db.llm_ok_results(item_id, "listing", PROMPT_VERSION, chash)
        return veto_from(rows, self.models)

    def _lowest(self, listing: Listing, stage: str, chash: str):
        """The least confident shadow answer below `high`, if there is one:
        the reason to ask the escalation model, and what it escalates from."""
        below = [
            r for r in self.db.llm_ok_results(listing.item_id, stage, PROMPT_VERSION, chash)
            if r["model"] in self.models and r["confidence"] != "high"
        ]
        return max(below, key=lambda r: CONFIDENCE.index(r["confidence"])) if below else None

    def _wanted(self, item_id: str, stage: str, model: str, chash: str) -> bool:
        ok, failed = self.db.llm_attempts(item_id, stage, model, PROMPT_VERSION, chash)
        return not ok and failed < C.LLM_MAX_ATTEMPTS

    def _ask(
        self,
        listing: Listing,
        stage: str,
        model: str,
        inp: LlmInput,
        escalated_from: int | None,
    ) -> int | None:
        result = self.client.identify(model, inp)
        if result.skipped:
            return None
        if not result.ok:
            self.last_error = f"{model}: {result.error}"
        verdict, mab, reason = self.would(listing, stage, result)
        return self.db.save_llm_result(
            item_id=listing.item_id,
            stage=stage,
            inp=inp,
            result=result,
            escalated_from=escalated_from,
            would_verdict=verdict,
            would_mab=mab,
            would_reason=reason,
        )

    def would(self, listing: Listing, stage: str, result: LlmResult):
        """What live mode would have said. Listing stage only: at close the
        question is identification, for the Observed column, not a verdict."""
        if stage != "listing":
            return None, None, "closing check: identification only"
        return self.engine.valuer.would_verdict(
            listing,
            ok=result.ok,
            catalogue_key=result.catalogue_key,
            confidence=result.confidence,
            condition=result.condition,
            evidence_verified=result.evidence_verified,
            red_flags=result.red_flags,
        )


def veto_from(rows, models: tuple[str, ...]) -> list[dict] | None:
    """The veto rule on stored rows: each model's latest answer must be
    high confidence and would-be REJECT_LLM. A model with no answer, a
    failed call or any other verdict means no veto — the alert goes out."""
    latest = {}
    for r in rows:
        if r["model"] in models:
            latest[r["model"]] = r
    if set(latest) != set(models):
        return None
    if all(r["confidence"] == "high" and r["would_verdict"] == "REJECT_LLM"
           for r in latest.values()):
        return [
            {"model": m, "reason": latest[m]["reason"], "why": latest[m]["would_reason"]}
            for m in models
        ]
    return None


def confident_key(answers: list, models: tuple[str, ...]) -> str | None:
    """The catalogue entry the shadow models confidently say an auction was.

    Each model's latest answer counts. Where both answered, both must be
    `high` and name the same entry; where only one answered, that one must be
    `high`. Anything else — a disagreement, a lower confidence, a null — is
    no confident identification. The escalation model has no say.
    """
    return confident_answer(answers, models)[0]


def confident_answer(answers: list, models: tuple[str, ...]) -> tuple[str | None, str | None]:
    """(entry, the models' condition reading) under the confident_key rule.
    Where the counting answers read the condition differently, the lower
    reading is the one returned."""
    latest = {}
    for a in answers:
        if a["model"] in models:
            latest[a["model"]] = a
    if not latest:
        return None, None
    keys = {a["catalogue_key"] for a in latest.values()}
    if len(keys) != 1 or None in keys:
        return None, None
    if any(a["confidence"] != "high" for a in latest.values()):
        return None, None
    reading = None
    for a in latest.values():
        reading = lower_grade(reading, _get(a, "condition"))
    return keys.pop(), reading


def _get(row, name):
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


@dataclass(frozen=True)
class ConfidentClosing:
    """One sold auction the shadow models confidently identified.
    `grade` is the lower of eBay's stated grade and the models' reading;
    `assumed` is true when neither gave one and GOOD was assumed."""

    key: str
    price: int
    grade: str
    assumed: bool


def confident_closings(db, models: tuple[str, ...] = C.LLM_SHADOW_MODELS):
    """(confident closings, per-entry count of Observed closings left out).
    Reads stored answers only; no model is called."""
    answers: dict[str, list] = {}
    for a in db.closing_answers():
        answers.setdefault(a["item_id"], []).append(a)
    found: list[ConfidentClosing] = []
    excluded: dict[str, int] = {}
    for c in db.sold_closings():
        key, reading = confident_answer(answers.get(c["item_id"], []), models)
        if key:
            grade = lower_grade(grade_of(c["condition_raw"], c["condition_id"]), reading)
            found.append(ConfidentClosing(key, c["final_price_pence"], grade or "GOOD", grade is None))
        rules = c["catalogue_key"]
        if rules and key != rules:
            excluded[rules] = excluded.get(rules, 0) + 1
    return found, excluded


def confident_observed(db, models: tuple[str, ...] = C.LLM_SHADOW_MODELS):
    """Per catalogue entry: (median, count, excluded) of sold auction
    closings the shadow models confidently identified as that entry.

    `excluded` counts the closings the existing Observed column counts for
    the entry — the rules gave it that reference — that are not confidently
    identified as it: unasked, unsure, or identified as something else. A
    closing can count towards an entry the rules did not give it. Reads
    stored answers only; no model is called.
    """
    found, excluded = confident_closings(db, models)
    prices: dict[str, list[int]] = {}
    for c in found:
        prices.setdefault(c.key, []).append(c.price)
    return {
        key: (median_pence(prices.get(key, [])), len(prices.get(key, [])),
              excluded.get(key, 0))
        for key in prices.keys() | excluded.keys()
    }


@dataclass(frozen=True)
class GradeSpread:
    grade: str
    median: int  # implied FMV, pence
    n: int
    assumed: int  # of n, how many had no grade and were taken as GOOD


@dataclass(frozen=True)
class Suggestion:
    value: int | None  # None until there are OBSERVED_MIN_AUCTIONS sales
    n: int
    diff_bp: int | None  # signed, of the current FMV point, towards zero
    by_grade: tuple[GradeSpread, ...]


def suggested_fmvs(catalogue, closings: list[ConfidentClosing]) -> dict[str, Suggestion]:
    """Per catalogue entry with confident sales: the FMV they imply.

    FMV is valued as MINT, and a sale at another grade is the FMV times that
    grade's COND_MULT. So each sale implies FMV = price / COND_MULT[grade],
    with the grade the lower of eBay's and the models' — the valuation's own
    rule — rounded down as money.py rounds a value. The suggestion is the
    median of those, once OBSERVED_MIN_AUCTIONS sales stand behind it; the
    spread by grade is always given, so a multiplier that is off shows as one
    grade sitting apart. A FOR_PARTS sale implies nothing (its multiplier is
    zero) and is left out. Display only: nothing reads this back.
    """
    implied: dict[str, list[tuple[str, bool, int]]] = {}
    for c in closings:
        mult = C.COND_MULT.get(c.grade, 0)
        if mult > 0:
            implied.setdefault(c.key, []).append((c.grade, c.assumed, div_bp(c.price, mult)))
    out = {}
    for ref in catalogue.references:
        rows = implied.get(ref.key)
        if not rows:
            continue
        spread = []
        for grade in sorted(C.COND_MULT, key=lambda g: -C.COND_MULT[g]):
            values = [v for g, _, v in rows if g == grade]
            if values:
                spread.append(GradeSpread(
                    grade, median_pence(values), len(values),
                    sum(1 for g, a, _ in rows if g == grade and a),
                ))
        value = median_pence([v for _, _, v in rows]) if len(rows) >= C.OBSERVED_MIN_AUCTIONS else None
        diff = None
        if value is not None and ref.point:
            delta = value - ref.point
            bp = abs(delta) * 10_000 // ref.point
            diff = bp if delta >= 0 else -bp
        out[ref.key] = Suggestion(value, len(rows), diff, tuple(spread))
    return out


def recompute_vetoes(engine) -> int:
    """Re-derive every veto from stored answers, after recompute_would."""
    from .db import listing_from_row

    items = [r["item_id"] for r in engine.db.query(
        "SELECT DISTINCT item_id FROM llm_results WHERE stage='listing'")]
    n = 0
    for item_id in items:
        row = engine.db.one("SELECT * FROM listings WHERE item_id=?", (item_id,))
        if row is None:
            continue
        refs = engine.valuer.catalogue.candidates(listing_from_row(row).title, C.LLM_CANDIDATES)
        chash = candidate_hash(tuple(Candidate(r.key, r.brand, r.model, r.notes) for r in refs))
        reasons = veto_from(
            engine.db.llm_ok_results(item_id, "listing", PROMPT_VERSION, chash),
            C.LLM_SHADOW_MODELS,
        )
        engine.db.set_veto(item_id, reasons)
        n += bool(reasons)
    return n


def recompute_would(engine) -> int:
    """Re-derive every stored would-verdict from its stored facts under the
    current constants and catalogue. Called by rescore; makes no model call."""
    from .db import listing_from_row

    n = 0
    for row in engine.db.llm_results_for_rescore():
        if row["stage"] != "listing":
            continue
        listing = listing_from_row(row)
        verdict, mab, reason = engine.valuer.would_verdict(
            listing,
            ok=bool(row["ok"]),
            catalogue_key=row["llm_key"],
            confidence=row["confidence"],
            condition=row["condition"],
            evidence_verified=bool(row["evidence_verified"]),
            red_flags=json.loads(row["red_flags_json"] or "[]"),
        )
        engine.db.update_llm_would(row["llm_id"], verdict, mab, reason)
        n += 1
    return n
