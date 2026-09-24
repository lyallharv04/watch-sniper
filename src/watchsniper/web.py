"""The dashboard. Server-rendered HTML from the standard library.

The presentation layer holds no arithmetic (requirement 16). Every figure on
every page is a stored integer number of pence formatted for display; nothing
here recomputes an FMV, a maximum bid or a margin. That is not a coding
convention, it is the reason there is no JavaScript framework here: a
presentation layer that *can* recompute a valuation eventually will, and then
what the operator sees and what the system decided drift apart.

There is no authentication and there is not meant to be. The service binds to
loopback by default and is reached over an SSH tunnel or a private network
overlay. Giving it a public hostname is what forced the previous attempt into
edge authentication, and removing the exposure is a better fix than guarding it.
"""

from __future__ import annotations

import html
import json
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C
from .money import fmt, parse_gbp
from .poller import Engine

CSS = """
:root{--bg:#faf9f7;--fg:#1a1918;--dim:#6b6862;--line:#e3e0da;--card:#fff;
--pass:#0d7a4a;--maybe:#8a6d00;--rej:#8a8580;--warn:#a83232;--accent:#2a5db0}
@media(prefers-color-scheme:dark){:root{--bg:#16151a;--fg:#e8e6e1;--dim:#95918a;
--line:#2c2a31;--card:#1d1c22;--pass:#4ec98a;--maybe:#d8b13a;--rej:#7b766f;
--warn:#f0736a;--accent:#7aa5f0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent)}
header{border-bottom:1px solid var(--line);padding:12px 20px;display:flex;
gap:18px;align-items:baseline;flex-wrap:wrap;position:sticky;top:0;
background:var(--bg);z-index:5}
header h1{font-size:15px;margin:0;font-weight:650;letter-spacing:-.01em}
header nav a{margin-right:14px;text-decoration:none;font-size:13px}
main{padding:18px 20px;max-width:1180px}
.banner{border:1px solid var(--warn);border-left-width:4px;border-radius:6px;
padding:10px 14px;margin:0 0 18px;font-size:13px;background:var(--card)}
.banner b{color:var(--warn)}
table{border-collapse:collapse;width:100%;font-size:13px}
th{text-align:left;font-weight:600;color:var(--dim);font-size:11px;
text-transform:uppercase;letter-spacing:.06em;padding:6px 8px;
border-bottom:1px solid var(--line)}
td{padding:9px 8px;border-bottom:1px solid var(--line);vertical-align:top}
tr:hover td{background:var(--card)}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.chip{display:inline-block;padding:1px 7px;border-radius:10px;font-size:11px;
font-weight:600;letter-spacing:.02em;white-space:nowrap}
.v-DEAL{background:var(--pass);color:var(--bg)}
.chip.rej{background:transparent;color:var(--rej);border:1px solid var(--line)}
.tag{font-size:10.5px;color:var(--dim);border:1px solid var(--line);
border-radius:4px;padding:0 5px;margin-right:4px;white-space:nowrap;
display:inline-block}
.tag.bad{color:var(--warn);border-color:var(--warn)}
.dim{color:var(--dim)}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:14px 16px;margin-bottom:16px}
.card h2{font-size:13px;margin:0 0 10px;text-transform:uppercase;
letter-spacing:.06em;color:var(--dim)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px}
button{font:inherit;font-size:12px;padding:3px 9px;border-radius:5px;
border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
input,select,textarea{font:inherit;font-size:13px;padding:4px 7px;
border:1px solid var(--line);border-radius:5px;background:var(--card);
color:var(--fg)}
form.inline{display:inline}
.filters{display:flex;gap:8px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.ledger{width:100%;font-size:13px;font-variant-numeric:tabular-nums}
.ledger td:last-child{text-align:right;white-space:nowrap}
.ledger tr:last-child td{font-weight:650;border-top:2px solid var(--line)}
code{font:12px ui-monospace,SFMono-Regular,Consolas,monospace;
background:var(--card);padding:1px 4px;border-radius:3px}
.muted-row td{opacity:.62}
"""

