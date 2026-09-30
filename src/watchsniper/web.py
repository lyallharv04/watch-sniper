"""The dashboard. Server-rendered HTML from the standard library.

The presentation layer holds no arithmetic (requirement 16). Every figure on
every page is a stored integer number of pence formatted for display; nothing
here recomputes an FMV, a maximum bid or a margin. That is not a coding
convention, it is the reason there is no JavaScript framework here: a
presentation layer that *can* recompute a valuation eventually will, and then
what the operator sees and what the system decided drift apart.

There is no login here and there is not meant to be. The service binds to
loopback and is reached through a Cloudflare Tunnel with Cloudflare Access in
front of it (DECISIONS A18), which does the authentication before a request
arrives.

The look follows the Claude Design handoff (docs/DASHBOARD_BRIEF.md was its
brief): phone first, a bottom navigation bar within thumb reach that becomes a
top bar on wide screens, a warm palette with a dark theme that follows the
system setting, and every uncertainty kept visibly attached to the number it
qualifies.
"""

from __future__ import annotations

import functools
import html
import json
import sqlite3
import struct
import urllib.parse
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as C
from .models import parse_ts
from .money import fmt, fmt_usd_micro, parse_gbp
from .poller import Engine

MONO = "ui-monospace,Menlo,Consolas,monospace"

