# AGENTS.md

This file gives Codex and other coding agents the working context for this repository.

## Project Overview

LEGO Scanner is a mobile-friendly local web app that identifies LEGO parts from phone camera photos.

The app:

- captures photos on iOS/Android,
- sends images to Brickognize for part/minifig detection,
- performs server-side and client-side color handling around LEGO colors,
- displays identified parts or minifigs,
- adds confirmed items to Rebrickable inventory lists,
- supports minifig price lookups through BrickLink.

Tech stack: Flask on Python 3, plus a vanilla JavaScript frontend. There is no frontend framework or build step.

## Commands

Run the development server:

```bash
python3 app.py
```

The app binds to `0.0.0.0` and uses `PORT` if set, otherwise `5001`.

Run with the existing ngrok helper:

```bash
./start.sh
```

Install dependencies:

```bash
pip3 install flask requests python-dotenv requests-oauthlib
```

Create local credentials:

```bash
cp .env.example .env
```

Then fill in real API credentials in `.env`.

## Environment Variables

Rebrickable:

- `REBRICKABLE_API_KEY`
- `REBRICKABLE_USER_TOKEN`

BrickLink OAuth1 price lookup:

- `BL_CONSUMER_KEY`
- `BL_CONSUMER_SECRET`
- `BL_TOKEN`
- `BL_TOKEN_SECRET`

Never commit `.env` or real credentials.

## Architecture

### Backend

`app.py` contains the Flask app and all API endpoints.

Parts endpoints:

- `GET /api/partlists`
- `POST /api/partlists`
- `DELETE /api/partlists/<id>`
- `GET /api/partlists/<id>/parts`
- `POST /api/add_part`

Minifig endpoints:

- `GET /api/minifiglists`
- `POST /api/minifiglists`
- `DELETE /api/minifiglists/<id>`
- `GET /api/minifig_sets/<set_num>`
- `POST /api/add_minifig`
- `GET /api/minifig_price/<fig_id>`

Identification and metadata endpoints:

- `POST /api/identify`
- `GET /api/colors`
- `GET /api/part/<part_num>`
- `GET /api/part_colors/<part_num>`

The core identification flow is photo upload, Brickognize detection, BrickLink to Rebrickable ID mapping, color candidate handling, then frontend display.

### Frontend

`templates/index.html` is a single-page app containing HTML, CSS, and vanilla JavaScript.

Primary screens:

- Scan
- Loading
- Identify
- Lists
- Success

The UI supports parts and minifigs modes, which change list endpoints, display text, and add behavior.

Important frontend behaviors:

- phone camera file input,
- canvas image processing and color sampling,
- EXIF/rotation handling for phone photos,
- color matching against candidate LEGO colors,
- quantity controls,
- part/minifig alternatives,
- list creation and deletion,
- dark mode styling.

## Data Flow

1. User captures or uploads a photo.
2. Frontend sends form data to `POST /api/identify`.
3. Flask forwards the image to Brickognize.
4. Brickognize returns detected items, bounding box data, candidate items, and candidate colors.
5. Backend maps BrickLink IDs to Rebrickable IDs:
   - parts use Rebrickable `bricklink_id` lookup,
   - minifigs search by cleaned name and rank by word overlap.
6. Frontend displays the best candidate, alternatives, colors, and quantity controls.
7. User confirms and adds the item.
8. Backend creates or updates the matching Rebrickable list entry, merging quantities when an item already exists.

## Development Guidance

- Keep changes small and direct. This project is intentionally simple.
- Prefer editing `app.py` and `templates/index.html` in place over introducing new frameworks or build tooling.
- Preserve the mobile-first phone-camera workflow.
- Test UI changes in a narrow/mobile viewport.
- Be careful with iOS Safari behavior around file inputs, orientation, async rendering, and number inputs.
- Keep API failures visible to the frontend; preserve meaningful status codes and JSON responses.
- Do not log secrets or expand debug logging around credentials.
- `/tmp/brk_full.json` may be written during identification for debugging; do not treat it as repo state.

## Styling Notes

All CSS lives in the `<style>` block in `templates/index.html`.

Current dark theme colors:

- `#0a0a0a` page background,
- `#1a1a1a` card backgrounds,
- `#222` secondary surfaces,
- `#0072CE` blue accent,
- `#fff`, `#aaa`, and `#888` text hierarchy.

Do not add a CSS framework for routine styling changes.

## Testing And Verification

There is no formal automated test suite in the current repo.

For a basic server check:

```bash
python3 app.py
```

Then open:

```text
http://localhost:5001
```

For phone testing, use the local IP on the same Wi-Fi network or run:

```bash
./start.sh
```

For changes touching identification, Rebrickable, Brickognize, or BrickLink, verify manually with real credentials.

For frontend changes, confirm:

- scan/upload still works,
- identify screen renders part and minifig candidates correctly,
- color selection remains usable,
- quantity resets appropriately for new scans,
- add/update actions hit the expected endpoints,
- text does not overflow on mobile screens.

## Key Files

- `app.py`: Flask server, Rebrickable calls, Brickognize calls, BrickLink OAuth1 pricing.
- `templates/index.html`: single-page UI, inline CSS, vanilla JavaScript, image/color handling.
- `static/`: UI image assets.
- `start.sh`: Flask plus ngrok launch helper.
- `.env.example`: placeholder environment variable names.
- `.env`: local secrets, ignored by git.

## Current Repo Notes

- The branch is ahead of `origin/main` by recent local commits.
- `CLAUDE.md` was added in the latest local commit and is the source this file was translated from.
- `static/parts_icon.jpg` and `static/parts_icon.png` are currently untracked user assets; do not overwrite or remove them without explicit direction.