NAV = [
    ("/", "Feed"),
    ("/?verdict=DEAL", "Deals"),
    ("/catalogue", "Catalogue"),
    ("/missing", "Not priced"),
    ("/outcomes", "Outcomes"),
    ("/health", "Health"),
    ("/constants", "Constants"),
]


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def page(title: str, body: str, engine: Engine) -> bytes:
    warnings = []
    if not engine.notifier.enabled:
        warnings.append(
            "Notifications are <b>off</b> — NTFY_TOPIC is not set, so nothing "
            "reaches your phone."
        )
    unver = engine.catalogue.unverified_count
    if unver:
        warnings.append(
            f"<b>{unver} of {len(engine.catalogue.references)} catalogue FMVs "
            "are unverified estimates.</b> Every valuation derived from one is "
            'marked <span class="tag bad">FMV UNVERIFIED</span>. Nothing here '
            "is a price until you have checked it."
        )
    warnings.append(
        "No fee rate in this system has been checked against a real invoice. "
        'See <a href="/constants">Constants</a>.'
    )
    banner = "".join(f'<div class="banner">{w}</div>' for w in warnings)
    nav = "".join(f'<a href="{h}">{e(t)}</a>' for h, t in NAV)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — watch sniper</title><style>{CSS}</style></head><body>
<header><h1>watch sniper</h1><nav>{nav}</nav></header>
<main>{banner}{body}</main></body></html>""".encode()


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------


def verdict_chip(verdict: str) -> str:
    label = verdict.replace("REJECT_", "")
    cls = "v-DEAL" if verdict == "DEAL" else "rej"
    return f'<span class="chip {cls}">{e(label)}</span>'


def listing_tags(scope: str | None, bracelet: str | None) -> str:
    """Scope and bracelet as read from the title. Labels only; not valued."""
    return "".join(
        f'<span class="tag">{e(v.lower().replace("_", " "))}</span>'
        for v in (scope, bracelet)
        if v
    )


CAVEAT_LABEL = {
    "FMV_UNVERIFIED": ("FMV UNVERIFIED", True),
    "AMBIGUOUS_MATCH": ("ambiguous match", True),
    "VARIANT_UNRESOLVED": ("variant unresolved", True),
    "SELLER_DATA_MISSING": ("no seller data", True),
    "POSTAGE_UNKNOWN": ("postage unknown", False),
    "FEES_UNVERIFIED": ("fees unverified", False),
    "BUYER_PROTECTION_UNVERIFIED": ("BP fee unverified", False),
    "BID_INCREMENTS_UNVERIFIED": ("increments unverified", False),
}


def caveat_tags(caveats: list[str], *, only_important: bool = False) -> str:
    out = []
    for c in caveats:
        label, important = CAVEAT_LABEL.get(c, (c.lower().replace("_", " "), False))
        if only_important and not important:
            continue
        out.append(f'<span class="tag{" bad" if important else ""}">{e(label)}</span>')
    return "".join(out)


def feed_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return (
            '<p class="dim">Nothing here yet. If the feed has been running a '
            "while and is still empty, that is itself a finding — check "
            "<a href='/health'>Health</a> for the poll log.</p>"
        )
    out = [
        "<table><thead><tr><th>Verdict</th><th>Listing</th><th>Reference</th>"
        '<th class="num">Price</th><th class="num">Max bid</th>'
        '<th class="num">Headroom</th><th>Label</th></tr></thead><tbody>'
    ]
    for r in rows:
        caveats = json.loads(r["caveats_json"] or "[]")
        cls = "" if r["verdict"] == "DEAL" else ' class="muted-row"'
        head = r["headroom_pence"]
        out.append(
            f"<tr{cls}><td>{verdict_chip(r['verdict'])}</td>"
            f"<td><a href='/item/{e(urllib.parse.quote(r['item_id']))}'>"
            f"{e(r['title'][:88])}</a> {listing_tags(r['scope'], r['bracelet'])}<br>"
            f"<span class='dim'>{'Auction' if r['is_auction'] else 'BIN'} · "
            f"{e(r['seller_account_type'].title() or 'seller type unknown')} · "
            f"{e(r['primary_reason'][:110])}</span></td>"
            f"<td>{e(r['catalogue_display'] or '—')}<br>"
            f"{caveat_tags(caveats, only_important=True)}</td>"
            f"<td class='num'>{fmt(r['eff_price_pence'])}<br>"
            f"<span class='dim'>{e(r['price_basis'])}</span></td>"
            f"<td class='num'>{fmt(r['mab_pence'])}</td>"
            f"<td class='num'>{fmt(head) if head is not None else '—'}</td>"
            f"<td>{label_buttons(r['item_id'], r['labels'])}</td></tr>"
        )
    return "".join(out) + "</tbody></table>"


LABELS = [
    ("bad_reject", "should have passed"),
    ("bad_pass", "should not have"),
    ("fmv_wrong", "FMV wrong"),
]


def label_buttons(item_id: str, existing: str | None) -> str:
    """Labelling is one click (requirement 6).

    A confirmation step here would be worse than useless: the cost of a
    mislabel is one row in a table the operator can see and re-label, and any
    friction at all means the labels never get applied, which costs the whole
    precision-and-recall measurement Phase 1 exists to produce.
    """
    have = set((existing or "").split(","))
    out = []
    for key, text in LABELS:
        mark = "✓ " if key in have else ""
        out.append(
            f"<form class='inline' method='post' action='/label'>"
            f"<input type='hidden' name='item_id' value='{e(item_id)}'>"
            f"<input type='hidden' name='label' value='{key}'>"
            f"<button title='{e(text)}'>{mark}{e(text)}</button></form> "
        )
    return "".join(out)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def render_feed(engine: Engine, params: dict) -> str:
    verdict = params.get("verdict", [""])[0]
    brand = params.get("brand", [""])[0]
    q = params.get("q", [""])[0]
    rows = engine.db.feed(verdict=verdict, brand=brand, query=q, limit=250)
    counts = engine.db.verdict_counts()

    options = ["", "DEAL"] + [
        r["verdict"] for r in counts if r["verdict"].startswith("REJECT_")
    ]
    opts = "".join(
        f"<option value='{e(o)}'{' selected' if o == verdict else ''}>"
        f"{e(o or 'everything')}</option>"
        for o in dict.fromkeys(options)
    )
    brands = sorted({r["catalogue_display"].split(" ")[0] for r in rows if r["catalogue_display"]})
    bopts = "".join(
        f"<option value='{e(b)}'{' selected' if b == brand else ''}>{e(b)}</option>"
        for b in ["", *brands]
    )
    tally = " · ".join(f"{e(r['verdict'])} {r['n']}" for r in counts) or "nothing yet"

    return f"""