CSS = """
:root{--bg:#f5ead8;--sf:#ebddc5;--card:#f9f4ed;--ink:#201e1d;--mute:#645c50;
--line:rgba(32,30,29,.13);--acc:#c67139;--accInk:#8c491a;--btnInk:#fff8ef;
--pos:#56633f;--sageBg:#e1eecc;--dealBg:#56633f;--dealInk:#f0fae1;--chip:#e4d8c4;
--chipInk:#474238;--neg:#9a4a17;--warnBg:#ffe1d0;--warnInk:#643312;color-scheme:light}
@media(prefers-color-scheme:dark){:root{--bg:#171411;--sf:#231f1b;--card:#2c2722;
--ink:#f3e9da;--mute:#b8ab96;--line:rgba(243,233,218,.13);--acc:#e0905a;
--accInk:#f6a06b;--btnInk:#1a120b;--pos:#bccc9f;--sageBg:#2f3824;--dealBg:#aebf92;
--dealInk:#1b1f12;--chip:#3a332c;--chipInk:#dcd3c4;--neg:#f6a06b;--warnBg:#3b2618;
--warnInk:#ffc6a5;color-scheme:dark}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.4 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
-webkit-font-smoothing:antialiased;
padding:env(safe-area-inset-top) env(safe-area-inset-right)
calc(72px + env(safe-area-inset-bottom)) env(safe-area-inset-left)}
a{color:inherit}a:hover{opacity:.8}
.top{padding:4px 16px 0;min-height:32px;display:flex;justify-content:space-between;
align-items:center;gap:10px}
.brand{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;
color:var(--mute)}
.back{font-size:14px;font-weight:600;text-decoration:none;color:var(--accInk);
min-height:40px;display:flex;align-items:center}
.nav{position:fixed;left:0;right:0;bottom:0;z-index:10;background:var(--card);
border-top:1px solid var(--line);display:grid;grid-template-columns:repeat(6,minmax(0,1fr));
padding:4px calc(4px + env(safe-area-inset-right)) max(10px,env(safe-area-inset-bottom))
calc(4px + env(safe-area-inset-left))}
.nav a{min-height:48px;display:flex;align-items:center;justify-content:center;
text-align:center;font-size:10.5px;line-height:1.15;font-weight:500;
text-decoration:none;color:var(--mute);border-radius:16px;padding:0 2px}
.nav a.on{font-weight:800;color:var(--ink);background:var(--sf)}
main{margin:0 auto}
.banners{padding:10px 12px 0}
.bbox{background:var(--warnBg);color:var(--warnInk);border-radius:20px;padding:10px 14px;
display:flex;flex-direction:column;gap:6px}
.banner{display:flex;gap:8px;align-items:baseline;font-size:12.5px;line-height:1.35}
.banner::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--acc);
flex:none;transform:translateY(-1px)}
.head{padding:18px 16px 0;display:flex;flex-direction:column;gap:12px}
.head.tight{gap:6px}
.titles{display:flex;flex-direction:column;gap:10px}
h1.page{margin:0;font-size:28px;font-weight:800;letter-spacing:-.02em}
.sub{font-size:12.5px;color:var(--mute)}
.seg{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:var(--sf);
border-radius:999px;padding:4px;gap:2px}
.seg a{min-height:40px;display:flex;align-items:center;justify-content:center;
border-radius:999px;font-size:13px;font-weight:550;text-decoration:none;color:var(--mute)}
.seg a.on{font-weight:750;background:var(--ink);color:var(--bg)}
details.filters{background:var(--card);border-radius:22px;padding:0 14px}
details.filters summary{min-height:46px;display:flex;align-items:center;
justify-content:space-between;gap:10px;font-size:14px;font-weight:600;cursor:pointer;
list-style:none}
details.filters summary::-webkit-details-marker{display:none}
summary .sum{font-size:12.5px;font-weight:500;color:var(--mute);text-align:right}
.form{display:flex;flex-direction:column;gap:10px}
details .form{padding:2px 0 14px}
.field{display:flex;flex-direction:column;gap:4px;font-size:12px;color:var(--mute)}
.input{min-height:44px;border-radius:999px;border:1px solid var(--line);
background:var(--sf);color:var(--ink);padding:0 14px;font:inherit;font-size:14px;
min-width:0;width:100%}
.big .input{padding:0 16px;font-size:15px}
textarea.input{min-height:80px;border-radius:20px;padding:12px 16px;resize:vertical}
.two{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:8px}
.two form{display:flex}
.btn{min-height:44px;border-radius:999px;border:0;background:var(--ink);
color:var(--bg);font:inherit;font-size:14px;font-weight:700;cursor:pointer;flex:1}
.btn.ghost{border:1px solid var(--line);background:transparent;color:var(--ink);
font-size:13px;font-weight:650}
.btn.tall{min-height:48px;font-size:15px}
.tally{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
.tally div{background:var(--sf);border-radius:14px;padding:7px 10px;display:flex;
flex-direction:column}
.tally b{font-size:18px;font-weight:800;font-variant-numeric:tabular-nums}
.tally span{font-size:10.5px;color:var(--mute);letter-spacing:.02em}
.caution{border:1.5px solid var(--acc);border-radius:20px;padding:10px 14px;
font-size:13px;line-height:1.4;color:var(--accInk)}
.list{padding:10px 12px 18px;display:flex;flex-direction:column;gap:10px}
.list.tight{padding-top:12px;gap:8px}
.card{background:var(--card);border-radius:24px}
.empty{padding:16px;font-size:14px;color:var(--mute)}
.row{padding:14px 14px 10px;display:flex;flex-direction:column;gap:10px}
.rtop{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.rv{display:flex;flex-direction:column;gap:5px;min-width:0}
.chip{align-self:flex-start;font-size:11.5px;font-weight:800;letter-spacing:.05em;
padding:3px 10px;border-radius:999px;background:var(--chip);color:var(--chipInk)}
.chip.deal{background:var(--dealBg);color:var(--dealInk)}
.reason{font-size:12.5px;color:var(--mute);line-height:1.3}
.below{display:flex;flex-direction:column;align-items:flex-end;flex:none}
.below b{font-size:26px;line-height:1;font-weight:800;letter-spacing:-.02em;
font-variant-numeric:tabular-nums}
.below span{font-size:11px;color:var(--mute);margin-top:3px}
.rtitle{display:flex;flex-direction:column;gap:3px}
.rtitle a{font-size:15.5px;font-weight:650;line-height:1.3;text-decoration:none;
color:var(--ink);text-wrap:pretty}
.meta{font-size:12.5px;color:var(--mute);display:flex;flex-wrap:wrap;gap:4px 8px;
align-items:center}
.tag{border:1px solid var(--line);border-radius:999px;padding:1px 8px;font-size:11px}
.strip{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));background:var(--sf);
border-radius:18px;padding:10px 4px}
.strip>div{display:flex;flex-direction:column;padding:0 10px;gap:1px}
.strip>div+div{border-left:1px solid var(--line)}
.lbl{font-size:10.5px;color:var(--mute);letter-spacing:.04em;text-transform:uppercase}
.strip b{font-size:16.5px;font-weight:750;font-variant-numeric:tabular-nums}
.strip small,.big3 small{font-size:11px;color:var(--mute)}
.pos{color:var(--pos)}.neg{color:var(--neg)}.nil{color:var(--mute)}
.cavs{display:flex;flex-wrap:wrap;gap:5px}
.cav{font-size:11px;font-weight:600;padding:3px 9px;border-radius:999px;
background:var(--chip);color:var(--chipInk)}
.cav.warn{font-weight:700;letter-spacing:.03em;background:var(--warnBg);color:var(--warnInk)}
.labels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px}
.labels button{min-height:44px;border-radius:999px;border:1px solid var(--line);
background:transparent;color:var(--mute);font:inherit;font-size:12px;font-weight:600;
line-height:1.15;padding:0 6px;cursor:pointer}
.labels.strong button{color:var(--ink)}
.labels button.on{background:var(--sf);color:var(--ink);border-color:transparent}
.ihead{padding:18px 16px 0;display:flex;flex-direction:column;gap:10px}
.ihead h1{margin:0;font-size:22px;line-height:1.2;font-weight:800;letter-spacing:-.01em}
.ihead .reason{font-size:14px}
.summary{padding:14px 12px 0}
.summary .card{border-radius:26px;padding:16px;display:flex;flex-direction:column;gap:14px}
.big3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px}
.big3>div{display:flex;flex-direction:column;gap:2px}
.big3 b{font-size:21px;font-weight:800;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.fmvline{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
border-top:1px solid var(--line);padding-top:12px;font-size:13px;color:var(--mute)}
.fmvline strong{color:var(--ink);font-variant-numeric:tabular-nums}
.fmvline .pctb{font-size:16px}
.actions{padding:12px 12px 0;display:flex;flex-direction:column;gap:8px}
.ebay{min-height:52px;border-radius:999px;background:var(--acc);color:var(--btnInk);
display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:750;
text-decoration:none}
.secs{padding:14px 12px 18px;display:flex;flex-direction:column;gap:10px}
.sec{padding:14px 16px 8px;display:flex;flex-direction:column}
.sec.pad{padding:14px 16px;gap:10px}
.sec h3{margin:0 0 6px;font-size:13px;font-weight:800;letter-spacing:.06em;
text-transform:uppercase;color:var(--mute)}
.sec.pad h3{margin:0}
.kv{display:flex;justify-content:space-between;gap:12px;padding:8px 0;
border-top:1px solid var(--line);font-size:14px}
.kv>span:first-child{color:var(--mute)}
.kv>span:last-child{font-weight:600;text-align:right;font-variant-numeric:tabular-nums;
overflow-wrap:anywhere}
.kvlist .kv:first-child{border-top:0}
.mono{font-family:"""+MONO+""";font-size:13px}
.accent{color:var(--accInk)}
.gate{display:grid;grid-template-columns:auto minmax(0,1fr);gap:2px 10px;padding:9px 0;
border-top:1px solid var(--line);align-items:baseline}
.pill{font-size:11px;font-weight:800;letter-spacing:.04em;padding:2px 8px;
border-radius:999px;background:var(--sageBg);color:var(--pos)}
.pill.fail{background:var(--warnBg);color:var(--warnInk)}
.gname{font:700 13px """+MONO+"""}
.gdetail{grid-column:2;font-size:13px;color:var(--mute);font-variant-numeric:tabular-nums;
overflow-wrap:anywhere}
.valhead{font-size:13.5px;line-height:1.4}
.ledger{background:var(--sf);border-radius:18px;padding:6px 12px;display:flex;
flex-direction:column}
.lr{display:flex;justify-content:space-between;align-items:baseline;gap:10px;
padding:7px 0;font-size:13.5px}
.lr small{display:block;font-size:11px;color:var(--mute)}
.lr .v{font-variant-numeric:tabular-nums;white-space:nowrap}
.lr.subtotal{font-weight:750;border-top:1px solid var(--line)}
.lr.final{padding:10px;font-size:15px;font-weight:750;margin:6px -6px 4px;
border-radius:14px;background:var(--sageBg);color:var(--pos)}
.warnnote{font-size:12px;color:var(--warnInk);background:var(--warnBg);
border-radius:14px;padding:8px 12px}
.lab{display:flex;flex-direction:column;gap:2px;padding:9px 0;border-top:1px solid var(--line)}
.lab b{font-size:14px;font-weight:650}.lab span{font-size:12.5px;color:var(--mute)}
.cat{border-radius:22px;padding:12px 14px;display:flex;flex-direction:column;gap:8px}
.cattop{display:flex;justify-content:space-between;gap:10px;align-items:flex-start}
.cattop>div{display:flex;flex-direction:column;gap:2px;min-width:0}
.cattop .ref{font-size:15px;font-weight:700;line-height:1.25}
.key{font:12px """+MONO+""";color:var(--mute);overflow-wrap:anywhere}
.ver{flex:none;font-size:11px;font-weight:700;padding:3px 9px;border-radius:999px;
background:var(--warnBg);color:var(--warnInk)}
.ver.ok{background:var(--sageBg);color:var(--pos)}
.cstrip{display:grid;grid-template-columns:1.1fr 1.3fr .8fr .8fr;gap:4px;
background:var(--sf);border-radius:16px;padding:8px 10px}
.cstrip>div{display:flex;flex-direction:column}
.cstrip .lbl{font-size:10px}
.cstrip b{font-size:14.5px;font-weight:750;font-variant-numeric:tabular-nums}
.cstrip small{font-size:10.5px;color:var(--mute)}
.cstrip.two{grid-template-columns:1fr}
.band{font-size:12.5px;color:var(--accInk);line-height:1.35}
.notes{font-size:12.5px;color:var(--mute);line-height:1.35}
.cattable{display:none}
.grp{border-radius:22px;padding:12px 14px;display:flex;flex-direction:column;gap:8px}
.grphead{display:flex;justify-content:space-between;align-items:baseline;gap:10px}
.grphead span{font-size:16px;font-weight:750}
.grphead b{font-size:22px;font-weight:800;font-variant-numeric:tabular-nums}
.examples{display:flex;flex-direction:column;gap:4px}
.examples div{font-size:12.5px;color:var(--mute);line-height:1.35;padding-left:10px;
border-left:2px solid var(--line)}
.outcome{padding:9px 0;border-top:1px solid var(--line);display:flex;flex-direction:column;gap:3px}
.outcome b{font-size:14px;font-weight:650}
.outcome span{font-size:12.5px;color:var(--mute);font-variant-numeric:tabular-nums}
.status{border-radius:24px;padding:16px;display:flex;gap:12px;align-items:center;
background:var(--sageBg);color:var(--pos)}
.status.stale{background:var(--acc);color:var(--btnInk)}
.status .dot{width:14px;height:14px;border-radius:50%;flex:none;background:currentColor}
.status>div{display:flex;flex-direction:column;gap:2px}
.status b{font-size:20px;font-weight:800;letter-spacing:-.01em}
.status span{font-size:13px;opacity:.85}
.jsonlink{font-size:13px;color:var(--accInk);padding:10px 0 6px;font-weight:600}
.pollh,.pollr{display:grid;grid-template-columns:48px 58px repeat(4,minmax(0,1fr));gap:4px}
.pollh{font-size:10.5px;color:var(--mute);letter-spacing:.04em;text-transform:uppercase;
padding:4px 0}
.pollh span:nth-child(n+3),.pollr span:nth-child(n+3){text-align:right}
.poll{border-top:1px solid var(--line);padding:8px 0;display:flex;flex-direction:column;gap:3px}
.pollr{font-size:13px;font-variant-numeric:tabular-nums}
.pollerr{font-size:12px;color:var(--accInk);overflow-wrap:anywhere}
.note{border-top:1px solid var(--line);padding:9px 0;display:flex;justify-content:space-between;
gap:10px;align-items:center}
.note>div{display:flex;flex-direction:column;gap:1px;min-width:0}
.note b{font-size:13.5px;font-weight:600;line-height:1.3}
.note span{font-size:12px;color:var(--mute)}
.sent{flex:none;font-size:11px;font-weight:800;letter-spacing:.04em;padding:3px 9px;
border-radius:999px;background:var(--sageBg);color:var(--pos)}
.sent.failed{background:var(--acc);color:var(--btnInk)}
.const{border-radius:22px;padding:12px 14px;display:flex;flex-direction:column;gap:6px}
.const>div{display:flex;justify-content:space-between;gap:10px;align-items:center}
.const .name{font:700 12.5px """+MONO+""";color:var(--mute);overflow-wrap:anywhere}
.const .val{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums;
letter-spacing:-.01em;overflow-wrap:anywhere}
.const .how{font-size:12.5px;color:var(--mute);line-height:1.4}
pre{white-space:pre-wrap;font:12px """+MONO+"""}
@media(min-width:900px){
body{padding-bottom:env(safe-area-inset-bottom)}
.top{position:sticky;top:0;z-index:10;background:var(--bg);padding:14px 28px;
border-bottom:1px solid var(--line);justify-content:flex-start;gap:6px}
.brand{order:1;font-size:15px;font-weight:800;letter-spacing:0;text-transform:none;
color:var(--ink);margin-right:auto}
.back{order:0;margin-right:10px}
.nav{order:2;position:static;display:flex;gap:6px;background:none;border:0;padding:0}
.nav a{min-height:0;padding:8px 14px;border-radius:999px;font-size:13.5px}
.nav a.on{background:var(--ink);color:var(--bg);font-weight:700}
main{max-width:760px}
main.wide{max-width:1280px}
.banners{padding:18px 16px 0}
main.wide .banners{padding:18px 28px 0}
.bbox{flex-direction:row;flex-wrap:wrap;gap:6px 28px;padding:10px 16px}
.banner{font-size:13px}
main.wide .head{padding:24px 28px 8px}
main.wide h1.page{font-size:30px}
.cathead{flex-direction:row;align-items:flex-end;flex-wrap:wrap;gap:12px}
.cathead .titles{display:flex;flex-direction:column;gap:4px;margin-right:auto}
.cathead .two{display:flex;gap:12px}
.cathead .btn{min-height:40px;padding:0 18px;font-size:13.5px;flex:none}
.catcards{display:none}
.cattable{display:block;padding:8px 20px 28px}
}
@media(min-width:1100px){
main.feed{max-width:1180px}
main.feed .cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));align-items:start}
}
.tablecard{background:var(--card);border-radius:24px;padding:6px 8px;overflow:auto}
.tablecard table{width:100%;border-collapse:collapse;font-size:13.5px;
font-variant-numeric:tabular-nums}
.tablecard th{text-align:left;color:var(--mute);font-size:11px;letter-spacing:.06em;
text-transform:uppercase;padding:10px;font-weight:700}
.tablecard td{padding:11px 10px;border-top:1px solid var(--line);vertical-align:top}
.tablecard .r{text-align:right;white-space:nowrap}
.llm{border-top:1px solid var(--line);padding:12px 0 4px}
.veto{display:flex;flex-direction:column;gap:4px;margin-top:6px}
.llmhead{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:4px}
.llmhead b{font-size:13.5px}.llmhead span{font-size:12px;color:var(--mute)}
.labels.two{grid-template-columns:repeat(2,minmax(0,1fr));margin-top:8px}
details.raw summary{font-size:12.5px;color:var(--mute);cursor:pointer;padding:6px 0}
details.raw pre{white-space:pre-wrap;word-break:break-word;font-size:11.5px;
font-family:ui-monospace,Menlo,Consolas,monospace;max-height:320px;overflow:auto}
"""

