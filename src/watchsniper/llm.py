"""The verification models: what goes in, what comes out, and what it costs.

Shadow mode only (DECISIONS.md A19). The contract every other module builds
against is in docs/LLM_CONTRACT.md; the shapes below are that contract in code.

Three rules this module exists to keep:

  * **Facts, never money.** A model names a catalogue candidate, a condition
    grade, labels and red flags. It is never shown a price or an FMV and no
    number it returns is used as money. valuation.py does all the arithmetic.
  * **Seller text is data.** Title, condition string, item specifics and
    description are written by the seller and may contain instructions. Each
    goes into the prompt inside tags carrying a random id generated per call,
    and the system prompt says text inside them must never be followed.
  * **Failure is rules-only.** A missing key, the daily spend cap, a timeout
    or an unparseable answer yields a result that says so. Nothing raises into
    the poller, and the rules verdict stands.

Transport is stdlib urllib (A1): no SDK, nothing to pip install.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from . import config as C

PROMPT_VERSION = "v1"

# Enums the model must answer in. Generated from the code that consumes them,
# so a prompt cannot drift from the enum it feeds (inherited D16).
CONFIDENCE = ("high", "medium", "low")
CONDITIONS = tuple(C.COND_MULT)  # MINT, EXCELLENT, GOOD, FAIR, FOR_PARTS
BOX_PAPERS = ("FULL_SET", "WATCH_PAPERS", "WATCH_BOX", "WATCH_ONLY")
BRACELETS = ("OEM_BRACELET", "OEM_STRAP", "AFTERMARKET")
EVIDENCE_SOURCES = ("title", "specific", "description")

STAGES = ("listing", "closing")


# --------------------------------------------------------------------------
# Input
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    """A catalogue reference offered to the model. No price, deliberately."""

    key: str
    brand: str
    model: str
    notes: str = ""


@dataclass(frozen=True)
class Image:
    media_type: str  # "image/jpeg" | "image/png" | "image/webp"
    data: bytes
    source_url: str = ""


@dataclass(frozen=True)
class LlmInput:
    item_id: str
    title: str
    condition_raw: str
    aspects: tuple[tuple[str, str], ...]
    description: str
    images: tuple[Image, ...]
    candidates: tuple[Candidate, ...]

    @property
    def candidate_hash(self) -> str:
        """Part of the cache key: a catalogue edit that changes the candidate
        set makes an earlier answer stale rather than silently reused."""
        return candidate_hash(self.candidates)

    @property
    def input_hash(self) -> str:
        """Everything the model saw, images included, plus the prompt version.
        Stored for audit; not the cache key (images are downloaded only on a
        cache miss)."""
        h = hashlib.sha256()
        h.update(PROMPT_VERSION.encode())
        h.update(
            json.dumps(
                [
                    self.item_id,
                    self.title,
                    self.condition_raw,
                    list(self.aspects),
                    self.description,
                    [c.__dict__ for c in self.candidates],
                ],
                sort_keys=True,
            ).encode()
        )
        for img in self.images:
            h.update(hashlib.sha256(img.data).digest())
        return h.hexdigest()[:16]


def candidate_hash(candidates: tuple[Candidate, ...] | list[Candidate]) -> str:
    payload = json.dumps([[c.key, c.notes] for c in candidates])
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

#: The JSON the model must return. `catalogue_key` is additionally required, in
#: code, to be one of the offered candidate keys.
OUTPUT_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "catalogue_key", "confidence", "condition", "box_papers", "bracelet",
        "evidence", "red_flags", "reason",
    ],
    "properties": {
        "catalogue_key": {"type": ["string", "null"]},
        "confidence": {"type": "string", "enum": list(CONFIDENCE)},
        "condition": {"type": ["string", "null"], "enum": [*CONDITIONS, None]},
        "box_papers": {"type": ["string", "null"], "enum": [*BOX_PAPERS, None]},
        "bracelet": {"type": ["string", "null"], "enum": [*BRACELETS, None]},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["source", "text"],
                "properties": {
                    "source": {"type": "string", "enum": list(EVIDENCE_SOURCES)},
                    "text": {"type": "string"},
                },
            },
        },
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
}


@dataclass
class LlmResult:
    """One attempted (or skipped) call. `skipped` set means no request was
    made and nothing is stored; `ok` False with `error` means a request was
    made and failed, and it is stored so Health can show it."""

    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION
    ok: bool = False
    skipped: str | None = None  # "no_api_key" | "spend_cap" | "mode_off"
    error: str | None = None
    catalogue_key: str | None = None
    confidence: str | None = None
    condition: str | None = None
    box_papers: str | None = None
    bracelet: str | None = None
    evidence: list[dict] = field(default_factory=list)
    evidence_verified: bool = False
    red_flags: list[str] = field(default_factory=list)
    reason: str = ""
    raw_response: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0
    latency_ms: int = 0


# --------------------------------------------------------------------------
# Transport and providers
# --------------------------------------------------------------------------


class Transport(Protocol):
    def post_json(
        self, url: str, headers: dict[str, str], body: dict, timeout: float
    ) -> tuple[int, dict]:
        """POST a JSON body; return (HTTP status, parsed JSON body). Raises
        only on a network failure or timeout (TimeoutError / OSError)."""
        ...


class Provider(Protocol):
    name: str  # "anthropic" | "gemini"

    def available(self) -> bool:
        """True when the key this provider needs is configured."""
        ...

    def call(
        self, model: str, inp: LlmInput, transport: Transport, tag_id: str
    ) -> LlmResult:
        """One request. Never raises; failures come back as ok=False."""
        ...


def provider_name(model: str) -> str:
    """"anthropic" for claude-*, "gemini" for gemini-*; ValueError otherwise."""
    raise NotImplementedError


def cost_micro_usd(model: str, input_tokens: int, output_tokens: int) -> int:
    """From C.LLM_PRICE_MICRO_USD_PER_MTOK, rounded up. An unpriced model
    raises KeyError: a call whose cost cannot be accounted is not made."""
    raise NotImplementedError


def system_prompt() -> str:
    raise NotImplementedError


def user_blocks(inp: LlmInput, tag_id: str) -> list[tuple[str, object]]:
    """Provider-neutral content: ("text", str) and ("image", Image) blocks,
    seller text wrapped in <seller-{tag_id} field="..."> ... </seller-{tag_id}>."""
    raise NotImplementedError


def parse_output(text: str, inp: LlmInput) -> dict:
    """Validate the model's JSON against OUTPUT_SCHEMA and the candidate set.
    Raises ValueError with a short reason on anything invalid."""
    raise NotImplementedError


def verify_evidence(evidence: list[dict], inp: LlmInput, catalogue_key: str | None) -> bool:
    """True only when code finds, in the named source of the input, at least
    one quoted evidence string that is not merely the brand name."""
    raise NotImplementedError


class LlmClient:
    """The one entry point the poller uses."""

    def __init__(
        self,
        *,
        spent_today: Callable[[], int],
        transport: Transport | None = None,
        providers: dict[str, Provider] | None = None,
        cap_micro_usd: int = C.LLM_DAILY_SPEND_CAP_MICRO_USD,
    ):
        raise NotImplementedError

    def available(self, model: str) -> bool:
        raise NotImplementedError

    def identify(self, model: str, inp: LlmInput) -> LlmResult:
        """Never raises. Skipped when the key is missing or today's spend has
        reached the cap; otherwise one request, accounted in cost_micro_usd."""
        raise NotImplementedError
