"""The full item, as Browse getItem returns it, for the verification models.

The search summary carries a title and a picture. The models also need the
item specifics, the seller's description and a few more pictures, which only
getItem returns. It is fetched once per item on first need, and the fetch the
closing check already makes is stored rather than repeated.

Everything here that came from the seller is untrusted text. It is stored as
received and reaches a model only as tagged data (docs/LLM_CONTRACT.md).
"""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

from . import config as C
from .llm import Image
from .models import utcnow

#: The one host pictures are fetched from.
IMAGE_HOST = "i.ebayimg.com"
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"})

_BLOCK_TAGS = frozenset(
    {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6",
     "ul", "ol", "table", "section", "article", "blockquote", "hr"}
)
_NL = "\n"
_WS = re.compile(r"\s+")
_HIDDEN_TAGS = frozenset({"script", "style", "noscript", "template", "head", "title"})
_SIZE_TOKEN = re.compile(r"(?<=/)s-l\d+(?=\.(?:jpe?g|png|webp|gif)(?:$|[?#]))", re.I)


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
    aspects: list[tuple[str, str]] = []
    for a in row.get("localizedAspects") or []:
        if not isinstance(a, dict):
            continue
        name = str(a.get("name") or "").strip()
        value = str(a.get("value") or "").strip()
        if name:
            aspects.append((name, value))

    html = row.get("description") or row.get("shortDescription") or ""
    text = html_to_text(str(html), max_chars)

    urls: list[str] = []
    candidates = [row.get("image")] + list(row.get("additionalImages") or [])
    for img in candidates:
        url = img.get("imageUrl") if isinstance(img, dict) else None
        if url and url not in urls:
            urls.append(url)

    return ItemDetails(
        item_id=str(row.get("itemId") or ""),
        fetched_at=fetched_at or utcnow(),
        aspects=aspects,
        description_text=text,
        image_urls=urls,
        raw=row,
    )


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in _HIDDEN_TAGS:
            self._hidden += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(_NL)

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append(_NL)

    def handle_endtag(self, tag):
        if tag in _HIDDEN_TAGS:
            self._hidden = max(0, self._hidden - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(_NL)

    def handle_data(self, data):
        # A line break in the source is whitespace; only tags break lines.
        if not self._hidden:
            self.parts.append(_WS.sub(" ", data))

    # Comments, declarations and processing instructions are dropped by
    # not overriding their handlers.


def end_state(row: dict, now: datetime) -> tuple[datetime | None, str | None]:
    """(when it ended, 'sold' | 'unsold'), or (None, None) while it is live.

    Measured on 91 live getItem responses (2026-09-29): a live Buy It Now
    carries no `itemEndDate` at all; an ended one carries it in the past, and
    `estimatedSoldQuantity` above zero says it sold. The availability status
    alone is not enough — ended auctions still report IN_STOCK — so it only
    settles a listing that is sold out without an end date.
    """
    from .models import parse_ts

    ended = parse_ts(row.get("itemEndDate"))
    availability = (row.get("estimatedAvailabilities") or [{}])[0] or {}
    sold = int(availability.get("estimatedSoldQuantity") or 0) > 0
    if ended is not None and ended <= now:
        return ended, "sold" if sold else "unsold"
    if availability.get("estimatedAvailabilityStatus") == "OUT_OF_STOCK" and sold:
        return None, "sold"
    return None, None


def html_to_text(html: str, max_chars: int) -> str:
    """Visible text only: script, style and comments dropped, entities
    decoded, whitespace collapsed, cut to `max_chars`. Block-level tags
    become line breaks. Malformed markup degrades; it never raises."""
    if not html:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # html.parser is lenient; this is belt and braces
        pass
    lines = []
    for line in "".join(parser.parts).split(_NL):
        line = " ".join(line.split())
        if line:
            lines.append(line)
    text = _NL.join(lines)
    return text[: max(0, max_chars)].rstrip()


def sized_image_url(url: str, edge_px: int = C.LLM_IMAGE_EDGE_PX) -> str:
    """eBay picture URLs end in a size token such as `s-l1600`; ask for
    `edge_px` instead. A URL without the token is returned unchanged."""
    return _SIZE_TOKEN.sub(f"s-l{int(edge_px)}", url, count=1)


def _urllib_open(url: str, timeout: float, limit: int) -> tuple[str, bytes]:
    from .net import ssl_context

    req = urllib.request.Request(url, headers={"User-Agent": "watchsniper"})
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
        # A redirect off the picture host is not followed into.
        final = urllib.parse.urlsplit(resp.geturl())
        if final.scheme != "https" or final.hostname != IMAGE_HOST:
            raise ValueError("redirected off the picture host")
        return resp.headers.get("Content-Type", ""), resp.read(limit)


def download_image(
    url: str,
    *,
    timeout: float = C.LLM_TIMEOUT_SECONDS,
    max_bytes: int = C.LLM_IMAGE_MAX_BYTES,
    opener=None,
) -> Image | None:
    """Fetch one picture over https from an eBay picture host only. None on
    any failure, a non-image content type, or a body over `max_bytes`."""
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname != IMAGE_HOST or parts.port:
            return None
        if opener is None:
            content_type, data = _urllib_open(url, timeout, max_bytes + 1)
        else:
            content_type, data = opener(url, timeout)
        media_type = str(content_type or "").split(";")[0].strip().lower()
        if media_type == "image/jpg":
            media_type = "image/jpeg"
        if media_type not in IMAGE_TYPES:
            return None
        data = bytes(data or b"")
        if not data or len(data) > max_bytes:
            return None
        return Image(media_type=media_type, data=data, source_url=url)
    except Exception:
        return None
