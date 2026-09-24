"""Getting the operator's attention, over a channel that needs no account.

ntfy.sh. You invent a topic name, the Android app subscribes to it, and this
process HTTP POSTs to it. There is no account, no API key, no VAPID keypair, no
service worker and no subscription that can silently expire — which between
them accounted for four of the fourteen components the previous attempt needed
to deliver one notification.

The cost of that simplicity: **a public ntfy topic is readable by anyone who
knows the topic name.** Nothing secret goes over it — the payload is a public
eBay listing and a price — but the topic name is the only thing standing
between your alerts and a stranger, so make it long and random. Self-hosting
ntfy on the same VPS later is a one-line change to NTFY_SERVER.

If NTFY_TOPIC is unset, notifications are disabled and everything else still
works. That is a deliberate degradation and it is stated on the dashboard, not
hidden.
"""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config as C
from .money import fmt
from .net import ssl_context
from .valuation import Assessment


@dataclass
class Notification:
    kind: str
    title: str
    body: str
    priority: str = "default"
    tags: str = ""
    click: str = ""
    item_id: str | None = None


class Notifier:
    """Sends notifications, or explains why it cannot."""

    def __init__(self, topic: str | None = None, server: str | None = None):
        self.topic = topic if topic is not None else C.NTFY_TOPIC
        self.server = (server or C.NTFY_SERVER or "https://ntfy.sh").rstrip("/")
        self._ssl = ssl_context()

    @property
    def enabled(self) -> bool:
        return bool(self.topic)

    @property
    def status(self) -> str:
        if not self.enabled:
            return (
                "disabled — NTFY_TOPIC is not set, so nothing will reach your "
                "phone. See .env.example."
            )
        return f"ntfy topic on {self.server} (topic name withheld)"

    def send(self, note: Notification) -> tuple[bool, str]:
        if not self.enabled:
            return False, "NTFY_TOPIC not set"
        headers = {
            "Title": note.title.encode("ascii", "replace").decode(),
            "Priority": note.priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if note.tags:
            headers["Tags"] = note.tags
        if note.click:
            headers["Click"] = note.click
        req = urllib.request.Request(
            f"{self.server}/{urllib.parse.quote(self.topic)}",
            data=note.body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15, context=self._ssl) as resp:
                return 200 <= resp.status < 300, f"HTTP {resp.status}"
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:200]}"
        except Exception as exc:  # network, TLS, DNS
            return False, f"{type(exc).__name__}: {exc}"


def alert_for(a: Assessment, dashboard_url: str = "") -> Notification:
    """Compose the alert for one listing worth looking at.

    Every alert states whether the FMV behind it is verified. An alert derived
    from an invented number that does not say so is worse than no alert,
    because it spends the operator's trust on a guess.
    """
    price = fmt(a.effective_price)
    mab = fmt(a.pessimistic.mab if a.pessimistic else None)
    head = fmt(a.headroom_pessimistic)

    if a.verdict == "PASS":
        title = f"{a.catalogue_display} at {price}"
        lead = f"Clears the pessimistic bid ceiling of {mab} by {head}."
        tags = "watch"
        priority = "high"
    else:
        title = f"Maybe: {a.catalogue_display} at {price}"
        lead = (
            f"Between the pessimistic ceiling {mab} and the optimistic "
            f"{fmt(a.optimistic.mab if a.optimistic else None)}. "
            f"Depends on {', '.join(a.unknown_fields) or 'the variant'}."
        )
        tags = "grey_question"
        priority = "default"

    lines = [
        lead,
        a.listing.title[:120],
        f"{'Auction' if a.listing.is_auction else 'Buy It Now'}"
        + (f", ends {a.listing.end_time_utc:%d %b %H:%M} UTC" if a.listing.end_time_utc else ""),
    ]
    if not a.fmv_verified:
        lines.append("FMV UNVERIFIED — this reference has never been checked "
                     "against a real sold price.")
    if "SELLER_DATA_MISSING" in a.caveats:
        lines.append("Seller feedback was not returned; check it yourself.")
    if dashboard_url:
        lines.append(dashboard_url)

    return Notification(
        kind="alert",
        title=title,
        body="\n".join(lines),
        priority=priority,
        tags=tags,
        click=a.listing.web_url,
        item_id=a.listing.item_id,
    )


def stalled(minutes: int) -> Notification:
    return Notification(
        kind="stalled",
        title="Watch sniper: ingestion has stopped",
        body=(
            f"No successful poll for {minutes} minutes. Nothing is being "
            "ingested, so the silence on this channel means nothing. Check the "
            "service on the VPS."
        ),
        priority="urgent",
        tags="warning",
    )


def recovered(minutes: int) -> Notification:
    return Notification(
        kind="recovered",
        title="Watch sniper: ingestion resumed",
        body=f"Polling is working again after {minutes} minutes down.",
        priority="default",
        tags="white_check_mark",
    )
