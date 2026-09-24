"""The single owner of every constant in the system (requirement 16).

No other module and no document restates a value from this file. Documents cite
the *name*. `python -m watchsniper constants` prints the live values, and the
dashboard renders them from here, so there is exactly one place a number can be
read from and exactly one place it can be changed.

Two registries make two requirements structural rather than a matter of
discipline:

    ENV_VARS    every environment variable the code reads, with the operator
                instruction for obtaining it. `.env.example` is *generated*
                from this, so requirement 20 cannot drift.
    UNVERIFIED  every constant whose value is an unchecked inheritance, with
                what would settle it. Valuations carry these forward as caveats,
                so requirement 19 cannot be forgotten.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .money import Pence, parse_gbp

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CATALOGUE_PATH = DATA_DIR / "catalogue.toml"
BLACKLIST_PATH = DATA_DIR / "blacklist.toml"
INCREMENTS_PATH = DATA_DIR / "bid_increments_gb.toml"

# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------

ENV_VARS: list[dict[str, str]] = []


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a .env file. Real environment always wins."""
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(ROOT / ".env")


def env(name: str, default: str | None, where: str, missing: str) -> str | None:
    """Read one environment variable and register it for `.env.example`.

    where    where the operator obtains the value
    missing  what happens if it is absent
    """
    ENV_VARS.append(
        {"name": name, "default": default or "", "where": where, "missing": missing}
    )
    return os.environ.get(name, default)


def env_int(name: str, default: int, where: str, missing: str) -> int:
    raw = env(name, str(default), where, missing)
    return int(raw) if raw else default


# --- eBay -----------------------------------------------------------------

EBAY_CLIENT_ID = env(
    "EBAY_CLIENT_ID",
    None,
    "developer.ebay.com/my/keys -> Production keyset -> 'App ID (Client ID)'",
    "The service refuses to start. Nothing can be ingested.",
)
EBAY_CLIENT_SECRET = env(
    "EBAY_CLIENT_SECRET",
    None,
    "developer.ebay.com/my/keys -> Production keyset -> 'Cert ID (Client Secret)'",
    "The service refuses to start. Nothing can be ingested.",
)
EBAY_ENV = env(
    "EBAY_ENV",
    "production",
    "Leave as 'production'. 'sandbox' has almost no UK watch listings.",
    "Defaults to production.",
)
EBAY_MARKETPLACE_ID = env(
    "EBAY_MARKETPLACE_ID",
    "EBAY_GB",
    "Do not change. UK-domestic sourcing is a hard requirement.",
    "Defaults to EBAY_GB.",
)
EBAY_CATEGORY_IDS = env(
    "EBAY_CATEGORY_IDS",
    "31387",
    "Leaf category for Wristwatches. Run `python -m watchsniper diagnose` to "
    "confirm it resolves on EBAY_GB before changing it.",
    "Defaults to 31387. Without a category filter, ~21% of results are straps "
    "and bracelets that name the watch they fit.",
)
EBAY_DAILY_CALL_CEILING = env_int(
    "EBAY_DAILY_CALL_CEILING",
    4800,
    "eBay's published Browse limit for your application, minus headroom. "
    "`diagnose` reports the limit if the API exposes it.",
    "Defaults to 4800.",
)

# --- Notifications --------------------------------------------------------

NTFY_TOPIC = env(
    "NTFY_TOPIC",
    None,
    "Invent one. Any long random string, e.g. `openssl rand -hex 12`, or just "
    "mash the keyboard. No account and no credential exist for ntfy.sh — the "
    "topic name IS the address, so anyone who guesses it reads your alerts. "
    "Install the ntfy app from the Play Store, add this topic, done.",
    "Notifications are disabled. The dashboard and the poller still work; you "
    "simply find out by looking rather than by being told.",
)
NTFY_SERVER = env(
    "NTFY_SERVER",
    "https://ntfy.sh",
    "Leave as-is unless you self-host ntfy on the VPS later.",
    "Defaults to the public ntfy.sh.",
)

# --- Service --------------------------------------------------------------

DB_PATH = env(
    "DB_PATH",
    str(ROOT / "watchsniper.db"),
    "A writable path on the VPS. A single SQLite file; back it up by copying it.",
    "Defaults to watchsniper.db beside the code.",
)
BIND_HOST = env(
    "BIND_HOST",
    "127.0.0.1",
    "Leave at 127.0.0.1 and reach the dashboard over an SSH tunnel or a "
    "Tailscale/WireGuard address. Setting 0.0.0.0 publishes an unauthenticated "
    "dashboard to the internet — there is no login and there is not meant to be.",
    "Defaults to loopback only.",
)
BIND_PORT = env_int("BIND_PORT", 8137, "Any free port.", "Defaults to 8137.")

