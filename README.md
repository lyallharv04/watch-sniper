# watch sniper

Finds underpriced enthusiast wristwatches on eBay UK, domestic listings only,
values them, and tells you when one is worth ten seconds of your attention.

**It cannot bid and it cannot buy.** It holds no eBay user OAuth token and
implements no flow that could obtain one. That is a property of what is absent
from the code, not a setting.

New here? [STATUS.md](STATUS.md) says what is running and what is not.
[DECISIONS.md](DECISIONS.md) says why every choice is what it is.
[CLAUDE.md](CLAUDE.md) is the standing spec.

---

## What it does

1. Sweeps eBay UK every 90 seconds for Buy It Now listings in the target band,
   and every 10 minutes for auctions.
2. Matches each listing to a reference in [`data/catalogue.toml`](data/catalogue.toml).
3. Values it once — the reference FMV adjusted for condition — and solves for
   the maximum it could pay and still clear the profit floor. Scope and
   bracelet are read from the title and shown as tags; they do not move the
   number.
4. Shows you every listing it saw, with its verdict and the reason, deals and
   rejections alike.
5. Pushes a notification to your phone when something clears every gate, and
   again if its price later drops.
6. Tells you if it has stopped.

Two verdicts:

| | |
|---|---|
| `DEAL` | Clears every gate: priced at or under the maximum allowable bid. |
| `REJECT_*` | Why not. Shown, not hidden — rejections are how the catalogue and the blacklist get debugged. |

---

## Running it

Python 3.11 or later. No other requirement.

```bash
python -m watchsniper diagnose
```

Run this first, always. It makes four real API calls and reports what the live
API actually returns, which is the only way to find out how it differs from
what the code assumes.

```bash
python -m watchsniper seed
```

One pass over the standing stock, so the dashboard is not empty on day one. No
notifications — alerting on several hundred week-old listings is not an alert.

```bash
python -m watchsniper serve
```

Poller and dashboard. Open the URL it prints.

Full deployment on a clean machine: [docs/DEPLOY.md](docs/DEPLOY.md).

### The other commands

| | |
|---|---|
| `catalogue` | The FMV worklist, unverified first, then by how many real listings each entry has priced. |
| `missing` | What the catalogue does not price, grouped and ranked by traffic. |
| `rescore` | Re-score every stored listing under the current constants. Offline, free, under a second. |
| `constants` | Every constant, and whether it has been verified. |
| `notify-test` | Send one test notification and say whether it went. |
| `selftest` | The test suite. |
| `env-example` | Regenerate `.env.example`. |

---

## Setting up notifications

No account and no credential. Two minutes:

1. Install **ntfy** from the Play Store.
2. Invent a long random topic name — `python -c "import secrets;print(secrets.token_hex(12))"`.
3. Subscribe to it in the app.
4. Put it in `.env` as `NTFY_TOPIC`.
5. `python -m watchsniper notify-test`.

The topic name is the address *and* the secret. Anyone who knows it can read
your alerts, so do not paste it anywhere. Nothing confidential travels over it
— the payload is a public eBay listing and a price.

---

## The thing to understand before you trust anything

**Every FMV in the catalogue is an unverified estimate written by an AI, and no
fee rate has been checked against a real invoice.** The dashboard says so on
every page and every alert says so too. That is not humility, it is the actual
state of the data.

The margin here is thin enough that a 15% FMV error consumes the whole profit on
a typical unit. So the two highest-value things you can do, both of which only
you can do, are:

1. **Find one eBay seller invoice and one purchase receipt from a private
   seller.** Between them they settle the final value fee, the regulatory
   operating fee, the per-order fee and the buyer protection schedule — every
   number that scales every maximum bid. It is an hour's work and it is worth
   more than anything else in this list.
2. **Price the top ten rows of `python -m watchsniper catalogue`** against real
   sold data, and set `verified = true`. Then `rescore` and see what your real
   numbers would have done to two weeks of real traffic.

Until those are done the system is an instrument for finding out whether the
plan works, not a system for making money. If after a fortnight the honest read
is that UK domestic flow at this band is too thin to be worth your time, saying
so is a successful outcome. That is what Phase 1 is for.

---

## Layout

```
data/catalogue.toml          every FMV. The critical path. You edit this
data/blacklist.toml          negation-aware phrase rules
data/bid_increments_gb.toml  unverified; only affects auction reachability
src/watchsniper/
  config.py                  the single owner of every constant
  money.py                   integer pence; the rounding policy
  fees.py                    the fee stack and the maximum-bid solver
  valuation.py               gates, the valuation, verdicts
  catalogue.py               loading and matching references
  blacklist.py               negation-aware matching, bid increments
  ebay.py                    Browse API, application token only
  models.py                  the listing shape, and eBay's JSON mapped onto it
  db.py                      SQLite; money is INTEGER pence
  poller.py                  the two sweeps and the deadman switch
  notify.py                  ntfy
  web.py                     the dashboard; holds no arithmetic
  diagnose.py                the live API check
  selftest.py                the tests
tests/corpus.toml            blacklist regression corpus, grown from real traffic
```

## Things that will bite you

**Money is an integer number of pence, everywhere.** Never a float, never a
`Decimal`, never a JSON number. `money.py` is the only module that turns it
into a string. The rounding policy points one way — value down, costs up, bid
ceiling down — so a rounding error can cost an opportunity and cannot cost
money.

**Editing `catalogue.toml` changes behaviour immediately** after a reload. It is
not a seed file that stops mattering.

**Re-scoring is free.** Run it after any change to the catalogue, the blacklist
or a constant. Tests passing is not the same as the funnel working, and every
matcher defect found so far was found by re-scoring real listings, not by the
suite.

**On a network that inspects TLS**, `pip install truststore`. Verification then
goes through the OS trust store. There is deliberately no way to turn
verification off.