#: (label, href, name used to mark it active)
NAV = [
    ("Feed", "/"),
    ("Catalogue", "/catalogue"),
    ("Not priced", "/missing"),
    ("Outcomes", "/outcomes"),
    ("Health", "/health"),
    ("Constants", "/constants"),
]

LABELS = [
    ("bad_reject", "should have passed"),
    ("bad_pass", "should not have"),
    ("fmv_wrong", "FMV wrong"),
]
LABEL_NAMES = dict(LABELS)

VERDICT_OPTIONS = [
    "", "all", "DEAL", "CHECK", "REJECT_CATALOGUE", "REJECT_BLACKLIST",
    "REJECT_SELLER", "REJECT_VIABLE", "REJECT_PRICE", "REJECT_LLM",
]
VERDICT_NAMES = {"": "default", "all": "all"}


def e(x) -> str:
    return html.escape("" if x is None else str(x))


def when(value) -> str:
    """A stored UTC timestamp as "06 Sep 19:35 UTC". Formatting only."""
    dt = parse_ts(value) if isinstance(value, str) else value
    return dt.strftime("%d %b %H:%M UTC") if dt else "—"


def page(
    title: str,
    body: str,
    engine: Engine,
    *,
    active: str = "",
    back: bool = False,
    width: str = "",
) -> bytes:
    banners = []
    if not engine.notifier.enabled:
        banners.append("Notifications are off: no ntfy topic is set.")
    unver = engine.catalogue.unverified_count
    if unver:
        banners.append(
            f"{unver} of {len(engine.catalogue.references)} catalogue FMVs are "
            "unverified estimates."
        )
    banners.append("No fee rate has been checked against a real invoice.")
    bbox = "".join(f'<div class="banner">{e(b)}</div>' for b in banners)
    nav = "".join(
        f'<a href="{h}"{" class=on" if label == active else ""}>{e(label)}</a>'
        for label, h in NAV
    )
    back_link = '<a class="back" href="/">← Feed</a>' if back else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<link rel="manifest" href="/manifest.json" crossorigin="use-credentials">