POLL_BIN_SECONDS = env_int(
    "POLL_BIN_SECONDS",
    90,
    "Seconds between Buy It Now sweeps. Genuinely underpriced BIN listings go "
    "in minutes, so this is the number that matters.",
    "Defaults to 90.",
)
POLL_AUCTION_SECONDS = env_int(
    "POLL_AUCTION_SECONDS",
    600,
    "Seconds between auction sweeps. Auctions have hours of runway, so this "
    "can be slow.",
    "Defaults to 600.",
)
HEARTBEAT_STALE_SECONDS = env_int(
    "HEARTBEAT_STALE_SECONDS",
    1800,
    "Seconds without a successful poll before you are told ingestion has "
    "stopped (requirement 7).",
    "Defaults to 1800 (30 minutes).",
)

# --------------------------------------------------------------------------
# Search band
# --------------------------------------------------------------------------

SEARCH_MIN_PRICE = parse_gbp("120.00")
SEARCH_MAX_PRICE = parse_gbp("800.00")
SEARCH_BRANDS = (
    "Hamilton",
    "Christopher Ward",
    "Tissot PRX",
    "Seiko Alpinist",
    "Baltic",
    "Farer",
    "Sinn",
)
SEARCH_PAGE_LIMIT = 200


def search_query() -> str:
    """One compound OR query covering every brand.

    Measured on the live API: this returns 836 in-band GB listings against 364
    for Hamilton alone, and the brand mix in the results matches the measured
    arrival mix — so the OR is genuinely searching all seven, not silently
    dropping the quoted terms. One call per sweep instead of seven takes the
    daily budget from roughly 7,700 calls to roughly 1,100.
    """
    terms = [f'"{b}"' if " " in b else b for b in SEARCH_BRANDS]
    return "(" + ",".join(terms) + ")"

# --------------------------------------------------------------------------
# Fees — sell side. Structure is fact; every rate below is unverified.
# --------------------------------------------------------------------------

FVF_BP = 1490
REG_OP_FEE_BP = 35
AD_RATE_BP = 200
ORDER_FEE = parse_gbp("0.30")

VAT_REGISTERED = False
FEE_VAT_MULT_BP = 12_000  # x1.20; not reclaimable while unregistered

# --------------------------------------------------------------------------
# Fees — buy side. Buyer Protection applies to PRIVATE sellers only.
# A tiered fee that is a function of the price is why the maximum allowable
# bid is solved for rather than divided out.
# --------------------------------------------------------------------------

BUYER_PROTECTION_FIXED = parse_gbp("0.10")
# (upper bound of the band in pence, marginal rate in basis points)
BUYER_PROTECTION_TIERS: tuple[tuple[Pence, int], ...] = (
    (parse_gbp("20.00"), 700),
    (parse_gbp("300.00"), 400),
    (parse_gbp("4000.00"), 200),
)

# --------------------------------------------------------------------------
# Margin
# --------------------------------------------------------------------------

TARGET_PROFIT_MARGIN_BP = 2000
MIN_ABSOLUTE_PROFIT = parse_gbp("75.00")

# --------------------------------------------------------------------------
# Logistics and service
# --------------------------------------------------------------------------

# Used only when a listing does not state its postage. When it does, the
# stated figure is used instead, so this is a fallback and not a second cost.
INBOUND_POSTAGE_ESTIMATE = parse_gbp("5.00")
OUTBOUND_POSTAGE = parse_gbp("9.50")

# There is deliberately no service-buffer constant. The inherited model carried
# both a bracelet multiplier and a flat "replace the bracelet" buffer, which
# charges for the same defect twice. The multiplier does that job; a buffer
# belongs to a *stated* fault, and Phase 1 does not read descriptions closely
# enough to find one. See DECISIONS A12.

# --------------------------------------------------------------------------
# Valuation multipliers, per 10,000
# --------------------------------------------------------------------------

COND_MULT = {
    "MINT": 10_000,
    "EXCELLENT": 9_300,
    "GOOD": 8_400,
    "FAIR": 7_000,
    "FOR_PARTS": 0,
}
SCOPE_MULT = {
    "FULL_SET": 10_000,
    "WATCH_PAPERS": 9_500,
    "WATCH_BOX": 9_400,
    "WATCH_ONLY": 8_800,
}
BRACELET_MULT = {
    "OEM_BRACELET": 10_000,
    "OEM_STRAP": 9_500,
    "AFTERMARKET": 8_800,
}

# What an unstated field is assumed to be at each end of the band. Most
# listings state neither scope nor bracelet, so these two rows carry more of
# the valuation error than the whole fee stack does.
PESSIMISTIC_UNKNOWN = {
    "condition": "GOOD",
    "scope": "WATCH_ONLY",
    "bracelet": "AFTERMARKET",
}
OPTIMISTIC_UNKNOWN = {
    "condition": "EXCELLENT",
    "scope": "FULL_SET",
    "bracelet": "OEM_BRACELET",
}

# --------------------------------------------------------------------------
# Seller guards
# --------------------------------------------------------------------------

MIN_SELLER_FEEDBACK_PCT_X100 = 9500  # 95.00%
MIN_SELLER_FEEDBACK_SCORE = 15
MAX_DOMESTIC_DELIVERY_DAYS = 7

