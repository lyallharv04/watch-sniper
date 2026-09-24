"""Command line entry point. `python -m watchsniper <command>`."""

from __future__ import annotations

import argparse
import sys
import time

from . import config as C
from .money import fmt

COMMANDS = """
  diagnose      one-shot live API check. Run this first, before anything else
  seed          one pass over the standing stock, so the dashboard is not
                empty on day one. Sends no notifications
  serve         start the poller and the dashboard
  rescore       re-score every stored listing under the current constants
  catalogue     print the FMV curation worklist, most-used unverified first
  missing       print what the catalogue does not price, ranked by traffic
  notify-test   send one test notification and say whether it arrived
  constants     print every constant and whether it has been verified
  env-example   regenerate .env.example from the variables the code reads
  selftest      run the test suite
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="watchsniper",
        description="UK-domestic eBay watch feed. Read-only: it holds no eBay "
        "user token and cannot bid or buy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=COMMANDS,
    )
    parser.add_argument("command", nargs="?", default="serve")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-poll", action="store_true",
                        help="dashboard only; do not call eBay")
    parser.add_argument("--pages", type=int, default=5,
                        help="seed only: pages of 200 to walk back through")
    args = parser.parse_args(argv)

    try:
        C.assert_phase_1()
    except C.UserTokenPresent as exc:
        print(exc, file=sys.stderr)
        return 2

    cmd = args.command
    if cmd == "diagnose":
        from . import diagnose

        result = diagnose.run()
        return 0 if result.get("ok") else 1

    if cmd == "env-example":
        print(render_env_example())
        return 0

    if cmd == "constants":
        return cmd_constants()

    if cmd == "catalogue":
        return cmd_catalogue()

    if cmd == "notify-test":
        return cmd_notify_test()

    if cmd == "missing":
        return cmd_missing()

    if cmd == "rescore":
        return cmd_rescore()

    if cmd == "selftest":
        from . import selftest

        return selftest.main()

    if cmd == "seed":
        return cmd_seed(args)

    if cmd == "serve":
        return cmd_serve(args)

    parser.print_help()
    return 1


# --------------------------------------------------------------------------


def _engine(with_client: bool = True):
    from .db import Database
    from .ebay import CallBudget, EbayClient
    from .poller import Engine

    db = Database(C.DB_PATH)
    client = None
    if with_client and not C.missing_credentials():
        client = EbayClient(
            C.EBAY_CLIENT_ID,
            C.EBAY_CLIENT_SECRET,
            env=C.EBAY_ENV or "production",
            marketplace=C.EBAY_MARKETPLACE_ID or "EBAY_GB",
            budget=CallBudget(C.EBAY_DAILY_CALL_CEILING),
        )
    return Engine(db, client)


def cmd_serve(args) -> int:
    from . import web

    host = args.host or C.BIND_HOST or "127.0.0.1"
    port = args.port or C.BIND_PORT

    missing = C.missing_credentials()
    if missing and not args.no_poll:
        print(f"Cannot poll: {', '.join(missing)} not set. See .env.example.",
              file=sys.stderr)
        return 2

    engine = _engine(with_client=not args.no_poll)
    engine.db.audit("system", "start", f"{host}:{port}")

    if not engine.notifier.enabled:
        print("NOTE: NTFY_TOPIC is not set — nothing will reach your phone.")
    print(f"catalogue: {len(engine.catalogue.references)} entries, "
          f"{engine.catalogue.unverified_count} unverified")
    print(f"constants fingerprint: {C.valuation_fingerprint()}")

    if engine.client is not None:
        engine.start()
        print(f"polling: BIN every {C.POLL_BIN_SECONDS}s, "
              f"auctions every {C.POLL_AUCTION_SECONDS}s")
    else:
        print("polling: OFF")

    httpd = web.serve(engine, host, port)
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    print(f"dashboard: http://{shown}:{port}")
    if host == "0.0.0.0":
        print("WARNING: bound to all interfaces. There is no authentication "
              "in this service. Put it behind a private network or a tunnel.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        engine.stop()
        httpd.shutdown()
        engine.db.audit("system", "stop", "")
        engine.db.close()
    return 0


def cmd_seed(args) -> int:
    """Walk the standing stock once, quietly.

    An empty dashboard on the first day tells the operator nothing, and the
    catalogue worklist is ordered by how many listings each reference has
    priced — which needs listings. Notifications are suppressed: alerting on
    several hundred listings that have been sitting there for a week is not an
    alert, it is a reason to stop reading them.
    """
    if C.missing_credentials():
        print("Cannot seed without credentials.", file=sys.stderr)
        return 2
    engine = _engine()
    for kind in ("bin", "auction"):
        result = engine.poll_once(kind, pages=args.pages, notify=False)
        print(f"{kind:8} seen={result.items_seen:5} new={result.items_new:5} "
              f"calls={result.http_calls}  would-alert={result.alerts}"
              + (f"  ERROR {result.error[:200]}" if result.error else ""))
    counts = {r["verdict"]: r["n"] for r in engine.db.verdict_counts()}
    total = sum(counts.values())
    print(f"\n{total} listings stored")
    for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {verdict:<28} {n:>6}  {100 * n / max(1, total):5.1f}%")
    engine.db.close()
    return 0


def cmd_constants() -> int:
    rows = [
        ("FVF_BP", f"{C.FVF_BP / 100:.2f}%"),
        ("REG_OP_FEE_BP", f"{C.REG_OP_FEE_BP / 100:.2f}%"),
        ("AD_RATE_BP", f"{C.AD_RATE_BP / 100:.2f}%"),
        ("ORDER_FEE", fmt(C.ORDER_FEE)),
        ("FEE_VAT_MULT_BP", f"x{C.FEE_VAT_MULT_BP / 10000:.2f}"),
        ("BUYER_PROTECTION_FIXED", fmt(C.BUYER_PROTECTION_FIXED)),
        ("BUYER_PROTECTION_TIERS",
         " / ".join(f"<={fmt(u)}@{bp / 100:.2f}%" for u, bp in C.BUYER_PROTECTION_TIERS)),
        ("TARGET_PROFIT_MARGIN_BP", f"{C.TARGET_PROFIT_MARGIN_BP / 100:.2f}%"),
        ("MIN_ABSOLUTE_PROFIT", fmt(C.MIN_ABSOLUTE_PROFIT)),
        ("INBOUND_POSTAGE_ESTIMATE", fmt(C.INBOUND_POSTAGE_ESTIMATE)),
        ("OUTBOUND_POSTAGE", fmt(C.OUTBOUND_POSTAGE)),
        ("COND_MULT", str(C.COND_MULT)),
        ("SEARCH_MIN_PRICE", fmt(C.SEARCH_MIN_PRICE)),
        ("SEARCH_MAX_PRICE", fmt(C.SEARCH_MAX_PRICE)),
        ("EBAY_CATEGORY_IDS", str(C.EBAY_CATEGORY_IDS)),
        ("MIN_SELLER_FEEDBACK_PCT_X100", f"{C.MIN_SELLER_FEEDBACK_PCT_X100 / 100:.2f}%"),
        ("MIN_SELLER_FEEDBACK_SCORE", str(C.MIN_SELLER_FEEDBACK_SCORE)),
        ("VAT_REGISTERED", str(C.VAT_REGISTERED)),
    ]
    width = max(len(n) for n, _ in rows)
    for name, value in rows:
        mark = "UNVERIFIED" if name in C.UNVERIFIED else ""
        print(f"{name:<{width}}  {value:<40} {mark}")
    print()
    print(f"fingerprint: {C.valuation_fingerprint()}")
    print(f"{len(C.UNVERIFIED)} constants are unverified. `python -m watchsniper "
          "constants` is the only place these are printed; no document restates them.")
    return 0


def cmd_catalogue() -> int:
    from .catalogue import Catalogue
    from .db import Database

    cat = Catalogue.load()
    usage = Database(C.DB_PATH).catalogue_usage()
    refs = sorted(cat.references, key=lambda r: (r.verified, -usage.get(r.key, 0)))
    print(f"{len(cat.references)} entries, {cat.unverified_count} unverified\n")
    print(f"{'ver':<4}{'listings':>9}  {'fmv':>9}  {'band':>19}  reference")
    for r in refs:
        band = f"{fmt(r.fmv_low)}-{fmt(r.fmv_high)}" if r.is_band else ""
        print(f"{'ok' if r.verified else '--':<4}{usage.get(r.key, 0):>9}  "
              f"{fmt(r.point):>9}  {band:>19}  {r.display}  [{r.key}]")
    print("\nWork down from the top: unverified first, then by how many real "
          "listings each has priced.")
    return 0


def cmd_notify_test() -> int:
    """Prove the alert channel, rather than assuming it.

    An alert path that has never delivered anything is not an alert path. The
    previous attempt shipped with a Web Push sink that had never sent a single
    notification, and there was no way to find that out short of waiting for a
    deal.
    """
    import secrets

    from .notify import Notification, Notifier

    n = Notifier()
    if not n.enabled:
        suggested = secrets.token_hex(12)
        print("NTFY_TOPIC is not set, so there is nothing to test.\n")
        print("To set it up, which takes about two minutes and needs no account:")
        print("  1. Install 'ntfy' from the Play Store on your Android phone.")
        print("  2. Add a subscription to this topic (or invent your own):")
        print(f"\n         {suggested}\n")
        print("  3. Put it in .env:")
        print(f"         NTFY_TOPIC={suggested}")
        print("  4. Run this command again.\n")
        print("The topic name is the only thing keeping strangers out, so use a")
        print("long random one and do not paste it anywhere public.")
        return 1

    ok, detail = n.send(
        Notification(
            kind="test",
            title="Watch sniper test",
            body="If you can read this on your phone, the alert channel works. "
            "Nothing else in this system will tell you it is broken.",
            priority="default",
            tags="white_check_mark",
        )
    )
    print(f"{'sent' if ok else 'FAILED'} — {detail}")
    print(f"channel: {n.status}")
    if ok:
        print("\nCheck your phone. If nothing arrived, the POST succeeded but the")
        print("subscription is wrong — the topic in the app must match exactly.")
    return 0 if ok else 1


def cmd_missing() -> int:
    from .catalogue import Catalogue
    from .db import Database

    db = Database(C.DB_PATH)
    titles = db.unmatched_titles()
    work = Catalogue.load().missing_worklist(titles)
    print(f"{len(titles)} listings matched no catalogue reference.\n")
    for label, n, examples in work:
        print(f"{n:>5}  {label}")
        for x in examples[:2]:
            print(f"         {x[:92]}")
    print("\nAdd a reference only when you can put a real number on it. An "
          "invented\nFMV widens the surface of numbers you cannot trust; "
          "unpriced is more honest.")
    return 0


def cmd_rescore() -> int:
    engine = _engine(with_client=False)
    started = time.time()
    counts = engine.rescore_all()
    total = sum(counts.values())
    print(f"re-scored {total} listings in {time.time() - started:.1f}s "
          f"under {C.valuation_fingerprint()}")
    for verdict, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {verdict:<28} {n:>6}  {100 * n / max(1, total):5.1f}%")
    return 0


def render_env_example() -> str:
    """Generate `.env.example` from the variables the code actually reads.

    Requirement 20 asks for a file listing every variable the system reads,
    each with where it comes from and what happens if it is missing, and no
    variable the code does not read. Generating it from the registry in
    config.py makes both halves true by construction; a test asserts the
    committed file still matches.
    """
    import textwrap

    out = [
        "# ===================================================================",
        "#  watch sniper — environment. Copy to `.env` and fill in.",
        "#",
        "#  GENERATED by `python -m watchsniper env-example`. Do not hand-edit:",
        "#  it is derived from the variables config.py actually reads, which is",
        "#  what stops it listing things the code ignores or omitting things it",
        "#  needs.",
        "#",
        "#  There is deliberately no eBay USER token here. Phase 1 uses the",
        "#  application (client-credentials) token, which cannot bid and cannot",
        "#  buy. Adding a user token is a startup failure, not a feature flag.",
        "# ===================================================================",
        "",
    ]
    seen = set()
    for var in C.ENV_VARS:
        if var["name"] in seen:
            continue
        seen.add(var["name"])
        out.append("# " + "-" * 66)
        for line in textwrap.wrap(f"WHERE: {var['where']}", 68):
            out.append(f"# {line}")
        for line in textwrap.wrap(f"IF MISSING: {var['missing']}", 68):
            out.append(f"# {line}")
        secret = "SECRET" in var["name"] or "TOPIC" in var["name"] or "ID" in var["name"]
        default = "" if secret and not var["default"] else var["default"]
        out.append(f"{var['name']}={default}")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
