"""Storage. One SQLite file, money as INTEGER pence, datetimes as UTC ISO-8601.

An embedded database rather than a database server: one writer, one reader,
tens of thousands of rows a year, and a backup that is `cp watchsniper.db`.
Money is INTEGER at the storage layer, so requirement 14 is enforced by the
schema and not only by convention — there is no column a float could be
written into without a type error being visible.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import Listing, parse_ts, utcnow
from .valuation import Assessment

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS listings (
    item_id                  TEXT PRIMARY KEY,
    first_seen_utc           TEXT NOT NULL,
    last_seen_utc            TEXT NOT NULL,
    title                    TEXT NOT NULL,
    web_url                  TEXT NOT NULL,
    is_auction               INTEGER NOT NULL,
    price_pence              INTEGER,
    shipping_pence           INTEGER,
    currency                 TEXT NOT NULL,
    condition_raw            TEXT NOT NULL,
    condition_id             TEXT NOT NULL,
    bid_count                INTEGER,
    seller_username          TEXT NOT NULL,
    seller_account_type      TEXT NOT NULL,
    seller_feedback_pct_x100 INTEGER,
    seller_feedback_score    INTEGER,
    item_location_country    TEXT NOT NULL,
    end_time_utc             TEXT,
    category_id              TEXT NOT NULL,
    raw_json                 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verdicts (
    item_id             TEXT PRIMARY KEY REFERENCES listings(item_id),
    computed_at_utc     TEXT NOT NULL,
    verdict             TEXT NOT NULL,
    primary_reason      TEXT NOT NULL,
    gates_json          TEXT NOT NULL,
    caveats_json        TEXT NOT NULL,
    scope               TEXT,
    bracelet            TEXT,
    catalogue_key       TEXT NOT NULL,
    catalogue_display   TEXT NOT NULL,
    fmv_verified        INTEGER NOT NULL,
    fmv_pence           INTEGER,
    eff_fmv_pence       INTEGER,
    mab_pence           INTEGER,
    price_pence         INTEGER,
    price_basis         TEXT NOT NULL,
    headroom_pence      INTEGER,
    derivation_json     TEXT NOT NULL,
    config_fingerprint  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_verdicts_verdict ON verdicts(verdict);

CREATE TABLE IF NOT EXISTS labels (
    id             INTEGER PRIMARY KEY,
    item_id        TEXT NOT NULL REFERENCES listings(item_id),
    label          TEXT NOT NULL,
    note           TEXT NOT NULL DEFAULT '',
    verdict_at_time TEXT NOT NULL DEFAULT '',
    created_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_labels_item ON labels(item_id);

CREATE TABLE IF NOT EXISTS outcomes (
    id                INTEGER PRIMARY KEY,
    item_id           TEXT NOT NULL,
    title             TEXT NOT NULL DEFAULT '',
    bought_at_utc     TEXT,
    buy_price_pence   INTEGER,
    predicted_fmv_pence INTEGER,
    sold_at_utc       TEXT,
    sell_price_pence  INTEGER,
    fees_paid_pence   INTEGER,
    note              TEXT NOT NULL DEFAULT '',
    created_at_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY,
    at_utc   TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS poll_runs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,
    started_at_utc  TEXT NOT NULL,
    finished_at_utc TEXT,
    http_calls      INTEGER NOT NULL DEFAULT 0,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    items_new       INTEGER NOT NULL DEFAULT 0,
    alerts          INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS ix_poll_started ON poll_runs(started_at_utc);

CREATE TABLE IF NOT EXISTS notifications (
    id       INTEGER PRIMARY KEY,
    at_utc   TEXT NOT NULL,
    kind     TEXT NOT NULL,
    item_id  TEXT,
    ok       INTEGER NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


class Database:
    """A single connection guarded by a lock.

    The write volume is a few hundred rows an hour at most, and the poller is
    the only writer, so contention is not a design concern. The lock exists
    because the dashboard reads from HTTP handler threads.
    """

    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        #: True when a verdicts table from the two-scenario model was dropped.
        #: Verdicts are derived data; the engine rebuilds them by re-scoring.
        self.verdicts_dropped = False
        with self._lock:
            cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(verdicts)")
            }
            if "mab_pess_pence" in cols:
                self._conn.execute("DROP TABLE verdicts")
                self.verdicts_dropped = True
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- primitives --------------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    # -- listings ----------------------------------------------------------

    def upsert_listing(self, listing: Listing) -> bool:
        """Store a listing. Returns True the first time this item is seen.

        Dedup is the primary key. There is no separate seen-set to keep in
        sync with it.
        """
        now = _iso(listing.fetched_at)
        with self._lock:
            cur = self._conn.execute(
                "SELECT 1 FROM listings WHERE item_id=?", (listing.item_id,)
            )
            is_new = cur.fetchone() is None
            self._conn.execute(
                """
                INSERT INTO listings (
                    item_id, first_seen_utc, last_seen_utc, title, web_url,
                    is_auction, price_pence, shipping_pence, currency,
                    condition_raw, condition_id, bid_count, seller_username,
                    seller_account_type, seller_feedback_pct_x100,
                    seller_feedback_score, item_location_country, end_time_utc,
                    category_id, raw_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(item_id) DO UPDATE SET
                    last_seen_utc=excluded.last_seen_utc,
                    price_pence=excluded.price_pence,
                    shipping_pence=excluded.shipping_pence,
                    bid_count=excluded.bid_count,
                    end_time_utc=excluded.end_time_utc,
                    raw_json=excluded.raw_json
                """,
                (
                    listing.item_id, now, now, listing.title, listing.web_url,
                    int(listing.is_auction), listing.price, listing.shipping,
                    listing.currency, listing.condition_raw, listing.condition_id,
                    listing.bid_count, listing.seller_username,
                    listing.seller_account_type, listing.seller_feedback_pct_x100,
                    listing.seller_feedback_score, listing.item_location_country,
                    _iso(listing.end_time_utc), listing.category_id,
                    listing.raw_json,
                ),
            )
            self._conn.commit()
        return is_new

    def save_verdict(self, a: Assessment) -> None:
        v = a.valuation
        self.execute(
            """
            INSERT INTO verdicts (
                item_id, computed_at_utc, verdict, primary_reason, gates_json,
                caveats_json, scope, bracelet, catalogue_key, catalogue_display,
                fmv_verified, fmv_pence, eff_fmv_pence, mab_pence, price_pence,
                price_basis, headroom_pence, derivation_json, config_fingerprint
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(item_id) DO UPDATE SET
                computed_at_utc=excluded.computed_at_utc,
                verdict=excluded.verdict,
                primary_reason=excluded.primary_reason,
                gates_json=excluded.gates_json,
                caveats_json=excluded.caveats_json,
                scope=excluded.scope,
                bracelet=excluded.bracelet,
                catalogue_key=excluded.catalogue_key,
                catalogue_display=excluded.catalogue_display,
                fmv_verified=excluded.fmv_verified,
                fmv_pence=excluded.fmv_pence,
                eff_fmv_pence=excluded.eff_fmv_pence,
                mab_pence=excluded.mab_pence,
                price_pence=excluded.price_pence,
                price_basis=excluded.price_basis,
                headroom_pence=excluded.headroom_pence,
                derivation_json=excluded.derivation_json,
                config_fingerprint=excluded.config_fingerprint
            """,
            (
                a.listing.item_id,
                _iso(utcnow()),
                a.verdict,
                a.primary_reason,
                json.dumps([g.__dict__ for g in a.gates]),
                json.dumps(a.caveats),
                a.scope,
                a.bracelet,
                a.catalogue_key,
                a.catalogue_display,
                int(a.fmv_verified),
                a.fmv or None,
                v.effective_fmv if v else None,
                v.mab if v else None,
                a.effective_price,
                a.price_basis,
                a.headroom,
                json.dumps(_valuation_json(v)),
                a.config_fingerprint,
            ),
        )

    def feed(
        self,
        *,
        verdict: str = "",
        brand: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        where, params = ["1=1"], []
        if verdict:
            where.append("v.verdict = ?")
            params.append(verdict)
        if brand:
            where.append("v.catalogue_display LIKE ?")
            params.append(f"{brand}%")
        if query:
            where.append("l.title LIKE ?")
            params.append(f"%{query}%")
        params.extend([limit, offset])
        return self.query(
            f"""
            SELECT l.*, v.verdict, v.primary_reason, v.catalogue_display,
                   v.catalogue_key, v.fmv_verified, v.fmv_pence, v.mab_pence,
                   v.price_pence AS eff_price_pence, v.price_basis,
                   v.headroom_pence, v.caveats_json, v.scope, v.bracelet,
                   (SELECT group_concat(label) FROM labels
                     WHERE labels.item_id = l.item_id) AS labels
              FROM listings l JOIN verdicts v ON v.item_id = l.item_id
             WHERE {' AND '.join(where)}
             ORDER BY l.first_seen_utc DESC
             LIMIT ? OFFSET ?
            """,
            params,
        )

    def verdict_counts(self, since_hours: int = 24 * 14) -> list[sqlite3.Row]:
        cutoff = _iso(utcnow() - timedelta(hours=since_hours))
        return self.query(
            """
            SELECT v.verdict, COUNT(*) AS n
              FROM verdicts v JOIN listings l ON l.item_id = v.item_id
             WHERE l.first_seen_utc >= ?
             GROUP BY v.verdict ORDER BY n DESC
            """,
            (cutoff,),
        )

    def catalogue_usage(self) -> dict[str, int]:
        rows = self.query(
            "SELECT catalogue_key, COUNT(*) n FROM verdicts "
            "WHERE catalogue_key <> '' GROUP BY catalogue_key"
        )
        return {r["catalogue_key"]: r["n"] for r in rows}

    def unmatched_titles(self) -> list[str]:
        return [
            r["title"]
            for r in self.query(
                "SELECT l.title FROM listings l JOIN verdicts v"
                " ON v.item_id = l.item_id WHERE v.verdict = 'REJECT_CATALOGUE'"
            )
        ]

    def item(self, item_id: str) -> sqlite3.Row | None:
        # `v.price_pence` is the effective price the gate used, which for an
        # auction is the next valid bid rather than the current one. It is
        # aliased so it cannot shadow the listing's own asking price — the two
        # differ exactly where the difference matters most.
        return self.one(
            "SELECT l.*, v.verdict, v.primary_reason, v.gates_json,"
            " v.caveats_json, v.scope, v.bracelet, v.catalogue_key,"
            " v.catalogue_display, v.fmv_verified, v.fmv_pence, v.mab_pence,"
            " v.price_pence AS eff_price_pence, v.price_basis, v.headroom_pence,"
            " v.derivation_json, v.config_fingerprint, v.computed_at_utc"
            " FROM listings l"
            " LEFT JOIN verdicts v ON v.item_id = l.item_id WHERE l.item_id = ?",
            (item_id,),
        )

    # -- labels, outcomes, audit ------------------------------------------

    def add_label(self, item_id: str, label: str, note: str = "") -> None:
        row = self.one("SELECT verdict FROM verdicts WHERE item_id=?", (item_id,))
        self.execute(
            "INSERT INTO labels (item_id,label,note,verdict_at_time,created_at_utc)"
            " VALUES (?,?,?,?,?)",
            (item_id, label, note, row["verdict"] if row else "", _iso(utcnow())),
        )
        self.audit("operator", f"label:{label}", item_id)

    def labels_for(self, item_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM labels WHERE item_id=? ORDER BY id DESC", (item_id,)
        )

    def audit(self, actor: str, action: str, detail: str = "") -> None:
        self.execute(
            "INSERT INTO audit (at_utc,actor,action,detail) VALUES (?,?,?,?)",
            (_iso(utcnow()), actor, action, detail),
        )

    def add_outcome(self, **kw: Any) -> None:
        cols = (
            "item_id", "title", "bought_at_utc", "buy_price_pence",
            "predicted_fmv_pence", "sold_at_utc", "sell_price_pence",
            "fees_paid_pence", "note",
        )
        values = [kw.get(c) for c in cols]
        self.execute(
            f"INSERT INTO outcomes ({','.join(cols)},created_at_utc) "
            f"VALUES ({','.join('?' * len(cols))},?)",
            [*values, _iso(utcnow())],
        )
        self.audit("operator", "outcome", str(kw.get("item_id", "")))

    # -- operations --------------------------------------------------------

    def start_poll(self, kind: str) -> int:
        cur = self.execute(
            "INSERT INTO poll_runs (kind,started_at_utc) VALUES (?,?)",
            (kind, _iso(utcnow())),
        )
        return int(cur.lastrowid or 0)

    def finish_poll(self, run_id: int, **kw: Any) -> None:
        self.execute(
            "UPDATE poll_runs SET finished_at_utc=?, http_calls=?, items_seen=?,"
            " items_new=?, alerts=?, error=? WHERE id=?",
            (
                _iso(utcnow()),
                kw.get("http_calls", 0),
                kw.get("items_seen", 0),
                kw.get("items_new", 0),
                kw.get("alerts", 0),
                kw.get("error"),
                run_id,
            ),
        )

    def last_successful_poll(self) -> datetime | None:
        row = self.one(
            "SELECT MAX(finished_at_utc) t FROM poll_runs WHERE error IS NULL"
            " AND finished_at_utc IS NOT NULL"
        )
        return parse_ts(row["t"]) if row and row["t"] else None

    def recent_polls(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM poll_runs ORDER BY id DESC LIMIT ?", (limit,)
        )

    def log_notification(
        self, kind: str, ok: bool, detail: str = "", item_id: str | None = None
    ) -> None:
        self.execute(
            "INSERT INTO notifications (at_utc,kind,item_id,ok,detail)"
            " VALUES (?,?,?,?,?)",
            (_iso(utcnow()), kind, item_id, int(ok), detail),
        )

    def notified_recently(self, item_id: str) -> bool:
        return (
            self.one(
                "SELECT 1 FROM notifications WHERE item_id=? AND ok=1", (item_id,)
            )
            is not None
        )

    def recent_notifications(self, limit: int = 25) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
        )

    def all_listings(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM listings ORDER BY first_seen_utc")


def _valuation_json(v) -> dict | None:
    if v is None:
        return None
    return {
        "fmv_reference": v.fmv_reference,
        "condition": v.condition,
        "condition_stated": v.condition_stated,
        "multiplier_bp": v.multiplier_bp,
        "effective_fmv": v.effective_fmv,
        "lines": v.bid.lines,
    }


def listing_from_row(row: sqlite3.Row) -> Listing:
    """Rebuild a Listing from storage, for offline re-scoring."""
    return Listing(
        item_id=row["item_id"],
        title=row["title"],
        web_url=row["web_url"],
        is_auction=bool(row["is_auction"]),
        price=row["price_pence"],
        shipping=row["shipping_pence"],
        currency=row["currency"],
        condition_raw=row["condition_raw"],
        condition_id=row["condition_id"],
        bid_count=row["bid_count"],
        seller_username=row["seller_username"],
        seller_account_type=row["seller_account_type"],
        seller_feedback_pct_x100=row["seller_feedback_pct_x100"],
        seller_feedback_score=row["seller_feedback_score"],
        item_location_country=row["item_location_country"],
        end_time_utc=parse_ts(row["end_time_utc"]),
        category_id=row["category_id"],
        fetched_at=parse_ts(row["first_seen_utc"]) or utcnow(),
        raw=json.loads(row["raw_json"]),
    )