<meta name="theme-color" content="{THEME_COLOR}" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="{THEME_COLOR_DARK}" media="(prefers-color-scheme: dark)">
<link rel="icon" href="/icon-192.png"><link rel="apple-touch-icon" href="/icon-192.png">
<title>{e(title)} — watch sniper</title><style>{CSS}</style></head><body>
<header class="top">{back_link}<span class="brand">Watch sniper</span><nav class="nav">{nav}</nav></header>
<main class="{width}"><div class="banners"><div class="bbox">{bbox}</div></div>{body}</main>
<script>if("serviceWorker" in navigator)navigator.serviceWorker.register("/sw.js")</script>
</body></html>""".encode()


# --------------------------------------------------------------------------
# Installable app (PWA)
#
# Enough for a phone to offer "Install", and nothing more. The manifest is
# linked with crossorigin="use-credentials" because the dashboard sits behind
# Cloudflare Access, and without credentials the manifest fetch would get the
# Access login page instead. The service worker caches nothing: every request
# goes to the network, so what is on screen is always what the system decided.
# --------------------------------------------------------------------------

THEME_COLOR = "#f5ead8"
THEME_COLOR_DARK = "#171411"
ICON_SIZES = (192, 512)

SERVICE_WORKER = """\
// Installability only. No fetch handler and no cache: every request goes to
// the network as if this file did not exist.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
"""


def manifest_json() -> bytes:
    return json.dumps(
        {
            "name": "watch sniper",
            "short_name": "watch sniper",
            "start_url": "/",
            "scope": "/",
            "display": "standalone",
            "background_color": THEME_COLOR,
            "theme_color": THEME_COLOR,
            "icons": [
                {"src": f"/icon-{s}.png", "sizes": f"{s}x{s}", "type": "image/png"}
                for s in ICON_SIZES
            ],
        }
    ).encode()


@functools.lru_cache(maxsize=None)
def icon_png(size: int) -> bytes:
    """A plain watch face — a cream ring and two hands on the terracotta accent.

    Drawn here with zlib rather than shipped as a binary, so the project stays
    standard-library only and has no image files to keep in step with the
    theme. Integer geometry on doubled coordinates, so the centre is exact.
    """
    bg, fg = (0xC6, 0x71, 0x39), (0xF9, 0xF4, 0xED)
    c = size - 1  # centre, in doubled coordinates
    outer, inner = size * 72 // 100, size * 58 // 100  # radii, doubled
    hand = max(2, size // 24)  # half-width, doubled
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")  # filter byte: none
        dy = 2 * y - c
        for x in range(size):
            dx = 2 * x - c
            d2 = dx * dx + dy * dy
            ring = inner * inner <= d2 <= outer * outer
            minute = abs(dx) <= hand and -inner * 8 // 10 <= dy <= 0
            hour = abs(dy) <= hand and 0 <= dx <= inner * 6 // 10
            row += bytes(fg if ring or minute or hour else bg)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------
# Fragments
# --------------------------------------------------------------------------


def pct(bp: int | None) -> str:
    """A stored basis-point figure as a percentage string. Formatting only."""
    return "—" if bp is None else f"{bp / 100:.1f}%"


def span(td) -> str:
    """A timedelta as words, for headings. Formatting only."""
    hours = int(td.total_seconds() // 3600)
    return f"{hours} hour{'' if hours == 1 else 's'}" if hours else str(td)


def verdict_chip(verdict: str) -> str:
    cls = "chip deal" if verdict == "DEAL" else "chip"
    return f'<span class="{cls}">{e(verdict)}</span>'


def headroom(pence) -> str:
    """Headroom coloured by sign: sage under the max bid, terracotta over."""
    if pence is None:
        return '<b class="nil">—</b>'
    return f'<b class="{"neg" if pence < 0 else "pos"}">{fmt(pence)}</b>'


def scope_tags(scope: str | None, bracelet: str | None) -> str:
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


def caveat_chips(caveats: list[str], *, only_important: bool = False) -> str:
    out = []
    for c in caveats:
        label, important = CAVEAT_LABEL.get(c, (c.lower().replace("_", " "), False))
        if only_important and not important:
            continue
        cls = "cav warn" if c == "FMV_UNVERIFIED" else "cav"
        out.append(f'<span class="{cls}">{e(label)}</span>')
    return "".join(out)


def label_form(item_id: str, existing, *, back: str = "/", strong: bool = False) -> str:
    """Labelling is one tap (requirement 6).

    A confirmation step here would be worse than useless: the cost of a
    mislabel is one row the operator can see and re-label, and any friction at
    all means the labels never get applied, which costs the whole
    precision-and-recall measurement Phase 1 exists to produce.
    """
    have = set(existing or ())
    buttons = "".join(
        f'<button name="label" value="{key}"{" class=on" if key in have else ""}>'
        f'{"✓ " if key in have else ""}{e(text)}</button>'
        for key, text in LABELS
    )
    return (
        f'<form method="post" action="/label" class="labels{" strong" if strong else ""}">'
        f'<input type="hidden" name="item_id" value="{e(item_id)}">'
        f'<input type="hidden" name="next" value="{e(back)}">{buttons}</form>'
    )


def safe_next(value: str) -> str:
    """Where a form may send you back to: a path on this site, nothing else.

    The page sends no Referer (Referrer-Policy: no-referrer), so the form says
    where it came from. Only a plain local path is honoured, so a crafted
    value cannot redirect off-site.
    """
    if value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return "/"


def llm_label_form(result_id: int, state: bool | None, back: str) -> str:
    """Right or wrong, on one model's answer. One tap, like the listing
    labels; the latest tap is the one that counts."""
    buttons = "".join(
        f'<button name="correct" value="{v}"{" class=on" if state is want else ""}>'
        f'{"✓ " if state is want else ""}{text}</button>'
        for v, want, text in (("1", True, "model right"), ("0", False, "model wrong"))
    )
    return (
        '<form method="post" action="/llm-label" class="labels two">'
        f'<input type="hidden" name="result_id" value="{result_id}">'
        f'<input type="hidden" name="next" value="{e(back)}">{buttons}</form>'
    )


def llm_card(r: sqlite3.Row, state: bool | None, back: str) -> str:
    """One stored model answer, as stored. What it would have meant is the
    would_* columns, computed by valuation; nothing is worked out here."""

    def kv(k, v, cls=""):
        return f'<div class="kv"><span>{e(k)}</span><span class="{cls}">{v}</span></div>'

    def label(v) -> str:
        return e(v.lower().replace("_", " ")) if v else "not stated"

    evidence = json.loads(r["evidence_json"] or "[]")
    flags = json.loads(r["red_flags_json"] or "[]")
    quotes = "<br>".join(
        f'{e(q.get("source"))}: “{e(q.get("text"))}”' for q in evidence
    ) or "none quoted"
    verified = (
        '<span class="pos">found in the listing</span>' if r["evidence_verified"]
        else '<span class="accent">not found in the listing</span>'
    )
    rows = []
    if r["ok"]:
        rows += [
            kv("Identified", e(r["catalogue_key"] or "none of the candidates"), "mono"),
            kv("Confidence", e(r["confidence"])),
            kv("Condition", label(r["condition"])),
            kv("Box / papers", label(r["box_papers"])),
            kv("Bracelet", label(r["bracelet"])),
            kv("Evidence", f"{quotes}<br>{verified}"),
            kv("Red flags", "<br>".join(e(f) for f in flags) or "none",
               "accent" if flags else ""),
            kv("Reason", e(r["reason"])),
        ]
    else:
        rows.append(kv("Error", e(r["error"]), "accent"))
    if r["would_verdict"] or r["would_reason"]:
        rows.append(kv(
            "Live mode would say",
            f'{e(r["would_verdict"] or "—")} · {e(r["would_reason"])}',
        ))
    if r["would_mab_pence"] is not None:
        rows.append(kv("Max bid on that reading", fmt(r["would_mab_pence"])))
    if r["escalated_from"]:
        rows.append(kv("Escalated from", f'result {r["escalated_from"]}'))
    rows.append(kv(
        "Tokens · cost · time",
        f'{r["input_tokens"]} in, {r["output_tokens"]} out · '
        f'{fmt_usd_micro(r["cost_micro_usd"])} · {r["latency_ms"]} ms',
    ))
    rows.append(kv(
        "Prompt · candidates",
        f'{e(r["prompt_version"])} · {e(r["candidate_hash"])}', "mono",
    ))
    chips = verdict_chip(r["would_verdict"]) if r["would_verdict"] else ""
    return f"""<div class="llm" id="llm-{r['id']}">