# --------------------------------------------------------------------------
# Exposure. Phase 1 displays these; nothing enforces them because nothing in
# Phase 1 can spend money. They are here so the display has one owner.
# --------------------------------------------------------------------------

WEEKLY_SPEND_CAP = parse_gbp("900.00")
DAILY_BURST_CAP = parse_gbp("600.00")
MAX_CONCURRENT_OPEN_BIDS = 3
MAX_EXPOSURE_PER_REF = parse_gbp("800.00")
VAT_THRESHOLD = parse_gbp("90000.00")
VAT_THRESHOLD_WARN = parse_gbp("75000.00")

# --------------------------------------------------------------------------
# Unverified constants (requirement 19)
# --------------------------------------------------------------------------

UNVERIFIED: dict[str, str] = {
    "FVF_BP": "One real eBay seller invoice for a watch in category 31387. "
    "Currently the 'up to 14.9%' headline band; the real rate may be 12.9%, "
    "worth roughly £8 of MAB per watch.",
    "REG_OP_FEE_BP": "The same seller invoice.",
    "ORDER_FEE": "The same seller invoice.",
    "AD_RATE_BP": "Your own choice of promoted-listing rate; 2% is assumed.",
    "BUYER_PROTECTION_TIERS": "One real eBay purchase receipt from a private "
    "seller, showing the buyer protection line.",
    "BUYER_PROTECTION_FIXED": "The same purchase receipt.",
    "BID_INCREMENTS": "eBay's published UK table at "
    "ebay.co.uk/help/buying/bidding/automatic-bidding?id=4014. The widely "
    "quoted table is the USD one and does not apply.",
    "COND_MULT": "Nothing has checked these against realised sales. They are "
    "inherited estimates.",
    "SCOPE_MULT": "As above.",
    "BRACELET_MULT": "As above.",
    "INBOUND_POSTAGE_ESTIMATE": "Your own purchase records.",
    "OUTBOUND_POSTAGE": "Your own postage receipts.",
    "EBAY_CATEGORY_IDS": "`python -m watchsniper diagnose` — it reports "
    "whether the category resolves on EBAY_GB.",
}

# Caveat codes attached to every valuation. Ordered by how much they move the
# number, worst first.
FEE_CAVEATS = ("FEES_UNVERIFIED", "BUYER_PROTECTION_UNVERIFIED")


def valuation_fingerprint() -> str:
    """A short hash of every constant that can move a valuation.

    Stored on each verdict so a row scored under old constants is visibly a
    different thing from a row scored under new ones.
    """
    parts = [
        FVF_BP,
        REG_OP_FEE_BP,
        AD_RATE_BP,
        ORDER_FEE,
        FEE_VAT_MULT_BP,
        BUYER_PROTECTION_FIXED,
        BUYER_PROTECTION_TIERS,
        TARGET_PROFIT_MARGIN_BP,
        MIN_ABSOLUTE_PROFIT,
        INBOUND_POSTAGE_ESTIMATE,
        OUTBOUND_POSTAGE,
        sorted(COND_MULT.items()),
        sorted(SCOPE_MULT.items()),
        sorted(BRACELET_MULT.items()),
        sorted(PESSIMISTIC_UNKNOWN.items()),
        sorted(OPTIMISTIC_UNKNOWN.items()),
        VAT_REGISTERED,
    ]
    return hashlib.sha256(repr(parts).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------
# The safety boundary (requirement 8)
# --------------------------------------------------------------------------

#: Environment variables that would indicate somebody had begun wiring a user
#: OAuth token into this process. Their presence is a hard startup failure, not
#: a warning, because the guarantee this project makes is that bidding is
#: impossible by construction and not disabled by a flag.
FORBIDDEN_ENV = (
    "EBAY_USER_TOKEN",
    "EBAY_USER_REFRESH_TOKEN",
    "EBAY_USER_ACCESS_TOKEN",
    "EBAY_OAUTH_REDIRECT_URI",
    "EBAY_RU_NAME",
)


class UserTokenPresent(RuntimeError):
    pass


def assert_phase_1() -> None:
    """Refuse to run if anything resembling a user OAuth token is in scope.

    The Browse API used here takes an application (client-credentials) token,
    which cannot bid and cannot buy. There is no code path in this repository
    that requests, stores or sends a user token; this check exists so that
    adding one is a loud failure rather than a quiet capability.
    """
    found = [k for k in FORBIDDEN_ENV if os.environ.get(k)]
    if found:
        raise UserTokenPresent(
            "Refusing to start: the environment contains "
            + ", ".join(found)
            + ". Phase 1 holds no eBay user OAuth token — that is what makes "
            "bidding impossible rather than merely disabled. Remove these "
            "variables, or you are running something this codebase does not "
            "claim to be."
        )


def missing_credentials() -> list[str]:
    return [
        n
        for n, v in (
            ("EBAY_CLIENT_ID", EBAY_CLIENT_ID),
            ("EBAY_CLIENT_SECRET", EBAY_CLIENT_SECRET),
        )
        if not v
    ]
