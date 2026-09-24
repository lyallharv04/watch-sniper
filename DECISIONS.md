# Decisions

Append-only. Dated. Reasoning only — a decision records *why*, and the value it
settled lives in code. If you want to know what a constant is, run
`python -m watchsniper constants`; if you want to know why it is that, read
here.

`A`-numbered rows are this rebuild's. `D`-numbered rows are inherited from the
V2.0 attempt and are carried forward with their reasoning intact, including the
ones this rebuild overturns — a decision you reversed without recording why you
believed it is a decision you will make again.

---

## 1. This rebuild — 2026-09-04

### A1. Python 3.13, standard library only, with one optional dependency

The whole runtime is `urllib`, `sqlite3`, `ssl`, `http.server`, `tomllib`,
`threading` and `re`. There is no framework, no ORM, no migration tool, no
package manager step and no `node_modules`.

The reasoning is the failure mode the brief names. The previous attempt reached
fourteen infrastructure components to show one person a list of watches, and
most of them were guarding each other rather than satisfying a requirement.
Every dependency is a thing that can break on a machine you are not looking at,
and this system's whole value is that it keeps running for a fortnight while
the operator is at work. `pip install` returning an error at 3am on a VPS is a
failure mode; not having a `pip install` step removes it.

`truststore` is the one optional dependency — see **A16**. Nothing breaks
without it except on a network doing TLS inspection.

### A2. SQLite, and money as INTEGER pence at the storage layer

Write volume is a few hundred rows an hour, there is exactly one writer, and
one person reads it. A database server buys concurrency, network access and
operational tooling, none of which is needed, and costs a container, a
migration tool, a connection string and a backup procedure.

Storing money as integers is the load-bearing half of this. Requirement 14 says
money is never a floating-point value; with a `Decimal`-in-Python-and-`NUMERIC`-in-storage
design that is a convention someone eventually breaks. With `INTEGER` columns
and a `Pence = int` alias it is a type error at the boundary. `money.py` is the
only module that knows how to turn pence into a string, and it is the last
thing that happens before display.

Rounding policy is stated once, in `money.py`, and points one way: value and
income round down, costs and required profit round up, the bid ceiling rounds
down. Every rounding choice therefore lowers the maximum bid, so a rounding
error can cost an opportunity and cannot cost money.

*Supersedes **D6** (Postgres as system of record) and completes **D7** (no
Redis) by removing the dormant `stage2` profile rather than keeping it unused.*

### A3. The dashboard is server-rendered HTML from `http.server`

No React, no Next.js, no TypeScript, no build step, no API contract to keep in
sync.

The direct argument is requirement 16 and its stronger form in the brief's §6:
requirement 16 is structurally easier to satisfy if the presentation layer
cannot recompute valuations. A TypeScript dashboard *can* recompute a margin,
and given a deadline and a missing field, one day it will — and then what the
operator sees and what the system decided are two different numbers. Rendering
stored pence into strings on the server makes the drift impossible rather than
forbidden.

The indirect argument is that a separate frontend was the root of the previous
attempt's security apparatus. It needed to be served from somewhere, which
meant a hostname, which meant authentication. See **A5**.

*Reinforces **D21**, which said the dashboard must never recompute FMV, MAB or
margin. That was enforced by review. It is now enforced by there being no
language there to do it in.*

### A4. Notifications go over ntfy.sh, a public topic with no account

The operator has an Android phone and no Telegram account. The requirement is a
channel he actually receives, and the constraint added on top is that it should
need no account and no credential.

ntfy meets both exactly: you invent a topic name, install the app, subscribe,
and anything that can make an HTTP POST can reach your phone. There is no
account to create, no key to rotate, no VAPID keypair, no service worker, and —
critically — no subscription object that can be silently invalidated. The
previous attempt needed a push-subscription health monitor because Web Push
subscriptions die quietly, and then needed an email fallback because the health
monitor needed somewhere to escalate to. Three components, all of them guarding
the first choice. Removing the choice removes all three.

