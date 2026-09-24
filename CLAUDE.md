# CLAUDE.md — the standing spec

What the system *is*. Why it is that way is in [DECISIONS.md](DECISIONS.md),
which is the single owner of every settled question. What is currently true is
in [STATUS.md](STATUS.md).

**No number appears in this file.** Constants live in `src/watchsniper/config.py`
and nowhere else; run `python -m watchsniper constants` to see them. A document
that restates a value is a document going stale, and a test fails the build if
one does.

---

## 0. Hard rules

| | |
|---|---|
| Sourcing | UK domestic only. Never purchase from abroad. |
| Marketplace | `EBAY_GB`. All money GBP. |
| Money | An integer number of pence. No float, anywhere, ever. |
| Time | Timezone-aware UTC at every boundary. |
| VAT | Not registered. eBay fee VAT is a real cost, not reclaimable. |
| Capability | No eBay *user* OAuth token exists. Bidding and buying are impossible by construction. |

---

## 1. Shape

One Python process. Three threads and an HTTP server.

```
   eBay Browse API  ──(application token, read-only)──┐
                                                      ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ poller.Engine                                                │
   │   thread 1  Buy It Now sweep      sort=newlyListed           │
   │   thread 2  auction sweep         sort=endingSoonest         │
   │             then closing prices   getItem, once per auction  │
   │   thread 3  watchdog — says so when 1 and 2 have stopped     │
   └───────────────────────────┬──────────────────────────────────┘
                               │ one compound query per sweep
                               ▼
        models.from_item_summary   →  Listing  (pence, aware UTC)
                               │
                               ▼
   ┌──────────────────────────────────────────────────────────────┐
   │ valuation.Valuer                                             │
   │   catalogue.match      title → reference, or nothing         │
   │   blacklist.check      negation-aware phrase rules           │
   │   every gate evaluated, all results kept                     │
   │   one valuation: FMV point × condition                       │
   │   fees.max_allowable_bid — solved                            │
   └───────────────────────────┬──────────────────────────────────┘
                               ▼
              SQLite  ─────────┬─────────────────►  notify → ntfy → phone
                               ▼
                   web  server-rendered HTML, no arithmetic, installable
                               │  loopback only
                               ▼
          cloudflared (own service) → Cloudflare Tunnel → Access → phone
```

Nothing else in the process. There is no message broker, no cache, no migration
tool and no frontend build. Outside it, on the same host, `cloudflared` runs as
its own service and Cloudflare Access does the authentication (§7, and
`docs/DEPLOY.md`).

---

## 2. Valuation

### One valuation

Each listing is valued once.

- **FMV point** — the catalogue entry's `fmv`, or the midpoint of `fmv_low` and
  `fmv_high` where the entry carries a band (rounded down).
- **Condition** — the only multiplier, `COND_MULT`. The grade comes from the
  condition string, then `conditionId`. An unstated grade is valued as `GOOD`,
  and the item page says it was assumed.

Scope of delivery and bracelet type are still read from the title, stored and
shown as tags on the dashboard. They are labels only and do not move the
number.

The verdict is `DEAL` or `REJECT_<gate>`. Uncertainty is shown as caveats rather
than as a band: `FMV_UNVERIFIED`, `VARIANT_UNRESOLVED` (a banded entry valued at
its midpoint), `AMBIGUOUS_MATCH`, `POSTAGE_UNKNOWN`, and an assumed condition.

### The maximum allowable bid

On a private-seller purchase eBay charges the buyer a tiered Buyer Protection
fee that is a function of the purchase price. The cost of bidding therefore
depends on the bid, so the maximum is **solved for**, never divided out.

```
effective FMV   = FMV point × condition                        (floors)
net proceeds    = effective FMV − platform fees − order fee
                                − outbound postage
required profit = max(percentage of effective FMV, absolute floor)
budget          = net proceeds − required profit
solve for B:      B + inbound postage + buyer_protection(B) ≤ budget
```

Solved by integer bisection to the penny. `fees.py` owns all of it.

Business sellers are a separate branch: their price is VAT-inclusive and no
buyer protection applies. While the operator is unregistered that reduces to
"no buyer protection fee" and nothing else — **if the operator registers, this
branch changes materially** and must be rebuilt rather than adjusted. There is
no registration flag in code; registering means rebuilding the arithmetic.

### Rounding

