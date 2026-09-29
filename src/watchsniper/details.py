"""The full item, as Browse getItem returns it, for the verification models.

The search summary carries a title and a picture. The models also need the
item specifics, the seller's description and a few more pictures, which only
getItem returns. It is fetched once per item on first need, and the fetch the
closing check already makes is stored rather than repeated.

Everything here that came from the seller is untrusted text. It is stored as
received and reaches a model only as tagged data (docs/LLM_CONTRACT.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from . import config as C
from .llm import Image


@dataclass
class ItemDetails:
    item_id: str
    fetched_at: datetime
    aspects: list[tuple[str, str]]
    description_text: str
    image_urls: list[str]
    raw: dict = field(default_factory=dict, repr=False)


def from_get_item(
    row: dict,
    *,
    fetched_at: datetime | None = None,
    max_chars: int = C.LLM_DESCRIPTION_CHARS,
) -> ItemDetails:
    """Map one getItem response. `localizedAspects` become (name, value)
    pairs; `description` is HTML-stripped and cut to `max_chars`; picture URLs
    are the main image then `additionalImages`, de-duplicated, in order."""
    raise NotImplementedError


def html_to_text(html: str, max_chars: int) -> str:
    """Visible text only: script, style and comments dropped, entities
    decoded, whitespace collapsed, cut to `max_chars`."""
    raise NotImplementedError


def sized_image_url(url: str, edge_px: int = C.LLM_IMAGE_EDGE_PX) -> str:
    """eBay picture URLs end in a size token such as `s-l1600`; ask for
    `edge_px` instead. A URL without the token is returned unchanged."""
    raise NotImplementedError


def download_image(
    url: str,
    *,
    timeout: float = C.LLM_TIMEOUT_SECONDS,
    max_bytes: int = C.LLM_IMAGE_MAX_BYTES,
    opener=None,
) -> Image | None:
    """Fetch one picture over https from an eBay picture host only. None on
    any failure, a non-image content type, or a body over `max_bytes`."""
    raise NotImplementedError
