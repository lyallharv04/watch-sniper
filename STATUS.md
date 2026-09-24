# Status

Current state only. What is running, what has actually happened, what to do
next. Nothing here is a decision — when something settles it moves to
[DECISIONS.md](DECISIONS.md) and this file stops mentioning it.

Last verified **2026-09-04**.

---

## Where this is

**Phase 1 is running against live eBay.** The service polls, scores, stores and
serves real listings, and has done so on this machine. That is the claim the
brief asks to be made carefully, so precisely:

| | |
|---|---|
| Credentials | Present and working. Production keyset. |
| Live diagnostic | Run. Passed. One standing finding (no rate-limit header). |
| Listings ingested from live eBay | 877 |
| Poll cycles, all successful | 13, zero errors |
| Browse API calls used | 19 |
| Dashboard | Serving those listings. Every page rendered and checked. |
| Notification channel | Verified end to end — published and read back with title, body, tags and click-through intact. |
| Tests | 65, passing, hermetic — no network, no credentials |
| Deployed to the VPS | **No.** It has run on the operator's desktop only. |

The one thing not done is deployment to the target host, because this session
did not run on it. [docs/DEPLOY.md](docs/DEPLOY.md) is written for someone
executing it blind, with the expected output of every step.

## The caveat that matters more than any of that

**Every FMV in the catalogue is an unverified estimate written by an AI, and no
fee rate has been checked against a real invoice.** 51 of 51 entries are
`verified = false`.

The system says so on every page and in every alert, and that is not modesty —
under these constants the profit floor binds around a reference FMV where a 15%
error consumes the entire margin. The two listings currently marked `PASS` are
seed numbers clearing a maximum bid computed from seed numbers. **Do not act on
them.**

---

## What one live run measured

877 listings, captured 2026-09-04, one compound query per sweep.

| Verdict | n | |
|---|---|---|
| `REJECT_CATALOGUE` | 391 | 44.6% |
| `REJECT_PRICE` | 345 | 39.3% |
| `REJECT_SELLER` | 112 | 12.8% |
| `REJECT_BLACKLIST` | 23 | 2.6% |
| `DEPENDS_ON_UNKNOWNS` | 4 | 0.5% |
| `PASS` | 2 | 0.2% |

84 of the 877 are auctions; 219 are business sellers, which take the
no-buyer-protection branch.

Two things this settles, both of which were open questions in the brief:

**The unknown-field defaults are not too harsh to surface anything.** Six
listings out of 877 reached the operator's attention. That is a usable signal
rate — neither an empty dashboard nor a flood — and it was measured rather than
guessed.

**The catalogue, not the arithmetic, is the binding constraint.** Nearly half of
all listings match no reference. The composition, from
`python -m watchsniper missing`:

- **Hamilton families the catalogue does not cover** — Khaki Navy, Khaki
  Aviation, American Classic, Intra-Matic, PSR, Jazzmaster, Khaki X-Wind. The
  inherited catalogue only ever priced Khaki Field.
- **Christopher Ward's discontinued tail** — C5 Malvern, C8 Flyer, C3 Malvern,
  C7 Rapide, C9 Harrison, C11 MSL.

These are ranked by real traffic on the "Not priced" screen. They have
deliberately **not** been filled in with invented numbers — see
[DECISIONS.md A17](DECISIONS.md) for why adding twenty more guesses makes the
problem worse rather than better.

## Bugs found by running it against live data, all fixed

Kept because each cost real debugging and each is now a test.

1. **TLS interception on the operator's network** broke every request with
   `Missing Authority Key Identifier`. OpenSSL rejects the re-signed chain that
   Windows accepts. Fixed by verifying through the OS trust store.
2. **Item detail pages 404'd.** eBay item ids contain pipes, so the path arrives
   percent-encoded and was never decoded.
3. **The listing's asking price was shadowed by the verdict's effective price**
   in the item query. Identical for Buy It Now, wrong for auctions — which is
   exactly where the difference matters.