<form class="filters" method="get">
  <select name="verdict">{opts}</select>
  <select name="brand">{bopts}</select>
  <input name="q" placeholder="title contains" value="{e(q)}">
  <button>filter</button>
  <span class="dim">last 14 days: {tally}</span>
</form>
{feed_table(rows)}
<p class="dim">Rejections are shown because they are how the blacklist and the
FMV table get debugged. Scope and bracelet tags are read from the title and do
not move the maximum bid.</p>"""


def render_item(engine: Engine, item_id: str) -> str:
    row = engine.db.item(item_id)
    if row is None:
        return "<p>No such listing.</p>"
    caveats = json.loads(row["caveats_json"] or "[]")
    gates = json.loads(row["gates_json"] or "[]")
    derivation = json.loads(row["derivation_json"] or "null")

    gate_rows = "".join(
        f"<tr><td>{'✓' if g['passed'] else '✗'}</td><td>{e(g['name'])}</td>"
        f"<td class='dim'>{e(g['detail'])}</td></tr>"
        for g in gates
    )

    ledger = ""
    if derivation:
        lines = "".join(
            f"<tr><td>{e(label)}</td><td>{fmt(value)}</td></tr>"
            for label, value in derivation["lines"]
        )
        assumed = "" if derivation["condition_stated"] else " (not stated, assumed)"
        ledger = f"""<div class="card"><h2>Valuation</h2>
<p class="dim">Reference FMV {fmt(derivation['fmv_reference'])} ×
{e(derivation['condition'])}{assumed} = {fmt(derivation['effective_fmv'])} effective.</p>
<table class="ledger">{lines}</table></div>"""

    labels = engine.db.labels_for(item_id)
    label_log = "".join(
        f"<li>{e(r['label'])} <span class='dim'>{e(r['created_at_utc'][:16])} "
        f"(verdict was {e(r['verdict_at_time'])})</span></li>"
        for r in labels
    ) or "<li class='dim'>none</li>"

    return f"""
<h2 style="margin-top:0">{e(row['title'])}</h2>
<p>{verdict_chip(row['verdict'])} {caveat_tags(caveats)}<br>
<span class="dim">{e(row['primary_reason'])}</span></p>
<p><a href="{e(row['web_url'])}" target="_blank" rel="noopener">Open on eBay ↗</a>
&nbsp;·&nbsp; {label_buttons(item_id, ','.join(r['label'] for r in labels))}</p>