Stated once, in `money.py`. Value and income round down; costs and required
profit round up; the bid ceiling rounds down. Every choice lowers the maximum
bid, so a rounding error can cost an opportunity and cannot cost money.

---

## 3. Gates

Every gate is evaluated for every listing and all results are stored. Gates
are evaluated in the order below and the verdict names the first failure; the
rest are on the item page. That is deliberate — a rejection carrying one reason
tells you nothing about the others, and requirement 3 exists so rejections can
be debugged.

| Gate | Fails when |
|---|---|
| `CATALOGUE` | No reference matched the title. There is no FMV, so nothing else can be said about price. |
| `BLACKLIST` | A negation-aware phrase rule fired on the title or the condition string. "For parts or not working" is caught here. |
| `SELLER` | Feedback percentage or rating count below the floor. Missing seller data is a caveat, never a rejection. |
| `VIABLE` | No bid at any price clears the profit floor. |
| `PRICE` | Above the maximum bid, or no usable price — including a listing that states no currency. |

UK location and GBP pricing are enforced by the search query's filters, not by
gates.

For auctions the price compared is the **next valid bid**, not the current one —
a listing can sit under the maximum and still be unreachable at the next
increment step.

A `DEAL` alerts once. It alerts again only if its price later drops below the
price quoted in the last delivered alert.

---

## 4. The catalogue

One human-editable TOML file. `verified` is per entry and starts false on all of
them; a valuation derived from an unverified entry is marked as such in the
dashboard and in every notification.

Matching: `excludes` disqualifies, `requires_any` gates, `aliases` or the
reference key admits. `priority` separates a specific variant from a catch-all.
An exact tie values the first entry and is flagged `AMBIGUOUS_MATCH`, naming
the others.

Two rules learned expensively:

- **Every alias must be distinctive on its own.** A bare `integra` matched
  "integrated" and priced a Tissot as a Farer.
- **`synthetic_key` is declared per entry, never inferred.** Sinn's genuine
  `556-I` and our invented `C60-TRIDENT-PRO-300` are the same shape and opposite
  in meaning, so no heuristic on the string can work.

Where a title genuinely cannot settle a variant, the entry carries `fmv_low` and
`fmv_high`. It is valued at their midpoint and flagged `VARIANT_UNRESOLVED`;
its `fmv` line is then ignored, so edit the band.

---

## 5. Blacklist

Negation-aware, because naive substring matching fails in both directions:
"water resistance untested" is boilerplate on most vintage listings, and "not a
replica" contains *replica*. A rule fires only where its pattern matches and no
negation matches within a window either side. Rules run on the title and,
separately, on eBay's condition string.

`tests/corpus.toml` is a build gate. Grow it from listings you have actually
labelled in the dashboard — a hand-authored corpus measures the author's
imagination, a labelled one measures the system.

---

## 6. External API

Browse API, `item_summary/search`, application (client-credentials) token,
`X-EBAY-C-MARKETPLACE-ID: EBAY_GB`. One compound OR query across all brands per
sweep. For Buy It Now a single page reaches far enough back to catch everything
new. The auction sweep pages on until a page ends beyond `AUCTION_HORIZON`, so
every auction ending inside it is seen.

Filters: price band, GBP, buying option, `itemLocationCountry:GB`, and the leaf
watch category. Sorted by newly listed for Buy It Now and ending soonest for
auctions.

Field-level facts confirmed against the live API — the ones that silently
discard listings if you get them wrong:

- `price` is **absent on most auctions**, which carry only `currentBidPrice`.
- `shippingOptions` may be missing entirely. That is unknown postage, not free
  postage.
- A price with no `currency` is not assumed GBP. The listing rejects on `PRICE`.
- eBay UK qualifies the generic pre-owned condition in the `condition` *string*
  while `conditionId` stays generic. The grade is in the string.

### Closing prices

After each auction sweep, every stored auction whose end time is more than
`CLOSING_CHECK_DELAY` past is fetched once with Browse `getItem`, which keeps
returning ended auctions with their final `currentBidPrice`, `bidCount` and
`estimatedAvailabilities`. The `closings` table stores the final price, the bid
count, whether it had bids, and `sold` (`estimatedSoldQuantity` above zero). An
auction with bids that missed its reserve has bids and is not sold. An absent
sold field is stored as unknown, never as unsold. A 404 is stored without a
price so it is not retried; any other failure retries next sweep. Buy It Now
disappearances are not tracked.

