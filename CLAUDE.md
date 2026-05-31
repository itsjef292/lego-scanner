# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Maintaining this file — keep it lean.** CLAUDE.md is reloaded into context on
> *every* turn, so size directly costs tokens and speed (the harness warns at 40k
> chars; a hook warns at 32k). Do **not** append changelog/"Recent Changes" entries
> here — those go in **`CHANGELOG.md`**. Machine setup, Tailscale, and Render
> deploy/keys detail go in **`SETUP.md`**. This file should hold only durable
> guidance Claude needs to work in the repo (architecture, commands, patterns,
> conventions). When something here goes stale, edit it in place rather than adding
> a new section.

## Project Overview

**Brick Scanner** is a mobile-friendly web app that identifies LEGO parts from phone camera photos. The app:
1. Captures photos on iOS/Android
2. Sends images to Brickognize API for part/minifig detection
3. Performs server-side color detection via canvas pixel sampling (LAB color space)
4. Displays identified parts and allows users to add them to Rebrickable inventory
5. Supports price lookups via BrickLink API

**Tech stack:** Flask (Python 3) backend + vanilla JavaScript frontend (no frameworks)

---

## Commands

### Running the App

```bash
# Basic development server (localhost:5001)
python3 app.py

# Foreground run that prints the private Tailscale URL (see start.sh).
# Stop the autostart agent first if it's loaded — both bind :5001.
./start.sh
```

> The app is normally kept running by a launchd autostart agent and reached
> privately over Tailscale — see **Private Access (Tailscale + autostart)** under
> Deployment below. `start.sh` is now for foreground/manual runs; the ngrok tunnel
> line was removed from it (the static ngrok domain config is left intact for
> optional future use).

### Dependencies

```bash
# Install required packages
pip3 install flask requests python-dotenv requests-oauthlib
```

### Offline Catalog (local search)

```bash
# Build the local SQLite catalog from the Rebrickable CSV dump in "Brick Parts/"
python3 build_brick_db.py        # produces ./brick_parts.db (~195 MB)

# Download the CSV dump from Rebrickable's public CDN (used on deploy + refresh)
python3 download_csvs.py         # → "Brick Parts/" (only the 9 tables the catalog uses)

# Daily auto-refresh: HEAD-check for changes, rebuild + atomically swap if changed
python3 refresh_catalog.py [--force]
```

> **⚠️ LOCAL-ONLY FEATURE — catalog refresh, change tracking, and the daily job
> do not run on Render (by design).** Render rebuilds `brick_parts.db` from scratch
> on every deploy onto an ephemeral filesystem, so there is no persisted prior
> catalog to diff against and no non-deploy rebuild trigger. All of this is gated
> off in production: `IS_RENDER` (the `RENDER` env var) makes `can_refresh` false →
> the scan-screen footer is hidden and `POST /api/catalog/refresh` returns 403. On
> Render the catalog is simply whatever was current at the last deploy. Making it
> work there would require persisting a prior catalog index (R2/S3 or a persistent
> disk) + a Render Cron Job — intentionally not done.

**Manual refresh button (scan screen, local only):** A footer at the bottom of `#screen-scan`
shows "Offline catalog — Updated <date> · <size>" and a "Check for updates" button.
Backend: `GET /api/catalog/status` (freshness + `can_refresh` + `last_changes`) and
`POST /api/catalog/refresh` (runs `refresh_catalog.run()` in a daemon thread; the
frontend polls status every 2s).

