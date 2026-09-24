# Deploying to a clean machine

Written for someone executing it blind. Each step says what you should see, so
you can tell a failure from a success without knowing the system.

Target: a small Linux VPS running continuously. Debian or Ubuntu assumed;
nothing here is distribution-specific beyond `apt`.

Total time: about fifteen minutes, plus however long you spend on step 7.

---

## 1. Python

```bash
python3 --version
```

Expect `Python 3.11` or later. If it is older:

```bash
sudo apt update && sudo apt install -y python3 python3-venv
```

There is nothing to `pip install`. The service uses only the standard library.
(`truststore` is optional and only needed on a network that intercepts TLS —
a VPS does not.)

## 2. The code

```bash
sudo mkdir -p /opt/watchsniper && sudo chown "$USER" /opt/watchsniper
# copy the repository into /opt/watchsniper, then:
cd /opt/watchsniper
ls src/watchsniper/config.py data/catalogue.toml
```

Expect both paths to print. If not, the copy is incomplete.

## 3. Credentials

```bash
cp .env.example .env
nano .env
```

Fill in `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` from
<https://developer.ebay.com/my/keys> — the **Production** keyset, where eBay
labels them "App ID (Client ID)" and "Cert ID (Client Secret)". Sandbox has
almost no UK watch listings and is not useful here.

Every other variable has a working default. Each one in `.env.example` says
where it comes from and what happens if it is missing.

```bash
chmod 600 .env
```

## 4. The live diagnostic — run this before anything else

```bash
cd /opt/watchsniper
PYTHONPATH=src python3 -m watchsniper diagnose
```

This makes four real API calls. Expect six numbered sections and, at the end,
`Findings: 1`.

What each section should show:

| Section | Good | Bad, and what it means |
|---|---|---|
| 1. token | `ok — <N> characters` | Anything else: the keyset is wrong, or it is the Sandbox one. |
| 2. category | a non-zero total, and the filter removing results | `total=0`: the category does not resolve on `EBAY_GB`. Do not proceed — every listing would be rejected as unpriced. |
| 3. field coverage | `50/50` on the seller fields | `0/50` on `sellerAccountType`: the fee branch cannot tell private from business sellers. The system still runs and flags it, but every valuation is less certain. |
| 4. rate limit | "none exposed" is expected and is the one standing finding | — |
| 5. raw listing | one item summary, then the same listing parsed | Parsed fields showing `None` where the raw JSON has a value is a mapping bug. |
| 6. auctions | a non-zero total | Most auctions having no `price` field is **normal and handled**. |

If section 1 fails with a certificate error mentioning *Authority Key
Identifier* or *unable to get local issuer*, something between you and eBay is
intercepting TLS. On a VPS that should not happen and is a real problem — check
the clock first (`timedatectl`). On an office network, `pip install truststore`.

## 5. Seed the database

```bash
PYTHONPATH=src python3 -m watchsniper seed
```

Expect two lines like `bin  seen=800 new=798 calls=4`, then a verdict breakdown.
Roughly half the listings rejecting as `REJECT_CATALOGUE` is expected on a fresh
catalogue and is not a fault — see step 7.

No notifications are sent. That is deliberate.

## 6. Notifications

No account and no credential needed.

1. Install **ntfy** from the Play Store on your Android phone.
2. Generate a topic name:
   ```bash
   python3 -c "import secrets;print(secrets.token_hex(12))"
   ```
3. Subscribe to that topic in the app.
4. Add it to `.env` as `NTFY_TOPIC=<the string>`.
5. ```bash
   PYTHONPATH=src python3 -m watchsniper notify-test
   ```
   Expect `sent — HTTP 200` **and** a notification on your phone. If the POST
   succeeds and nothing arrives, the topic in the app does not match `.env`
   exactly.

The topic name is both the address and the only secret. Anyone who knows it can
read your alerts.

## 7. Curate the catalogue — the part only you can do

```bash
PYTHONPATH=src python3 -m watchsniper catalogue   # what exists, unverified first
PYTHONPATH=src python3 -m watchsniper missing     # what is not priced at all
```

Edit `data/catalogue.toml`, set real numbers, flip `verified = true`, then:

```bash
PYTHONPATH=src python3 -m watchsniper rescore
```

Offline, free, under a second, and it shows what your numbers would have done to
every listing already captured. Do this before you trust a single alert.

## 8. Run it as a service

```bash
sudo cp deploy/watchsniper.service /etc/systemd/system/
sudo sed -i "s/^User=.*/User=$USER/" /etc/systemd/system/watchsniper.service
sudo systemctl daemon-reload
sudo systemctl enable --now watchsniper
sudo systemctl status watchsniper
```

Expect `active (running)`.

```bash
curl -s localhost:8137/api/health
```

Expect JSON with `"stale": false` and a recent `last_successful_poll`. Note that
this endpoint returns HTTP 503 when ingestion is stale — that is intentional, so
an external uptime check can use it.

## 9. Reaching the dashboard

The service binds to `127.0.0.1` and has **no authentication**. Do not change
that by publishing it; pick one of these instead.

**An SSH tunnel** — nothing to install, works immediately:

```bash
ssh -N -L 8137:127.0.0.1:8137 user@your-vps
```

Then open <http://localhost:8137> on your own machine.

**Tailscale** — better if you want it on your phone too:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
tailscale ip -4          # note the 100.x.y.z address
```

Set `BIND_HOST` to that address in `.env` and restart. The dashboard is then
reachable at `http://100.x.y.z:8137` from any device on your tailnet and from
nowhere else.

Do not set `BIND_HOST=0.0.0.0`. The service prints a warning if you do, because
that publishes an unauthenticated dashboard to the internet.

## 10. Backups

The entire state is one file.

```bash
sqlite3 /opt/watchsniper/watchsniper.db ".backup /tmp/watchsniper-$(date +%F).db"
```

Copy it somewhere else. `data/*.toml` and `.env` are the only other things worth
keeping, and `.env` should not leave the machine.

---

## When something goes wrong

| Symptom | Look at |
|---|---|
| No alerts for days | `/health`. If `stale` is false and polls are succeeding, this is a real finding about UK deal flow, not a fault. |
| Every listing rejected as unpriced | `python -m watchsniper missing`. The catalogue does not cover what is actually being listed. |
| Everything passing | A fee constant or a multiplier is wrong in the profitable direction, which is the direction that loses money. Check `/constants`. |
| Ingestion stalled | `journalctl -u watchsniper -n 100`. The watchdog should already have told you. |
| Certificate errors | Step 4. |
| Dashboard shows a figure you disagree with | Open the item page. Every line of the arithmetic is there. |
