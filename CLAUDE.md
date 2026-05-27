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

# With ngrok tunnel for public access (see start.sh)
./start.sh
```

### Dependencies

```bash
# Install required packages
pip3 install flask requests python-dotenv requests-oauthlib
```

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

## Architecture

### Backend (app.py)

Flask server with 10+ endpoints:

**Parts Management:**
- `GET /api/partlists` — Fetch user's parts lists
- `POST /api/partlists` — Create new parts list
- `DELETE /api/partlists/<id>` — Delete list
- `GET /api/partlists/<id>/parts` — Get parts in a list (paginated, with color-specific images)
- `GET /api/partlists/<id>/parts/<part_num>/<color_id>` — Check if specific part/color exists in list
- `GET /api/part_in_lists/<part_num>/<color_id>` — Find all lists containing a specific part/color with quantities
- `POST /api/add_part` — Add/update part in list (merges quantities if exists)
- `POST /api/remove_part_one` — Decrement part quantity by 1 (delete if qty becomes 0)

**Minifig Management:**
- `GET /api/minifiglists` — Fetch minifig lists
- `POST /api/minifiglists` — Create minifig list
- `GET /api/minifig_sets/<set_num>` — Get sets containing a minifig
- `POST /api/add_minifig` — Add minifig to list
- `GET /api/minifig_price/<fig_id>` — Fetch BrickLink pricing (OAuth1)

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
- **Color matching logic** — Prioritizes hue distance for chromatic colors, LAB distance for achromatic; penalizes Trans-, Glow-in-Dark, Satin colors unless explicitly detected
- **Quantity management** — Resets to 1 on each scan to prevent user error; merges with existing inventory on add
- **Inventory status checking** — Real-time lookup when selecting a color to show if part/color combo is already in selected list (with quick remove button)
- **Cross-list inventory** — "Found in" section shows all lists containing the part with quick +/− buttons to adjust across lists
- **Smart list selection** — Optional default list on scan screen; list picker modal only appears if no default selected
- **Inline list creation** — Create new lists directly from picker modals without navigation
- **Expandable parts** — Minifigure parts section is collapsible to reduce visual clutter
- **List management UI** — Add/remove buttons in list view for quick quantity adjustments
- **Color-specific images** — Cache and display correct images for each part/color variant
- **Dark mode** — Nearly black backgrounds (#0a0a0a) with white text, blue accent (#0072CE)

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

All CSS is in `<style>` within index.html. Dark mode uses:
- `#0a0a0a` — Page background
- `#1a1a1a` — Card backgrounds
- `#222` — Secondary surfaces
- `#0072CE` — Blue accent (buttons, active states)
- `#fff` / `#aaa` / `#888` — Text hierarchy

Inventory UI colors:
- Green (#19a64a) for "already in inventory" state
- Red (#3a1618 background, #ffb8bf text) for remove buttons

No CSS files or preprocessors; inline styles for specific elements.

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

**Local (.env file):**
```
REBRICKABLE_API_KEY=REDACTED_REBRICKABLE_API_KEY
REBRICKABLE_USER_TOKEN=REDACTED_REBRICKABLE_USER_TOKEN
BL_CONSUMER_KEY=REDACTED_BL_CONSUMER_KEY
BL_CONSUMER_SECRET=REDACTED_BL_CONSUMER_SECRET
BL_TOKEN=REDACTED_BL_TOKEN
BL_TOKEN_SECRET=REDACTED_BL_TOKEN_SECRET
```

**Cloud (Render environment variables):**
- Same 6 variables set in Render dashboard
- Never committed to git (for security)
- Used by production instance

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

---

## Key Files

- **app.py** — Flask server, all API endpoints, OAuth1 signing for BrickLink
- **templates/index.html** — 5200+ lines: HTML, CSS, vanilla JS, canvas color detection
- **static/** — Minifig PNG, brick SVG (parts tab icon)
- **.env** — API credentials (git-ignored)
