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
   │   two valuations: pessimistic and optimistic                 │
   │   fees.max_allowable_bid — solved, per scenario              │
   └───────────────────────────┬──────────────────────────────────┘
                               ▼
              SQLite  ─────────┬─────────────────►  notify → ntfy → phone
                               ▼
                   web  server-rendered HTML, no arithmetic
```

Nothing else. There is no message broker, no cache, no migration tool, no
frontend build, no reverse proxy and no edge authentication. Each of those was
considered and each is recorded as rejected in DECISIONS.md against a
requirement, not against another component.

---

## 2. Valuation

### The band

Most listings state neither scope of delivery nor bracelet type, and those
unknowns move the number further than the whole fee stack does. So each listing
is valued twice.

- **Pessimistic** — every unstated field takes its worst plausible value, and
  the reference FMV takes the bottom of its band.
- **Optimistic** — every unstated field takes its best, and the reference FMV
  takes the top.

The gate uses the pessimistic figure. `PASS` therefore means *a deal even on the
worst reading*. A listing clearing only the optimistic ceiling is
`DEPENDS_ON_UNKNOWNS`, and the dashboard names the fields it depends on.

The width of the band is the uncertainty. That is requirement 4 satisfied
structurally rather than by a confidence score.

### The maximum allowable bid

On a private-seller purchase eBay charges the buyer a tiered Buyer Protection
fee that is a function of the purchase price. The cost of bidding therefore
depends on the bid, so the maximum is **solved for**, never divided out.

```
effective FMV   = reference × condition × scope × bracelet     (floors)
net proceeds    = effective FMV − platform fees − order fee
                                − outbound postage − service buffer
required profit = max(percentage of effective FMV, absolute floor)
budget          = net proceeds − required profit
solve for B:      B + inbound postage + buyer_protection(B) ≤ budget
```

Solved by integer bisection to the penny. `fees.py` owns all of it.

Business sellers are a separate branch: their price is VAT-inclusive and no
buyer protection applies. While the operator is unregistered that reduces to
"no buyer protection fee" and nothing else — **if he registers, this branch
changes materially** and must be rebuilt rather than adjusted.

### Rounding

Stated once, in `money.py`. Value and income round down; costs and required
profit round up; the bid ceiling rounds down. Every choice lowers the maximum
bid, so a rounding error can cost an opportunity and cannot cost money.

---

## 3. Gates

Every gate is evaluated for every listing and all results are stored. The
verdict names the first failure in cost order; the rest are on the item page.
That is deliberate — a rejection carrying one reason tells you nothing about the
other seven, and requirement 3 exists so rejections can be debugged.

| Gate | Fails when |
|---|---|
| `CATALOGUE` | No reference matched the title. There is no FMV, so nothing else can be said about price. |
| `BLACKLIST` | A negation-aware phrase rule fired. |
| `DOMESTIC` | Not located in GB. |
| `CONDITION` | For parts or not working. |
| `SELLER` | Feedback percentage or rating count below the floor. Missing seller data is a caveat, never a rejection. |
| `CURRENCY` | Priced in something other than GBP. |
| `VIABLE` | No bid at any price clears the profit floor. |
| `PRICE` | Above the maximum bid even on the optimistic reading. |

For auctions the price compared is the **next valid bid**, not the current one —
a listing can sit under the maximum and still be unreachable at the next
increment step.

---

## 4. The catalogue

One human-editable TOML file. `verified` is per entry and starts false on all of
them; a valuation derived from an unverified entry is marked as such in the
dashboard and in every notification.

Matching: `excludes` disqualifies, `requires_any` gates, `aliases` or the
reference key admits. `priority` separates a specific variant from a catch-all.
An ambiguous match widens the band across every tied entry rather than picking
one, because ambiguity is a data gap and should look like one.

Two rules learned expensively:

- **Every alias must be distinctive on its own.** A bare `integra` matched
  "integrated" and priced a Tissot as a Farer.
- **`synthetic_key` is declared per entry, never inferred.** Sinn's genuine
  `556-I` and our invented `C60-TRIDENT-PRO-300` are the same shape and opposite
  in meaning, so no heuristic on the string can work.

Where a title genuinely cannot settle a variant, the entry carries `fmv_low` and
`fmv_high` instead of pretending to a point.

---

## 5. Blacklist

Negation-aware, because naive substring matching fails in both directions:
"water resistance untested" is boilerplate on most vintage listings, and "not a
replica" contains *replica*. A rule fires only where its pattern matches and no
negation matches within a window either side.

`tests/corpus.toml` is a build gate. Grow it from listings you have actually
labelled in the dashboard — a hand-authored corpus measures the author's
imagination, a labelled one measures the system.

---

## 6. External API

Browse API, `item_summary/search`, application (client-credentials) token,
`X-EBAY-C-MARKETPLACE-ID: EBAY_GB`. One compound OR query across all brands per
sweep; a single page reaches far enough back to catch everything new.

Filters: price band, GBP, buying option, `itemLocationCountry:GB`, and the leaf
watch category. Sorted by newly listed for Buy It Now and ending soonest for
auctions.

Field-level facts confirmed against the live API — the ones that silently
discard listings if you get them wrong:

- `price` is **absent on most auctions**, which carry only `currentBidPrice`.
- `shippingOptions` may be missing entirely. That is unknown postage, not free
  postage.
- eBay UK qualifies the generic pre-owned condition in the `condition` *string*
  while `conditionId` stays generic. The grade is in the string.

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

The dashboard has no authentication and is not meant to. It binds to loopback
and is reached over a tunnel or a private network. Do not give it a public
hostname; that is the choice that forced the previous attempt into five layers
of edge security defending a problem it had created.

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