<div class="llmhead"><b>{e(r['model'])}</b>{chips}<span>{e(r['stage'])} · {when(r['requested_at_utc'])} · result {r['id']}</span></div>
{"".join(rows)}
<details class="raw"><summary>Raw response</summary><pre>{e(r['raw_response'])}</pre></details>
{llm_label_form(r['id'], state, back)}</div>"""


END_STATE_TEXT = {
    "sold": "sold",
    "unsold": "not sold",
    "gone": "removed",
    "ended": "sale unknown",
}


def ended_chip(r: sqlite3.Row) -> str:
    """ENDED, when it ended and whether it sold; or, for a Buy It Now the
    ended check does not cover, that it has not been checked. All of it is
    decided in the query; this only words it."""
    if r["is_ended"]:
        state = END_STATE_TEXT.get(r["end_state"] or "", "")
        detail = " · ".join(x for x in (when(r["ended_display_utc"]), state) if x)
        return f'<span class="cav warn">ENDED · {e(detail)}</span>'
    if r["end_not_checked"]:
        return '<span class="cav">end not checked</span>'
    return ""


def veto_note(r: sqlite3.Row, *, full: bool = False) -> str:
    """VETOED, with both models' reasons, on a DEAL whose alert the shadow
    veto suppressed (DECISIONS.md A20). The verdict itself is unchanged."""
    if r["verdict"] != "DEAL" or not r["veto_reasons_json"]:
        return ""
    reasons = json.loads(r["veto_reasons_json"])
    lines = "".join(
        f'<div class="reason"><b>{e(x["model"])}</b>: {e(x["reason"])}'
        + (f' <span class="nil">({e(x["why"])})</span>' if full and x.get("why") else "")
        + "</div>"
        for x in reasons
    )
    head = ('<div class="cavs"><span class="cav warn">VETOED — alert not sent; '
            'both models would reject it</span></div>')
    return f'<div class="veto">{head}{lines}</div>'


def feed_card(r: sqlite3.Row, back: str = "/") -> str:
    caveats = json.loads(r["caveats_json"] or "[]")
    fmv = r["fmv_pence"]
    ref = f"→ {e(r['catalogue_display'])}" if r["catalogue_display"] else "no reference"
    meta = [
        f"{'Auction' if r['is_auction'] else 'BIN'} · "
        f"{e(r['seller_account_type'].title() or 'seller type unknown')}",
        ref,
    ]
    if r["is_auction"]:
        bids = r["bid_count"] or 0
        meta.append(f"ends {when(r['end_time_utc'])} · {bids} bid{'' if bids == 1 else 's'}")
    return f"""<article class="card row">
<div class="rtop"><div class="rv">{verdict_chip(r['verdict'])}<span class="reason">{e(r['primary_reason'])}</span></div>
<div class="below"><b>{pct(r['below_fmv_bp'])}</b><span>{f"below {fmt(fmv)} FMV" if fmv else "no FMV"}</span></div></div>
<div class="rtitle"><a href="/item/{e(urllib.parse.quote(r['item_id'], safe=''))}">{e(r['title'])}</a>
<div class="meta">{"".join(f"<span>{m}</span>" for m in meta)}{scope_tags(r['scope'], r['bracelet'])}</div></div>
<div class="strip"><div><span class="lbl">Price</span><b>{fmt(r['eff_price_pence'])}</b><small>{e(r['price_basis'])}</small></div>
<div><span class="lbl">Max bid</span><b>{fmt(r['mab_pence'])}</b></div>
<div><span class="lbl">Headroom</span>{headroom(r['headroom_pence'])}</div></div>
<div class="cavs">{ended_chip(r)}{caveat_chips(caveats, only_important=True)}</div>
{veto_note(r)}
{label_form(r['item_id'], (r['labels'] or '').split(','), back=back)}
</article>"""


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


def render_feed(engine: Engine, params: dict) -> str:
    view = "auctions" if params.get("view", [""])[0] == "auctions" else "bin"
    verdict = params.get("verdict", [""])[0]
    brand = params.get("brand", [""])[0]
    q = params.get("q", [""])[0]
    limit = 250
    # On the auction view a DEAL is shown whatever its end time, in its own
    # section above the ending-soon list, which then leaves it out. An alert
    # for an auction ending in three days must land somewhere on the feed.
    deal_section = view == "auctions" and verdict in ("", "DEAL")
    deals = (
        engine.db.feed(view="auctions", verdict="DEAL", brand=brand, query=q,
                       limit=limit, ending_within=None)
        if deal_section else []
    )
    rows = engine.db.feed(
        view=view, verdict=verdict, brand=brand, query=q, limit=limit,
        ending_within=C.AUCTION_ENDING_SOON,
        exclude_verdict="DEAL" if deal_section else "",
    )
    counts = {r["verdict"]: r["n"] for r in engine.db.verdict_counts()}

    current = 2 if verdict == "all" else 1 if view == "auctions" else 0
    segs = "".join(
        f'<a href="{h}"{" class=on" if i == current else ""}>{label}</a>'
        for i, (label, h) in enumerate(
            [("Buy It Now", "/"), ("Auctions", "/?view=auctions"), ("All", "/?verdict=all")]
        )
    )
    opts = "".join(
        f"<option value='{e(o)}'{' selected' if o == verdict else ''}>"
        f"{e(VERDICT_NAMES.get(o, o))}</option>"
        for o in VERDICT_OPTIONS
    )
    brands = sorted({r["catalogue_display"].split(" ")[0] for r in rows if r["catalogue_display"]})
    bopts = "".join(
        f"<option value='{e(b)}'{' selected' if b == brand else ''}>{e(b or 'Any')}</option>"
        for b in ["", *brands]
    )
    tally = "".join(
        f"<div><b>{counts.get(v, 0)}</b><span>{e(v.replace('REJECT_', ''))}</span></div>"
        for v in VERDICT_OPTIONS[2:]
    )
    summary = f"{VERDICT_NAMES.get(verdict, verdict)} · {brand or 'any brand'}"
    if q:
        summary += f" · “{q}”"
    is_open = " open" if (verdict or brand or q) else ""

    n = len(rows)
    if verdict == "all":
        count = f"{n} rows"
    elif view == "auctions":
        count = f"{n} auction{'' if n == 1 else 's'} ending within {span(C.AUCTION_ENDING_SOON)}"
    else:
        count = f"{n} listing{'' if n == 1 else 's'}"
    if n >= limit:
        count += " (the limit)"
    count += " · sorted by % below FMV"

    caution = (
        '<div class="caution"><strong>Prices are the next valid bid now.</strong> '
        "Auctions usually close well above it. Check the end time and bid count on "
        "each item before judging a price.</div>"
        if view == "auctions" and verdict != "all"
        else ""
    )
    kept = {k: v for k, v in (("view", view if view != "bin" else ""),
                               ("verdict", verdict), ("brand", brand), ("q", q)) if v}
    back = "/" + (f"?{urllib.parse.urlencode(kept)}" if kept else "")
    cards = "".join(feed_card(r, back) for r in rows) or (
        '<div class="card empty">Nothing here. If the feed has been running a while '
        'and is still empty, that is itself a finding — check '
        '<a href="/health">Health</a> for the poll log.</div>'
        if not deals
        else f'<div class="card empty">No other auction ends within {span(C.AUCTION_ENDING_SOON)}.</div>'
    )
    count_line = f'<div class="sub">{e(count)}</div>'
    deal_block = (
        f'<div class="list cards">{"".join(feed_card(r, back) for r in deals)}</div>'
        f'<div class="head tight">{count_line}</div>'
        if deals else ""
    )
    head_count = (
        f'<div class="sub">{len(deals)} DEAL auction{"" if len(deals) == 1 else "s"} '
        "· any end time · sorted by % below FMV</div>"
        if deals else count_line
    )
    return f"""