4. **`C60 Trident Mk3 Pro 300` matched nothing.** Real titles interleave a
   generation marker, so a single long alias never matched. Eight live listings
   were unpriced for this reason.
5. **`Custom Tiffany Dial` was surfaced as actionable.** The inherited rule only
   matched the literal phrase "custom coloured dial".
6. **eBay UK's condition grade was being discarded.** `conditionId` stays
   generic while the `condition` string says "Pre-owned - Excellent". Reading it
   halved the number of listings whose condition is unknown.

Points 4, 5 and 6 were all found by looking at the dashboard, not by the test
suite, which was green throughout. That is the general lesson: tests passing is
not the same as the funnel working, and `rescore` is free.

---

## Next, in order

### 1. Two documents, one hour, and you are the only one who can do it

Find **one eBay seller invoice** for a watch and **one purchase receipt from a
private seller**. Between them they settle the final value fee, the regulatory
operating fee, the per-order fee and the buyer protection schedule — every
constant that scales every maximum bid. Correct them in `config.py`, run
`rescore`, and the whole model becomes worth arguing with.

This is the highest-value cheap task in the project and nothing else on this
list is worth as much.

### 2. Price the top ten catalogue rows

`python -m watchsniper catalogue` orders them unverified-first, then by how many
real listings each has actually priced. The top few cover most of the traffic.
Set real numbers, flip `verified = true`, `rescore`, and see what your numbers
would have done to the captured set.

### 3. Deploy

[docs/DEPLOY.md](docs/DEPLOY.md). Fifteen minutes, plus step 7. Then leave it
alone for a fortnight.

### 4. Label, then say a number out loud

Every row has three one-click labels. After two weeks of labelled traffic there
is enough to state a precision and a recall figure. Until then, claims about
whether the blacklist is trustworthy are opinions.

### 5. Log outcomes

The `Outcomes` screen. Ten completed buy-and-sell cycles tells you whether
realised margin matches predicted margin. If it sits consistently below, the
correct response is to fix the constants, not to buy more.

---

## Still open

Recorded so none of them gets closed by assumption.

| Question | Settled by | Status |
|---|---|---|
| Real UK final value fee for this category | A real seller invoice | Open. Next step 1. |
| Real buyer protection schedule | A real purchase receipt | Open. Next step 1. |
| GBP bid increment table | eBay's published UK table | Open. Only affects auction reachability, and the plausible tables differ by a pound or two. |
| Whether the seed FMVs are right | Your own sales | Open. Nothing is trustworthy until this moves. |
| Real UK deal flow rate | Two weeks of Phase 1 data | Open. One day of capture is not a rate — the previous attempt extrapolated a 2.3-hour window and overstated flow by 5.4x. |
| Blacklist precision and recall | Your labels | Open. Corpus is 21 cases, three from live traffic. |
| Whether unknown-field defaults are too pessimistic | Comparing pass rates | **Answered for now:** 6 actionable in 877. Not an empty dashboard. Re-check with real FMVs. |
| Whether categories resolve on `EBAY_GB` | The diagnostic | **Answered: yes.** Leaf 31387, filter applied. |
| Whether seller fields come back | The diagnostic | **Answered: yes**, all populated on 50/50 rows. |
| Whether bid *timing* affects realised price | Outcome log on contested lots | Open, and Phase 2 depends on it. |
| Whether the Offer API is approved | Outside your control | Open. Nothing depends on it. |
| Whether a sold-comps vendor's fields match its docs | A live field audit | Deferred with the whole comps question. The previous attempt's audit passed and found 44% Best Offer rows. |

## Not done, deliberately

- **Phase 2.** Nothing in this repository prepares for it. That is the rule the
  brief sets and the mistake the previous attempt made.
- **LLM extraction of unstated fields.** [A10](DECISIONS.md). The band exists so
  it is measurable later whether this pays.
- **Sold comps.** [A11](DECISIONS.md). They cannot calibrate without realised
  sales to check against, so the outcome log comes first.
- **Twenty more catalogue entries.** [A17](DECISIONS.md).
- **A VAT-registered valuation branch.** [A15](DECISIONS.md).
