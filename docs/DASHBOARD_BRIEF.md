# Dashboard redesign brief

For a designer redesigning the watch sniper dashboard from scratch. It describes
only what the code does today (`src/watchsniper/web.py`, `db.py`,
`valuation.py`, `notify.py`). Example values are real, from the 2026-09-04
capture re-scored under the current model; constants can change, and
`/constants` shows the live ones.

## The tool and its one user

Watch sniper finds underpriced enthusiast wristwatches on eBay UK, values each
listing against a hand-kept catalogue of fair market values (FMV), and pushes
the good ones to a phone. **It cannot bid or buy.**

One person uses it. It sits behind Cloudflare Access, which handles login
before any page loads, so the dashboard has **no login screen, accounts or
settings**. The operator's job: spot a bargain, research it (open it on eBay,
check the photos, sanity-check the FMV), and decide whether to buy it by hand.
Mostly on a phone, often straight from a push notification. Their second job is
teaching the system: one-tap labels on wrong calls, and curating catalogue FMVs.

## Screens

Every page shows up to three warning banners at the top: notifications off
(when no ntfy topic is set), "51 of 51 catalogue FMVs are unverified
estimates", and "No fee rate … has been checked against a real invoice".

**Feed** — `/` (Buy It Now), `/?view=auctions` (auctions ending within six
hours), `?verdict=all` (everything, both formats). Up to 250 rows, sorted by
% below FMV. By default it hides unmatched listings and blacklist rejections.
Filters: verdict (default, `all`, `DEAL`, each `REJECT_*`), brand, "title
contains"; plus a 14-day tally of verdict counts. Each row:

| Field | Example |
|---|---|
| Verdict · primary reason | `DEAL` · "Clears every gate." |
| Title (links to item page) | "Tissot PRX Chronograph p475" |
| Format · seller type | BIN · Individual |
| Scope / bracelet tags (from title; labels only) | "watch box" (usually absent) |
| Matched reference | "Tissot PRX Chronograph" |
| Important caveat tags | FMV UNVERIFIED, variant unresolved, ambiguous match, no seller data |
| Below FMV | 79.1% |
| FMV | £600.00 |
| Price · price basis | £125.50 · "buy it now" (auctions: "next bid") |
| Max bid | £272.87 |
| Headroom (max bid − price) | £147.37; negative when over: -£24.27 |
| Label buttons | should have passed · should not have · FMV wrong |

**Item page** — `/item/<id>`. Title; verdict, every caveat tag, and the primary
reason; "Open on eBay"; label buttons. Then:

- *Listing*: asking/current £125.50, postage £4.52 (can be unknown), price the
  gate used, format, bids, ends (UTC), condition "Pre-owned - Good",
  scope/bracelet, location GB.
- *Seller*: account type INDIVIDUAL/BUSINESS, feedback 100.0%, 792 ratings,
  buyer protection charged (private) or not (business).
- *Reference*: "Tissot PRX Chronograph", key `PRX-CHRONO`, FMV £600.00,
  verified "no — an estimate", scored under `36fb19fc2424` (config fingerprint).
- *Gates*: all five, pass/fail with detail, e.g. `SELLER` "100.00% over 792
  ratings", `PRICE` "buy it now 12550p vs MAB 27287p".
