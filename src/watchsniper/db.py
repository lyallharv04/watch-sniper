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

from .details import ItemDetails
from .models import Listing, parse_ts, utcnow
from .money import median_pence
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
    raw_json                 TEXT NOT NULL,
    -- Set from getItem once a listing is known to have ended (CLAUDE.md §6).
    -- end_state is 'sold', 'unsold' or 'gone' (getItem 404).
    ended_at_utc             TEXT,
    end_state                TEXT,
    end_checked_at_utc       TEXT
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
    below_fmv_bp        INTEGER,
    derivation_json     TEXT NOT NULL,
    config_fingerprint  TEXT NOT NULL,
    llm_candidate       INTEGER NOT NULL DEFAULT 0
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

CREATE TABLE IF NOT EXISTS closings (
    item_id           TEXT PRIMARY KEY REFERENCES listings(item_id),
    checked_at_utc    TEXT NOT NULL,
    final_price_pence INTEGER,
    bid_count         INTEGER NOT NULL DEFAULT 0,
    had_bids          INTEGER NOT NULL,
    detail            TEXT NOT NULL DEFAULT '',
    sold              INTEGER
);

-- The full item as getItem returns it, for the verification models. Written
-- on first need and at close. Seller-supplied text is stored as received and
-- only ever reaches a model as tagged data (docs/LLM_CONTRACT.md).
CREATE TABLE IF NOT EXISTS item_details (
    item_id           TEXT PRIMARY KEY REFERENCES listings(item_id),
    fetched_at_utc    TEXT NOT NULL,
    aspects_json      TEXT NOT NULL,
    description_text  TEXT NOT NULL,
    image_urls_json   TEXT NOT NULL,
    raw_json          TEXT NOT NULL
);

