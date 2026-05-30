# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
cp .env.example .env    # then fill in real values (see "Required environment variables" above)
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
```
The MagicDNS hostname differs per machine; `./start.sh` auto-detects and prints
the current URL. See **Private Access (Tailscale + autostart)** under Deployment.

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

**Minifig Management:**
- `GET /api/minifiglists` — Fetch minifig lists
- `POST /api/minifiglists` — Create minifig list
- `GET /api/minifig_sets/<set_num>` — Get sets containing a minifig
- `POST /api/add_minifig` — Add minifig to list
- `GET /api/minifig_price/<fig_id>` — BrickLink last-6-months sold price, Used + New (OAuth1; via `_bl_sold_price`) + theme category
- `GET /api/set_price/<set_num>` — BrickLink last-6-months sold price for a set, Used + New (`_bl_sold_price("SET", …)`; bare set numbers default to `-1`)

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
- **List live search** — A search box in the Lists view filters the whole list **as you type** (part #, name, or colour) with an `N of M parts` count. The full list is loaded once into memory via `/api/partlists/<id>/parts_all` (`_listAllParts`, `renderListParts`/`filterListParts`); replaces the old Load-More pagination. The `+/−` steppers keep the in-memory list + count in sync.
- **Voice quick-add mode** — A persisted (localStorage) toggle in the "Add by voice" modal that adds spoken parts **straight to the selected list with no confirm card** (re-arms the mic for rapid entry); falls back to the confirm card when no list is selected or no colour was heard.
- **Lazy image loading** — `lazyLoadImages()` (IntersectionObserver, 300px margin, `data-src`) on the set-details Parts/Minifigs lists. Avoids both iOS Safari's broken native `loading="lazy"` for dynamic rows **and** the connection-pool exhaustion ("?" broken-image flood) from rendering hundreds of `<img>` at once on large sets.
- **Color-specific images** — Cache and display correct images for each part/color variant
- **Design system** — See `.interface-design/system.md` (sorting-station direction). **Inter** (display/body) + **Space Mono** (catalog data — part #s, ids, quantities, dates). **Azure** accent (`#3B9EFF`, the `--yellow` token); **bluish-gray** elevation (LEGO's real structural neutral, in the azure hue family): `--bg #0C1014` → `--surface #141A22` → `--surface2 #1B2330` → `--surface3 #232E3D`; low-opacity bluish seams; glossy ABS **stud** colour chips; baseplate scan **socket** (not a magnifying glass); unified inline-SVG tab icons (`currentColor`). CSS custom properties throughout.
- **Loading screen** — CSS scan-beam animation (yellow bar sweeping across corner-bracket frame); hidden SVG kept in DOM for JS `animateScan()` compat; 2×4 LEGO brick SVG (isometric 3/4 view with 8 studs, radial gradient stud tops). Shows a **simulated progress %** (`#loadingPct`, `startLoadingProgress()`/`finishLoadingProgress()`): `/api/identify` is one opaque request with no progress events, so it eases toward ~90% during the wait and snaps to 100% on response.

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

**UI Redesign — "sorting station" + Inter (May 2026):**
- Full visual overhaul via the `interface-design` plugin, captured in
  `.interface-design/system.md`. Direction: a tidy LEGO **sorting station** —
  precise + tactile + quietly playful, with the part photos carrying the colour
  while the chrome is a neutral tray.
- **Palette remapped onto the existing tokens** (names kept — `--yellow` is still
  the accent, now azure `#3B9EFF`): **bluish-gray elevation** (LEGO's real
  structural neutral, same hue as the accent) `--bg #0C1014` → `--surface` →
  `--surface2` → `--surface3`, `--socket` for inset inputs, low-opacity bluish
  seam borders, four-level ink. The whole app re-skins because everything routes
  through these vars.
- **Type → Inter** (display/body; clean, neutral, Porsche-Next-like) + **Space
  Mono** retained for catalog data only.
- **Signature: the stud.** Scan target is now a **baseplate socket** (dashed azure
  drop-ring + stud-grid texture), not a magnifying glass. Every colour swatch
  (`.color-dot`, `.swatch-btn .dot`, `.color-swatch`, `.part-item-color-dot`) gets
  the glossy ABS **stud sheen** via a shared `::after` + `--stud-sheen`.