<div class="grid">
  <div class="card"><h2>Listing</h2>
    <table class="ledger">
      <tr><td>Asking / current</td><td>{fmt(row['price_pence'])}</td></tr>
      <tr><td>Postage</td><td>{fmt(row['shipping_pence'])}</td></tr>
      <tr><td>Gate used</td><td>{e(row['price_basis'])} {fmt(row['eff_price_pence'])}</td></tr>
      <tr><td>Format</td><td>{'Auction' if row['is_auction'] else 'Buy It Now'}</td></tr>
      <tr><td>Bids</td><td>{e(row['bid_count'] if row['bid_count'] is not None else '—')}</td></tr>
      <tr><td>Ends</td><td>{e((row['end_time_utc'] or '—')[:16])}</td></tr>
      <tr><td>Condition</td><td>{e(row['condition_raw'] or '—')}</td></tr>
      <tr><td>Scope / bracelet</td><td>{listing_tags(row['scope'], row['bracelet']) or '—'}</td></tr>
      <tr><td>Location</td><td>{e(row['item_location_country'] or '—')}</td></tr>
    </table></div>
  <div class="card"><h2>Seller</h2>
    <table class="ledger">
      <tr><td>Account type</td><td>{e(row['seller_account_type'] or 'not returned')}</td></tr>
      <tr><td>Feedback</td><td>{(str(row['seller_feedback_pct_x100'] / 100) + '%') if row['seller_feedback_pct_x100'] is not None else 'not returned'}</td></tr>
      <tr><td>Ratings</td><td>{e(row['seller_feedback_score'] if row['seller_feedback_score'] is not None else 'not returned')}</td></tr>
      <tr><td>Buyer protection</td><td>{'not charged (business)' if row['seller_account_type'] == 'BUSINESS' else 'charged (private)'}</td></tr>
    </table></div>
  <div class="card"><h2>Reference</h2>
    <table class="ledger">
      <tr><td>Matched</td><td>{e(row['catalogue_display'] or 'none')}</td></tr>
      <tr><td>Key</td><td><code>{e(row['catalogue_key'] or '—')}</code></td></tr>
      <tr><td>FMV</td><td>{fmt(row['fmv_pence'])}</td></tr>
      <tr><td>Verified</td><td>{'yes' if row['fmv_verified'] else '<b>no — an estimate</b>'}</td></tr>
      <tr><td>Scored under</td><td><code>{e(row['config_fingerprint'])}</code></td></tr>
    </table></div>
</div>

<div class="card"><h2>Gates</h2><table>{gate_rows}</table></div>
{ledger}
<div class="card"><h2>Your labels</h2><ul>{label_log}</ul></div>"""


def render_missing(engine: Engine) -> str:
    titles = engine.db.unmatched_titles()
    work = engine.catalogue.missing_worklist(titles)
    if not work:
        return "<p class='dim'>Every listing seen so far matched a reference.</p>"
    rows = "".join(
        f"<tr><td class='num'>{n}</td><td><b>{e(label)}</b></td>"
        f"<td class='dim'>{'<br>'.join(e(x[:88]) for x in eg)}</td></tr>"
        for label, n, eg in work
    )
    return f"""
<div class="card"><h2>What the catalogue does not price</h2>
<p>{len(titles)} listings matched no reference, grouped by brand and the model
word that follows it, most frequent first. This is the add-a-reference
worklist, ordered by real UK traffic rather than by guesswork.</p>
<p class="dim">The grouping is a rough heuristic and nothing derived from it
reaches a valuation — it only tells you where to look. Add an entry to
<code>{e(C.CATALOGUE_PATH.name)}</code>, reload, and re-score.</p>
<p><b>Add a reference only when you can put a real number on it.</b> An
invented FMV does not widen coverage, it widens the surface of numbers you
cannot trust — and unpriced is a more honest output than confidently wrong.</p>
</div>
<table><thead><tr><th class="num">Listings</th><th>Looks like</th>
<th>Examples</th></tr></thead><tbody>{rows}</tbody></table>"""


def render_catalogue(engine: Engine) -> str:
    usage = engine.db.catalogue_usage()
    refs = sorted(
        engine.catalogue.references,
        key=lambda r: (r.verified, -usage.get(r.key, 0), r.brand),
    )
    rows = "".join(
        f"<tr><td>{'✓' if r.verified else '<b>—</b>'}</td>"
        f"<td>{e(r.display)}</td><td><code>{e(r.key)}</code></td>"
        f"<td class='num'>{fmt(r.point)}</td>"
        f"<td class='num'>{f'{fmt(r.fmv_low)} – {fmt(r.fmv_high)}' if r.is_band else ''}</td>"
        f"<td class='num'>{usage.get(r.key, 0)}</td>"
        f"<td class='dim'>{e(r.notes[:120])}</td></tr>"
        for r in refs
    )
    return f"""