<div class="head"><h1 class="page">Feed</h1>
<div class="seg">{segs}</div>
<details class="filters"{is_open}><summary><span>Filters</span><span class="sum">{e(summary)}</span></summary>
<form class="form" method="get">
<input type="hidden" name="view" value="{e(view)}">
<label class="field">Verdict<select class="input" name="verdict">{opts}</select></label>
<div class="two"><label class="field">Brand<select class="input" name="brand">{bopts}</select></label>
<label class="field">Title contains<input class="input" name="q" placeholder="e.g. chrono" value="{e(q)}"></label></div>
<button class="btn" type="submit">Apply</button>
<div class="form" style="gap:6px;padding-top:4px"><div class="sub" style="font-size:12px">Last 14 days</div>
<div class="tally">{tally}</div></div>
</form></details>
{caution}
{head_count}</div>
{deal_block}
<div class="list cards">{cards}</div>"""


def render_item(engine: Engine, item_id: str) -> str:
    row = engine.db.item(item_id)
    if row is None:
        return '<div class="list"><div class="card empty">No such listing.</div></div>'
    caveats = json.loads(row["caveats_json"] or "[]")
    gates = json.loads(row["gates_json"] or "[]")
    derivation = json.loads(row["derivation_json"] or "null")
    labels = engine.db.labels_for(item_id)

    price = row["eff_price_pence"]
    basis = row["price_basis"]
    if price is None:
        price, basis = row["price_pence"], "asking"
    fmv = row["fmv_pence"]
    fmv_line = (
        f'<div class="fmvline"><span>FMV <strong>{fmt(fmv)}</strong> · '
        f'{"verified" if row["fmv_verified"] else "estimate"}</span>'
        f'<span><strong class="pctb">{pct(row["below_fmv_bp"])}</strong> below FMV</span></div>'
        if fmv
        else '<div class="fmvline"><span>No catalogue match, so no FMV.</span></div>'
    )

    def kv(k, v, cls=""):
        return f'<div class="kv"><span>{e(k)}</span><span class="{cls}">{v}</span></div>'

    fb = row["seller_feedback_pct_x100"]
    listing = "".join([
        kv("Asking / current", fmt(row["price_pence"])),
        kv("Postage", fmt(row["shipping_pence"]) if row["shipping_pence"] is not None else "unknown"),
        kv("Price the gate used", f"{fmt(row['eff_price_pence'])} {e(row['price_basis'])}"),
        kv("Format", "Auction" if row["is_auction"] else "Buy It Now"),
        kv("Bids", e(row["bid_count"] if row["bid_count"] is not None else "—")),
        kv("Ends", when(row["end_time_utc"])),
        kv("Ended", ended_chip(row) or ("live when last checked " + when(row["end_checked_at_utc"])
                                         if row["end_checked_at_utc"] else "not known to have ended")),
        kv("Condition", e(row["condition_raw"] or "—")),
        kv("Scope / bracelet", e(" · ".join(
            v.lower().replace("_", " ") for v in (row["scope"], row["bracelet"]) if v
        ) or "—")),
        kv("Location", e(row["item_location_country"] or "—")),
    ])
    seller = "".join([
        kv("Account type", e(row["seller_account_type"] or "not returned")),
        kv("Feedback", f"{fb / 100}%" if fb is not None else "not returned"),
        kv("Ratings", e(row["seller_feedback_score"] if row["seller_feedback_score"] is not None else "not returned")),
        kv("Buyer protection", "not charged (business)" if row["seller_account_type"] == "BUSINESS" else "charged (private)"),
    ])
    reference = "".join([
        kv("Reference", e(row["catalogue_display"] or "none")),
        kv("Key", e(row["catalogue_key"] or "—"), "mono"),
        kv("FMV", fmt(fmv)),
        kv("Verified", "yes" if row["fmv_verified"] else "no — an estimate",
           "" if row["fmv_verified"] else "accent"),
        kv("Scored under", e(row["config_fingerprint"]), "mono"),
    ])
    gate_rows = "".join(
        f'<div class="gate"><span class="pill{"" if g["passed"] else " fail"}">'
        f'{"pass" if g["passed"] else "fail"}</span><span class="gname">{e(g["name"])}</span>'
        f'<span class="gdetail">{e(g["detail"])}</span></div>'
        for g in gates
    )

    valuation = ""
    if derivation:
        def line(label, value):
            kind = (
                "final" if label == "Maximum allowable bid"
                else "subtotal" if label in ("Net proceeds", "Acquisition budget")
                else ""
            )
            note = "<small>FMV × condition multiplier</small>" if label.startswith("Sale price") else ""
            return (f'<div class="lr {kind}"><span>{e(label)}{note}</span>'
                    f'<span class="v">{fmt(value)}</span></div>')

        assumed = "" if derivation["condition_stated"] else " (not stated, assumed)"
        warn = (
            '<div class="warnnote">FMV and fee rates are unverified estimates.</div>'
            if "FMV_UNVERIFIED" in caveats
            else '<div class="warnnote">Fee rates are unverified estimates.</div>'
        )
        valuation = f"""<div class="card sec pad"><h3>Valuation</h3>
<div class="valhead">Reference FMV {fmt(derivation['fmv_reference'])} × {e(derivation['condition'])}{assumed} = <strong>{fmt(derivation['effective_fmv'])} effective</strong></div>
<div class="ledger">{"".join(line(k, v) for k, v in derivation["lines"])}</div>{warn}</div>"""

    back = f"/item/{urllib.parse.quote(item_id, safe='')}"
    results = engine.db.llm_results_for(item_id)
    llm_section = ""
    if results:
        state = engine.db.llm_label_state(item_id)
        llm_section = (
            '<div class="card sec"><h3>Verification models — shadow</h3>'
            '<div class="reason">Shadow mode: these answers changed nothing above. '
            '<a href="/llm-review">Review sample</a></div>'
            + "".join(llm_card(r, state.get(r["id"]), back) for r in results)
            + "</div>"
        )

    label_log = "".join(
        f'<div class="lab"><b>{e(LABEL_NAMES.get(r["label"], r["label"]))}</b>'
        f'<span>{when(r["created_at_utc"])} · verdict then {e(r["verdict_at_time"] or "—")}</span></div>'
        for r in labels
    ) or '<div class="lab"><span>none yet</span></div>'

    return f"""
<div class="ihead"><div class="cavs" style="align-items:center">{verdict_chip(row['verdict'])}{ended_chip(row)}{caveat_chips(caveats, only_important=True)}</div>
<h1>{e(row['title'])}</h1><div class="reason">{e(row['primary_reason'])}</div>{veto_note(row, full=True)}</div>
<div class="summary"><div class="card">
<div class="big3"><div><span class="lbl">Price</span><b>{fmt(price)}</b><small>{e(basis)}</small></div>
<div><span class="lbl">Max bid</span><b>{fmt(row['mab_pence'])}</b></div>
<div><span class="lbl">Headroom</span>{headroom(row['headroom_pence'])}</div></div>
{fmv_line}</div></div>
<div class="actions"><a class="ebay" href="{e(row['web_url'])}" target="_blank" rel="noopener">Open on eBay</a>
{label_form(item_id, [r['label'] for r in labels], strong=True,
            back=f"/item/{urllib.parse.quote(item_id, safe='')}")}</div>
<div class="secs">
<div class="card sec"><h3>Listing</h3>{listing}</div>
<div class="card sec"><h3>Seller</h3>{seller}</div>
<div class="card sec"><h3>Reference</h3>{reference}</div>
<div class="card sec"><h3>Gates</h3>{gate_rows}</div>
{valuation}
{llm_section}
<div class="card sec"><h3>Your labels</h3>{label_log}</div>
</div>"""


def render_missing(engine: Engine) -> str:
    work = engine.catalogue.missing_worklist(engine.db.unmatched_titles())
    groups = "".join(
        f'<div class="card grp"><div class="grphead"><span>{e(label)}</span><b>{n}</b></div>'
        f'<div class="examples">{"".join(f"<div>{e(x)}</div>" for x in eg)}</div></div>'
        for label, n, eg in work
    ) or '<div class="card empty">Every listing seen so far matched a reference.</div>'
    return f"""
