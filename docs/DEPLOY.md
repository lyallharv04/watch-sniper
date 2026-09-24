# Deploying to a clean machine

Written for someone executing it blind. Each step says what you should see, so
you can tell a failure from a success without knowing the system.

Target: an Ubuntu server (22.04 or 24.04) running continuously, reached from
your phone through a Cloudflare Tunnel with Cloudflare Access in front of it.

```
phone ──https──► Cloudflare Access (login) ──► Cloudflare Tunnel
                                                   │ outbound only
                                     cloudflared service on the server
                                                   │
                                     http://127.0.0.1:8137  watchsniper service
```

The service itself has **no login**. Access is the only thing between the
dashboard and the internet, so the order below matters: the Access application
is created *before* the public hostname exists, and the service never listens
on anything but loopback. No inbound port is opened on the server.

Total time: about half an hour, plus however long you spend on step 11.

You need: SSH access to the server with `sudo`, a domain on Cloudflare, and a
Cloudflare Zero Trust organisation (the free plan is enough).

---

## 1. Packages

```bash
sudo apt update
sudo apt install -y python3 sqlite3 git
python3 --version
```

Expect `Python 3.11` or later (Ubuntu 22.04 ships 3.10 — if you see that,
`sudo apt install -y python3.11` and use `python3.11` wherever this guide says
`python3`, including `ExecStart` in the unit file).

There is nothing to `pip install`. The service uses only the standard library.

## 2. A user for the service

```bash
sudo adduser --system --group --home /opt/watchsniper --shell /usr/sbin/nologin watchsniper
```

Expect `Adding system user 'watchsniper'`. The account cannot log in; it only
runs the service and owns its files.

## 3. The code

```bash
sudo git clone <your repository URL> /opt/watchsniper
sudo chown -R watchsniper:watchsniper /opt/watchsniper
ls /opt/watchsniper/src/watchsniper/config.py /opt/watchsniper/data/catalogue.toml
```

Expect both paths to print. If you copied the files another way (`rsync`,
`scp`), the `chown` is still needed — the service writes its database into
`/opt/watchsniper`.

Every command below that runs the application does so as `watchsniper`, from
`/opt/watchsniper`:

```bash
cd /opt/watchsniper
alias ws='sudo -u watchsniper env PYTHONPATH=src python3 -m watchsniper'
```

## 4. Credentials

```bash
sudo -u watchsniper cp .env.example .env
sudo -u watchsniper chmod 600 .env
sudo -u watchsniper nano .env
```

Fill in `EBAY_CLIENT_ID` and `EBAY_CLIENT_SECRET` from
<https://developer.ebay.com/my/keys> — the **Production** keyset, where eBay
labels them "App ID (Client ID)" and "Cert ID (Client Secret)". Sandbox has
almost no UK watch listings and is not useful here.

Leave `BIND_HOST` at `127.0.0.1`. The unit file pins it there regardless.

Set `PUBLIC_BASE_URL` to the hostname you will give the tunnel in steps 9 and
10, with `https://` and no trailing slash, e.g.
`PUBLIC_BASE_URL=https://watches.example.com`. It is what the dashboard links
in phone alerts point at. Without it they point at `http://localhost:8137`,
which opens nothing on a phone. If you change it later, restart the service.

Every other variable has a working default. Each one in `.env.example` says
where it comes from and what happens if it is missing.

## 5. The live diagnostic — run this before anything else

```bash
ws diagnose
```

This makes four real API calls. Expect six numbered sections and, at the end,
`Findings: 1`.