The cost, stated plainly and repeated in `notify.py` and `.env.example`: a
public ntfy topic is readable by anyone who knows the topic name. Nothing
secret travels over it — the payload is a public eBay listing and a price — but
the topic name is the only thing between your alerts and a stranger, so it must
be long and random. Self-hosting ntfy on the same VPS later changes one
environment variable.

Verified working on 2026-09-04: published and read back with title, body, tags
and click-through intact.

*Supersedes **D19** (Web Push primary, email backstop).*

### A5. Bind to loopback. No authentication, because no exposure

The dashboard listens on `127.0.0.1` and is reached over an SSH tunnel or a
private network overlay such as Tailscale or WireGuard. There is no login page,
no session, no edge authentication and no public hostname.

This is the brief's §6 point, and it is the right one: most of the previous
attempt's security apparatus existed to defend a public hostname it had chosen
to have. Cloudflare Tunnel, Cloudflare Access, an origin IP allowlist, a shared
secret header and a short-lived IP certificate that had to be renewed twice
daily or the dashboard went dark — five moving parts, none of which protects
against anything if the hostname simply does not exist. Removing the exposure
is a different and better kind of fix from guarding it.

The service refuses nothing if you set `BIND_HOST=0.0.0.0`, but it prints a
warning on startup saying what you have just done, because an unauthenticated
dashboard on the open internet is a real thing to have done accidentally.

*Supersedes **D20**, **D30** and **D31**.*

### A6. Bidding is impossible by construction, and three things enforce it

Requirement 8 asks that the system hold no eBay user OAuth token. Three
mechanisms, deliberately of different kinds, because a single mechanism is a
single thing to forget:

1. `ebay.py` implements the client-credentials grant and nothing else. There is
   no authorization-code flow, no redirect URI handler and no refresh-token
   store. The capability is absent, not disabled.
2. `config.assert_phase_1()` refuses to start if anything user-token-shaped is
   in the environment. It runs before any other work in `__main__`.
3. A test greps every source file for `place_proxy_bid`, `buy.offer.auction`
   and `authorization_code`, and fails if it finds one.

The third is the one that matters in six months. The first two protect against
the environment; only the third protects against a future edit.

*Carries forward **D2** unchanged and strengthens it.*

### A8. The maximum allowable bid is solved by integer bisection

`bid + buyer_protection(bid)` is strictly increasing in `bid`, so bisecting to
the penny is exact. A closed form per fee tier would be faster and would have
three tier boundaries to get wrong; nothing here runs often enough for that
trade to make sense. A test asserts the answer is tight — one penny more does
not fit — across the whole plausible FMV range, which is a stronger claim than
any single golden case.

The golden case still exists and still reproduces exactly: Tissot PRX at the
inherited seed FMV, mint, full set, £201.71. It agrees with the inherited
arithmetic to the penny, which is the evidence that the port did not quietly
change the model.

*Carries forward **D5**.*

### A9. The FMV catalogue is one TOML file the operator edits directly

Not a database table with an import step, not a JSON seed that stops mattering
after first boot. The previous attempt had `brand_catalog.json` marked
seed-only, with the live catalogue in a `catalog_entries` table — so editing the
obvious file did nothing, which is a trap laid for your future self.

TOML because it takes comments, and the comments are half the point: the file
opens with what `fmv` means, how to curate it and how to check your work.
`verified` is per entry and starts `false` on all of them.

Reloading is a button. Re-scoring every captured listing under a new number
takes under a second, offline and free, and is the single most useful thing the
operator can do after editing — it shows what the new number would have done to
real traffic rather than to a hypothetical.

### A10. No LLM extraction in Phase 1

The inherited design routed low-confidence listings to a vision model to
resolve scope, bracelet and Christopher Ward logo era.