<div class="head tight"><h1 class="page">Not priced</h1>
<div class="sub">Unmatched titles by brand and model word, most frequent first</div></div>
<div class="list tight">{groups}</div>"""


def render_catalogue(engine: Engine) -> str:
    from .shadow import confident_observed

    usage = engine.db.catalogue_usage()
    observed = engine.db.observed_closings()
    confident = confident_observed(engine.db)
    refs = sorted(
        engine.catalogue.references,
        key=lambda r: (r.verified, -usage.get(r.key, 0), r.brand),
    )

    def obs(key: str):
        median, n, unsold = observed.get(key, (None, 0, 0))
        shown = n >= C.OBSERVED_MIN_AUCTIONS
        return (fmt(median) if shown else "—"), n, shown, unsold

    def conf(key: str):
        """The confident-only median, shown from the same minimum count."""
        median, n, excluded = confident.get(key, (None, 0, 0))
        shown = n >= C.OBSERVED_MIN_AUCTIONS
        return (fmt(median) if shown else "—"), n, shown, excluded

    def band(r) -> str:
        return f"{fmt(r.fmv_low)}–{fmt(r.fmv_high)} · valued at midpoint" if r.is_band else ""

    cards, trs = [], []
    for r in refs:
        median, n, shown, unsold = obs(r.key)
        sold_note = f"{n} sold" if shown else f"fewer than {C.OBSERVED_MIN_AUCTIONS} sold"
        cmedian, cn, cshown, cexcl = conf(r.key)
        conf_note = (f"{cn} sold" if cshown else f"{cn} of {C.OBSERVED_MIN_AUCTIONS} needed") + f" · {cexcl} excluded"
        ver = '<span class="ver ok">✓ verified</span>' if r.verified else '<span class="ver">— unverified</span>'
        cards.append(f"""<div class="card cat">
<div class="cattop"><div><span class="ref">{e(r.display)}</span><span class="key">{e(r.key)}</span></div>{ver}</div>
<div class="cstrip"><div><span class="lbl">FMV</span><b>{fmt(r.point)}</b></div>
<div><span class="lbl">Observed</span><b>{median}</b><small>{sold_note}</small></div>
<div><span class="lbl">Unsold</span><b>{unsold}</b></div>
<div><span class="lbl">Priced</span><b>{usage.get(r.key, 0)}</b></div></div>
<div class="cstrip two"><div><span class="lbl">Observed, confident only</span><b>{cmedian}</b><small>{conf_note}</small></div></div>
{f'<div class="band">Band: {band(r)}</div>' if r.is_band else ''}
{f'<div class="notes">{e(r.notes)}</div>' if r.notes else ''}</div>""")
        trs.append(
            f"<tr><td class='{'pos' if r.verified else 'accent'}' style='font-weight:700'>"
            f"{'✓' if r.verified else '—'}</td>"
            f"<td style='font-weight:650'>{e(r.display)}</td><td class='key'>{e(r.key)}</td>"
            f"<td class='r' style='font-weight:700'>{fmt(r.point)}</td>"
            f"<td class='accent' style='font-size:12.5px;max-width:190px'>{band(r)}</td>"
            f"<td class='r'><span style='font-weight:650'>{median}</span> "
            f"<span class='nil'>{f'({n})' if shown else ''}</span></td>"
            f"<td class='r'><span style='font-weight:650'>{cmedian}</span> "
            f"<span class='nil'>({cn}; {cexcl} excl.)</span></td>"
            f"<td class='r'>{unsold}</td><td class='r'>{usage.get(r.key, 0)}</td>"
            f"<td class='nil' style='font-size:12.5px;max-width:220px'>{e(r.notes)}</td></tr>"
        )
    return f"""
<div class="head cathead" style="gap:10px"><div class="titles"><h1 class="page">Catalogue</h1>
<div class="sub">{len(refs)} entries · unverified first, then by traffic</div></div>
<div class="two"><form method="post" action="/reload"><button class="btn ghost">reload catalogue</button></form>
<form method="post" action="/rescore"><button class="btn" style="font-size:13px;font-weight:650">re-score everything</button></form></div></div>
<div class="list tight catcards">{"".join(cards)}</div>
<div class="cattable"><div class="tablecard"><table>
<thead><tr><th>Ver.</th><th>Reference</th><th>Key</th><th class="r">FMV</th><th>Band</th>
<th class="r" title="Median closing price of auctions eBay reports as sold, with the count; a dash below the minimum count">Observed</th>
<th class="r" title="Median of sold closings the shadow models confidently identified as this entry — both agreeing at high confidence, or the one that answered. In brackets: how many, and how many of the Observed closings it left out.">Confident</th>
<th class="r" title="Auctions that ended without a sale: no bids, or reserve not met">Unsold</th>
<th class="r">Priced</th><th>Notes</th></tr></thead>
<tbody>{"".join(trs)}</tbody></table></div></div>"""


def render_health(engine: Engine) -> str:
    h = engine.health()
    stale = h["stale"]
    last = h["last_successful_poll"]
    err = h["last_error"]

    def kv(k, v, cls=""):
        return f'<div class="kv"><span>{e(k)}</span><span class="{cls}">{v}</span></div>'

    polls = "".join(
        f'<div class="poll"><div class="pollr"><span>{e(r["started_at_utc"][11:16])}</span>'
        f'<span class="nil">{e(r["kind"])}</span><span>{r["http_calls"]}</span>'
        f'<span>{r["items_seen"]}</span><span>{r["items_new"]}</span><span>{r["alerts"]}</span></div>'
        + (f'<div class="pollerr">Error: {e(r["error"][:200])}</div>' if r["error"] else "")
        + "</div>"
        for r in engine.db.recent_polls()
    ) or '<div class="poll"><span class="nil">none yet</span></div>'

    names = {"stalled": "Ingestion stopped", "recovered": "Ingestion resumed", "test": "Test notification"}

    def note_title(r) -> str:
        if r["kind"] not in ("alert", "vetoed"):
            return names.get(r["kind"], r["kind"])
        what = r["catalogue_display"] or r["item_id"] or "alert"
        if r["kind"] == "vetoed":
            what = f"Vetoed: {what}"
        return f"{what} at {fmt(r['price_pence'])}" if r["price_pence"] is not None else what

    notes = "".join(
        f'<div class="note"><div><b>{e(note_title(r))}</b><span>{when(r["at_utc"])}</span></div>'
        f'<span class="sent{"" if r["ok"] else " failed"}">'
        f'{"not sent" if r["kind"] == "vetoed" else "sent" if r["ok"] else "FAILED"}</span></div>'
        for r in engine.db.recent_notifications()
    ) or '<div class="note"><span>none yet</span></div>'

    stats = "".join(
        kv(r["model"],
           f'{r["calls"]} calls · {r["errors"]} errors · {fmt_usd_micro(r["cost"])}',
           "accent" if r["errors"] else "")
        for r in engine.db.llm_stats_today()
    ) or kv("Calls today", "none")
    llm_err = engine.db.llm_last_error()
    err_text = (
        e(f"{llm_err['model']} {when(llm_err['requested_at_utc'])}: {(llm_err['error'] or '')[:140]}")
        if llm_err else "none"
    )
    shadow_err = h["llm_last_error"]
    llm = f"""<div class="card sec kvlist" style="padding:10px 16px 6px"><h3 style="margin-top:6px">Verification models</h3>
{kv("Mode", e(h['llm_mode']), "" if h['llm_mode'] == "off" else "accent")}
{kv("Spend today (UTC)", f"{fmt_usd_micro(h['llm_spent_today_micro_usd'])} of {fmt_usd_micro(h['llm_cap_micro_usd'])} cap")}
{stats}
{kv("Last model error", err_text, "accent" if llm_err else "")}
{kv("Last shadow failure", e(shadow_err[:140]) if shadow_err else "none", "accent" if shadow_err else "")}
<a class="jsonlink" href="/llm-review">Review sample and labels per model</a></div>"""

    return f"""