**Limited Release APIs are not used and must not be.** The Offer API needs a
user token, which is the one thing this system must not hold. The Order API is
not obtainable. Marketplace Insights is closed to new applicants.

---

## 7. Safety boundary

Requirement 8 is enforced three ways, deliberately of different kinds:

1. `ebay.py` implements the client-credentials grant and no other. The
   capability is absent.
2. `config.assert_phase_1()` refuses to start if a user-token-shaped variable is
   in the environment. It runs before anything else.
3. A test greps every source file for the bidding API and fails on a match.

The first two protect against the environment. Only the third protects against a
future edit, and that is the one that matters in six months.

The dashboard itself has no authentication. It binds to loopback — the systemd
unit sets `BIND_HOST` in the environment, which overrides `.env` — and is
reached only through a token-based Cloudflare Tunnel, with `cloudflared`
installed as its own service. Cloudflare Access in front of the tunnel's
hostname is the only authentication, so the Access application must exist and
cover the whole hostname before the hostname is routed. No inbound port is
opened. The steps and their order are in `docs/DEPLOY.md`.

---

## 7a. Dashboard

Two default views, both sorted by how far the price the gate used sits below
the catalogue FMV (`below_fmv_bp`, computed in valuation and stored, so the web
layer still does no arithmetic):

- **Buy It Now** — `/`.
- **Auctions ending within `AUCTION_ENDING_SOON`** — `/?view=auctions`.

Both hide unmatched listings and `REJECT_BLACKLIST` by default. Choosing a
verdict filters within the view; "everything" shows every listing in either
format.

The Catalogue page's **Observed** column is the median closing price of sold
auctions per entry, with the count behind it, blank below
`OBSERVED_MIN_AUCTIONS`; **Unsold** counts auctions that ended without a sale.
Blacklist-rejected listings are excluded from both.

The dashboard is an installable PWA: `/manifest.json` (linked with
`crossorigin="use-credentials"` so the fetch carries the Access cookie), icons
drawn in code, and a service worker that registers and caches nothing.

---

## 8. Brand notes

Carried forward as domain knowledge. UK liquidity differs sharply from the US
market these notes were originally written against.

| Brand | Traps | UK supply |
|---|---|---|
| **Tissot** | PRX Quartz versus Powermatic 80 is the highest confusion risk in the catalogue — sunburst dial versus waffle, and roughly half the money. A title stating neither must stay ambiguous. | Good; highest arrival rate |
| **Christopher Ward** | Logo era dictates price: cursive *Chr. Ward* lowest, then left-aligned text, then the twin-flags applied logo highest. Rarely in the title. CW sells direct in GBP, so grey margins are thin. Titles interleave a Mk generation marker. | Strong (British) |
| **Hamilton** | Highest replica volume in the catalogue, concentrated on the Khaki Field Mechanical. Check ETA 2801 / H-50 movement finishing and the weight of the inner 24-hour numerals. Most UK flow is families the catalogue does not price yet. | Good |
| **Seiko** | Auto-reject "Mumbai Specials" — vintage 6309/7002 with aftermarket coloured dials. Clone brands write the model name and never write "homage", so the Alpinist entry requires the brand in the title. | Good |
| **Farer** | Inspect for original colour-matched hands; aftermarket hands destroy resale demand. Several model names are ordinary English words and must be brand-qualified. | Moderate (British) |
| **Baltic** | Beads-of-rice bracelet commands a premium over Tropic rubber. Bronze patinates, so condition language is unreliable. | Thin (French) |
| **Sinn** | OEM H-Link solid bracelet adds materially over leather. Zero UK standing stock when measured, and the 556 trades above the search ceiling. | Very thin (German) |

A long estimated handling time is the reliable tell for a dropshipped grey
import wearing a GB address.

---

## 9. Phase 2 — not built

Nothing in this repository prepares for it, on purpose. Dormant scaffolding is
a maintenance cost that pays nothing, and the previous attempt carried a broker
behind an unused profile and a second deployment target that never ran.

When it is built, the fact to build against is that **eBay bids by proxy**. A
maximum submitted early wins at the same price as the same maximum submitted at
the last second, against the same competing maxima. Late placement buys only
informational advantage, which is smallest in thin markets. If that holds, there
is no scheduler, no armed/fired state machine and no clock synchronisation — and
requirement 13, that no money moves while the operator is asleep, becomes free
rather than engineered.

Phase 2 should not start until the outcome log says predicted margin matches
realised margin. Firing faster at wrong numbers loses money faster.