Dropped, for a reason that is not cost. Phase 1's product is a labelled record
of how often the valuation model was right and in which direction it was wrong.
An LLM that fills in an unstated field converts a *known* unknown into a
*confident* value, and the resulting error is then indistinguishable from FMV
error in the outcome log. Emitting the band instead keeps the two separable,
and makes it measurable later whether resolving those fields is worth what it
costs — which is a question you cannot answer once you have already been
answering it.

It also removes an API key, a daily spend cap, a response cache and a prompt
that must be kept in sync with an enum.

**What would reverse this:** band width, per reference, on listings that reach
`DEPENDS_ON_UNKNOWNS`. If a material share of actionable listings sit in a wide
band that a photograph would collapse, vision earns its place. That number is
in the dashboard now; it was not before.

### A11. No sold-comps vendor in Phase 1

The inherited work settled on CompSniper and validated it against a live key.
That was real work and the finding — 44% of returned rows were Best Offer and
would have biased FMV upward if aggregated — is worth keeping.

Deferred anyway, for the reason the brief's §6 gives: sold comps cannot
calibrate themselves without realised sales to check against. `SOLD_BAND_EXPECTED_RATIO`
was assumed at 0.80 and could only ever be earned from the operator's own
outcomes. Building the outcome log first is the shorter path to the same place,
and it removes a paid dependency on a vendor whose supply of eBay data can stop
without notice.

The outcome log exists and is the first screen that will contain a real,
unarguable number.

*Defers **D9**, **D10**, **D11**, **D12** and **D28**. None is reversed — the
reasoning in all five still stands and should be re-read before building it.*

### A13. One compound query per sweep, not one per brand

Measured against the live API on 2026-09-04: a single OR query across all seven
brands returns 836 in-band GB listings, against 364 for Hamilton alone, and the
brand mix in the results matches the measured arrival mix — so the OR is
genuinely searching all seven. One 200-row `newlyListed` page reaches about
twelve days back at the measured arrival rate, so one page per sweep catches
everything new with enormous margin.

Seven calls per sweep would have been roughly 7,700 Browse calls a day, over any
plausible ceiling. One call per sweep is roughly 1,100. The call budget stopped
being a design constraint, which is why there is no token bucket and no
degradation ladder beyond a single "slow down when the budget runs low" branch.

### A14. `.env.example` is generated, not written

Requirement 20 asks for a file listing every variable the code reads, each with
where it comes from and what happens without it, and no variable the code
ignores. Both halves of that go stale the first time someone adds a variable in
a hurry.

`config.env()` registers each variable as it reads it, and `.env.example` is
rendered from that registry. A test asserts the committed file still matches. It
cannot list a variable the code does not read, and it cannot omit one it does.

### A15. There is no VAT-registered valuation branch

The inherited model carried a second arithmetic path for the registered case,
with its own golden figure. `VAT_REGISTERED` is `False`, the operator is under
the threshold, and the registered path was as unverified as everything else —
so it was a second unchecked model kept warm for a state the business is not in.

Registering would reduce every maximum bid materially and would need the
arithmetic rebuilt against an accountant's advice anyway, not resurrected from a
constant. `VAT_REGISTERED` remains as the single flag that would need to change,
and the threshold figures remain so the display has one owner.

### A16. TLS uses the operating system's trust store where it can

The operator's network runs a TLS-inspecting proxy. Python's bundled OpenSSL
rejects the re-signed chain outright — `certificate verify failed: Missing
Authority Key Identifier` — even though the corporate root is installed and
trusted by Windows, because OpenSSL builds the chain itself instead of asking
the OS. Confirmed on first live contact.

`truststore` delegates verification to the platform verifier and fixes it. It is
the project's only dependency and is needed only on a network like that; a plain
VPS uses the stdlib fallback.

**There is deliberately no way to disable verification.** Not a flag, not an
environment variable. This process informs what the operator pays for a watch,
and a switch that accepts any certificate is exactly the kind of thing that gets
turned on during a debugging session and left on.

