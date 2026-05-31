# Setup & Operations

Machine setup, private (Tailscale) access, and Render deployment detail for Brick
Scanner. (Moved out of `CLAUDE.md` to keep that file lean.) For the day-to-day
dev workflow and the "push to main" deploy trigger, see `CLAUDE.md`. For
historical change notes, see `CHANGELOG.md`.

---

## Setting Up on a New Machine

Full rebuild from a clean macOS install. Three things are **not** in git and must
be handled explicitly: the **`.env` secrets**, the **offline catalog** (rebuildable),
and the **launchd agents** (installed per-machine).

> **⚡ Easy path — Apple Migration Assistant:** If you migrate your home folder to
> the new Mac and keep the **same username**, everything comes across as-is —
> `.env`, `brick_parts.db`, and the installed `~/Library/LaunchAgents/*.plist`
> agents — and just works. The steps below are for a **clean install + fresh
> `git clone`**, where those three are absent.

**1. Prerequisites**
```bash
xcode-select --install                              # provides /usr/bin/python3 (used by app + agents)
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # Homebrew
```

**2. Clone + Python deps**
```bash
git clone https://github.com/itsjef292/brick-scanner.git "Brick Scanner" && cd "Brick Scanner"
pip3 install -r requirements.txt
# (requirements.txt pins are for Render/Docker; locally the unpinned set also works:
#  pip3 install flask requests python-dotenv requests-oauthlib)
```

**3. 🔴 API credentials — the one thing nothing in git can give you.** The real
values live only in the old machine's `.env` (git-ignored, by design) and in the
Render dashboard. Either copy the old `.env` over, or regenerate all six:
```bash
cp .env.example .env    # then fill in real values (see "Required environment variables" below)
```
Get them from Rebrickable (account → Settings → API) and BrickLink (My Account →
API → Access Tokens). **Note:** the same six values are also set in the Render
dashboard (Environment tab) for production. *If regenerating, update Render too.*

**4. Build the offline catalog** (~330 MB; optional — the app degrades gracefully
without it, falling back to the live Rebrickable API):
```bash
python3 download_csvs.py     # → "Brick Parts/"  (public CDN, no auth)
python3 build_brick_db.py    # → brick_parts.db  (~9s build)
```

**5. Tailscale** (private phone access):
```bash
brew install --cask tailscale   # launch it, sign into the SAME account as your phone
# Then serve over HTTPS so the phone's camera/mic work (CLI isn't on PATH):
/Applications/Tailscale.app/Contents/MacOS/Tailscale serve --bg 5001
```
The MagicDNS hostname differs per machine; `./start.sh` prints the raw `http://…:5001`
URL, and `… Tailscale serve status` prints the **HTTPS** URL (needed for live camera +
mic). See **Private Access (Tailscale + autostart)** below.

**6. Install the launchd agents** (autostart + daily refresh). The committed
`.plist` files are templates with `__PROJECT_DIR__`; the installer fills in this
machine's real path, so username/location don't matter:
```bash
./install_agents.sh    # substitutes paths, copies to ~/Library/LaunchAgents, loads both agents
```
This starts the always-on Flask server (`com.brickscanner.app`) and schedules the
07:30-local catalog refresh (`com.brickscanner.catalog-refresh`). Re-running it is
safe. (To run in the foreground instead, `launchctl stop com.brickscanner.app`
first, then `./start.sh` — both bind :5001.)

**7. Verify**
```bash
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5001/   # expect 200
launchctl list | grep brickscanner                                # both agents present
```

**Required environment variables in `.env`:**
- `REBRICKABLE_API_KEY` — From Rebrickable account → Settings → API
- `REBRICKABLE_USER_TOKEN` — User token from Rebrickable
- `BL_CONSUMER_KEY`, `BL_CONSUMER_SECRET`, `BL_TOKEN`, `BL_TOKEN_SECRET` — BrickLink OAuth1 credentials (for minifig pricing)

---

## Private Access (Tailscale + autostart) — local-only

Instead of (or alongside) Render, the local instance is reachable privately
from a phone over **Tailscale** — no public exposure, no ngrok, no port
forwarding. The app binds `0.0.0.0:5001`, so it's available on the tailnet
interface; traffic is WireGuard-encrypted and limited to devices signed into
the same tailnet (`itsjeff292@`). Plain `http://…:5001` works for everything
*except* the browser-camera features — **Live camera auto-scan** and the "Add by
voice" mic both need a **secure context** (`getUserMedia`/Web Speech are blocked
over plain HTTP), so use the **HTTPS** URL below on the phone to get those. The
"Take Photo" capture flow still works over plain HTTP.