-- One row per model call attempted. Facts only: no money column is a model's
-- output. The would_* columns are what live mode would have decided, computed
-- by code from the stored facts and recomputed by rescore without a call.
CREATE TABLE IF NOT EXISTS llm_results (
    id                INTEGER PRIMARY KEY,
    item_id           TEXT NOT NULL REFERENCES listings(item_id),
    stage             TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_version    TEXT NOT NULL,
    candidates_json   TEXT NOT NULL,
    candidate_hash    TEXT NOT NULL,
    input_hash        TEXT NOT NULL,
    requested_at_utc  TEXT NOT NULL,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    ok                INTEGER NOT NULL,
    error             TEXT,
    catalogue_key     TEXT,
    confidence        TEXT,
    condition         TEXT,
    box_papers        TEXT,
    bracelet          TEXT,
    evidence_json     TEXT NOT NULL DEFAULT '[]',
    evidence_verified INTEGER NOT NULL DEFAULT 0,
    red_flags_json    TEXT NOT NULL DEFAULT '[]',
    reason            TEXT NOT NULL DEFAULT '',
    raw_response      TEXT NOT NULL DEFAULT '',
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cost_micro_usd    INTEGER NOT NULL DEFAULT 0,
    escalated_from    INTEGER REFERENCES llm_results(id),
    would_verdict     TEXT,
    would_mab_pence   INTEGER,
    would_reason      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_llm_key
    ON llm_results(item_id, stage, model, prompt_version, candidate_hash);
CREATE INDEX IF NOT EXISTS ix_llm_requested ON llm_results(requested_at_utc);

CREATE TABLE IF NOT EXISTS llm_labels (
    id              INTEGER PRIMARY KEY,
    llm_result_id   INTEGER NOT NULL REFERENCES llm_results(id),
    item_id         TEXT NOT NULL,
    correct         INTEGER NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at_utc  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_llm_labels_result ON llm_labels(llm_result_id);

-- A shadow veto on a listing's alert (DECISIONS.md A20): set while both
-- shadow models' current answers would reject it with high confidence.
-- reasons_json holds each model's reason. Derived; rescore rebuilds it.
CREATE TABLE IF NOT EXISTS vetoes (
    item_id         TEXT PRIMARY KEY REFERENCES listings(item_id),
    decided_at_utc  TEXT NOT NULL,
    reasons_json    TEXT NOT NULL
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
    detail   TEXT NOT NULL DEFAULT '',
    price_pence INTEGER
);
"""


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


#: What the dashboard needs to mark an ended listing, computed here so the page
#: compares no dates. Takes one parameter: now, as ISO-8601 UTC. An auction past
#: its end time is ended even before the closing check has fetched it.
_END_COLUMNS = """
    CASE WHEN l.ended_at_utc IS NOT NULL
           OR (l.is_auction = 1 AND l.end_time_utc <= ?) THEN 1 ELSE 0 END AS is_ended,
    COALESCE(l.ended_at_utc, CASE WHEN l.is_auction = 1 THEN l.end_time_utc END)
        AS ended_display_utc,
    CASE WHEN l.is_auction = 0 AND COALESCE(v.catalogue_key, '') = ''
         THEN 1 ELSE 0 END AS end_not_checked
"""


def _midnight_utc() -> datetime:
    return utcnow().replace(hour=0, minute=0, second=0, microsecond=0)


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
        #: True when a verdicts table in an older shape was dropped. Verdicts
        #: are derived data; the engine rebuilds them by re-scoring.
        self.verdicts_dropped = False
        with self._lock:
            cols = {
                r["name"]
                for r in self._conn.execute("PRAGMA table_info(verdicts)")
            }
            if cols and "llm_candidate" not in cols:
                self._conn.execute("DROP TABLE verdicts")
                self.verdicts_dropped = True
            self._conn.executescript(SCHEMA)
            # Columns added after their tables; older files lack them.
            for table, column, kind in (
                ("notifications", "price_pence", "INTEGER"),
                ("closings", "sold", "INTEGER"),
                ("listings", "ended_at_utc", "TEXT"),
                ("listings", "end_state", "TEXT"),
                ("listings", "end_checked_at_utc", "TEXT"),
            ):
                existing = {
                    r["name"]
                    for r in self._conn.execute(f"PRAGMA table_info({table})")
                }
                if column not in existing:
                    self._conn.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {kind}"
                    )
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
                    ended_at_utc=NULL,
                    end_state=NULL,
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
                price_basis, headroom_pence, below_fmv_bp, derivation_json,
                config_fingerprint, llm_candidate
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                below_fmv_bp=excluded.below_fmv_bp,
                derivation_json=excluded.derivation_json,
                config_fingerprint=excluded.config_fingerprint,
                llm_candidate=excluded.llm_candidate
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
                a.below_fmv_bp,
                json.dumps(_valuation_json(v)),
                a.config_fingerprint,
                int(a.llm_candidate),
            ),
        )

    def feed(
        self,
        *,
        view: str = "bin",
        verdict: str = "",
        brand: str = "",
        query: str = "",
        limit: int = 100,
        offset: int = 0,
        ending_within: timedelta | None = None,
        exclude_verdict: str = "",
    ) -> list[sqlite3.Row]:
        """Listings with their verdicts, furthest below FMV first.

        `view` "bin" is Buy It Now; "auctions" is auctions ending between now
        and `ending_within` from now. `verdict` "" (the default) means
        catalogue-matched listings the blacklist did not reject; "all" means
        everything, in either format, ignoring the view; anything else is an
        exact verdict within the view. Unpriced rows sort last, newest first.
        `ending_within` None on the auction view means any future end time.
        `exclude_verdict` drops one verdict, so a list shown under a section
        of that verdict does not repeat it.
        """
        where, params = ["1=1"], []
        now = utcnow().replace(microsecond=0)
        if verdict != "all":
            # The two views never show an ended listing; "all" shows it marked.
            where.append("l.ended_at_utc IS NULL")
            if view == "auctions":
                where.append("l.is_auction = 1 AND l.end_time_utc > ?")
                params.append(_iso(now))
                if ending_within is not None:
                    where.append("l.end_time_utc <= ?")
                    params.append(_iso(now + ending_within))
            else:
                where.append("l.is_auction = 0")
        if verdict == "":
            where.append("v.catalogue_key <> '' AND v.verdict <> 'REJECT_BLACKLIST'")
        elif verdict != "all":
            where.append("v.verdict = ?")
            params.append(verdict)
        if exclude_verdict:
            where.append("v.verdict <> ?")
            params.append(exclude_verdict)
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
                   v.headroom_pence, v.below_fmv_bp, v.caveats_json, v.scope,
                   v.bracelet, {_END_COLUMNS},
                   (SELECT reasons_json FROM vetoes
                     WHERE vetoes.item_id = l.item_id) AS veto_reasons_json,
                   (SELECT group_concat(label) FROM labels
                     WHERE labels.item_id = l.item_id) AS labels
              FROM listings l JOIN verdicts v ON v.item_id = l.item_id
             WHERE {' AND '.join(where)}
             ORDER BY v.below_fmv_bp IS NULL, v.below_fmv_bp DESC,
                      l.first_seen_utc DESC
             LIMIT ? OFFSET ?
            """,
            [_iso(now), *params],
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

    def observed_closings(self) -> dict[str, tuple[int | None, int, int]]:
        """Per catalogue entry: (median sold price, sold count, unsold count).

        The median counts only auctions eBay reports as sold with a GBP price;
        an auction with bids that missed its reserve is unsold. Closings with
        no sold field (stored before it was recorded) count as neither.
        Listings the blacklist rejects are left out: a bracelet sold alone or
        a custom dial matches the reference's name but is not the reference.
        The median of an even count is the lower-rounded mean of the middle two.
        """
        prices: dict[str, list[int]] = {}
        unsold: dict[str, int] = {}
        for r in self.query(
            "SELECT v.catalogue_key, c.final_price_pence, c.sold FROM closings c"
            " JOIN verdicts v ON v.item_id = c.item_id"
            " WHERE c.sold IS NOT NULL"
            " AND v.catalogue_key <> '' AND v.verdict <> 'REJECT_BLACKLIST'"
        ):
            key = r["catalogue_key"]
            if not r["sold"]:
                unsold[key] = unsold.get(key, 0) + 1
            elif r["final_price_pence"] is not None:
                prices.setdefault(key, []).append(r["final_price_pence"])
        return {
            key: (median_pence(prices.get(key, [])), len(prices.get(key, [])),
                  unsold.get(key, 0))
            for key in prices.keys() | unsold.keys()
        }

    def sold_closings(self) -> list[sqlite3.Row]:
        """Every auction eBay reports as sold with a GBP price, with the
        reference the rules gave it. Blacklist rejections are left out, as
        for the Observed column."""
        return self.query(
            "SELECT c.item_id, c.final_price_pence, v.catalogue_key,"
            " l.condition_raw, l.condition_id FROM closings c"
            " JOIN verdicts v ON v.item_id = c.item_id"
            " JOIN listings l ON l.item_id = c.item_id"
            " WHERE c.sold = 1 AND c.final_price_pence IS NOT NULL"
            " AND v.verdict <> 'REJECT_BLACKLIST'"
        )

    def closing_answers(self) -> list[sqlite3.Row]:
        """Successful model answers from the closing stage, oldest first."""
        return self.query(
            "SELECT item_id, model, catalogue_key, confidence, condition"
            " FROM llm_results WHERE stage = 'closing' AND ok = 1 ORDER BY id"
        )

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
            " v.below_fmv_bp, v.derivation_json, v.config_fingerprint,"
            f" v.computed_at_utc, {_END_COLUMNS},"
            " (SELECT reasons_json FROM vetoes WHERE vetoes.item_id = l.item_id)"
            " AS veto_reasons_json"
            " FROM listings l"
            " LEFT JOIN verdicts v ON v.item_id = l.item_id WHERE l.item_id = ?",
            (_iso(utcnow()), item_id),
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
        self,
        kind: str,
        ok: bool,
        detail: str = "",
        item_id: str | None = None,
        price: int | None = None,
    ) -> None:
        self.execute(
            "INSERT INTO notifications (at_utc,kind,item_id,ok,detail,price_pence)"
            " VALUES (?,?,?,?,?,?)",
            (_iso(utcnow()), kind, item_id, int(ok), detail, price),
        )

    def last_alert(self, item_id: str) -> sqlite3.Row | None:
        """The most recent successfully delivered alert for this item, or the
        most recent vetoed one, if any.

        Its `price_pence` is the effective price the alert quoted; NULL on
        rows written before the column existed. A vetoed alert counts, so a
        vetoed listing is not re-vetoed every sweep: like an alert, it is
        news again only at a lower price.
        """
        return self.one(
            "SELECT price_pence FROM notifications WHERE item_id=?"
            " AND ((ok=1 AND kind='alert') OR kind='vetoed')"
            " ORDER BY id DESC LIMIT 1",
            (item_id,),
        )

    def recent_notifications(self, limit: int = 25) -> list[sqlite3.Row]:
        """Recent notifications, with the matched reference for alert titles."""
        return self.query(
            "SELECT n.*, v.catalogue_display FROM notifications n"
            " LEFT JOIN verdicts v ON v.item_id = n.item_id"
            " ORDER BY n.id DESC LIMIT ?",
            (limit,),
        )

    def auctions_awaiting_close(self, ended_before: datetime) -> list[str]:
        """Stored auctions whose end time has passed and have no closing yet."""
        cutoff = _iso(ended_before.replace(microsecond=0))
        return [
            r["item_id"]
            for r in self.query(
                "SELECT l.item_id FROM listings l"
                " LEFT JOIN closings c ON c.item_id = l.item_id"
                " WHERE l.is_auction = 1 AND c.item_id IS NULL"
                " AND l.end_time_utc IS NOT NULL AND l.end_time_utc <= ?"
                " ORDER BY l.end_time_utc",
                (cutoff,),
            )
        ]

    def save_closing(
        self,
        item_id: str,
        final_price: int | None,
        bid_count: int,
        sold: bool | None,
        detail: str = "",
    ) -> None:
        self.execute(
            "INSERT OR REPLACE INTO closings (item_id,checked_at_utc,"
            "final_price_pence,bid_count,had_bids,sold,detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (
                item_id, _iso(utcnow()), final_price, bid_count,
                int(bid_count > 0), None if sold is None else int(sold), detail,
            ),
        )

    # -- item details (getItem, for the verification models) ----------------

    def save_item_details(self, d: ItemDetails) -> None:
        """Insert or replace. The listing must already be stored (FK)."""
        self.execute(
            "INSERT OR REPLACE INTO item_details (item_id,fetched_at_utc,"
            "aspects_json,description_text,image_urls_json,raw_json)"
            " VALUES (?,?,?,?,?,?)",
            (
                d.item_id,
                _iso(d.fetched_at),
                json.dumps([[n, v] for n, v in d.aspects]),
                d.description_text,
                json.dumps(list(d.image_urls)),
                json.dumps(d.raw, default=str),
            ),
        )

    def item_details(self, item_id: str) -> ItemDetails | None:
        row = self.one("SELECT * FROM item_details WHERE item_id=?", (item_id,))
        if row is None:
            return None
        return ItemDetails(
            item_id=row["item_id"],
            fetched_at=parse_ts(row["fetched_at_utc"]) or utcnow(),
            aspects=[(str(n), str(v)) for n, v in json.loads(row["aspects_json"])],
            description_text=row["description_text"],
            image_urls=list(json.loads(row["image_urls_json"])),
            raw=json.loads(row["raw_json"]),
        )

    # -- verification model results (shadow mode) ---------------------------

    def save_llm_result(
        self,
        *,
        item_id: str,
        stage: str,
        inp,
        result,
        escalated_from: int | None,
        would_verdict: str | None,
        would_mab: int | None,
        would_reason: str,
    ) -> int:
        """One attempted call, facts and all. `inp` is the LlmInput it was
        asked about; the images themselves are not stored, only their hash."""
        cur = self.execute(
            "INSERT INTO llm_results (item_id,stage,provider,model,prompt_version,"
            "candidates_json,candidate_hash,input_hash,requested_at_utc,latency_ms,"
            "ok,error,catalogue_key,confidence,condition,box_papers,bracelet,"
            "evidence_json,evidence_verified,red_flags_json,reason,raw_response,"
            "input_tokens,output_tokens,cost_micro_usd,escalated_from,"
            "would_verdict,would_mab_pence,would_reason)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item_id, stage, result.provider, result.model, result.prompt_version,
                json.dumps([c.__dict__ for c in inp.candidates]),
                inp.candidate_hash, inp.input_hash, _iso(utcnow()),
                result.latency_ms, int(result.ok), result.error,
                result.catalogue_key, result.confidence, result.condition,
                result.box_papers, result.bracelet, json.dumps(result.evidence),
                int(result.evidence_verified), json.dumps(result.red_flags),
                result.reason, result.raw_response, result.input_tokens,
                result.output_tokens, result.cost_micro_usd, escalated_from,
                would_verdict, would_mab, would_reason,
            ),
        )
        return int(cur.lastrowid or 0)

    def llm_attempts(
        self, item_id: str, stage: str, model: str, prompt_version: str, chash: str
    ) -> tuple[bool, int]:
        """(an ok answer exists, failed attempts) under one cache key."""
        row = self.one(
            "SELECT COALESCE(SUM(ok),0) good, COALESCE(SUM(1-ok),0) bad"
            " FROM llm_results WHERE item_id=? AND stage=? AND model=?"
            " AND prompt_version=? AND candidate_hash=?",
            (item_id, stage, model, prompt_version, chash),
        )
        return bool(row["good"]), int(row["bad"])

    def llm_ok_results(
        self, item_id: str, stage: str, prompt_version: str, chash: str
    ) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM llm_results WHERE item_id=? AND stage=? AND ok=1"
            " AND prompt_version=? AND candidate_hash=? ORDER BY id",
            (item_id, stage, prompt_version, chash),
        )

    def llm_results_for(self, item_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM llm_results WHERE item_id=? ORDER BY id DESC", (item_id,)
        )

    def llm_spent_today(self) -> int:
        """Micro-USD accounted since midnight UTC, every model."""
        row = self.one(
            "SELECT COALESCE(SUM(cost_micro_usd),0) s FROM llm_results"
            " WHERE requested_at_utc >= ?",
            (_iso(_midnight_utc()),),
        )
        return int(row["s"])

    def llm_stats_today(self) -> list[sqlite3.Row]:
        """Per model since midnight UTC: calls, errors, micro-USD."""
        return self.query(
            "SELECT model, COUNT(*) calls, SUM(1-ok) errors,"
            " SUM(cost_micro_usd) cost FROM llm_results"
            " WHERE requested_at_utc >= ? GROUP BY model ORDER BY model",
            (_iso(_midnight_utc()),),
        )

    def llm_last_error(self) -> sqlite3.Row | None:
        return self.one(
            "SELECT model, requested_at_utc, error FROM llm_results"
            " WHERE ok=0 ORDER BY id DESC LIMIT 1"
        )

    def llm_results_for_rescore(self) -> list[sqlite3.Row]:
        """Every stored result with its listing, for recomputing would_*."""
        return self.query(
            "SELECT l.*, r.id AS llm_id, r.stage, r.ok, r.catalogue_key AS"
            " llm_key, r.confidence, r.condition, r.evidence_verified,"
            " r.red_flags_json FROM llm_results r"
            " JOIN listings l ON l.item_id = r.item_id"
        )

    def add_llm_label(self, result_id: int, correct: bool, note: str = "") -> None:
        """Right or wrong, on one model result. The latest label counts."""
        row = self.one("SELECT item_id FROM llm_results WHERE id=?", (result_id,))
        if row is None:
            return
        self.execute(
            "INSERT INTO llm_labels (llm_result_id,item_id,correct,note,created_at_utc)"
            " VALUES (?,?,?,?,?)",
            (result_id, row["item_id"], int(correct), note, _iso(utcnow())),
        )
        self.audit("operator", f"llm_label:{'right' if correct else 'wrong'}", str(result_id))

    def llm_label_state(self, item_id: str) -> dict[int, bool]:
        """The latest right/wrong label per result of one item."""
        return {
            r["llm_result_id"]: bool(r["correct"])
            for r in self.query(
                "SELECT llm_result_id, correct FROM llm_labels WHERE id IN"
                " (SELECT MAX(id) FROM llm_labels WHERE item_id=? GROUP BY llm_result_id)",
                (item_id,),
            )
        }

    def llm_label_tally(self) -> list[sqlite3.Row]:
        """Per model, counting each result's latest label once: right, wrong."""
        return self.query(
            "SELECT r.model, SUM(l.correct) right_n, SUM(1-l.correct) wrong_n"
            " FROM llm_labels l JOIN llm_results r ON r.id = l.llm_result_id"
            " WHERE l.id IN (SELECT MAX(id) FROM llm_labels GROUP BY llm_result_id)"
            " GROUP BY r.model ORDER BY r.model"
        )

    def llm_review_sample(self, limit: int = 20) -> list[sqlite3.Row]:
        """A fresh random sample of results live mode would have rejected,
        so the models' false negatives get labelled as well as their passes."""
        return self.query(
            "SELECT r.*, l.title FROM llm_results r"
            " JOIN listings l ON l.item_id = r.item_id"
            " WHERE r.would_verdict = 'REJECT_LLM' ORDER BY random() LIMIT ?",
            (limit,),
        )

    def set_veto(self, item_id: str, reasons: list[dict] | None) -> None:
        if reasons:
            self.execute(
                "INSERT OR REPLACE INTO vetoes (item_id,decided_at_utc,reasons_json)"
                " VALUES (?,?,?)",
                (item_id, _iso(utcnow()), json.dumps(reasons)),
            )
        else:
            self.execute("DELETE FROM vetoes WHERE item_id=?", (item_id,))

    def veto(self, item_id: str) -> list[dict] | None:
        row = self.one("SELECT reasons_json FROM vetoes WHERE item_id=?", (item_id,))
        return json.loads(row["reasons_json"]) if row else None

    def update_llm_would(
        self, result_id: int, verdict: str | None, mab: int | None, reason: str
    ) -> None:
        self.execute(
            "UPDATE llm_results SET would_verdict=?, would_mab_pence=?,"
            " would_reason=? WHERE id=?",
            (verdict, mab, reason, result_id),
        )

    # -- ended listings ------------------------------------------------------

    def mark_ended(
        self, item_id: str, ended_at: datetime | None, state: str, checked_at: datetime
    ) -> None:
        self.execute(
            "UPDATE listings SET ended_at_utc=?, end_state=?, end_checked_at_utc=?"
            " WHERE item_id=?",
            (_iso(ended_at or checked_at), state, _iso(checked_at), item_id),
        )

    def mark_checked_live(self, item_id: str, checked_at: datetime) -> None:
        self.execute(
            "UPDATE listings SET end_checked_at_utc=? WHERE item_id=?",
            (_iso(checked_at), item_id),
        )

    def listings_to_check_ended(
        self, not_seen_since: datetime, checked_before: datetime, limit: int
    ) -> list[str]:
        """Catalogue-matched Buy It Now listings that the latest sweep did not
        return, not known to have ended, and not checked since
        `checked_before`. Never-checked first, then the longest since a check.

        A listing the sweep did return needs no check: search returns only
        live listings. One it did not return has either ended or aged off the
        first page, and only getItem can say which.
        """
        return [
            r["item_id"]
            for r in self.query(
                "SELECT l.item_id FROM listings l JOIN verdicts v ON v.item_id = l.item_id"
                " WHERE l.is_auction = 0 AND v.catalogue_key <> ''"
                " AND l.ended_at_utc IS NULL AND l.last_seen_utc < ?"
                " AND (l.end_checked_at_utc IS NULL OR l.end_checked_at_utc < ?)"
                " ORDER BY l.end_checked_at_utc IS NOT NULL, l.end_checked_at_utc,"
                " l.last_seen_utc DESC LIMIT ?",
                (_iso(not_seen_since), _iso(checked_before), limit),
            )
        ]

    def unended_bin_details(self) -> list[sqlite3.Row]:
        """Stored getItem responses for Buy It Now listings not yet marked
        ended. Auctions are left to the closing check, which fetches them
        after their end and so reads the final sold state."""
        return self.query(
            "SELECT d.item_id, d.raw_json, d.fetched_at_utc FROM item_details d"
            " JOIN listings l ON l.item_id = d.item_id"
            " WHERE l.is_auction = 0 AND l.ended_at_utc IS NULL"
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