**Change tracking:** Each rebuild diffs the old vs new catalog (`refresh_catalog._diff_catalog`,
run before the atomic swap while both DBs exist) and writes the result to
`.catalog_changes.json` (capped at `CHANGES_CAP`=500/category). It records, per category
(`part_num`/`fig_num`/`set_num`): **added**, **removed**, and **renamed** (name changed for
the same number); plus **set-content changes** — sets whose inventory composition changed,
detected via a cheap per-set signature `(distinct part/color lines, total qty)` from
`inventories`⋈`inventory_parts` (`_set_signatures`). This is what Rebrickable's frequent
`inventories`-table updates actually represent. The record **always includes the list of
updated `tables`**, and is written on every refresh that had a prior catalog to diff — even
when no items/contents changed — so the UI can still show *which* tables updated (e.g.
`inventories`, `themes`). The scan-screen footer renders this as a collapsible list
(`#catalogChanges` / `_renderChanges()`): a summary line ("May 29 update — +2/−1/~3 sets,
5 sets changed") that expands to an "Updated tables" line plus grouped SETS/FIGS/PARTS with
green `+` / red `−` / blue `~` rows and a "Set contents changed (N)" group. Hidden only when
there are no changes *and* no table info. The footer is hidden where `can_refresh` is false —
i.e. on Render (`IS_RENDER`, detected via the `RENDER` env var), where refresh is
disabled (returns 403) since the filesystem is ephemeral.

**Daily refresh automation (local dev):** `refresh_catalog.py` checks Rebrickable's
CDN via cheap HEAD requests (ETag/Last-Modified vs `.catalog_manifest.json`); if any
table changed it re-downloads the full dump, rebuilds into `brick_parts.db.new`, and
`os.replace`s it in. The dev server opens a fresh SQLite connection per request, so it
picks up the new DB with **no restart** (zero-downtime swap). Scheduled daily at 04:30
via a launchd LaunchAgent (`com.brickscanner.catalog-refresh.plist` → `refresh_catalog.sh`);
install/uninstall instructions are in the plist header. Logs: `catalog_refresh.log`
(clean, timestamped) and `catalog_refresh.launchd.log` (raw stdout/stderr). All refresh
artifacts are git-ignored.

The `Brick Parts/` folder (Rebrickable CSV bulk download) and the generated
`brick_parts.db` are **git-ignored** (local dev only). The app degrades
gracefully when `brick_parts.db` is absent — offline search returns a
"not available" notice and everything else works unchanged, so production
(which has no DB) is unaffected.

### Environment Setup

```bash
# Copy template and fill in API credentials
cp .env.example .env
```

**Required environment variables in `.env`:**
- `REBRICKABLE_API_KEY` — From Rebrickable account → Settings → API
- `REBRICKABLE_USER_TOKEN` — User token from Rebrickable
- `BL_CONSUMER_KEY`, `BL_CONSUMER_SECRET`, `BL_TOKEN`, `BL_TOKEN_SECRET` — BrickLink OAuth1 credentials (for minifig pricing)

---

## Setting Up on a New Machine

See **`SETUP.md`** for the full clean-install walkthrough (`.env` secrets, offline
catalog rebuild, launchd agents via `./install_agents.sh`, and the Migration
Assistant shortcut).

---

## Architecture

### Backend (app.py)

Flask server with 10+ endpoints:

**Parts Management:**
- `GET /api/partlists` — Fetch user's parts lists
- `POST /api/partlists` — Create new parts list
- `DELETE /api/partlists/<id>` — Delete list
- `GET /api/partlists/<id>/parts` — Get parts in a list (paginated, with color-specific images)
- `GET /api/partlists/<id>/parts_all` — Flat, lightweight dump of the **entire** list (throttled paging, no per-part image fan-out) for the Lists-screen **live search**. Color-specific images are overlaid from the local `part_colors` table (`_local_part_color_imgs`, ~94% coverage, zero API calls), falling back to the generic part image; graceful when the DB is absent.
- `GET /api/partlists/<id>/parts/<part_num>/<color_id>` — Check if specific part/color exists in list
- `GET /api/part_in_lists/<part_num>/<color_id>` — Find all lists containing a specific part/color with quantities
- `POST /api/add_part` — Add/update part in list (merges quantities if exists)
- `POST /api/remove_part_one` — Decrement part quantity by 1 (delete if qty becomes 0)
- `GET /api/partlists/<id>/bricklink_wanted` — Export a parts list as BrickLink Wanted List XML (part_num→BrickLink id via `bl_aliases`, color→BrickLink color via `bl_colors`)

**Minifig info:**
- `GET /api/minifig_sets/<set_num>` — Get sets containing a minifig
- `GET /api/minifig_price/<fig_id>` — BrickLink last-6-months sold price, Used + New (OAuth1; via `_bl_sold_price`) + theme category
- `GET /api/set_price/<set_num>` — BrickLink last-6-months sold price for a set, Used + New (`_bl_sold_price("SET", …)`; bare set numbers default to `-1`)
- `GET/POST /api/minifiglists` — legacy synthetic single-list shim (Rebrickable has no minifig lists); largely vestigial now that the collection is local.

**Owned Sets ("My Sets" — the user's Rebrickable set collection at `/users/{token}/sets/`):**
- `GET /api/owned_sets` — list every owned set (set_num, name, year, num_parts, img, quantity, condition, price_paid)
- `GET /api/owned_sets/<set_num>` — `{owned, quantity, condition, price_paid}` for one set
- `POST /api/add_set` — add a set (merges quantity if already owned)
- `POST /api/remove_set_one` — decrement an owned set by 1 (deletes the entry + its metadata at 0)
- `POST /api/owned_sets/<set_num>/meta` — save purchase **condition** (`used`/`new`) + **price_paid**. Stored in local `.set_meta.json` (Rebrickable's set collection only holds quantity). LOCAL-ONLY — empty on Render.

**Owned Minifigs ("My Minifigs") — a fully LOCAL collection:**
Rebrickable's `/users/{token}/minifigs/` is **read-only** (GET-only, no per-item route; it just aggregates minifigs from owned sets — POST returns 405), so there's no server-side owned-minifig list. The entire collection lives in local `.minifig_collection.json` keyed by fig_num (`{quantity, condition, price_paid, name, img_url}`). LOCAL-ONLY — empty on Render.
- `GET /api/owned_minifigs` — name-sorted list (fig_num, name, num_parts, img, quantity, condition, price_paid)
- `GET /api/owned_minifigs/<fig_num>` — `{owned, quantity, condition, price_paid}`
- `POST /api/add_minifig` — add (merges quantity; body carries name/img_url for offline-friendly display)
- `POST /api/remove_minifig_one` — decrement by 1 (deletes the entry at 0)
- `POST /api/owned_minifigs/<fig_num>/meta` — save condition + price_paid (no-op if not owned)
- Shared JSON helpers: `_load_meta` / `_save_meta` / `_clean_meta` (used by both set + minifig stores).

**Offline Catalog Search:**
- `GET /api/local/search?q=&type=parts|minifigs|sets&limit=` — Search by name or catalog number. Prefers the local SQLite catalog (`brick_parts.db`, no Rebrickable quota); **falls back to the live Rebrickable API when the DB is absent** (e.g. production). Response includes `"source": "offline" | "api"` so the UI can badge the data source. **BrickLink minifig ids** (e.g. `sw0131`): Rebrickable exposes no BrickLink minifig ids, so when a minifig query matches a BrickLink-id pattern and has no local hit, the id is translated to a name via the BrickLink API (`_bricklink_minifig_name`) and the best-matching Rebrickable figs are returned as **candidates** (`_local_minifig_search_by_name`, ranked by word overlap) along with a `"bl_match": {id, name}` field — the user picks the right one (names diverge between catalogs, so it's deliberately not a single auto-pick).

**Core Identification:**
- `POST /api/identify` — Submit photo to Brickognize API, return detected parts with color candidates
- `GET /api/colors` — Fetch all LEGO colors (paginated, cached)
- `GET /api/part/<part_num>` — Get part metadata from Rebrickable
- `GET /api/part_colors/<part_num>` — Get available colors for a part

**Key flow:** Photo → Brickognize detection → BrickLink↔Rebrickable ID mapping → Canvas color sampling → Return candidates

### Frontend (templates/index.html)

Single-page app with 5 screens:
1. **Scan** — Camera input, list selector, create list UI
2. **Loading** — Spinner while Brickognize processes
3. **Identify** — Part details, color picker, price/sets (if minifig), alternatives, quantity controls, add button
4. **Lists** — Browse and manage part inventory
5. **Success** — Confirmation after adding

**Modes:** Parts vs. Minifigs (affects list endpoints and UI text)

**Key features:**
- **Live camera auto-scan** — Hands-free viewfinder (`#liveVideo`) that captures a frame every ~1.5s → `/api/identify`, presenting the first hit with top `score ≥ 0.55` (`startLiveScan`/`liveTick`/`syncLiveScan`, hooked into `goTo`/`switchMode`/`load`/`visibilitychange`). **Needs a secure context** (HTTPS/localhost) — falls back to the "Take Photo" flow over plain HTTP (e.g. phone on the tailnet without `tailscale serve`). Persisted toggle `localStorage 'liveScan'`.
- **Canvas color detection** — Samples pixels from bounding box (or center 40% fallback) in LAB color space for perceptual matching
- **EXIF rotation handling** — Converts portrait camera images (EXIF orientation 6) to landscape raw coordinates for bbox alignment
- **Color matching logic** — Prioritizes hue distance for chromatic colors, LAB distance for achromatic; penalizes Trans-, Glow-in-Dark, Satin colors unless explicitly detected. Selection depends on how many colors Brickognize predicts: **exactly one → trust it outright** (Brickognize is confident; pixel sampling can mislead — a well-lit Dark Green reads close to Green); **multiple/none → match the sampled pixel against the part's full palette** (every color it comes in), with the predicted colors applied as a **prior** (`findClosestLegoColor(..., preferredIds, trustShortlist)`: shortlist members get a −15 bonus) rather than a hard restriction — Brickognize's shortlist sometimes omits the true color (e.g. Dark Azure), so restricting to it caused wrong picks. The `preferred` single-id −30 bonus is still used by the alternatives re-match path.
- **Quantity management** — Resets to 1 on each scan to prevent user error; merges with existing inventory on add
- **Inventory status checking** — Real-time lookup when selecting a color to show if part/color combo is already in selected list (with quick remove button)
- **Cross-list inventory** — "Found in" section shows all lists containing the part with quick +/− buttons to adjust across lists
- **Smart list selection** — Optional default list on scan screen; list picker modal only appears if no default selected
- **Inline list creation** — Create new lists directly from picker modals without navigation
- **Expandable parts** — Minifigure parts section is collapsible to reduce visual clutter
- **List management UI** — Add/remove buttons in list view for quick quantity adjustments
- **Swipe-left to remove one** — Parts-list, My Sets, and My Minifigs rows reveal a red "Remove 1" button on left-swipe (iOS-Mail style). Reusable `makeSwipeRemovable(rowEl, onRemove, label)` + `consumeSwipe(rowEl)` (swallows the tap on swiped/open rows that also have a tap action). Parts share `mutateListPart()` with the +/− buttons.
- **Swipe-from-left-edge to go back** — iOS-style back gesture (`backSwipe` IIFE, `BACK_TARGETS`): right-swipe from `<28px` on detail screens → parent (identify/success → scan, set-details → My Sets). Excluded on the top-level tab screens (no conflict with the opposite-direction swipe-to-delete); guarded by `_overlayOpen()`; `goTo` clears residual transforms.
- **Pull-to-refresh** — pull down at `scrollTop 0` on browse screens to reload (`pullToRefresh` IIFE + `_ptrRefreshFn` map: Lists/Sets/Cart/Set-details/My-Minifigs). Vertical-locked (no clash with the back swipe), `_overlayOpen()`-guarded; `#ptrIndicator` spinner + `overscroll-behavior-y: contain`. The list loaders **return their fetch promise**, so the spinner runs until the fetch actually resolves (400ms floor).
- **Auto-refresh on resume** — `refreshActiveScreenData()` (reusing `_ptrRefreshFn`) re-fetches the active screen's data on `visibilitychange`→visible (after >2s hidden) + `pageshow`, fixing the iOS PWA "stale until force-close" resume behavior.
- **Gated auto-reload on new version** — server injects a content-hash app version (`_app_version` → `/api/version` + `<meta name="app-version">`); on resume `checkForUpdate()` compares and reloads to pick up new app code, but only when `_safeToReload()` (not mid scan/identify/loading/success, no modal, not live-scanning) — else defers via `_updatePending` until the next `goTo` to a safe screen. So the installed PWA self-updates without interrupting a scan.
- **List live search** — A search box in the Lists view filters the whole list **as you type** (part #, name, or colour) with an `N of M parts` count. The full list is loaded once into memory via `/api/partlists/<id>/parts_all` (`_listAllParts`, `renderListParts`/`filterListParts`); replaces the old Load-More pagination. The `+/−` steppers keep the in-memory list + count in sync.
- **Voice quick-add mode** — A persisted (localStorage) toggle in the "Add by voice" modal that adds spoken parts **straight to the selected list with no confirm card** (re-arms the mic for rapid entry); falls back to the confirm card when no list is selected or no colour was heard.
- **Lazy image loading** — `lazyLoadImages()` (IntersectionObserver, 300px margin, `data-src`) on the set-details Parts/Minifigs lists. Avoids both iOS Safari's broken native `loading="lazy"` for dynamic rows **and** the connection-pool exhaustion ("?" broken-image flood) from rendering hundreds of `<img>` at once on large sets.
- **Color-specific images** — Cache and display correct images for each part/color variant
- **Design system** — See `.interface-design/system.md` (sorting-station direction). **Inter** (display/body) + **Space Mono** (catalog data — part #s, ids, quantities, dates). **Azure** accent (`#3B9EFF`, the `--yellow` token); **bluish-gray** elevation (LEGO's real structural neutral, in the azure hue family): `--bg #0C1014` → `--surface #141A22` → `--surface2 #1B2330` → `--surface3 #232E3D`; low-opacity bluish seams; glossy ABS **stud** colour chips; baseplate scan **socket** (not a magnifying glass); unified inline-SVG tab icons (`currentColor`). CSS custom properties throughout.
- **Loading screen** — CSS scan-beam animation (yellow bar sweeping across corner-bracket frame); hidden SVG kept in DOM for JS `animateScan()` compat; 2×4 LEGO brick SVG (isometric 3/4 view with 8 studs, radial gradient stud tops). Shows a **simulated progress %** (`#loadingPct`, `startLoadingProgress()`/`finishLoadingProgress()`): `/api/identify` is one opaque request with no progress events, so it eases toward ~90% during the wait and snaps to 100% on response.

- **Installable PWA** — `manifest.webmanifest` (`display: standalone`) + a root-scoped service worker (`/sw.js`) make it "Add to Home Screen"-able with an app icon, full-screen chrome, and an offline shell. SW caching: navigations network-first, `/static/` stale-while-revalidate, **`/api/` + cross-origin never cached** (data stays live). Backend serves `/sw.js` (with `Service-Worker-Allowed: /`) + `/manifest.webmanifest`. Secure-context only (HTTPS/localhost). Icons generated from `static/app-icon.svg` via `qlmanage`+`sips`.

**No external JS frameworks** — Pure vanilla JS with event listeners and DOM manipulation

---

## Data Flow: Photo → Identified Part

1. **Capture:** User clicks "Take Photo" → native file input → EXIF-rotated image
2. **Upload:** Form data sent to `POST /api/identify` with image file
3. **Brickognize:** Server forwards to Brickognize internal API
   - Returns `detected_items[0]` with bounding box, candidate parts, candidate colors
4. **ID Mapping:** Convert BrickLink IDs to Rebrickable:
   - Parts: Query `GET /lego/parts/?bricklink_id=...`
   - Minifigs: Search by name (word overlap ranking) due to no BrickLink filter
5. **Color Detection:** Canvas pixel sampling:
   - Tight crop from bbox (if valid) → median RGB
   - Fallback crop (center 40%) → histogram peak approach
   - Convert to LAB, match against server-provided candidates or all colors
6. **Display:** Show part image, metadata, color options, price (minifigs), sets, alternatives
7. **Inventory Check:** When user selects a color, async query to `GET /api/partlists/<id>/parts/<part_num>/<color_id>` returns current quantity if already in list
8. **Add/Remove:** User clicks "Add to List" → `POST /api/add_part` merges or creates entry; or clicks "Remove 1" → `POST /api/remove_part_one` decrements

---

## Recent Changes

Full change history has moved to **`CHANGELOG.md`** (kept out of this file so
`CLAUDE.md` stays small enough to load efficiently). For specifics of any past
change, prefer `git log` / `CHANGELOG.md` over expanding this file again.

---

## Common Development Patterns

### Rate Limiting: Adding New Rebrickable API Calls

**CRITICAL: All Rebrickable API calls must use the `rebrickable_get()` function to respect the 60 req/min rate limit.**

When adding a new endpoint that calls Rebrickable:

```python
# ❌ WRONG - Direct requests bypass rate limiting
resp = requests.get(f"{RB_BASE}/lego/...", params={"key": API_KEY})

# ✅ CORRECT - Uses throttled wrapper
resp = rebrickable_get("/lego/...", params={"key": API_KEY})
```

For pagination loops, the throttling is automatic per request:

```python
url = f"{RB_BASE}/lego/colors/"
while url:
    resp = rebrickable_get(url, params={"key": API_KEY, "page_size": 200})
    # Process response, throttling is applied automatically
    url = resp.json().get("next")
```

**Frontend pagination:** Add 1000+ ms delays between paginated requests:
```javascript
// Add delay between paginated API calls
if (hasMore) {
  await new Promise(resolve => setTimeout(resolve, 1200));
}
```

### Adding a New Part List Feature

1. Add backend endpoint to app.py (use Rebrickable's `/users/{token}/partlists/...` routes as reference)
2. **Use `rebrickable_get()` for all Rebrickable API calls** to respect rate limits
3. Fetch color list if needed: `GET /api/colors` is already cached across requests
4. Update index.html UI and JavaScript handlers
5. Test on iOS Safari (rendering quirks with form inputs, async operations)

### Debugging Color Detection

1. Add `id="debugSwatch"` and `id="debugLabel"` divs (already in HTML) to visualize sampled color
2. Check `/tmp/brk_full.json` (written on each identify) for raw Brickognize response
3. Verify bounding box coordinates are correct in LAB→RGB conversion

### Inventory Status Checking

When a user selects a color in the identify screen:

1. Frontend calls `checkInventoryStatus()` which queries `GET /api/partlists/<list_id>/parts/<part_num>/<color_id>`
2. Backend returns `{"quantity": N, "_exists": true}` or `{"quantity": 0, "_exists": false}`
3. Frontend renders `renderInventoryStatus()` which shows:
   - Green checkmark + "Already in inventory" if exists
   - "Remove 1" button for quick decrement
   - Different state if color not yet selected ("Select a color first")
4. If quantity > 1, decrement; if quantity == 1, delete entirely via `POST /api/remove_part_one`

**Key implementation details:**
- Inventory check is async; triggers when color selected (`selectColor()` calls `checkInventoryStatus()`)
- Uses `inventoryCheckToken` to prevent race conditions when rapidly changing colors
- Shows error messages in `.list-msg` div for network failures
- List view has +/- buttons that immediately adjust quantities without navigation

### Cross-List Inventory Tracking

When a user selects a color, the app also calls `fetchPartInLists()` to display "Found in" section:

1. Frontend calls `fetchPartInLists()` which queries `GET /api/part_in_lists/<part_num>/<color_id>`
2. Backend fetches all user lists, checks each for the part, returns array with list names and quantities
3. Frontend renders each list with +/− buttons for quick quantity adjustment
4. `quickAddPartToList()` and `quickRemovePartFromList()` handle the adjustments without navigation

**Key implementation details:**
- Shows all lists containing the part simultaneously (different from single list selection)
- Uses inline quantity display: `<span class="list-qty-${list_id}">`
- Buttons immediately call API and update UI (no page reload)
- Section hidden if part not found in any list or color not selected

### Styling Changes

All CSS is in `<style>` within index.html. Full direction + component patterns live
in **`.interface-design/system.md`** — read it before any UI work. The design uses
CSS custom properties defined in `:root` (bluish-gray elevation, azure accent):

```css
--yellow: #3B9EFF   /* azure accent — the single accent (named "yellow" for historical reasons) */
--bg: #0C1014       /* page / baseplate */
--surface: #141A22  /* card surface */
--surface2: #1B2330 /* raised / hover */
--surface3: #232E3D /* inputs, secondary buttons, thumbnails */
--socket: #080B0F   /* inset inputs / the scan socket centre */
--border: rgba(150,180,215,0.10)        /* standard bluish seam */
--border-bright: rgba(150,180,215,0.20) /* emphasis seam */
--text: #EAEEF4     /* primary ink */
--muted: #9EAAB9    /* secondary ink / labels */
--muted2: #697686   /* tertiary / placeholder */
--stud-sheen: radial-gradient(...)      /* glossy ABS gloss on stud colour chips */
--font-display: 'Inter'      /* UI + headings (clean, neutral) */
--font-body: 'Inter'         /* body text */
--font-mono: 'Space Mono'    /* catalog data ONLY: part #s, ids, quantities, prices, dates */
```

Inventory / semantic colors (meaning only):
- Green (`--green #46C97E` / `--green-dim` bg) for "already in inventory" state
- Red (`--red #F0564B` / `--red-bg` bg / `--red-text` text) for remove buttons

No CSS files or preprocessors; inline styles for specific elements.
When editing styles, **always use CSS custom properties** (`var(--yellow)`, `var(--surface3)`, etc.)
rather than hardcoded hex — in CSS *and* in JS-generated inline styles — so the design
system stays consistent. Don't reintroduce emoji chrome icons, the magnifying-glass scan
metaphor, flat rectangular swatches, or the global dot-grid texture (see system.md "Avoid").

---

## Deployment & Development Workflow

**Two independent instances:**
1. **Local Development** — `http://127.0.0.1:5001` (run `python3 app.py`;
   auto-reloads via Flask debug mode). Also reachable privately from a phone over
   Tailscale — see **Private Access** in `SETUP.md`.
2. **Cloud Production** — `https://brick-scanner.onrender.com` (Render.com).
   Public URL; auto-redeploys ~1-2 min after a push to GitHub `main`.

**Golden Rule:** All development happens locally. Only push to `main` when
explicitly instructed.

**To deploy:** when the user says **"Push to main"** / **"Deploy this"**:
```bash
git add . && git commit -m "descriptive message" && git push origin main
```
Render auto-redeploys from the push — no manual Render steps needed.

> Render/Tailscale stack detail, the autostart launchd agent, API-key management,
> and cost estimates live in **`SETUP.md`**. Secrets are git-ignored and also set
> in the Render dashboard (`render.yaml` uses `sync: false`); never commit real
> values.

---

## Testing on Device

```bash
# Local development server:
python3 app.py
# Open on phone: http://127.0.0.1:5001 (same Wi-Fi)

# Or use public cloud URL:
# https://brick-scanner.onrender.com (anywhere)
```

**Common issues:**
- CORS: Brickognize/Rebrickable requests go through Flask backend, not browser
- EXIF: iPhone always returns portrait; bbox must be rotated for alignment
- Safari form inputs: Type conversions (number ↔ text) can cause issues; reset early in function
- Cache: Hard refresh on iPhone with Cmd+Shift+R to clear cache
- **Safari overflow clipping:** `overflow-x: hidden` on `.screen` creates a new stacking context in Safari that clips `position:absolute` children. Fix: move absolutely-positioned popups/dropdowns out of the clipped ancestor as sibling elements in normal document flow instead.

---

## Key Files

- **app.py** — Flask server, all API endpoints, OAuth1 signing for BrickLink
- **templates/index.html** — 5500+ lines: HTML, CSS, vanilla JS, canvas color detection
- **.interface-design/system.md** — design system (direction, tokens, typography, component patterns). Read before any UI change.
- **build_brick_db.py** — Builds `brick_parts.db` (offline search) from the `Brick Parts/` CSV dump
- **static/** — Minifig PNG, brick SVG (header logo; tab icons are now inline SVGs); PWA assets: `manifest.webmanifest`, `sw.js`, `app-icon.svg` + generated `icon-192/512.png` & `apple-touch-icon.png`
- **.env** — API credentials (git-ignored)
- **brick_parts.db / Brick Parts/** — Offline catalog DB + source CSVs (git-ignored, local dev only)
- **SETUP.md** — new-machine setup, private (Tailscale) access, Render deploy/keys/cost detail
- **CHANGELOG.md** — full history of notable changes (moved out of this file)
- **start.sh** — Foreground run; auto-detects and prints the private Tailscale URL
- **install_agents.sh** — installs/refreshes both launchd agents for the current machine (substitutes `__PROJECT_DIR__` in the plist templates → `~/Library/LaunchAgents`, loads them); makes the agents path/user-independent
- **com.brickscanner.app.plist** — launchd autostart agent template for the Flask server (local-only; runs at login, restarts on crash → `app.log`)
- **com.brickscanner.catalog-refresh.plist / refresh_catalog.sh** — launchd daily catalog-refresh job template + self-locating wrapper, 07:30 ET (local-only)