<div class="card"><h2>Curating this</h2>
<p>Rows are ordered <b>unverified first, then by how many real listings each
one has actually priced</b>. Work down from the top; the first few cover most
of the traffic.</p>
<p>Edit <code>{e(C.CATALOGUE_PATH.name)}</code>, set <code>verified = true</code>,
then press reload and re-score. Re-scoring is offline and free — it replays
every listing already captured through your new number, so you see immediately
what it would have done to real traffic.</p>
<form class="inline" method="post" action="/reload"><button>reload catalogue</button></form>
<form class="inline" method="post" action="/rescore"><button>re-score everything</button></form>
</div>
<table><thead><tr><th>Ver.</th><th>Reference</th><th>Key</th>
<th class="num">FMV</th><th class="num">Band</th><th class="num">Listings priced</th>
<th>Notes</th></tr></thead><tbody>{rows}</tbody></table>"""


def render_health(engine: Engine) -> str:
    h = engine.health()
    polls = "".join(
        f"<tr><td>{e(r['started_at_utc'][11:19])}</td><td>{e(r['kind'])}</td>"
        f"<td class='num'>{r['http_calls']}</td><td class='num'>{r['items_seen']}</td>"
        f"<td class='num'>{r['items_new']}</td><td class='num'>{r['alerts']}</td>"
        f"<td class='dim'>{e((r['error'] or 'ok')[:140])}</td></tr>"
        for r in engine.db.recent_polls()
    )
    notes = "".join(
        f"<tr><td>{e(r['at_utc'][11:19])}</td><td>{e(r['kind'])}</td>"
        f"<td>{'sent' if r['ok'] else 'FAILED'}</td>"
        f"<td class='dim'>{e(r['detail'][:120])}</td></tr>"
        for r in engine.db.recent_notifications()
    ) or "<tr><td class='dim' colspan=4>none yet</td></tr>"

    stale = (
        "<b style='color:var(--warn)'>STALE — ingestion has stopped</b>"
        if h["stale"]
        else "healthy"
    )
    return f"""