*Carries forward the inherited "there is deliberately no `verify=False` option".*

### A17. Coverage gaps are shown, not filled with invented numbers

On first live contact, 45% of listings matched no catalogue reference — almost
all of them Hamilton families outside Khaki Field (Jazzmaster, Khaki Aviation,
Khaki Navy, American Classic, Intra-Matic, PSR) and Christopher Ward's
discontinued tail (C5 Malvern, C8 Flyer, C9 Harrison, C11 MSL, C7 Rapide).

The tempting fix is to add twenty more references. The inherited project did
exactly that, going from 20 entries to 50, and its own status file records the
result honestly: coverage improved and *trust got worse*, because there were now
thirty more invented numbers than before.

So the coverage gap is surfaced as a ranked worklist instead — the "Not priced"
screen groups unmatched listings by brand and model and orders them by real UK
traffic. Adding a reference is then a decision the operator makes when he can
put a real number on it. Unpriced is a more honest output than confidently
wrong, and a ranked list of what to price next is worth more than twenty guesses.

---

## 2. Inherited decisions, and what happened to them

Carried forward from the V2.0 attempt. Reasoning preserved; status is this
rebuild's.

| # | What it said | Status |
|---|---|---|
| D1 | UK domestic only, `EBAY_GB`, `itemLocationCountry:GB`. Operator requirement; the original spec sourced exclusively from abroad. | **Kept.** |
| D2 | Two stages; Phase 1 holds no eBay user token, so bidding is impossible by construction rather than disabled by a flag. | **Kept**, strengthened — see **A6**. |
| D3 | Manual execution loses almost nothing, because eBay auctions are proxy auctions with a hard close and the alpha is in finding the mispriced listing, not in shaving seconds. | **Kept.** It is also why Phase 2 needs no scheduler — see the note below. |
| D4 | Not VAT registered; eBay fee VAT is therefore not reclaimable, and the threshold monitor is a real guard. | **Kept.** The second arithmetic branch is not — see **A15**. |
| D5 | The maximum bid is solved for, not divided out, because buyer protection is a function of the bid. | **Kept.** See **A8**. |
| D6 | Postgres is the system of record, because Redis is not durable, queryable or auditable. | **Superseded by A2.** The argument against Redis was right and still holds; the argument for a *server* did not survive one writer and one reader. |
| D7 | Stage 1 needs neither Celery nor Redis; dedup is the listings primary key and scheduling is two loops with a sleep. | **Kept and completed.** The dormant Redis service behind a `stage2` profile is gone. Dormant scaffolding is a maintenance cost that pays nothing. |
| D8 | Comps come from a curated catalogue, because Marketplace Insights is closed and `findCompletedItems` is retired. | **Kept.** |
| D9 | Tier 2 sold comps from CompSniper; a managed scraper carries the block risk on the vendor's account rather than yours. Validated live. | **Deferred — A11.** Not reversed. Re-read before building. |
| D10 | Tier 2 produces a band, never a reverse-normalised point, because dividing by the same multiplier the live pipeline multiplies by compounds the error upward. | **Deferred with D9** — but the *idea* is now everywhere: **A7** is the same insight applied to live listings instead of comps. |
| D11 | Best Offer rows are stored but never aggregated, because eBay reports the asking price on those. Every exclusion is counted. | **Deferred with D9.** The finding that 44% of live rows were Best Offer is the reason to keep this rule when the time comes. |
| D12 | Tier 2 writes suggestions, not silent rewrites. | **Deferred with D9.** |
| D13 | Capital caps are rolling windows, never calendar periods, because a calendar reset lets you spend the cap twice in twelve hours. | **Kept as constants, displayed, unenforced** — nothing in Phase 1 can spend money. The reasoning is what matters when Phase 2 enforces them. |
| D14 | The VAT threshold monitor is the actual guard; the spend caps govern purchases and the threshold is on sales. | **Kept as constants.** Not tracked until there are outcomes to track. |
| D15 | Snipe offsets T-8s primary, T-4s retry, because T-3s has no retry margin. | **Phase 2, and probably moot.** Given D3, precise late placement buys only informational advantage, which is smallest in thin markets. A scheduler, an armed/fired state machine and clock synchronisation may all be avoidable — and avoiding them makes requirement 13 (no money moves while the operator sleeps) free rather than engineered. |
| D16 | LLM prompt enum lists are generated from the Python enums, because hand-written ones drift. | **Moot — A10.** There is no prompt. |
| D17 | Stage 1 biases toward recall, because a human adjudicates every alert. | **Kept in spirit.** `DEPENDS_ON_UNKNOWNS` is the recall bias: it surfaces the marginal case and labels it marginal. |
| D18 | Blacklist matching is negation-aware with a labelled corpus, because naive matching fails both ways. | **Kept**, rules ported intact. The corpus is now a build gate and has grown from live traffic. |
| D19 | Web Push primary, email backstop, no Telegram, no iOS. | **Superseded by A4.** The constraint that produced it — no Telegram, no iOS — is unchanged and is what ntfy satisfies. |
| D20 | Dashboard behind Cloudflare Tunnel and Access, because that surface reaches money and auth should never be hand-rolled. | **Superseded by A5.** "Never hand-roll auth" is right. Not having a public surface is better than either. |
| D21 | The dashboard never recomputes FMV, MAB or margin in TypeScript. | **Kept and made structural — A3.** |
| D22 | Host is a plain VPS, not a scale-to-zero platform, because this is a stateful poller that must never sleep. | **Kept.** |
| D23 | The Offer API application was filed to start the clock, but nothing depends on it. | **Kept.** |
| D24 | Deadman heartbeat, because a detection system that dies silently at 3am is useless. | **Kept.** The push-subscription-health half is moot: **A4** removed the thing it monitored. |
| D25 | `MAX_EXPOSURE_PER_REF` stays at the higher figure; self-competition is real but hypothetical, and deal flow binds first. | **Kept as a constant.** Revisit if you ever hold three of one reference. |
| D26 | Sinn loses its poller slot; measured zero UK standing stock, and the 556 trades above the search ceiling. | **Partially reversed.** With one compound query (**A13**) a brand costs nothing to include, so Sinn is back in the query — the reason to drop it was per-brand call cost, and that reason is gone. Its entries were always retained. |
| D27 | Widen the catalogue rather than narrowing the brand list, because dropping brands shrinks the funnel. | **Kept as direction, qualified by A17.** Widen it with real numbers, not with more estimates. |
| D28 | `SOLD_BAND_EXPECTED_RATIO` is retained but is not a gate. | **Deferred with D9.** |
| D29 | The live rejection gates recorded in cost order, so "are we over-gating?" is answerable from measurement. | **Kept and improved.** Every gate is now evaluated and stored for every listing, not just the first one to fail, so the question is answerable per gate rather than per listing. |
| D30 | Deployment Option B: dashboard on Cloudflare behind Access, FastAPI on the VPS reachable over the internet. | **Superseded by A5.** |
| D31 | The public origin is defended in three layers, none sufficient alone. | **Superseded by A5.** Every layer was defending the exposure created by D30. |
| D32 | An upstream 5xx from the comps vendor retries once, on its own budget, because a 502 means eBay blocked the vendor and retrying does not unblock them. | **Deferred with D9.** |