- **All five tab icons** unified to inline monochrome SVGs (`.tab-ico`/`.tab-brick`,
  `fill: currentColor`) — no more emoji; Sets is two scattered bricks.
- Swept all raw hex in CSS **and** JS-generated markup onto the token scale
  (Set-details + Cart/gap-analysis included); removed the global dot-grid texture
  and the harsh 2px accent header rule. **Presentation-only — no behaviour changed.**

**List live search + colour-specific list images (May 2026):**
- Lists view gained a **search box that filters as you type** (part #, name, colour;
  `N of M parts` count). Full list pulled once into memory via the new lightweight
  `GET /api/partlists/<id>/parts_all` (throttled paging, no per-part image fan-out);
  `renderListParts`/`filterListParts` filter in memory. Replaced Load-More pagination.
- `parts_all` overlays **colour-specific images from the local catalog**
  (`_local_part_color_imgs` over `part_colors`, derived from the bulk dump's
  `inventory_parts.img_url`, ~94% coverage, **zero API calls**), falling back to the
  generic part image; graceful on Render (no DB).

**Voice quick-add + iOS lazy thumbnails (May 2026):**
- "Add by voice" gained a persisted **Quick add** toggle: adds spoken parts straight
  to the selected list with no confirm card and re-arms the mic for rapid entry;
  falls back to the confirm card when no list/colour is available.
- `lazyLoadImages()` (IntersectionObserver, `data-src`) on the set-details
  Parts/Minifigs lists — fixes the iOS "?" broken-image flood on large sets (e.g.
  Rivendell, ~991 parts) caused by rendering hundreds of `<img>` at once, and the
  unreliable native `loading="lazy"` for dynamic rows.