| Section | Good | Bad, and what it means |
|---|---|---|
| 1. token | `ok — <N> characters` | Anything else: the keyset is wrong, or it is the Sandbox one. |
| 2. category | a non-zero total, and the filter removing results | `total=0`: the category does not resolve on `EBAY_GB`. Do not proceed — every listing would be rejected as unpriced. |
| 3. field coverage | `50/50` on the seller fields | `0/50` on `sellerAccountType`: the fee branch cannot tell private from business sellers. The system still runs and flags it. |
| 4. rate limit | "none exposed" is expected and is the one standing finding | — |
| 5. raw listing | one item summary, then the same listing parsed | Parsed fields showing `None` where the raw JSON has a value is a mapping bug. |
| 6. auctions | a non-zero total | Most auctions having no `price` field is **normal and handled**. |

A certificate error in section 1 on a server usually means a wrong clock —
check `timedatectl`.

## 6. Seed the database

```bash
ws seed
```

Expect two lines like `bin  seen=800 new=798 calls=4`, then a verdict
breakdown. Roughly half the listings rejecting as `REJECT_CATALOGUE` is
expected on a fresh catalogue — see step 11. No notifications are sent.

## 7. Notifications

No account and no credential needed.

1. Install **ntfy** from the Play Store on your Android phone.
2. Generate a topic name:
   ```bash
   python3 -c "import secrets;print(secrets.token_hex(12))"
   ```
3. Subscribe to that topic in the app.
4. Add it to `.env` as `NTFY_TOPIC=<the string>`.
5. ```bash
   ws notify-test
   ```
   Expect `sent — HTTP 200` **and** a notification on your phone. If the POST
   succeeds and nothing arrives, the topic in the app does not match `.env`
   exactly.

The topic name is both the address and the only secret. Anyone who knows it can
read your alerts.

## 8. Run it as a service

```bash
sudo cp deploy/watchsniper.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watchsniper
systemctl status watchsniper --no-pager
curl -s http://127.0.0.1:8137/api/health
```

Expect `active (running)`, then JSON with `"stale": false`. The first poll
happens within a couple of minutes; until then `last_successful_poll` may be
from the seed.

The unit runs as `watchsniper`, can write only `/opt/watchsniper`, and sets
`BIND_HOST=127.0.0.1` in its environment, which overrides `.env`.

## 9. Cloudflare Access — before the dashboard has a hostname

In the Cloudflare dashboard, open **Zero Trust**.

1. **Access → Applications → Add an application → Self-hosted.**
2. Application name: `watch sniper`. Session duration: something long, such as
   `1 month` — the installed app on your phone otherwise asks you to log in
   again every day.
3. Application domain: the hostname you will use in step 10, e.g.
   `watches.example.com`. Leave the path empty so the whole site is covered,
   including `/manifest.json`, `/sw.js` and the icons.
4. Identity provider: **One-time PIN** is enough (a code sent to your email).
5. Add a policy: name `operator`, action **Allow**, include **Emails** →
   your own address. Nothing else.
6. Save.

Nothing is reachable yet — there is no DNS record for the hostname.

## 10. Cloudflare Tunnel — token-based, as its own service

**Create the tunnel** in Zero Trust: **Networks → Tunnels → Create a tunnel →
Cloudflared**, name it `watchsniper`, and save. The next page shows an install
command containing a long token after `service install`. Copy only the token.

**Install cloudflared** from Cloudflare's apt repository:

```bash
sudo mkdir -p --mode=0755 /usr/share/keyrings
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo 'deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] https://pkg.cloudflare.com/cloudflared any main' \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt update && sudo apt install -y cloudflared
```

**Install it as a service with the token:**

```bash
sudo cloudflared service install <TOKEN>
systemctl status cloudflared --no-pager
```

Expect `active (running)`, and the tunnel showing **Healthy** in the Zero Trust
dashboard within a minute. The token is stored in the `cloudflared` unit; it is
not part of this repository and does not go in `.env`.

**Route the hostname to the service.** Back in the tunnel's configuration:
**Public Hostname → Add a public hostname**:

| Field | Value |
|---|---|
| Subdomain / Domain | the hostname from step 9, e.g. `watches` / `example.com` |
| Type | `HTTP` |
| URL | `localhost:8137` |