- *Valuation*: "Reference FMV £600.00 × GOOD = £504.00 effective" (adds "not
  stated, assumed" when the condition was guessed), then the ledger below.
- *Your labels*: each label with its time and the verdict at the time.

**Catalogue** — `/catalogue`. All 51 reference entries, unverified first and
then by traffic, with "reload catalogue" and "re-score everything" buttons.
Columns: verified (✓/—), reference "Tissot PRX Powermatic 80 40mm", key
`T137.407.11.041.00`, FMV £380.00, band (only on banded entries, e.g. "PRX
movement not stated" £165.00–£420.00, valued at its midpoint), **Observed** £340.20 (3) = median closing price of *sold* auctions
and how many (blank under three), **Unsold** 3 = auctions that ended without a
sale, listings priced 69, notes.

**Not priced** — `/missing`. Unmatched titles grouped by brand + model word,
most frequent first: "Hamilton khaki navy — 21", with three example titles.

**Outcomes** — `/outcomes`. A manual form (item id, description, paid,
predicted FMV, sold for, fees, note) and its log with realised margin. Empty
today.

**Health** — `/health`. Healthy or "STALE — ingestion has stopped", last
successful poll, Browse calls today (87 of 4800), last error, alert channel,
config fingerprint; tables of recent polls (time, kind, calls, seen, new,
alerts, error) and notifications (sent/FAILED). `/api/health` is JSON.

**Constants** — `/constants`. Each constant, its value, what would verify it,
and an "unverified" tag.

## Verdicts, % below FMV, the price derivation

`DEAL` means every gate passed. Otherwise the verdict names the first failure:
`REJECT_CATALOGUE` (no reference matched, so no FMV; 392 of 886 listings),
`REJECT_BLACKLIST` (a phrase rule fired, e.g. a bracelet sold alone; 23),
`REJECT_SELLER` (feedback below the floor, e.g. "12 ratings"; 114),
`REJECT_VIABLE` (no price could make a profit) and `REJECT_PRICE` (above the
max bid, or no usable price; 354). Only 3 are `DEAL`.

**% below FMV** = (FMV − price the gate used) ÷ FMV, against the catalogue
FMV before the condition adjustment. It is a sort key, **not** a verdict: the
third row today is 57.3% below FMV and still `REJECT_PRICE`, because its max
bid is £100.73.

**The derivation** solves for the most you can pay and still hit the profit
floor after resale fees:

```
Sale price (effective FMV)      £504.00   FMV × condition multiplier
eBay fees @ 17.25% x VAT       -£104.33
Per-order fee inc. VAT            -£0.36
Outbound postage                  -£9.50
Net proceeds                     £389.81
Required profit                 -£100.80
Acquisition budget               £289.01
Inbound postage                   -£4.52
Buyer protection at this bid     -£11.62
Maximum allowable bid            £272.87
```

## What is unreliable, and must stay visible

- **Every FMV is an unverified estimate** (51 of 51), as is every fee rate.
  Every valuation and every alert says so.
- **Auction prices rise a lot before the end.** The price shown is the next
  valid bid *now*. 28 of 31 sold auctions closed above the last price seen,
  e.g. £180.00 (20 bids) → £410.00 (34 bids). A cheap auction hours out is
  not a bargain yet; the end time and bid count matter as much as the price.
- **Observed medians rest on very few auctions**: three or four today, and
  hidden below three. The count must stay attached to the median.
- Smaller gaps: unknown postage, an assumed condition, an ambiguous match, and
  a banded entry valued at its midpoint.

## Alerts

A `DEAL` sends one ntfy push to the phone, and another only if its price later
drops. Title "Tissot PRX Chronograph at £125.50"; body "Under the maximum bid
of £272.87 by £147.37.", the listing title, "Buy It Now" or "Auction, ends
06 Sep 19:35 UTC", an FMV-unverified warning, and a link to the item page on
the dashboard. **Tapping the notification opens the eBay listing**; the
dashboard link in the body is the route into this UI.

## Hard constraints

- Server-rendered HTML from Python's standard library (`http.server`). No
  framework, no build step, no external assets: CSS and JS are inline in the
  page; plain HTML forms POST to `/label`, `/reload`, `/rescore`, `/outcome`.
- **The page does no arithmetic.** Every figure is a stored value formatted for
  display. A design may not call for a number that isn't in the lists above.
- Must remain an installable PWA: `/manifest.json` linked with
  `crossorigin="use-credentials"`, `/sw.js` (caches nothing), icons at
  `/icon-192.png` and `/icon-512.png`.
- Phone-first: one-handed use and readable tables at phone width, with
  safe-area insets respected in standalone mode. Wide screens must work too.
- Light and dark themes, following the system setting.

## Non-goals

No bidding or buying, no purchase or order tracking beyond the manual outcomes
form, no new endpoints or data fields, no login/account/settings screens, no
offline mode, no client-side recalculation of any value.