---

## 3. What first live contact changed — 2026-09-04

Recorded because live behaviour that contradicts a plan is information.

- **Category 31387 resolves on `EBAY_GB` and the filter is applied.** 366
  in-band results with it against 3,865 without. This was an open question and
  it is now closed. Only the leaf is used; the parent id the inherited docs
  listed as "Vintage" is an ancestor of straps as well as watches.
- **Every seller field arrives populated.** `sellerAccountType`,
  `feedbackPercentage` and `feedbackScore` came back on 50 of 50 rows. The fee
  branch and the seller guard both work. The code still degrades gracefully with
  a caveat if they stop arriving, because a field that is present today is not
  a guarantee.
- **eBay exposes no rate-limit header.** `EBAY_DAILY_CALL_CEILING` cannot be
  checked from a response and has to be taken from the published figure.
- **80% of auctions carry no `price` field**, only `currentBidPrice` — 16 of 20
  on the live sweep. The inherited bug report said this dropped 6.7% of
  listings; on the auction sweep specifically it would have dropped four fifths
  of them.
- **eBay UK qualifies "Pre-owned" with a grade** in the `condition` string —
  "Pre-owned - Excellent", "- Good", "- Fair" — while `conditionId` stays 3000.
  That is a seller-declared grade on a fixed scale and it was being discarded.
  Reading it cut `DEPENDS_ON_UNKNOWNS` from 8 to 4 on the captured set, because
  fewer listings have an unknown condition to widen the band.