<div class="grid">
<div class="card"><h2>Ingestion</h2><table class="ledger">
<tr><td>State</td><td>{stale}</td></tr>
<tr><td>Last successful poll</td><td>{e(h['last_successful_poll'] or 'never')}</td></tr>
<tr><td>Running since</td><td>{e(h['started_at'])}</td></tr>
<tr><td>Browse calls today</td><td>{h['calls_used']} of {h['calls_used'] + h['calls_remaining']}</td></tr>
<tr><td>Last error</td><td>{e((h['last_error'] or 'none')[:200])}</td></tr>
</table></div>
<div class="card"><h2>Alerting</h2><table class="ledger">
<tr><td>Channel</td><td>{e(h['notifier'])}</td></tr>
</table></div>
<div class="card"><h2>Valuation</h2><table class="ledger">
<tr><td>Catalogue entries</td><td>{h['catalogue_entries']}</td></tr>
<tr><td>Unverified</td><td>{h['catalogue_unverified']}</td></tr>
<tr><td>Constant fingerprint</td><td><code>{e(h['fingerprint'])}</code></td></tr>
</table></div>
</div>
<div class="card"><h2>Recent polls</h2><table><thead><tr><th>Time</th><th>Kind</th>
<th class="num">Calls</th><th class="num">Seen</th><th class="num">New</th>
<th class="num">Alerts</th><th>Error</th></tr></thead><tbody>{polls}</tbody></table></div>
<div class="card"><h2>Notifications</h2><table><tbody>{notes}</tbody></table></div>"""


def render_constants(engine: Engine) -> str:
    def row(name, value, note=""):
        flag = C.UNVERIFIED.get(name)
        return (
            f"<tr><td><code>{e(name)}</code></td><td class='num'>{e(value)}</td>"
            f"<td class='dim'>{e(flag or note)}</td>"
            f"<td>{'<span class=\"tag bad\">unverified</span>' if flag else ''}</td></tr>"
        )

    items = [
        ("FVF_BP", f"{C.FVF_BP / 100:.2f}%"),
        ("REG_OP_FEE_BP", f"{C.REG_OP_FEE_BP / 100:.2f}%"),
        ("AD_RATE_BP", f"{C.AD_RATE_BP / 100:.2f}%"),
        ("ORDER_FEE", fmt(C.ORDER_FEE)),
        ("FEE_VAT_MULT_BP", f"×{C.FEE_VAT_MULT_BP / 10000:.2f}"),
        ("BUYER_PROTECTION_FIXED", fmt(C.BUYER_PROTECTION_FIXED)),
        (
            "BUYER_PROTECTION_TIERS",
            " / ".join(
                f"to {fmt(u)} @ {bp / 100:.2f}%" for u, bp in C.BUYER_PROTECTION_TIERS
            ),
        ),
        ("TARGET_PROFIT_MARGIN_BP", f"{C.TARGET_PROFIT_MARGIN_BP / 100:.2f}%"),
        ("MIN_ABSOLUTE_PROFIT", fmt(C.MIN_ABSOLUTE_PROFIT)),
        ("INBOUND_POSTAGE_ESTIMATE", fmt(C.INBOUND_POSTAGE_ESTIMATE)),
        ("OUTBOUND_POSTAGE", fmt(C.OUTBOUND_POSTAGE)),
        ("COND_MULT", str(C.COND_MULT)),
        ("MIN_SELLER_FEEDBACK_PCT_X100", f"{C.MIN_SELLER_FEEDBACK_PCT_X100 / 100:.2f}%"),
        ("MIN_SELLER_FEEDBACK_SCORE", C.MIN_SELLER_FEEDBACK_SCORE),
        ("SEARCH_MIN_PRICE", fmt(C.SEARCH_MIN_PRICE)),
        ("SEARCH_MAX_PRICE", fmt(C.SEARCH_MAX_PRICE)),
        ("EBAY_CATEGORY_IDS", C.EBAY_CATEGORY_IDS),
        ("WEEKLY_SPEND_CAP", fmt(C.WEEKLY_SPEND_CAP)),
        ("DAILY_BURST_CAP", fmt(C.DAILY_BURST_CAP)),
        ("MAX_EXPOSURE_PER_REF", fmt(C.MAX_EXPOSURE_PER_REF)),
        ("VAT_REGISTERED", C.VAT_REGISTERED),
    ]
    body = "".join(row(n, v) for n, v in items)
    return f"""
<div class="card"><h2>Where these live</h2>
<p>All of them in <code>src/watchsniper/config.py</code>, which is the only
place any of them exists. This page reads that module — it does not restate it —
so a value shown here cannot disagree with the value used to price a listing.</p>
<p>The spend caps are displayed and <b>not enforced</b>, because nothing in
Phase 1 can spend money. They are here so that the figure has one owner before
anything needs to enforce it.</p></div>
<table><thead><tr><th>Constant</th><th class="num">Value</th>
<th>What would settle it</th><th></th></tr></thead><tbody>{body}</tbody></table>"""


def render_outcomes(engine: Engine) -> str:
    rows = engine.db.query("SELECT * FROM outcomes ORDER BY id DESC")
    body = "".join(
        f"<tr><td>{e(r['title'][:60] or r['item_id'])}</td>"
        f"<td class='num'>{fmt(r['buy_price_pence'])}</td>"
        f"<td class='num'>{fmt(r['predicted_fmv_pence'])}</td>"
        f"<td class='num'>{fmt(r['sell_price_pence'])}</td>"
        f"<td class='num'>{fmt(r['fees_paid_pence'])}</td>"
        f"<td class='num'>{fmt((r['sell_price_pence'] or 0) - (r['buy_price_pence'] or 0) - (r['fees_paid_pence'] or 0)) if r['sell_price_pence'] else '—'}</td>"
        f"<td class='dim'>{e(r['note'][:80])}</td></tr>"
        for r in rows
    ) or "<tr><td colspan=7 class='dim'>nothing logged yet</td></tr>"
    return f"""