Save. Cloudflare creates the DNS record.

**Check the lock is on** from a private browser window: open
`https://watches.example.com`. Expect the Cloudflare Access login page, not the
dashboard. If you see the dashboard without logging in, stop and fix the Access
application's domain before going further.

No firewall change is needed for any of this: cloudflared connects outbound.
If you use `ufw`, allow SSH and nothing else:

```bash
sudo ufw allow OpenSSH && sudo ufw enable
```

## 11. Curate the catalogue — the part only you can do

```bash
ws catalogue   # what exists, unverified first
ws missing     # what is not priced at all
```

Edit `data/catalogue.toml` (as `watchsniper`), set real numbers, flip
`verified = true`, then:

```bash
ws rescore
```

Offline, free, under a second, and it shows what your numbers would have done to
every listing already captured. Do this before you trust a single alert.

## 12. Install it on your phone

1. Open the hostname in Chrome on Android and complete the Access login.
2. Chrome's menu → **Install app** (or **Add to Home screen**).
3. Open it from the home screen. It runs full-screen, like an app.

The app caches nothing; every screen is fetched live. When the Access session
expires it shows the Access login again.

If **Install app** does not appear, open the site on a desktop Chrome, then
DevTools → Application → Manifest. An error there fetching the manifest or an
icon means Access is answering that request with its login page; check that
the Access application's domain covers the whole hostname with no path.

## 13. Backups

The entire state is one file.

```bash
sudo -u watchsniper sqlite3 /opt/watchsniper/watchsniper.db \
  ".backup /opt/watchsniper/backup-$(date +%F).db"
```

Copy it somewhere else. `data/*.toml` and `.env` are the only other things worth
keeping, and `.env` should not leave the machine.

## 14. Updating

```bash
cd /opt/watchsniper
sudo -u watchsniper git pull
sudo systemctl restart watchsniper
```

A schema change is applied on start. Verdicts are derived data: if their shape
changed they are rebuilt by re-scoring, automatically.

## 15. Final check — from outside, on mobile data

On your phone, turn Wi-Fi **off** so the request comes from the mobile
network, not from anywhere near the server. Open a private (incognito) tab
and go to the hostname, e.g. `https://watches.example.com`.

Expect the **Cloudflare Access login page first** — the email one-time-PIN
prompt — and the dashboard only after you have logged in.

If the dashboard, or any part of it, appears before the Access login, stop:
the dashboard is exposed. Take the public hostname off the tunnel
(**Networks → Tunnels → watchsniper → Public Hostname → delete**) until the
Access application's domain matches the hostname exactly, then repeat this
step.

---

## When something goes wrong

| Symptom | Look at |
|---|---|
| Hostname shows Cloudflare error 502 or 1033 | The tunnel is up but the service is not answering: `systemctl status watchsniper`, then `curl -s http://127.0.0.1:8137/api/health` on the server. 1033 alone means cloudflared itself is down: `systemctl status cloudflared`. |
| Dashboard reachable without logging in | The Access application's domain does not match the public hostname. Fix it before anything else. |
| Login loop on the phone | Clear the site's cookies in Chrome, or lengthen the Access session duration. |
| No alerts for days | `/health`. If `stale` is false and polls are succeeding, this is a real finding about UK deal flow, not a fault. |
| Every listing rejected as unpriced | `ws missing`. The catalogue does not cover what is actually being listed. |
| Everything passing | A fee constant is wrong in the profitable direction, which is the direction that loses money. Check `/constants`. |
| Ingestion stalled | `journalctl -u watchsniper -n 100`. The watchdog should already have told you. |
| Dashboard shows a figure you disagree with | Open the item page. Every line of the arithmetic is there. |

`/api/health` returns HTTP 503 when ingestion is stale. It sits behind Access
like every other path, so an external uptime monitor needs an Access service
token to reach it; on the server itself `curl` to `127.0.0.1` always works.
