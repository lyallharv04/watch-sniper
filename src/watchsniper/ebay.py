"""eBay Browse API client. Application token only.

There is deliberately no authorization-code flow, no refresh-token storage and
no user-scoped call anywhere in this module. The client-credentials token it
obtains grants read access to active listings and cannot bid or buy. That is
requirement 8, and it is a property of what is absent from this file rather
than of a flag set somewhere in it.

stdlib urllib rather than `requests`, for two reasons. It removes the only
runtime dependency the project would otherwise have, and on a network that
inspects TLS — as the operator's does — `ssl.create_default_context()` reads
the operating system trust store, where `certifi` ships its own bundle and
fails.
"""

from __future__ import annotations

import base64
import gzip
import json
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import config as C
from .net import explain_failure, ssl_context

_BASES = {
    "production": ("https://api.ebay.com", "https://api.ebay.com"),
    "sandbox": ("https://api.sandbox.ebay.com", "https://api.sandbox.ebay.com"),
}

USER_AGENT = "watchsniper/3.0 (+single-operator; read-only Browse)"


class EbayError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class RateLimited(EbayError):
    pass


class BudgetExhausted(EbayError):
    pass


@dataclass
class CallBudget:
    """A day's worth of Browse calls, reset on the UTC date rolling over.

    Degrades rather than hard-stops where the caller allows it: the poller
    lengthens its sleep as the budget runs down, so a long day of retries
    slows ingestion instead of ending it.
    """

    ceiling: int
    used: int = 0
    day: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self, day: str) -> None:
        with self._lock:
            if day != self.day:
                self.day, self.used = day, 0
            if self.used >= self.ceiling:
                raise BudgetExhausted(
                    f"daily Browse call ceiling reached ({self.ceiling})"
                )
            self.used += 1

    @property
    def remaining(self) -> int:
        return max(0, self.ceiling - self.used)


class EbayClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        env: str = "production",
        marketplace: str = "EBAY_GB",
        budget: CallBudget | None = None,
        timeout: int = 30,
    ):
        if not client_id or not client_secret:
            raise EbayError("EBAY_CLIENT_ID and EBAY_CLIENT_SECRET are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.oauth_base, self.api_base = _BASES[env]
        self.marketplace = marketplace
        self.budget = budget or CallBudget(C.EBAY_DAILY_CALL_CEILING)
        self.timeout = timeout
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()
        self._ssl = ssl_context()
        #: Populated from response headers when eBay exposes them.
        self.last_rate_limit_headers: dict[str, str] = {}

    # -- auth ---------------------------------------------------------------

    def _fetch_token(self) -> None:
        """Client-credentials grant. The only OAuth flow in this codebase."""
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "scope": "https://api.ebay.com/oauth/api_scope",
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.oauth_base}/identity/v1/oauth2/token",
            data=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.timeout, context=self._ssl
            ) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:  # pragma: no cover - network
            raise EbayError(
                "OAuth token request rejected. Check that the keyset is the "
                "PRODUCTION one and that EBAY_CLIENT_ID / EBAY_CLIENT_SECRET "
                "are the App ID and Cert ID respectively.",
                exc.code,
                exc.read().decode(errors="replace")[:2000],
            ) from exc
        except ssl.SSLError as exc:  # pragma: no cover - network
            raise EbayError(explain_failure(exc)) from exc
        self._token = data["access_token"]
        # Refresh five minutes early; a token that expires mid-poll is a
        # confusing 401 rather than an obvious one.
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200)) - 300

    def token(self) -> str:
        with self._lock:
            if not self._token or time.time() >= self._token_expires_at:
                self._fetch_token()
            assert self._token
            return self._token

    # -- transport ----------------------------------------------------------

    def _get(self, path: str, params: dict[str, str], *, day: str) -> dict:
        self.budget.take(day)
        url = f"{self.api_base}{path}?{urllib.parse.urlencode(params)}"
        last: Exception | None = None
        for attempt in range(4):
            req = urllib.request.Request(
                url,
                headers={
                    "Authorization": f"Bearer {self.token()}",
                    "X-EBAY-C-MARKETPLACE-ID": self.marketplace,
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with urllib.request.urlopen(
                    req, timeout=self.timeout, context=self._ssl
                ) as resp:
                    self._record_rate_headers(resp.headers)
                    payload = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        payload = gzip.decompress(payload)
                    return json.loads(payload.decode())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode(errors="replace")[:2000]
                self._record_rate_headers(exc.headers)
                if exc.code == 401 and attempt == 0:
                    with self._lock:
                        self._token = None
                    continue
                if exc.code == 429:
                    last = RateLimited("Browse API rate limited", 429, body)
                elif 500 <= exc.code < 600:
                    last = EbayError("Browse API server error", exc.code, body)
                else:
                    raise EbayError(
                        f"Browse API returned {exc.code}", exc.code, body
                    ) from exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
                last = EbayError(f"network error talking to eBay: {exc}")
            time.sleep(min(2**attempt, 8) * (1 + attempt * 0.1))
        raise last or EbayError("Browse API request failed")

    def _record_rate_headers(self, headers) -> None:
        for k, v in (headers or {}).items():
            if "rate" in k.lower() or "limit" in k.lower():
                self.last_rate_limit_headers[k] = v

    # -- Browse -------------------------------------------------------------

    def search(
        self,
        *,
        q: str,
        sort: str,
        buying_options: str,
        category_ids: str,
        min_price: int,
        max_price: int,
        limit: int,
        offset: int = 0,
        day: str,
        extra_filters: str = "",
    ) -> dict:
        """One item_summary/search call.

        Prices are passed in whole pounds because eBay's price filter takes a
        decimal string; the pence-precision comparison happens locally against
        the returned amount, never in the filter.
        """
        filters = [
            f"price:[{min_price // 100}..{max_price // 100}]",
            "priceCurrency:GBP",
            f"buyingOptions:{{{buying_options}}}",
            f"itemLocationCountry:{C.EBAY_MARKETPLACE_ID[-2:]}",
        ]
        if extra_filters:
            filters.append(extra_filters)
        params = {
            "q": q,
            "filter": ",".join(filters),
            "sort": sort,
            "limit": str(limit),
            "offset": str(offset),
        }
        if category_ids:
            params["category_ids"] = category_ids
        return self._get("/buy/browse/v1/item_summary/search", params, day=day)

    def get_item(self, item_id: str, *, day: str) -> dict:
        return self._get(
            f"/buy/browse/v1/item/{urllib.parse.quote(item_id, safe='')}",
            {},
            day=day,
        )