- **Real titles interleave a generation marker.** "C60 Trident **Mk3** Pro 300"
  does not contain the substring "c60 trident pro 300", and eight live listings
  were unpriced for exactly that reason. Alias matching on a single long phrase
  is fragile; the fix was to split it into an alias plus a `requires_any` term.
- **"Custom Tiffany Dial" was not blacklisted** and surfaced as actionable. The
  inherited rule matched only the literal phrase "custom coloured dial". A
  custom dial is not a discounted PRX, it is a different and much less liquid
  object.

---

## 4. Archive

Entries no longer in force, kept verbatim so their reasoning is not lost.

Archived 2026-09-24: **A7** and **A12** are superseded by the single
valuation (FMV point × condition only; scope and bracelet are labels).
A12's reasoning rested on `BRACELET_MULT`, which no longer exists, and the
service-buffer parameter it kept has been removed.

### A7. Every listing gets two valuations, not one

Most listings state neither scope of delivery nor bracelet type, and the brief
is explicit that these unknowns move the valuation further than the entire fee
stack does. The inherited model handled that by assuming the worst and emitting
one number, which compounds to roughly 65% of reference FMV once condition is
included — pessimistic enough that very little surfaces, and, worse, silent
about *why* it did not.

So each listing gets a pessimistic valuation, in which every unstated field
takes its worst plausible value, and an optimistic one, in which each takes its
best. The pass/fail gate uses the pessimistic figure, so a `PASS` means it is a
deal on the worst reading. A listing that clears the optimistic ceiling but not
the pessimistic one gets its own verdict, `DEPENDS_ON_UNKNOWNS`, naming the
fields it depends on.

This is requirement 4 made concrete: the width of the band *is* the uncertainty,
visible at a glance without arithmetic, and a wide band says "data gap" where a
single number would have said "no". It also makes two of the brief's open
questions measurable rather than arguable — the ratio of `PASS` to
`DEPENDS_ON_UNKNOWNS` says whether the unknown-field defaults are too harsh, and
the band width per reference says where reading photographs would actually pay.

A catalogue entry may also carry `fmv_low`/`fmv_high` directly, which folds
variant ambiguity into the same mechanism. A Christopher Ward C60 whose logo era
is unknown, or a Tissot PRX whose movement is unstated, is a band rather than a
guess.

### A12. There is no service-buffer constant

The inherited model applied both `BRACELET_MULT["AFTERMARKET"]` and a flat
`SERVICE_BUFFER_OEM_BRACELET`, which charges for the same defect twice and
therefore under-bids on every listing with an aftermarket bracelet. The
multiplier does that job. A flat buffer belongs to a *stated* fault, and Phase 1
does not read descriptions closely enough to find one.

The parameter still exists in `fees.sell_side` and renders as a £0.00 line in
the derivation, so the operator can see it was considered rather than forgotten.