**Sold price for sets + minifigs (May 2026):**
- BrickLink **last-6-months sold price** (Used + New: avg, min–max range, # sales)
  shown for minifigs (existing identify panel) and now **sets** (new panel on the
  set-details screen). Uses BrickLink's `guide_type=sold` price guide.
- Backend: shared `_bl_sold_price(item_type, item_no)` helper (both U/N); `GET
  /api/set_price/<set_num>` (SET type; bare numbers default to `-1`, matching
  BrickLink/Rebrickable set ids like `75300-1`). `minifig_price` refactored onto it.
- Frontend: `_renderSoldPriceCards()` shared renderer; set-details fetches/render
  into `#setPriceSection`/`#setPriceGrid`. (eBay was considered but its sold-data
  API is approval-gated + 90-day only; BrickLink is LEGO-specific and already
  integrated.)

**Export to BrickLink Wanted List (May 2026):**
- Lists screen → "🛒 Export to BrickLink Wanted List" builds the selected parts
  list as BrickLink Wanted List **XML** (upload format: `<ITEM><ITEMTYPE>P…<ITEMID><COLOR><MINQTY>`)
  in a modal with **Copy** / **Download** + upload steps (BrickLink → Want → Create
  Wanted List → Upload).
- `GET /api/partlists/<id>/bricklink_wanted` pages the whole list from Rebrickable
  and converts each entry: **part_num → BrickLink item id** (`_rb_part_to_bl`, reverse
  of `bl_aliases`; falls back to the part_num) and **color id → BrickLink color id**
  (`_rb_color_to_bl` via the new `bl_colors` table). Returns `{xml, item_count,
  total_qty, unmapped_colors}`; colors with no BrickLink mapping are emitted without
  `<COLOR>` ("any color") and counted.
- `bl_colors` (rebrickable color id → BrickLink color id) is harvested in
  `build_brick_db` (`harvest_bl_colors`, 1 request, ~216/275 colors mapped), same
  every-build/graceful pattern as `bl_aliases`. (Minifig lists can't be exported —
  Rebrickable exposes no BrickLink minifig ids to reverse-map.)

**Add by Voice (May 2026):**
- "🎤 Add by voice" button on the Parts scan screen opens a modal where you speak
  (or dictate/type) a **part number, color, and quantity** — e.g. *"3068b dark green 2"*.
- **One-tap mic** uses the Web Speech API (`SpeechRecognition`/`webkitSpeechRecognition`),
  shown only in a **secure context** (HTTPS or `localhost`). Over plain HTTP (e.g. the
  phone on the tailnet) the mic is hidden and the text box + **keyboard dictation** are
  used instead — same parser, works everywhere. (Enable HTTPS on the tailnet via
  `tailscale serve` to get the one-tap mic on the phone.)
- `parseVoiceInput()` extracts: **color** (longest catalog color-name phrase match
  against the `colors` list), **quantity** (explicit `quantity/qty/times/x N`, or a small
  trailing number; number-words supported), and **part number** (the normalized
  remainder). The part is resolved **BrickLink-first** via `GET /api/resolve_part/<id>`
  (`_local_resolve_part`: exact → `bl_aliases` BrickLink map → mold heuristic) since
  users speak BrickLink numbers (e.g. "3068" → 3068b); only if that 404s does it fall
  back to fuzzy name/number search (`_pickVoicePart`).
- Reuses the **identify screen as the confirm card**: `submitVoiceText()` → `openPartFromSearch()`
  (now awaited) → pre-fills quantity and `applyColor(parsed color)`; the user reviews and
  taps the existing "Add to List" (so list selection / picker behavior is unchanged).

**Catalog Change-Tracking — renames, set contents & tables (May 2026):**
- `_diff_catalog` now records, per category, **added / removed / renamed** items
  (rename = name changed for the same `part_num`/`fig_num`/`set_num`), plus
  **set-content changes** — sets whose inventory composition changed, detected via
  a cheap per-set signature `(distinct part/color lines, total qty)` from
  `inventories`⋈`inventory_parts` (`_set_signatures`). This is what Rebrickable's
  frequent `inventories`-table updates actually represent.
- The `.catalog_changes.json` record **always includes the updated `tables` list**
  and is written on every refresh that had a prior catalog to diff — even with no
  item/content changes — so the footer can still show *which* tables updated.
  (Previously an inventories/themes-only update wrote nothing → footer showed only
  "Catalog updated (N tables)" with no detail.)
- Frontend `_renderChanges` renders an "Updated tables" line, blue `~` rename rows,
  and a "Set contents changed (N)" group; the panel shows whenever there's any
  change or table info. New CSS: `.cc-sign.ren`, `.cc-tables`.

**Private Access via Tailscale + autostart (May 2026):**
- App reachable privately from a phone over **Tailscale** (`0.0.0.0:5001` on the
  tailnet, WireGuard-encrypted, no public exposure / ngrok / port forwarding) —
  see **Private Access (Tailscale + autostart)** under Deployment.
- `start.sh` drops the ngrok tunnel and prints the auto-detected Tailscale URL
  (ngrok static-domain config left intact for optional reuse).
- `com.brickscanner.app.plist`: launchd LaunchAgent runs the Flask server at login
  and restarts it on crash (`KeepAlive`); local-only. Logs to `app.log` (git-ignored).
- Daily catalog-refresh job moved **04:30 → 07:30 ET** (just after Rebrickable's
  ~07:12 ET catalog update); launchd uses local time so it tracks DST.

**Image Preview — set details + identify screen (May 2026):**
- In the Sets tab, tapping a part or minifig thumbnail in a set's Parts/Minifigures
  list opens the full-screen image modal. Reused `openImageModal` with an optional
  `linkType` arg so minifigs link to BrickLink `M=` catalog pages (parts keep `P=`).
- On the **identify screen**, the identified item's catalog image (`#catalogImg`) is
  tappable too (set in `populateCardInfo`) — opens the same modal, reading the live
  src so color-specific part images enlarge, with the BrickLink link using `M=`/`P=`
  by item type.

**Offline Catalog Search (May 2026):**
- New local search over the full Rebrickable catalog (~62k parts, ~16k minifigs, ~26k sets) backed by a local SQLite DB — instant and not subject to the 60 req/min Rebrickable rate limit
- `build_brick_db.py` loads the `Brick Parts/` CSV dump into `brick_parts.db` (parts, minifigs, sets, colors, categories, themes, inventories; derives per-part thumbnails and distinct part/color combos). It also **harvests a BrickLink→Rebrickable part-id map** (`bl_aliases` table) via `harvest_bl_aliases()` — Rebrickable's parts *list* endpoint includes `external_ids` inline, so the full map is ~63 throttled requests (~1-2 min), not 62k. Runs on **every build** (local + Render, so production resolves identically); graceful — with no `REBRICKABLE_API_KEY` or on API failure the table is left empty and resolution falls back to the identity/mold heuristic + live API.
- Backend: `GET /api/local/search?q=&type=parts|minifigs|sets` — prefers the local DB, **falls back to the live Rebrickable API when the DB is absent** (`_api_search_fallback()`), returning `"source": "offline" | "api"`
- Frontend: parts/minifigs scan screens now search by **name or number** (results dropdown, `.local-result`); Sets tab search repointed from `/api/search_sets` to the local DB. Clicking a part/minifig result opens the existing identify screen (view + add-to-list); set results open the existing set-details screen
- **Data-source badge** (`sourceBadge()` / `.source-badge`): a sticky header above search results showing 🟢 "Offline catalog" (local DB, no quota) or 🟡 "Rebrickable API" (live fallback) + result count
- **Scanning also uses the local catalog** when present (each falls back to the live API if the DB is absent or has no local data for that item):
  - `/api/identify` resolves BrickLink→Rebrickable part ids (`_local_resolve_part`: (1) exact identity match — covers most standard parts; (2) **authoritative `bl_aliases` lookup** — a full BrickLink→Rebrickable map harvested from Rebrickable's `external_ids`, picking the most-common mold when a BrickLink id maps to several; (3) **mold-variant heuristic fallback** — a bare BrickLink number like `3068` maps to the most-common suffixed Rebrickable mold `3068b` "with groove" via inventory frequency) and minifig fig_nums by word overlap (`_local_resolve_minifig`) locally — previously up to ~5 *un-throttled* Rebrickable calls per scan. **Candidate color ids** are also resolved by NAME via the local catalog (`_local_color_id_by_name`): Brickognize returns BrickLink-namespaced color ids (e.g. `color-156` = Medium Azure ≠ Rebrickable 156), so the numeric id is replaced with the correct Rebrickable id by matching the color name.
  - **Shared candidate colors:** Brickognize predicts the scanned object's colour but only attaches `candidate_colors` to some part guesses. `/api/identify` shares the first non-empty colour shortlist across *all* detected items, so a mis-ranked primary part (e.g. a 6×6 tile guessed over the real 2×2) still carries the azure shortlist — otherwise the matcher falls back to that part's full palette and picks a wrong nearby colour the object isn't (azure → Dark Turquoise when the mis-ranked part doesn't come in Medium Azure).
  - `/api/colors` (+ `/api/colors-hybrid`) → `_local_all_colors` (full ~275-color list, instant, no quota). **Critical for color matching:** the live Rebrickable colors fetch is rate-limited and degrades to a tiny 45-color `FALLBACK_COLORS` that omits Medium Azure and most specialty colors — when that happened, the frontend's name-based candidate mapping dropped those colors and auto-selected a wrong nearby color (e.g. azure → Blue). Local-first fixes this.
  - `/api/part_colors/<part_num>` → `_local_part_colors` (color picker + accurate `num_sets` from inventories)
  - `/api/minifig_sets/<fig>` → `_local_minifig_sets`; `/api/minifig_parts/<fig>` → `_local_minifig_parts`
  - Still live (cannot be local): photo recognition (Brickognize), minifig pricing (BrickLink), and all user-inventory calls (`partlists`, inventory checks, `part_in_lists`)
- `Brick Parts/` and `brick_parts.db` are git-ignored (local dev only)

**Frontend Redesign (May 2026):**
- Complete visual overhaul of `templates/index.html` — all JS and functionality preserved
- **Design system:** CSS custom properties (`--yellow`/`--bg`/`--surface` etc.), Google Fonts (Barlow Condensed + Barlow + Space Mono)
- **Color scheme:** Azure blue (`#0080FF`) as primary accent replacing `#0072CE`; deep black background (`#080808`) with subtle stud-grid dot texture
- **Mode tabs:** Compact pill buttons; active tab gets solid blue fill
- **Loading screen:** CSS scan-beam animation replaces SVG animation visually; SVG kept hidden in DOM for JS compat; 2×4 LEGO brick SVG with proper 3/4 isometric perspective, 8 studs, radial gradient dome highlights
- **Styling patterns:** Uppercase Barlow Condensed labels, Space Mono for numbers/IDs, corner-bracket decorators on scan area
- **Mobile overflow fix:** `html/body { overflow-x: hidden }`, `.file-input-row` uses `flex-wrap` so file input takes full-width line and buttons wrap below — prevents horizontal scroll on narrow iPhones

**Cross-List Inventory Tracking (May 2026):**
- New endpoint `GET /api/part_in_lists/<part_num>/<color_id>` — Shows which lists contain a scanned part with quantities
- "Found in:" section displays on identify screen after selecting a color
- Quick +/− buttons on each list to adjust quantities without navigating away
- Quantities update instantly with visual feedback

**Minifigure Parts UI Improvements (May 2026):**
- Minifigure parts section now expandable/collapsible with arrow toggle (▶ → ▼)
- Parts display in horizontal layout: image left, text right (cleaner and more scannable)
- "Add Parts" button moved to quantity row (more prominent, easier to reach)
- Parts section collapsed by default to reduce visual clutter

**List Selection & Modal Improvements (May 2026):**
- "No list selected" option added to scan screen dropdown — users can deselect lists
- List picker modal only appears when needed:
  - If default list is selected: adds part directly without modal
  - If no list selected: shows modal to choose list
- Both list picker modals now support creating new lists inline:
  - "+ Create New List" button in modal toggles creation form
  - New list automatically selected after creation
  - Available for both regular parts and minifigure bulk add

**Inventory Status & Management (May 2026):** 
- Added inventory checking: When a user selects a color on the identify screen, the app queries if that part/color is already in the selected list
- Shows inventory status UI with current quantity and "Remove 1" button for quick decrements
- Added `GET /api/partlists/<id>/parts/<part_num>/<color_id>` endpoint for checking specific part/color existence
- Added `POST /api/remove_part_one` endpoint to decrement or delete items
- Enhanced list view with +/- buttons for quick quantity adjustments (green for add, red for remove)
- Implemented color-specific image caching with `PART_COLOR_IMAGE_CACHE` to improve performance and accuracy

**Dark Mode (May 2026):** Complete CSS color palette swap from light theme to dark:
- Body: #f2f2f7 → #0a0a0a | Text: #111 → #fff
- Cards: #fff → #1a1a1a | Secondary: #f2f2f7 → #222
- Borders: #ddd → #444 | Blue accent preserved (#0072CE)

**Image URL Fix:** Rebrickable `part_img_url` now used for parts (fallback to BrickLink) to avoid dead image links. Color-specific images are now cached for better performance.

**Quantity Reset:** Moved to start of identify screen to prevent async rendering timing issues on iOS Safari.

**Sets Search Results Overflow Fix (May 2026):**
- `setSearchResults` div was `position:absolute` inside `.sets-search-card` (`position:relative`)
- `.screen` has `overflow-x:hidden`, which Safari treats as creating a new overflow context — clipping absolutely positioned descendants
- Fix: moved `setSearchResults` outside the card as a sibling div in normal document flow; removed `position:relative` from `.sets-search-card`

**Rate Limiting & Security Improvements (May 2026):**

*Rate Limiting (60 req/min compliance):*
- Implemented request throttler to enforce 1 request/second to Rebrickable API
- Added `throttle_rebrickable_request()` function that delays requests as needed
- Created `rebrickable_get()` wrapper for all Rebrickable API calls
- Updated all key endpoints to use throttled function:
  - `/api/partlists` — Uses throttled request
  - `/api/colors-hybrid` — Pagination respects rate limit
  - `/api/partlists/<id>/parts` — Pagination with per-page delays
  - `/api/part/<part_num>` — Single-part lookups throttled
  - `/api/part_colors/<part_num>` — Color list fetches throttled
  - `/api/minifiglists` — Minifig list loads throttled
- Frontend pagination delay increased from 500ms to 1200ms for gap analysis
- Rate limit counter shows usage per minute in logs: `⏳ Rate limit: waiting X.XXs (N/60 requests used)`

*Security Fixes (XSS prevention):*
- Added `escapeHtml()` utility function to safely escape HTML special characters
- Fixed XSS in error messages by escaping API responses before `innerHTML` insertion
- Replaced weak inline `onclick` handlers with event listeners for set search results
- Set names and URLs now stored in data attributes and escaped before rendering
- Image URLs validated with onerror fallback to prevent protocol injection
- Rate limit status codes (429/503) now preserved from API instead of converted to 200

**Implementation details:**
- All Rebrickable API calls go through `rebrickable_get()` which applies throttling
- Backend automatically sleeps before each request to maintain 1 req/sec average
- Request counter tracks per-minute usage with automatic reset
- Frontend error messages safely escape API response text
- Event-based DOM updates prevent attribute injection vectors

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

### Current Setup (May 2026)

**Two independent instances:**
1. **Local Development** — `http://127.0.0.1:5001` (on your Mac)
   - Used for testing features before deployment
   - Run with: `python3 app.py`
   - Automatically reloads on code changes (via Flask debug mode)

2. **Cloud Production** — `https://brick-scanner.onrender.com` (Render.com)
   - Public URL accessible from anywhere
   - Auto-deploys when you push to GitHub
   - ~$5-50/month depending on usage

### Private Access (Tailscale + autostart) — local-only

Instead of (or alongside) Render, the local instance is reachable privately
from a phone over **Tailscale** — no public exposure, no ngrok, no port
forwarding. The app binds `0.0.0.0:5001`, so it's available on the tailnet
interface; traffic is WireGuard-encrypted and limited to devices signed into
the same tailnet (`itsjeff292@`). The app uses a native file input (not
`getUserMedia`), so plain HTTP is fine — no HTTPS needed.

- **Reach it from the phone:** install the Tailscale app, sign into the same
  account, then open `https://jefs-macbook-pro.<tailnet>.ts.net` (HTTPS via
  Tailscale Serve — see below) or `http://…:5001` (raw, no mic). `start.sh`
  auto-detects and prints both.
- **HTTPS via Tailscale Serve:** `tailscale serve --bg 5001` proxies the tailnet
  host's :443 → local :5001 with a real (Let's Encrypt) cert, giving
  `https://<host>.<tailnet>.ts.net`. This is a **secure context**, so the browser
  mic ("Add by voice") works on the phone (plain HTTP isn't a secure context).
  Persists across reboots; tailnet-only (not public). Requires HTTPS certs enabled
  once in the tailnet admin console (Settings → **HTTPS Certificates → Enable**).
  Disable with `tailscale serve --https=443 off`; inspect with `tailscale serve status`.
- **Tailscale install (macOS):** `brew install --cask tailscale` (the GUI app
  auto-starts at login and stays connected). The CLI lives at
  `/Applications/Tailscale.app/Contents/MacOS/Tailscale` (`… status` / `… ip -4`).

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

### Development Workflow

**Golden Rule:** All development happens locally. Only push to main when explicitly instructed.

```bash
# 1. Make changes locally
# Edit app.py, templates/index.html, etc.

# 2. Test on local server
python3 app.py
# Visit http://127.0.0.1:5001 on phone/browser

# 3. When ready to deploy, say "push to main"
# Claude will then:
git add .
git commit -m "descriptive message"
git push origin main

# 4. Render auto-redeploys (takes ~1 minute)
```

### Deployment Architecture

**Local Stack:**
- Flask development server on `localhost:5001`
- Uses `.env` for API credentials
- Quick iteration and testing

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
- `DEPLOY.md` — Detailed deployment instructions

### How to Deploy Updates

**When you want to push changes to production:**

Say: **"Push to main"** or **"Deploy this"**

I will:
1. Stage all changes
2. Create a commit with descriptive message
3. Push to GitHub (`git push origin main`)
4. Render automatically redeploys within 1-2 minutes

**The cloud instance updates automatically — no manual Render steps needed.**

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
- **static/** — Minifig PNG, brick SVG (header logo; tab icons are now inline SVGs)
- **.env** — API credentials (git-ignored)
- **brick_parts.db / Brick Parts/** — Offline catalog DB + source CSVs (git-ignored, local dev only)
- **start.sh** — Foreground run; auto-detects and prints the private Tailscale URL
- **install_agents.sh** — installs/refreshes both launchd agents for the current machine (substitutes `__PROJECT_DIR__` in the plist templates → `~/Library/LaunchAgents`, loads them); makes the agents path/user-independent
- **com.brickscanner.app.plist** — launchd autostart agent template for the Flask server (local-only; runs at login, restarts on crash → `app.log`)
- **com.brickscanner.catalog-refresh.plist / refresh_catalog.sh** — launchd daily catalog-refresh job template + self-locating wrapper, 07:30 ET (local-only)