<div class="head"><h1 class="page">Health</h1></div>
<div class="secs" style="padding-top:12px">
<div class="status{' stale' if stale else ''}"><span class="dot"></span><div>
<b>{"STALE — ingestion has stopped" if stale else "Healthy"}</b>
<span>Last successful poll {when(last) if last else "never"}</span></div></div>
<div class="card sec kvlist" style="padding:10px 16px 6px">
{kv("Browse calls today", f"{h['calls_used']} of {h['calls_used'] + h['calls_remaining']}")}
{kv("Last error", e(err[:140]) if err else "none", "accent" if err else "")}
{kv("Alert channel", "ntfy" if engine.notifier.enabled else "none")}
{kv("Config fingerprint", e(h['fingerprint']), "mono")}
<a class="jsonlink" href="/api/health">/api/health as JSON</a></div>
{llm}
<div class="card sec" style="padding:14px 14px 8px"><h3>Recent polls</h3>
<div class="pollh"><span>Time</span><span>Kind</span><span>Calls</span><span>Seen</span><span>New</span><span>Alerts</span></div>
{polls}</div>
<div class="card sec" style="padding:14px 14px 8px"><h3>Notifications</h3>{notes}</div>
</div>"""


def render_llm_review(engine: Engine) -> str:
    """Labels on model answers, per model, and a fresh random sample of the
    answers live mode would have rejected. Live mode waits on these numbers
    (DECISIONS.md A19), and a rejection nobody looks at is never labelled."""
    tally = "".join(
        f'<div class="kv"><span>{e(r["model"])}</span>'
        f'<span>{r["right_n"]} right · {r["wrong_n"]} wrong</span></div>'
        for r in engine.db.llm_label_tally()
    ) or '<div class="kv"><span>No model answer labelled yet.</span></div>'
    cards = "".join(
        f'<div class="card sec"><div class="rtitle"><a href="/item/'
        f'{e(urllib.parse.quote(r["item_id"], safe=""))}#llm-{r["id"]}">{e(r["title"])}</a></div>'
        + llm_card(r, engine.db.llm_label_state(r["item_id"]).get(r["id"]), "/llm-review")
        + "</div>"
        for r in engine.db.llm_review_sample()
    ) or '<div class="card empty">No answer that live mode would have rejected yet.</div>'
    return f"""
<div class="head tight"><h1 class="page">Model review</h1>
<div class="sub">Shadow mode · labels per model, then a random sample of would-be rejections</div></div>
<div class="secs"><div class="card sec kvlist" style="padding:10px 16px 6px"><h3 style="margin-top:6px">Labels</h3>{tally}</div>
{cards}</div>"""


def render_constants(engine: Engine) -> str:
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
        ("COND_MULT", " · ".join(f"{k} ×{v / 10000:.2f}" for k, v in C.COND_MULT.items())),
        ("MIN_SELLER_FEEDBACK_PCT_X100", f"{C.MIN_SELLER_FEEDBACK_PCT_X100 / 100:.2f}%"),
        ("MIN_SELLER_FEEDBACK_SCORE", C.MIN_SELLER_FEEDBACK_SCORE),
        ("SEARCH_MIN_PRICE", fmt(C.SEARCH_MIN_PRICE)),
        ("SEARCH_MAX_PRICE", fmt(C.SEARCH_MAX_PRICE)),
        ("AUCTION_HORIZON", str(C.AUCTION_HORIZON)),
        ("CLOSING_CHECK_DELAY", str(C.CLOSING_CHECK_DELAY)),
        ("OBSERVED_MIN_AUCTIONS", C.OBSERVED_MIN_AUCTIONS),
        ("AUCTION_ENDING_SOON", str(C.AUCTION_ENDING_SOON)),
        ("EBAY_CATEGORY_IDS", C.EBAY_CATEGORY_IDS),
    ]
    cards = "".join(
        f'<div class="card const"><div><span class="name">{e(name)}</span>'
        + ('<span class="ver">unverified</span>' if name in C.UNVERIFIED else "")
        + f'</div><span class="val">{e(value)}</span>'
        + (f'<span class="how">Verify with: {e(C.UNVERIFIED[name])}</span>' if name in C.UNVERIFIED else "")
        + "</div>"
        for name, value in items
    )
    return f"""
<div class="head tight"><h1 class="page">Constants</h1>
<div class="sub">Read from config.py, the only place any of them exists.</div></div>
<div class="list tight">{cards}</div>"""


def render_outcomes(engine: Engine) -> str:
    rows = engine.db.query("SELECT * FROM outcomes ORDER BY id DESC")

    def realised(r) -> str:
        if not r["sell_price_pence"]:
            return "—"
        return fmt(
            (r["sell_price_pence"] or 0) - (r["buy_price_pence"] or 0) - (r["fees_paid_pence"] or 0)
        )

    log = "".join(
        f'<div class="outcome"><b>{e(r["title"] or r["item_id"])}</b>'
        f'<span>Paid {fmt(r["buy_price_pence"])} · predicted FMV {fmt(r["predicted_fmv_pence"])} · '
        f'sold {fmt(r["sell_price_pence"])} · fees {fmt(r["fees_paid_pence"])} · '
        f'realised {realised(r)}</span>'
        + (f'<span>{e(r["note"])}</span>' if r["note"] else "")
        + "</div>"
        for r in rows
    ) or (
        '<div style="font-size:14px;color:var(--mute);padding:14px 0 6px">No outcomes '
        "recorded yet. Each entry will show its realised margin here.</div>"
    )
    fields = [
        ("Item id", "item_id", "text", "v1|236104771452|0"),
        ("Description", "title", "text", ""),
        ("Paid", "buy_price", "decimal", "£"),
        ("Predicted FMV", "predicted_fmv", "decimal", "£"),
        ("Sold for", "sell_price", "decimal", "£"),
        ("Fees", "fees_paid", "decimal", "£"),
    ]
    inputs = "".join(
        f'<label class="field">{label}<input class="input" name="{name}" '
        f'inputmode="{mode}" placeholder="{e(ph)}"></label>'
        for label, name, mode, ph in fields
    )
    return f"""
<div class="head"><h1 class="page">Outcomes</h1></div>
<div class="secs" style="padding-top:12px">
<form method="post" action="/outcome" class="card sec pad form big"><h3>Record an outcome</h3>
{inputs}
<label class="field">Note<textarea class="input" name="note"></textarea></label>
<button class="btn tall">Record outcome</button></form>
<div class="card sec pad" style="gap:6px"><h3>Log</h3>{log}</div>
</div>"""


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
                self._send(page("Feed", render_feed(eng, params), eng, active="Feed", width="feed"))
            elif path.startswith("/item/"):
                # eBay item ids contain pipes ("v1|1234|0"), so the path
                # segment arrives percent-encoded and must be decoded.
                item_id = urllib.parse.unquote(path[len("/item/"):])
                self._send(page("Listing", render_item(eng, item_id), eng, active="Feed", back=True))
            elif path == "/catalogue":
                self._send(page("Catalogue", render_catalogue(eng), eng, active="Catalogue", width="wide"))
            elif path == "/missing":
                self._send(page("Not priced", render_missing(eng), eng, active="Not priced"))
            elif path == "/health":
                self._send(page("Health", render_health(eng), eng, active="Health"))
            elif path == "/llm-review":
                self._send(page("Model review", render_llm_review(eng), eng, active="Health"))
            elif path == "/constants":
                self._send(page("Constants", render_constants(eng), eng, active="Constants"))
            elif path == "/outcomes":
                self._send(page("Outcomes", render_outcomes(eng), eng, active="Outcomes"))
            elif path == "/manifest.json":
                self._send(manifest_json(), 200, "application/manifest+json")
            elif path == "/sw.js":
                self._send(SERVICE_WORKER.encode(), 200, "text/javascript")
            elif path in {f"/icon-{s}.png" for s in ICON_SIZES}:
                self._send(icon_png(int(path[6:-4])), 200, "image/png")
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
                self._send(
                    page("Not found", '<div class="list"><div class="card empty">No such page.</div></div>', eng),
                    404,
                )
        except Exception as exc:  # a broken page must not take the server down
            import traceback

            self._send(
                page(
                    "Error",
                    f'<div class="list"><div class="card empty"><pre>{e(traceback.format_exc(limit=6))}</pre></div></div>',
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
            if get("next"):
                self._redirect(safe_next(get("next")))
            else:
                self._back()
        elif path == "/llm-label":
            if get("result_id").isdigit() and get("correct") in ("0", "1"):
                eng.db.add_llm_label(int(get("result_id")), get("correct") == "1", get("note"))
            self._redirect(safe_next(get("next")) if get("next") else "/llm-review")
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