<div class="card"><h2>Log a buy or a sale</h2>
<p class="dim">This is the only unbiased signal the system will ever have.
Ten completed cycles is what tells you whether predicted margin matches
realised margin — and if it does not, the answer is to fix the constants, not
to buy more.</p>
<form method="post" action="/outcome">
<p><input name="item_id" placeholder="eBay item id" size="18">
<input name="title" placeholder="what it was" size="34"></p>
<p><input name="buy_price" placeholder="paid £" size="9">
<input name="predicted_fmv" placeholder="predicted FMV £" size="14">
<input name="sell_price" placeholder="sold for £" size="11">
<input name="fees_paid" placeholder="fees £" size="9"></p>
<p><input name="note" placeholder="note" size="60"> <button>log it</button></p>
</form></div>
<table><thead><tr><th>Item</th><th class="num">Paid</th>
<th class="num">Predicted FMV</th><th class="num">Sold</th><th class="num">Fees</th>
<th class="num">Realised</th><th>Note</th></tr></thead><tbody>{body}</tbody></table>"""


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    engine: Engine
    server_version = "watchsniper"

    def log_message(self, fmt_: str, *args) -> None:  # quieter default logging
        pass

    def _send(self, body: bytes, status: int = 200, ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, to: str) -> None:
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def _back(self) -> None:
        """Return to the page the form was posted from.

        Only the path and query of the Referer are used. Redirecting to a
        whole header value would let anything that can make this browser POST
        choose where it lands afterwards, and there is no reason to allow it.
        """
        ref = urllib.parse.urlparse(self.headers.get("Referer", ""))
        target = ref.path or "/"
        if ref.query:
            target += "?" + ref.query
        self._redirect(target if target.startswith("/") else "/")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        path = parsed.path
        eng = self.engine
        try:
            if path == "/":
                self._send(page("Feed", render_feed(eng, params), eng))
            elif path.startswith("/item/"):
                # eBay item ids contain pipes ("v1|1234|0"), so the path
                # segment arrives percent-encoded and must be decoded.
                item_id = urllib.parse.unquote(path[len("/item/"):])
                self._send(page("Listing", render_item(eng, item_id), eng))
            elif path == "/catalogue":
                self._send(page("Catalogue", render_catalogue(eng), eng))
            elif path == "/missing":
                self._send(page("Not priced", render_missing(eng), eng))
            elif path == "/health":
                self._send(page("Health", render_health(eng), eng))
            elif path == "/constants":
                self._send(page("Constants", render_constants(eng), eng))
            elif path == "/outcomes":
                self._send(page("Outcomes", render_outcomes(eng), eng))
            elif path == "/api/health":
                h = eng.health()
                self._send(
                    json.dumps(
                        {
                            k: (v.isoformat() if hasattr(v, "isoformat") else v)
                            for k, v in h.items()
                        }
                    ).encode(),
                    200 if not h["stale"] else 503,
                    "application/json",
                )
            else:
                self._send(page("Not found", "<p>No such page.</p>", eng), 404)
        except Exception as exc:  # a broken page must not take the server down
            import traceback

            self._send(
                page(
                    "Error",
                    f"<pre>{e(traceback.format_exc(limit=6))}</pre>",
                    eng,
                ),
                500,
            )
            del exc

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        get = lambda k: form.get(k, [""])[0].strip()  # noqa: E731
        eng = self.engine
        path = urllib.parse.urlparse(self.path).path

        if path == "/label":
            eng.db.add_label(get("item_id"), get("label"), get("note"))
            self._back()
        elif path == "/reload":
            eng.reload_catalogue()
            self._redirect("/catalogue")
        elif path == "/rescore":
            eng.rescore_all()
            self._redirect("/catalogue")
        elif path == "/outcome":
            money = lambda k: parse_gbp(get(k)) if get(k) else None  # noqa: E731
            eng.db.add_outcome(
                item_id=get("item_id"),
                title=get("title"),
                buy_price_pence=money("buy_price"),
                predicted_fmv_pence=money("predicted_fmv"),
                sell_price_pence=money("sell_price"),
                fees_paid_pence=money("fees_paid"),
                bought_at_utc=None,
                sold_at_utc=None,
                note=get("note"),
            )
            self._redirect("/outcomes")
        else:
            self._send(b"no", 404, "text/plain")


def serve(engine: Engine, host: str, port: int) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"engine": engine})
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