- **Reach it from the phone:** install the Tailscale app, sign into the same
  account, then open the **HTTPS** URL (live camera + mic) — currently
  **`https://jefs-macbook-pro.tailbdd458.ts.net`** — or `http://…:5001` (raw, no
  camera/mic). `start.sh` auto-detects and prints the raw URL.
- **HTTPS via Tailscale Serve (recommended — enables live camera + mic):**
  `tailscale serve --bg 5001` proxies the tailnet host's :443 → local :5001 with a
  real (Let's Encrypt) cert, giving `https://<host>.<tailnet>.ts.net`. This is a
  **secure context**, so the live viewfinder and mic work on the phone. `--bg`
  persists across reboots; tailnet-only (not public). Requires HTTPS certs enabled
  once in the tailnet admin console (Settings → **HTTPS Certificates → Enable**).
  Verify with `tailscale serve status` (shows the URL + proxy target); disable with
  `tailscale serve --https=443 off`.
- **Tailscale install (macOS):** `brew install --cask tailscale` (the GUI app
  auto-starts at login and stays connected). **The CLI is not on `PATH`** with the
  GUI app — it lives at `/Applications/Tailscale.app/Contents/MacOS/Tailscale`, so
  run that full path (e.g. `…/Tailscale serve --bg 5001`) or add
  `alias tailscale='/Applications/Tailscale.app/Contents/MacOS/Tailscale'` to
  `~/.zshrc`. (`… status` / `… ip -4` for tailnet info.)

**Autostart agent (`com.brickscanner.app.plist`):** a launchd LaunchAgent that
keeps the Flask server up so the app is always reachable while the Mac is logged
in. `RunAtLoad` starts it at login; `KeepAlive` restarts it on crash/exit.
`WorkingDirectory` is the project dir (so `load_dotenv()` finds `.env`); runs
`/usr/bin/python3 app.py`; logs to `app.log` (git-ignored). Install/stop/uninstall
instructions are in the plist header (`launchctl load|stop|start|unload …`).

> **Caveats:** (1) LaunchAgents start at *login*, not pre-login boot — for
> unattended uptime after a reboot, enable automatic login and prevent sleep.
> (2) The agent and `start.sh` both bind `:5001` — don't run both; `launchctl
> stop com.brickscanner.app` before a foreground `start.sh`. (3) This is
> local-only (like the catalog-refresh agent); Render is unaffected.

---

## Render Deployment Detail

**Two independent instances:**
1. **Local Development** — `http://127.0.0.1:5001` (on your Mac). Run with
   `python3 app.py`; auto-reloads on code changes (Flask debug mode).
2. **Cloud Production** — `https://brick-scanner.onrender.com` (Render.com).
   Public URL; auto-deploys on push to GitHub; ~$5-50/month depending on usage.

**Render Stack:**
- `Dockerfile` for containerization
- `requirements.txt` with dependencies
- `gunicorn` WSGI server (production-grade)
- Environment variables set in Render console
- Auto-redeploy on GitHub push via webhook

**Files involved in deployment:**
- `Dockerfile` — Container config
- `requirements.txt` — Python dependencies
- `.dockerignore` — Excludes unnecessary files from build
- `render.yaml` — Render-specific configuration
- `DEPLOY.md` — (older Google Cloud Run notes; current prod is Render)

### API Key Management

**Local (.env file — git-ignored, never commit real values):**
```
REBRICKABLE_API_KEY=<your-rebrickable-api-key>
REBRICKABLE_USER_TOKEN=<your-rebrickable-user-token>
BL_CONSUMER_KEY=<your-bricklink-consumer-key>
BL_CONSUMER_SECRET=<your-bricklink-consumer-secret>
BL_TOKEN=<your-bricklink-token>
BL_TOKEN_SECRET=<your-bricklink-token-secret>
```

**Cloud (Render environment variables):**
- Same 6 variables set in the Render dashboard (Environment tab)
- `render.yaml` declares them with `sync: false` so the blueprint never stores or exposes the values
- Never committed to git (for security). NOTE: earlier revisions committed real keys in `render.yaml`/`CLAUDE.md` — those values are in git history and must be rotated.

### Cost Estimation

**Render.com pricing (as of May 2026):**
- 50,000 scans/month: ~$5-10
- 500,000 scans/month: ~$50-100
- 5,000,000 scans/month: ~$500-1,000

Free tier covers small hobby usage.
