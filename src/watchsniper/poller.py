"""Ingestion, scoring and the deadman switch.

Two loops in two threads. Buy It Now listings are polled fast, because a
genuinely underpriced one is gone in minutes; auctions are polled slowly,
because they have hours of runway and, since eBay bids by proxy, nothing is
gained by watching one closely. A third thread does nothing but notice when the
other two have stopped.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass
from datetime import timezone

from . import config as C
from . import notify
from .blacklist import Blacklist, Increments
from .catalogue import Catalogue
from .db import Database
from .ebay import BudgetExhausted, EbayClient, EbayError
from .models import from_item_summary, utcnow
from .valuation import Assessment, Valuer


def _today() -> str:
    return utcnow().strftime("%Y-%m-%d")


@dataclass
class PollResult:
    kind: str
    items_seen: int = 0
    items_new: int = 0
    alerts: int = 0
    http_calls: int = 0
    error: str | None = None


class Engine:
    """Everything the poller and the dashboard both need, built once."""

    def __init__(self, db: Database, client: EbayClient | None = None):
        self.db = db
        self.catalogue = Catalogue.load()
        self.blacklist = Blacklist.load()
        self.increments = Increments.load()
        self.valuer = Valuer(self.catalogue, self.blacklist, self.increments)
        self.client = client
        self.notifier = notify.Notifier()
        self.started_at = utcnow()
        self._stop = threading.Event()
        self._stall_notified = False
        self.last_error: str | None = None
        if db.verdicts_dropped:
            self.rescore_all()

    def reload_catalogue(self) -> None:
        """Pick up an edit to catalogue.toml without a restart."""
        self.catalogue = Catalogue.load()
        self.valuer = Valuer(self.catalogue, self.blacklist, self.increments)
        self.db.audit("operator", "reload_catalogue", self.catalogue.as_of)

    # -- scoring -----------------------------------------------------------

    def score(self, listing) -> Assessment:
        assessment = self.valuer.assess(listing)
        self.db.save_verdict(assessment)
        return assessment

    def rescore_all(self) -> dict[str, int]:
        """Re-run every stored listing through the current constants.

        Free, offline, and the single most useful thing to run after editing
        the catalogue or a fee rate: it shows what the new number would have
        done to real traffic instead of to a hypothetical.
        """
        from .db import listing_from_row

        counts: dict[str, int] = {}
        for row in self.db.all_listings():
            a = self.score(listing_from_row(row))
            counts[a.verdict] = counts.get(a.verdict, 0) + 1
        self.db.audit("system", "rescore", C.valuation_fingerprint())
        return counts

    # -- one sweep ---------------------------------------------------------

    def poll_once(self, kind: str, pages: int = 1, notify: bool = True) -> PollResult:
        """One sweep. `pages` above 1 walks back through the standing stock.

        A single 200-row `newlyListed` page reaches roughly twelve days back at
        the measured arrival rate, so one page per sweep is ample for catching
        new Buy It Now listings; paging there is only for the initial seed.

        The auction sweep is sorted ending-soonest, so one page covers only the
        next few hundred endings. It keeps paging until the last row on a page
        ends beyond AUCTION_HORIZON, so every auction ending inside the horizon
        is seen on each sweep.
        """
        assert self.client is not None, "no eBay client configured"
        result = PollResult(kind=kind)
        run_id = self.db.start_poll(kind)
        before = self.client.budget.used
        horizon = utcnow() + C.AUCTION_HORIZON
        try:
            sort = "newlyListed" if kind == "bin" else "endingSoonest"
            buying = "FIXED_PRICE" if kind == "bin" else "AUCTION"
            page_no = 0
            while True:
                page = self.client.search(
                    q=C.search_query(),
                    sort=sort,
                    buying_options=buying,
                    category_ids=C.EBAY_CATEGORY_IDS or "",
                    min_price=C.SEARCH_MIN_PRICE,
                    max_price=C.SEARCH_MAX_PRICE,
                    limit=C.SEARCH_PAGE_LIMIT,
                    offset=page_no * C.SEARCH_PAGE_LIMIT,
                    day=_today(),
                )
                rows = page.get("itemSummaries") or []
                listing = None
                for row in rows:
                    result.items_seen += 1
                    listing = from_item_summary(row)
                    if not listing.item_id:
                        continue
                    is_new = self.db.upsert_listing(listing)
                    assessment = self.score(listing)
                    if is_new:
                        result.items_new += 1
                    if self._should_notify(assessment, is_new):
                        result.alerts += 1
                        if notify:
                            self._notify(assessment)
                page_no += 1
                if len(rows) < C.SEARCH_PAGE_LIMIT:
                    break
                # Ending-soonest order: if the last row still ends inside the
                # horizon, the next page may hold more that do.
                inside_horizon = (
                    kind == "auction"
                    and listing is not None
                    and listing.end_time_utc is not None
                    and listing.end_time_utc <= horizon
                )
                if page_no >= pages and not inside_horizon:
                    break
            self.last_error = None
        except BudgetExhausted as exc:
            result.error = str(exc)
            self.last_error = result.error
        except EbayError as exc:
            result.error = f"{exc} {getattr(exc, 'body', '')[:300]}".strip()
            self.last_error = result.error
        except Exception:  # a bug in our own code must not kill the loop
            result.error = traceback.format_exc(limit=3)
            self.last_error = result.error

        result.http_calls = self.client.budget.used - before
        self.db.finish_poll(
            run_id,
            http_calls=result.http_calls,
            items_seen=result.items_seen,
            items_new=result.items_new,
            alerts=result.alerts,
            error=result.error,
        )
        return result

    def _should_notify(self, a: Assessment, is_new: bool) -> bool:
        if not a.is_actionable:
            return False
        last = self.db.last_alert(a.listing.item_id)
        if last is not None:
            # Already alerted: only a lower price than the one quoted is news.
            # A pre-migration alert has no recorded price and never re-alerts.
            return (
                last["price_pence"] is not None
                and a.effective_price is not None
                and a.effective_price < last["price_pence"]
            )
        # An auction seen again after a bid can become actionable when it was
        # not before, so notify on transition rather than only on first sight.
        return is_new or a.listing.is_auction

    def _notify(self, a: Assessment) -> None:
        note = notify.alert_for(a, self._item_url(a.listing.item_id))
        ok, detail = self.notifier.send(note)
        self.db.log_notification(
            "alert", ok, detail, a.listing.item_id, a.effective_price
        )

    def _item_url(self, item_id: str) -> str:
        host = "localhost" if C.BIND_HOST in ("0.0.0.0", "127.0.0.1") else C.BIND_HOST
        return f"http://{host}:{C.BIND_PORT}/item/{item_id}"

    # -- loops -------------------------------------------------------------

    def run_loop(self, kind: str, interval: int) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            result = self.poll_once(kind)
            # Back off as the daily call budget runs down rather than stopping
            # dead at the ceiling: a slow feed beats no feed.
            assert self.client is not None
            fraction_left = self.client.budget.remaining / max(
                1, self.client.budget.ceiling
            )
            wait = interval if fraction_left > 0.2 else interval * 4
            if result.error:
                wait = max(wait, 60)
            self._stop.wait(max(5.0, wait - (time.monotonic() - started)))

    def run_watchdog(self) -> None:
        """Tell the operator when ingestion has stopped (requirement 7).

        A detector that dies silently is worse than no detector, because
        silence on the alert channel is indistinguishable from "nothing good
        came up" — which is what it looks like most days.
        """
        while not self._stop.is_set():
            self._stop.wait(60)
            if self._stop.is_set():
                return
            last = self.db.last_successful_poll() or self.started_at
            age = (utcnow() - last.astimezone(timezone.utc)).total_seconds()
            if age > C.HEARTBEAT_STALE_SECONDS and not self._stall_notified:
                ok, detail = self.notifier.send(notify.stalled(int(age // 60)))
                self.db.log_notification("stalled", ok, detail)
                self._stall_notified = True
            elif age <= C.HEARTBEAT_STALE_SECONDS and self._stall_notified:
                ok, detail = self.notifier.send(notify.recovered(int(age // 60)))
                self.db.log_notification("recovered", ok, detail)
                self._stall_notified = False

    def start(self) -> list[threading.Thread]:
        threads = [
            threading.Thread(
                target=self.run_loop, args=("bin", C.POLL_BIN_SECONDS),
                name="poll-bin", daemon=True,
            ),
            threading.Thread(
                target=self.run_loop, args=("auction", C.POLL_AUCTION_SECONDS),
                name="poll-auction", daemon=True,
            ),
            threading.Thread(target=self.run_watchdog, name="watchdog", daemon=True),
        ]
        for t in threads:
            t.start()
        return threads

    def stop(self) -> None:
        self._stop.set()

    # -- health ------------------------------------------------------------

    def health(self) -> dict:
        last = self.db.last_successful_poll()
        age = (utcnow() - last).total_seconds() if last else None
        return {
            "started_at": self.started_at,
            "last_successful_poll": last,
            "seconds_since_poll": age,
            "stale": age is None or age > C.HEARTBEAT_STALE_SECONDS,
            "calls_used": self.client.budget.used if self.client else 0,
            "calls_remaining": self.client.budget.remaining if self.client else 0,
            "notifier": self.notifier.status,
            "last_error": self.last_error,
            "catalogue_entries": len(self.catalogue.references),
            "catalogue_unverified": self.catalogue.unverified_count,
            "fingerprint": C.valuation_fingerprint(),
        }
