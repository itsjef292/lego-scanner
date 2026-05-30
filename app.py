from flask import Flask, render_template, request, jsonify
import os
import re
import sys
import json
import datetime
import sqlite3
import requests
import time
import threading
from dotenv import load_dotenv
from requests_oauthlib import OAuth1

load_dotenv()

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

BL_CONSUMER_KEY    = os.environ.get("BL_CONSUMER_KEY", "")
BL_CONSUMER_SECRET = os.environ.get("BL_CONSUMER_SECRET", "")
BL_TOKEN           = os.environ.get("BL_TOKEN", "")
BL_TOKEN_SECRET    = os.environ.get("BL_TOKEN_SECRET", "")

API_KEY = os.environ.get("REBRICKABLE_API_KEY", "")
USER_TOKEN = os.environ.get("REBRICKABLE_USER_TOKEN", "")
RB_BASE = "https://rebrickable.com/api/v3"
BL_BASE = "https://api.bricklink.com/api/store/v1"
PART_COLOR_IMAGE_CACHE = {}
COLORS_CACHE = {"data": None, "timestamp": None}  # Cache colors to avoid repeated API calls
COLORS_CACHE_DURATION = 3600  # 1 hour in seconds
RATE_LIMIT_STATUS = {"is_limited": False, "reset_time": None}  # Track rate limit state

# ── Offline catalog (local SQLite built from the Rebrickable CSV dump) ─────────
# build_brick_db.py loads "Brick Parts/*.csv" into this file. It powers offline
# search (parts/minifigs/sets) so lookups don't consume the 60 req/min API quota.
# Absent on production → offline search degrades gracefully (returns a notice).
_HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL_DB_PATH = os.path.join(_HERE, "brick_parts.db")
CATALOG_MANIFEST_PATH = os.path.join(_HERE, ".catalog_manifest.json")
CATALOG_CHANGES_PATH = os.path.join(_HERE, ".catalog_changes.json")

# Manual catalog-refresh state (the "refresh now" button on the scan screen).
# LOCAL-ONLY FEATURE: refresh + change tracking are disabled on Render. Render's
# filesystem is ephemeral (DB rebuilt from scratch each deploy), so there's no
# prior catalog to diff and no rebuild trigger. When IS_RENDER, can_refresh is
# false (footer hidden) and /api/catalog/refresh returns 403.
IS_RENDER = os.environ.get("RENDER") is not None
_catalog_lock = threading.Lock()
_catalog_state = {"running": False, "last_result": None}


def local_db():
    """Return a read-only-ish connection to the local catalog, or None if absent."""
    if not os.path.exists(LOCAL_DB_PATH):
        return None
    conn = sqlite3.connect(LOCAL_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ── Local-catalog lookups used during a scan (each falls back to the live API
#    in the caller when it returns None, so production without a DB is unchanged).

def _local_resolve_part(bl_id):
    """Map a BrickLink part id → Rebrickable part using the local catalog.

    Heuristic: Rebrickable aligns its part_num with BrickLink's for the vast
    majority of standard parts, so a BrickLink id that exists verbatim in the
    parts table is the same physical part. Printed/variant parts whose numbers
    differ simply miss here → caller falls back to the authoritative API.
    Returns {part_num, name, img_url} or None.
    """
    if not bl_id:
        return None
    conn = local_db()
    if conn is None:
        return None
    try:
        # 1. Exact identity match — covers the vast majority of standard parts.
        row = conn.execute(
            "SELECT part_num, name, img_url FROM parts WHERE part_num = ?",
            (bl_id,),
        ).fetchone()
        if row:
            return dict(row)

        # 2. Authoritative BrickLink→Rebrickable alias, harvested from Rebrickable's
        #    external_ids (bl_aliases table). A BrickLink id can map to several
        #    Rebrickable molds (e.g. 3068 → 3068a/3068b) — pick the one in the most
        #    set inventories. Wrapped in try/except so a DB built before this table
        #    existed degrades to the heuristic below.
        try:
            row = conn.execute(
                """
                SELECT p.part_num, p.name, p.img_url,
                       (SELECT COUNT(*) FROM inventory_parts ip
                        WHERE ip.part_num = p.part_num) AS freq
                FROM bl_aliases a
                JOIN parts p ON p.part_num = a.part_num
                WHERE a.bl_id = ?
                ORDER BY freq DESC, p.part_num
                LIMIT 1
                """,
                (bl_id,),
            ).fetchone()
            if row:
                return {"part_num": row["part_num"], "name": row["name"],
                        "img_url": row["img_url"]}
        except sqlite3.OperationalError:
            pass  # bl_aliases table absent (older DB) — fall through to heuristic

        # 3. Mold-variant fallback. BrickLink often uses a bare number (e.g. 3068)
        #    where Rebrickable splits molds with a single-letter suffix
        #    (3068a "without groove" / 3068b "with groove"). Match part_num =
        #    bl_id + exactly one lowercase letter (GLOB has no trailing *, so
        #    printed variants like 3068bpr0001 are excluded) and pick the variant
        #    that appears in the most set inventories — i.e. the common modern
        #    part (3068b, 7961 sets, over 3068a, 144). This keeps the single most
        #    common LEGO parts resolving locally instead of depending on a live
        #    API call that can time out or hit the rate limit mid-scan.
        if bl_id.isalnum():
            row = conn.execute(
                """
                SELECT p.part_num, p.name, p.img_url,
                       (SELECT COUNT(*) FROM inventory_parts ip
                        WHERE ip.part_num = p.part_num) AS freq
                FROM parts p
                WHERE p.part_num GLOB ?
                ORDER BY freq DESC, p.part_num
                LIMIT 1
                """,
                (bl_id + "[a-z]",),
            ).fetchone()
            if row:
                return {"part_num": row["part_num"], "name": row["name"],
                        "img_url": row["img_url"]}
        return None
    finally:
        conn.close()


def _local_all_colors():
    """Full color list from the local catalog in Rebrickable-API shape, or None.

    The catalog's colors table is complete (~275 colors) and instant, vs the
    live Rebrickable fetch which is rate-limited and degrades to a tiny 45-color
    FALLBACK list that omits Medium Azure and most specialty colors — which made
    color matching pick a wrong nearby color. Returns [{id,name,rgb,is_trans}].
    """
    conn = local_db()
    if conn is None:
        return None
    try:
        rows = conn.execute("SELECT id, name, rgb, is_trans FROM colors").fetchall()
        return [
            {"id": r["id"], "name": r["name"], "rgb": r["rgb"] or "",
             "is_trans": str(r["is_trans"]).strip().lower() in ("true", "1", "t")}
            for r in rows
        ]
    finally:
        conn.close()


def _local_color_id_by_name(name):
    """Resolve a color NAME → Rebrickable color id via the local catalog.

    Brickognize returns BrickLink-namespaced color ids (e.g. color-156 =
    Medium Azure), not Rebrickable ids, but the color *names* line up. Returns
    the int id or None.
    """
    if not name:
        return None
    conn = local_db()
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT id FROM colors WHERE name = ? COLLATE NOCASE", (name,)
        ).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _local_resolve_minifig(name):
    """Find the best fig_num in the local catalog by word overlap with `name`,
    mirroring the live-API search heuristic. Returns {fig_num, name, img_url} or None.
    """
    if not name:
        return None
    conn = local_db()
    if conn is None:
        return None
    try:
        search_name = re.split(r' - | \(', name)[0].strip()
        if not search_name:
            return None
        rows = conn.execute(
            "SELECT fig_num, name, img_url FROM minifigs WHERE name LIKE ? LIMIT 50",
            (f"%{search_name}%",),
        ).fetchall()
        if not rows:
            return None
        full_words = set(re.findall(r'\w+', name.lower()))
        best = max(rows, key=lambda r: len(full_words & set(re.findall(r'\w+', r["name"].lower()))))
        return dict(best)
    finally:
        conn.close()


def _bricklink_minifig_name(bl_id):
    """Look up a BrickLink minifig id (e.g. sw0131) → its catalog name via the
    BrickLink API. Rebrickable exposes no BrickLink minifig ids, so the name is
    the only bridge to a Rebrickable fig. Returns the name or None on any failure.
    """
    if not (BL_CONSUMER_KEY and BL_TOKEN):
        return None
    try:
        auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
        resp = requests.get(f"{BL_BASE}/items/MINIFIG/{bl_id}", auth=auth, timeout=8)
        if resp.status_code == 200:
            return ((resp.json() or {}).get("data") or {}).get("name") or None
    except Exception:
        pass
    return None


def _local_minifig_search_by_name(name, limit=20):
    """Find Rebrickable minifigs whose names best overlap a (BrickLink) name.
    Used to surface candidates for a BrickLink minifig id; returns a ranked list
    of row dicts (best word-overlap first). Names diverge between catalogs, so
    this is intentionally a candidate list for the user to choose from, not a
    single auto-pick.
    """
    conn = local_db()
    if conn is None:
        return []
    try:
        toks = re.findall(r'[a-z0-9]+', name.lower())
        keys = sorted({w for w in toks if len(w) >= 4}, key=len, reverse=True) \
            or sorted({w for w in toks if len(w) >= 3}, key=len, reverse=True)
        if not keys:
            return []
        keys = keys[:2]
        where = " OR ".join("name LIKE ?" for _ in keys)
        rows = conn.execute(
            f"SELECT fig_num, name, num_parts, img_url FROM minifigs WHERE {where} LIMIT 300",
            [f"%{k}%" for k in keys],
        ).fetchall()
        full = set(toks)
        ranked = sorted(
            rows,
            key=lambda r: len(full & set(re.findall(r'\w+', r["name"].lower()))),
            reverse=True,
        )
        return [dict(r) for r in ranked[:limit]]
    finally:
        conn.close()


def _local_part_colors(part_num):
    """Available colors for a part (+ num_sets), from the local catalog.
    Returns a list shaped like Rebrickable's /parts/<n>/colors/ results, or None.
    """
    conn = local_db()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            """
            SELECT ip.color_id AS color_id, c.name AS color_name, c.rgb AS rgb,
                   COUNT(DISTINCT inv.set_num) AS num_sets
            FROM inventory_parts ip
            JOIN inventories inv ON inv.id = ip.inventory_id
            LEFT JOIN colors c ON c.id = ip.color_id
            WHERE ip.part_num = ?
            GROUP BY ip.color_id, c.name, c.rgb
            ORDER BY num_sets DESC
            """,
            (part_num,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _local_part_color_imgs(conn, pairs):
    """Look up color-specific part images from the local catalog in one query.

    `pairs` is an iterable of (part_num, color_id). Returns {(part_num, color_id):
    img_url} for the combos that have a color-specific image in `part_colors`
    (derived at build time from the bulk dump's inventory_parts.img_url — ~94%
    coverage, zero API calls). Combos with no local image are simply absent.
    """
    out = {}
    if conn is None:
        return out
    seen = {(pn, int(cid)) for pn, cid in pairs if pn and cid is not None}
    if not seen:
        return out
    try:
        # Chunk to stay under SQLite's variable limit; the PK index makes each
        # (part_num, color_id) lookup a direct hit.
        items = list(seen)
        for i in range(0, len(items), 400):
            chunk = items[i:i + 400]
            clause = " OR ".join("(part_num = ? AND color_id = ?)" for _ in chunk)
            params = [v for pair in chunk for v in pair]
            for row in conn.execute(
                f"SELECT part_num, color_id, img_url FROM part_colors "
                f"WHERE ({clause}) AND img_url IS NOT NULL AND img_url != ''",
                params,
            ):
                out[(row["part_num"], int(row["color_id"]))] = row["img_url"]
    except sqlite3.OperationalError:
        pass
    return out


def _local_minifig_sets(fig_num):
    """Sets that contain a minifig, from the local catalog.
    Returns a list shaped like Rebrickable's /minifigs/<f>/sets/ results, or None.
    """
    conn = local_db()
    if conn is None:
        return None
    try:
        rows = conn.execute(
            """
            SELECT s.set_num, s.name AS name, s.year AS year, s.img_url AS set_img_url
            FROM inventory_minifigs im
            JOIN inventories inv ON inv.id = im.inventory_id
            JOIN sets s ON s.set_num = inv.set_num
            WHERE im.fig_num = ?
            GROUP BY s.set_num
            ORDER BY s.year DESC
            LIMIT 30
            """,
            (fig_num,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _local_minifig_parts(fig_num):
    """Parts that make up a minifig (from its own latest inventory), from the
    local catalog. Returns a list shaped like Rebrickable's /minifigs/<f>/parts/
    results (nested part/color objects), or None.
    """
    conn = local_db()
    if conn is None:
        return None
    try:
        inv = conn.execute(
            "SELECT id FROM inventories WHERE set_num = ? ORDER BY version DESC LIMIT 1",
            (fig_num,),
        ).fetchone()
        if inv is None:
            return []
        rows = conn.execute(
            """
            SELECT ip.part_num, p.name AS part_name, ip.img_url AS part_img,
                   ip.color_id, c.name AS color_name, c.rgb AS rgb, ip.quantity
            FROM inventory_parts ip
            LEFT JOIN parts p ON p.part_num = ip.part_num
            LEFT JOIN colors c ON c.id = ip.color_id
            WHERE ip.inventory_id = ?
            ORDER BY ip.quantity DESC
            """,
            (inv["id"],),
        ).fetchall()
        return [{
            "part": {
                "part_num": r["part_num"],
                "name": r["part_name"],
                "part_img_url": r["part_img"],
            },
            "color": {
                "id": r["color_id"],
                "name": r["color_name"],
                "rgb": r["rgb"],
            },
            "quantity": r["quantity"],
        } for r in rows]
    finally:
        conn.close()

# ── Rate Limiter for Rebrickable API (60 req/min = 1 req/sec) ──────────────────
# IMPORTANT: This limiter is only correct when the app runs as a SINGLE process
# (gunicorn --workers 1) with multiple threads. Each thread atomically reserves a
# 1-second slot under a shared lock, so outbound requests are globally spaced ≥1s
# apart and never exceed 60/min. Running multiple workers would give each its own
# limiter and multiply the real rate — do not raise --workers above 1.
RB_MIN_INTERVAL = 1.0  # seconds between requests (60 req/min)
_rb_lock = threading.Lock()
_rb_next_slot = 0.0          # monotonic time the next request may be sent
_rb_window_start = 0.0       # wall-clock start of current logging window
_rb_window_count = 0         # requests sent in current logging window


def throttle_rebrickable_request():
    """Globally space Rebrickable requests ≥1s apart across all threads.

    Each caller reserves the next available time slot under a lock (fast), then
    sleeps until that slot WITHOUT holding the lock, so threads don't block each
    other while waiting — they just queue up one slot apart.
    """
    global _rb_next_slot, _rb_window_start, _rb_window_count

    with _rb_lock:
        now = time.monotonic()
        slot = max(now, _rb_next_slot)
        _rb_next_slot = slot + RB_MIN_INTERVAL
        wait = slot - now

        # Per-minute counter purely for log visibility
        wall = time.time()
        if wall - _rb_window_start >= 60:
            _rb_window_start = wall
            _rb_window_count = 0
        _rb_window_count += 1
        count = _rb_window_count

    if wait > 0:
        print(f"⏳ Rate limit: waiting {wait:.2f}s before next Rebrickable request ({count}/60 this minute)")
        time.sleep(wait)

def rebrickable_get(endpoint, params=None):
    """Make a throttled GET request to Rebrickable API"""
    throttle_rebrickable_request()
    try:
        # Handle both full URLs (from pagination) and endpoint paths
        if endpoint.startswith("http"):
            url = endpoint
            # Pagination URLs already include params
            resp = requests.get(url, timeout=10)
        else:
            url = f"{RB_BASE}{endpoint}"
            resp = requests.get(url, params=params or {}, timeout=10)
        return resp
    except Exception as e:
        print(f"⚠ Rebrickable request error: {e}")
        return None

def check_rate_limited(resp):
    """Check if response indicates rate limiting"""
    # HTTP 429 = Too Many Requests (standard rate limit)
    # HTTP 503 = Service Unavailable (sometimes rate limits)
    # Connection errors (1006) also indicate rate limiting
    if resp.status_code in [429, 503]:
        return True
    if hasattr(resp, 'text') and ('rate' in resp.text.lower() or 'limit' in resp.text.lower()):
        return True
    return False

# Fallback color list for when APIs are down
FALLBACK_COLORS = [
    {"id": 1, "name": "White"}, {"id": 2, "name": "Tan"}, {"id": 3, "name": "Light Gray"},
    {"id": 4, "name": "Dark Gray"}, {"id": 5, "name": "Black"}, {"id": 6, "name": "Dark Red"},
    {"id": 7, "name": "Red"}, {"id": 8, "name": "Dark Orange"}, {"id": 9, "name": "Orange"},
    {"id": 10, "name": "Yellow"}, {"id": 11, "name": "Dark Tan"}, {"id": 12, "name": "Dark Green"},
    {"id": 13, "name": "Green"}, {"id": 14, "name": "Dark Blue"}, {"id": 15, "name": "Blue"},
    {"id": 16, "name": "Dark Purple"}, {"id": 17, "name": "Purple"}, {"id": 18, "name": "Dark Pink"},
    {"id": 19, "name": "Pink"}, {"id": 20, "name": "Dark Brown"}, {"id": 21, "name": "Brown"},
    {"id": 22, "name": "Reddish Brown"}, {"id": 23, "name": "Trans-Black"},
    {"id": 24, "name": "Trans-Red"}, {"id": 25, "name": "Trans-Orange"},
    {"id": 26, "name": "Trans-Yellow"}, {"id": 27, "name": "Trans-Clear"},
    {"id": 28, "name": "Trans-Light Blue"}, {"id": 29, "name": "Trans-Blue"},
    {"id": 30, "name": "Trans-Green"}, {"id": 31, "name": "Trans-Brown"},
    {"id": 32, "name": "Trans-Bright Green"}, {"id": 33, "name": "Flat Silver"},
    {"id": 34, "name": "Chrome Silver"}, {"id": 35, "name": "Pearl Gold"},
    {"id": 36, "name": "Pearl Dark Gray"}, {"id": 37, "name": "Pearl Light Gray"},
    {"id": 38, "name": "Light Bluish Gray"}, {"id": 39, "name": "Dark Bluish Gray"},
    {"id": 40, "name": "Sand Green"}, {"id": 41, "name": "Medium Orange"},
    {"id": 42, "name": "Trans-Neon Orange"}, {"id": 43, "name": "Trans-Neon Green"},
    {"id": 44, "name": "Chrome Gold"}, {"id": 45, "name": "Chrome Black"}
]

# BrickLink OAuth1 helper
def bricklink_request(method, endpoint):
    """Make authenticated request to BrickLink API"""
    auth = OAuth1(
        BL_CONSUMER_KEY,
        BL_CONSUMER_SECRET,
        BL_TOKEN,
        BL_TOKEN_SECRET
    )
    url = f"{BL_BASE}{endpoint}"
    try:
        resp = requests.request(method, url, auth=auth, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"BrickLink API error: {e}")
        return None


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/partlists")
def get_partlists():
    try:
        resp = rebrickable_get(
            f"/users/{USER_TOKEN}/partlists/",
            params={"key": API_KEY}
        )
        # If rate limited, preserve the 429 status for frontend to detect
        if resp.status_code in [429, 503]:
            return jsonify({"results": [], "error": "Rate limited or service unavailable"}), resp.status_code
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        print(f"⚠ Error fetching partlists: {e}")
        # Return 503 for other errors so frontend knows something went wrong
        return jsonify({"results": [], "error": str(e)}), 503


@app.route("/api/status")
def get_status():
    """Check API status and rate limit state"""
    import time
    status = {
        "api_available": True,
        "rate_limited": RATE_LIMIT_STATUS["is_limited"],
        "cache_status": {
            "colors_cached": COLORS_CACHE["data"] is not None,
            "colors_age_minutes": None
        }
    }

    # Calculate color cache age
    if COLORS_CACHE["timestamp"]:
        age_seconds = time.time() - COLORS_CACHE["timestamp"]
        status["cache_status"]["colors_age_minutes"] = round(age_seconds / 60, 1)

    # If rate limited, try a simple test call to see if it's cleared
    if RATE_LIMIT_STATUS["is_limited"]:
        try:
            resp = requests.get(f"{RB_BASE}/lego/colors/?key={API_KEY}&page_size=1", timeout=5)
            if resp.status_code == 200:
                RATE_LIMIT_STATUS["is_limited"] = False
                status["rate_limited"] = False
                print("✓ Rate limit appears to be cleared")
        except:
            pass

    return jsonify(status)


@app.route("/api/catalog/status")
def catalog_status():
    """Offline-catalog freshness + refresh capability (for the scan-screen footer)."""
    present = os.path.exists(LOCAL_DB_PATH)
    info = {
        "present": present,
        "can_refresh": not IS_RENDER,
        "running": _catalog_state["running"],
        "last_result": _catalog_state["last_result"],
    }
    if present:
        mt = os.path.getmtime(LOCAL_DB_PATH)
        dt = datetime.datetime.fromtimestamp(mt)
        info["last_updated_iso"] = dt.isoformat(timespec="seconds")
        # e.g. "May 28, 2026 at 3:30 PM" (strip leading zero from hour portably)
        info["last_updated_human"] = dt.strftime("%b %d, %Y at %I:%M %p").replace(" 0", " ")
        info["db_size_mb"] = round(os.path.getsize(LOCAL_DB_PATH) / 1_000_000, 1)
    # Date of the underlying Rebrickable data, if we have a manifest.
    try:
        with open(CATALOG_MANIFEST_PATH) as f:
            man = json.load(f)
        info["data_date"] = man.get("inventory_parts", {}).get("last_modified")
    except (OSError, ValueError):
        pass
    # What the most recent update added/removed (parts/minifigs/sets), if recorded.
    try:
        with open(CATALOG_CHANGES_PATH) as f:
            info["last_changes"] = json.load(f)
    except (OSError, ValueError):
        pass
    return jsonify(info)


@app.route("/api/catalog/refresh", methods=["POST"])
def catalog_refresh():
    """Kick off a manual catalog refresh in the background (local dev only)."""
    if IS_RENDER:
        return jsonify({"error": "Refresh is only available on the local dev instance"}), 403

    with _catalog_lock:
        if _catalog_state["running"]:
            return jsonify({"status": "running"}), 202
        _catalog_state["running"] = True
        _catalog_state["last_result"] = None

    force = bool((request.get_json(silent=True) or {}).get("force"))

    def _worker():
        try:
            import refresh_catalog
            result = refresh_catalog.run(force=force)
        except Exception as e:
            print(f"⚠ Catalog refresh error: {e}")
            result = {"ok": False, "changed": False, "message": str(e), "updated": []}
        with _catalog_lock:
            _catalog_state["running"] = False
            _catalog_state["last_result"] = result

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/colors")
def get_colors():
    """Fetch colors with 1-hour cache."""
    import time

    # Use cached colors if available
    if COLORS_CACHE["data"]:
        now = time.time()
        if (now - COLORS_CACHE["timestamp"]) < COLORS_CACHE_DURATION:
            print(f"✓ Returning cached colors from /api/colors")
            return jsonify(COLORS_CACHE["data"])

    # If cache is stale or empty, delegate to hybrid endpoint
    return get_colors_hybrid()


@app.route("/api/colors-hybrid")
def get_colors_hybrid():
    """Fetch colors from Rebrickable, with fallback. Uses 1-hour cache."""
    import time

    # Check cache
    now = time.time()
    if COLORS_CACHE["data"] and (now - COLORS_CACHE["timestamp"]) < COLORS_CACHE_DURATION:
        print(f"✓ Returning cached colors ({len(COLORS_CACHE['data'])} colors)")
        return jsonify(COLORS_CACHE["data"])

    # Prefer the local catalog: complete (~275 colors) and instant, no Rebrickable
    # quota. Falls back to the live API + FALLBACK_COLORS only when the DB is absent.
    local_colors = _local_all_colors()
    if local_colors:
        local_colors.sort(key=lambda c: c["name"])
        print(f"✓ Returning {len(local_colors)} colors from local catalog")
        COLORS_CACHE["data"] = local_colors
        COLORS_CACHE["timestamp"] = now
        return jsonify(local_colors)

    all_colors = []

    # Fetch from Rebrickable only (BrickLink adds too many API calls)
    try:
        url = f"{RB_BASE}/lego/colors/"
        while url:
            # Use throttled request to respect rate limit
            resp = rebrickable_get(url, params={"key": API_KEY, "page_size": 200})

            if resp is None:
                print(f"⚠ Rebrickable request failed, using fallback")
                all_colors = FALLBACK_COLORS
                break

            # Check for rate limit or service error
            if resp.status_code in [429, 503]:
                print(f"⚠ Rebrickable rate limited/unavailable ({resp.status_code}), using fallback")
                RATE_LIMIT_STATUS["is_limited"] = True
                all_colors = FALLBACK_COLORS
                break

            resp.raise_for_status()
            data = resp.json()
            for color in data.get("results", []):
                all_colors.append(color)
            url = data.get("next")

        if all_colors:
            print(f"✓ Fetched {len(all_colors)} colors from Rebrickable")
    except Exception as e:
        print(f"⚠ Rebrickable colors error: {e}, using fallback")
        all_colors = FALLBACK_COLORS

    # If no colors, use fallback
    if len(all_colors) == 0:
        all_colors = FALLBACK_COLORS
        print(f"⚠ Using fallback color list ({len(all_colors)} colors)")

    all_colors.sort(key=lambda c: c["name"])

    # Cache the result
    COLORS_CACHE["data"] = all_colors
    COLORS_CACHE["timestamp"] = now

    return jsonify(all_colors)


@app.route("/api/verify-part/<part_num>")
def verify_part(part_num):
    """Verify part exists in Rebrickable or BrickLink"""
    result = {
        "part_num": part_num,
        "found": False,
        "sources": []
    }

    # Try Rebrickable first
    try:
        resp = requests.get(
            f"{RB_BASE}/lego/parts/{part_num}/",
            params={"key": API_KEY},
            timeout=10
        )
        if resp.status_code == 200:
            data = resp.json()
            result["found"] = True
            result["sources"].append({
                "api": "Rebrickable",
                "name": data.get("name"),
                "part_num": data.get("part_num")
            })
    except Exception as e:
        print(f"Rebrickable part lookup error: {e}")

    # Try BrickLink if not found
    if not result["found"]:
        try:
            bl_data = bricklink_request("GET", f"/items/PART/{part_num}")
            if bl_data and "results" in bl_data and len(bl_data["results"]) > 0:
                item = bl_data["results"][0]
                result["found"] = True
                result["sources"].append({
                    "api": "BrickLink",
                    "name": item.get("name"),
                    "part_num": item.get("no")
                })
        except Exception as e:
            print(f"BrickLink part lookup error: {e}")

    return jsonify(result)


def _brk_color_id(color_id_str):
    """Convert Brickognize 'color-N' → Rebrickable color id string 'N'."""
    if color_id_str and color_id_str.startswith("color-"):
        return color_id_str[len("color-"):]
    return None


@app.route("/api/identify", methods=["POST"])
def identify():
    if "image" not in request.files:
        return jsonify({"error": "No image provided"}), 400
    image = request.files["image"]
    img_bytes = image.read()
    try:
        # Use Brickognize's internal endpoint which returns server-side candidate_colors
        resp = requests.post(
            "https://api.brickognize.com/internal/search/",
            params={"external_catalogs": "bricklink", "predict_color": "true"},
            files={"query_image": (image.filename, img_bytes, image.content_type)},
            headers={"Origin": "https://brickognize.com", "Referer": "https://brickognize.com/"},
            timeout=30,
        )
        idata = resp.json()

        # Convert internal format → our existing format
        detected = (idata.get("detected_items") or [{}])[0]
        bb_list = detected.get("bounding_boxes") or []
        bounding_box = bb_list[0] if bb_list else {}

        items = []
        for ci in detected.get("candidate_items", []):
            raw_id = ci.get("id", "")
            item_type = "minifig" if ci.get("type") in ("minifig", "fig") else "part"
            bl_id = raw_id.replace("part-", "").replace("minifig-", "").replace("fig-", "")
            ext = next((e for e in ci.get("external_items", [])
                        if e.get("catalog_name") == "bricklink"), {})
            bl_external_id = ext.get("external_id", bl_id)

            # Resolve BrickLink ID → Rebrickable ID
            # Minifigs default to "fig-{bl_id}" format even if search fails
            rb_id = f"fig-{bl_id}" if item_type == "minifig" else bl_id
            if item_type == "minifig":
                name = ci.get("name", "")
                # Prefer the offline catalog (no API quota); fall back to the live API.
                local_fig = _local_resolve_minifig(name)
                if local_fig:
                    rb_id = local_fig["fig_num"]
                else:
                    try:
                        # Rebrickable doesn't support bricklink_id filtering for minifigs.
                        # Strip color/variant suffix (after " - " or "(") for a cleaner search term,
                        # then pick the result with the most word overlap against the full name.
                        search_name = re.split(r' - | \(', name)[0].strip() if name else ""
                        if search_name:
                            rb_resp = requests.get(
                                f"{RB_BASE}/lego/minifigs/",
                                params={"key": API_KEY, "search": search_name, "page_size": 8},
                                timeout=5,
                            )
                            if rb_resp.status_code == 200:
                                results = rb_resp.json().get("results", [])
                                if results:
                                    full_words = set(re.findall(r'\w+', name.lower()))
                                    def _overlap(r):
                                        return len(full_words & set(re.findall(r'\w+', r['name'].lower())))
                                    rb_id = max(results, key=_overlap)['set_num']
                    except Exception:
                        pass
                img_url = f"https://img.bricklink.com/ItemImage/MN/0/{bl_external_id}.png"
            else:
                # Prefer the offline catalog (no API quota); fall back to the live API.
                local_part = _local_resolve_part(bl_external_id)
                if local_part:
                    rb_id = local_part["part_num"]
                    img_url = local_part["img_url"] or \
                        f"https://img.bricklink.com/ItemImage/PN/0/{bl_external_id}.png"
                else:
                    rb_part = None
                    try:
                        rb_resp = requests.get(
                            f"{RB_BASE}/lego/parts/",
                            params={"key": API_KEY, "bricklink_id": bl_external_id},
                            timeout=5,
                        )
                        if rb_resp.status_code == 200:
                            results = rb_resp.json().get("results", [])
                            if results:
                                rb_part = results[0]
                                rb_id = rb_part["part_num"]
                    except Exception:
                        pass
                    # Use Rebrickable image if available, otherwise BrickLink
                    if rb_part and rb_part.get("part_img_url"):
                        img_url = rb_part["part_img_url"]
                    else:
                        img_url = f"https://img.bricklink.com/ItemImage/PN/0/{bl_external_id}.png"

            # Build candidate_colors with Rebrickable IDs (parts only).
            # Brickognize's numeric color ids are BrickLink-namespaced (e.g.
            # color-156 = Medium Azure ≠ Rebrickable 156), so resolve the correct
            # Rebrickable id by NAME via the local catalog; fall back to the raw
            # stripped id only when the name can't be resolved.
            rb_colors = []
            if item_type != "minifig":
                for c in ci.get("candidate_colors", []):
                    cname = c.get("name", "")
                    c_rb_id = _local_color_id_by_name(cname)
                    if c_rb_id is None:
                        c_rb_id = _brk_color_id(c.get("id", ""))
                    if c_rb_id:
                        rb_colors.append({"id": str(c_rb_id), "name": cname})

            item = {
                "id": rb_id,
                "bl_id": bl_external_id,
                "name": ci.get("name", ""),
                "img_url": img_url,
                "external_sites": [{"name": "bricklink", "url": ext.get("url", "")}] if ext else [],
                "type": item_type,
                "score": ci.get("score", 0),
            }
            if rb_colors:
                item["candidate_colors"] = rb_colors
            items.append(item)

        # Brickognize predicts the scanned object's colour but only attaches the
        # candidate_colors to some part guesses (e.g. the 2x2 tile, not a mis-ranked
        # 6x6). The object's colour is the same whichever part it guesses, so share
        # the first non-empty colour shortlist with every item that lacks one.
        # Otherwise the colour matcher falls back to that part's full palette and can
        # pick a wrong nearby colour the object isn't (e.g. azure → Dark Turquoise
        # when the mis-ranked part doesn't even come in Medium Azure).
        shared_colors = next((it["candidate_colors"] for it in items
                              if it.get("candidate_colors")), None)
        if shared_colors:
            for it in items:
                if not it.get("candidate_colors"):
                    it["candidate_colors"] = shared_colors

        data = {
            "listing_id": idata.get("id", ""),
            "bounding_box": bounding_box,
            "items": items,
        }

        import json as _j
        with open('/tmp/brk_full.json', 'w') as f:
            _j.dump(data, f, indent=2)
        return jsonify(data), 200
    except requests.exceptions.RequestException as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/partlists/<int:list_id>/parts")
def get_partlist_parts(list_id):
    page = request.args.get("page", 1)
    resp = rebrickable_get(
        f"/users/{USER_TOKEN}/partlists/{list_id}/parts/",
        params={"key": API_KEY, "page_size": 50, "page": page},
    )
    data = resp.json()
    if resp.status_code == 200:
        for item in data.get("results", []):
            part_num = (item.get("part") or {}).get("part_num")
            color_id = (item.get("color") or {}).get("id")
            img_url = _part_color_img_url(part_num, color_id)
            if img_url:
                item["_accurate_img_url"] = img_url
    return jsonify(data), resp.status_code


@app.route("/api/partlists/<int:list_id>/parts_all")
def get_partlist_parts_all(list_id):
    """Flat, lightweight dump of an ENTIRE parts list for client-side search.

    Pages through Rebrickable (throttled via rebrickable_get). For each entry the
    color-specific image is pulled from the local catalog (`part_colors`, ~94%
    coverage, zero API calls), falling back to the generic part_img_url when the
    combo has no local image. Loading the whole list stays cheap — just the paged
    list calls plus a couple of batched local lookups, no per-part API fan-out.
    Powers the live search box in the Lists view, where the full set must be in
    memory to filter as the user types.
    """
    out = []
    page = 1
    while page <= 200:  # safety cap (~20k parts at page_size 100)
        resp = rebrickable_get(
            f"/users/{USER_TOKEN}/partlists/{list_id}/parts/",
            params={"key": API_KEY, "page_size": 100, "page": page},
        )
        if resp is None or resp.status_code != 200:
            if page == 1:
                return jsonify({"error": "Couldn't fetch list parts", "results": []}), \
                    (resp.status_code if resp is not None else 502)
            break  # partial list is better than none
        data = resp.json()
        for it in data.get("results", []):
            part = it.get("part") or {}
            color = it.get("color") or {}
            out.append({
                "part_num": part.get("part_num"),
                "name": part.get("name"),
                "img_url": part.get("part_img_url"),  # generic fallback
                "color_id": color.get("id"),
                "color_name": color.get("name"),
                "rgb": color.get("rgb"),
                "quantity": it.get("quantity") or 1,
            })
        if not data.get("next"):
            break
        page += 1

    # Overlay color-specific images from the local catalog (one batched query).
    conn = local_db()
    if conn is not None:
        try:
            imgs = _local_part_color_imgs(conn, [(p["part_num"], p["color_id"]) for p in out])
            for p in out:
                if p["color_id"] is None:
                    continue
                local_img = imgs.get((p["part_num"], int(p["color_id"])))
                if local_img:
                    p["img_url"] = local_img
        finally:
            conn.close()

    return jsonify({"results": out, "count": len(out)})


def _rb_part_to_bl(conn, part_num):
    """Rebrickable part_num → BrickLink item id (reverse of bl_aliases). Falls
    back to the part_num itself (identical for most standard parts)."""
    if conn is not None and part_num:
        try:
            row = conn.execute(
                "SELECT bl_id FROM bl_aliases WHERE part_num = ? ORDER BY length(bl_id), bl_id LIMIT 1",
                (part_num,),
            ).fetchone()
            if row:
                return row[0]
        except sqlite3.OperationalError:
            pass
    return part_num


def _rb_color_to_bl(conn, color_id):
    """Rebrickable color id → BrickLink color id (bl_colors), or None if unmapped."""
    if conn is not None and color_id is not None:
        try:
            row = conn.execute("SELECT bl_id FROM bl_colors WHERE rb_id = ?", (color_id,)).fetchone()
            if row:
                return row[0]
        except sqlite3.OperationalError:
            pass
    return None


@app.route("/api/partlists/<int:list_id>/bricklink_wanted")
def export_bricklink_wanted(list_id):
    """Export a parts list as a BrickLink Wanted List XML (upload format).

    Translates each Rebrickable part_num → BrickLink item id (bl_aliases) and
    color id → BrickLink color id (bl_colors). Returns {xml, item_count,
    total_qty, unmapped_colors}. Parts whose color has no BrickLink mapping are
    included without a <COLOR> (BrickLink treats as any color) and counted.
    """
    from xml.sax.saxutils import escape
    conn = local_db()
    try:
        items = []
        page = 1
        while page <= 200:  # safety cap (~20k parts at page_size 100)
            resp = rebrickable_get(
                f"/users/{USER_TOKEN}/partlists/{list_id}/parts/",
                params={"key": API_KEY, "page_size": 100, "page": page},
            )
            if resp is None or resp.status_code != 200:
                return jsonify({"error": "Couldn't fetch list parts from Rebrickable"}), 502
            data = resp.json()
            for it in data.get("results", []):
                items.append((
                    (it.get("part") or {}).get("part_num"),
                    (it.get("color") or {}).get("id"),
                    it.get("quantity") or 1,
                ))
            if not data.get("next"):
                break
            page += 1

        lines = ["<INVENTORY>"]
        total_qty = 0
        unmapped_colors = 0
        for part_num, color_id, qty in items:
            if not part_num:
                continue
            bl_item = _rb_part_to_bl(conn, part_num)
            bl_color = _rb_color_to_bl(conn, color_id)
            total_qty += int(qty)
            lines.append("  <ITEM>")
            lines.append("    <ITEMTYPE>P</ITEMTYPE>")
            lines.append(f"    <ITEMID>{escape(str(bl_item))}</ITEMID>")
            if bl_color is not None:
                lines.append(f"    <COLOR>{int(bl_color)}</COLOR>")
            else:
                unmapped_colors += 1
            lines.append(f"    <MINQTY>{int(qty)}</MINQTY>")
            lines.append("  </ITEM>")
        lines.append("</INVENTORY>")

        return jsonify({
            "xml": "\n".join(lines),
            "item_count": len([i for i in items if i[0]]),
            "total_qty": total_qty,
            "unmapped_colors": unmapped_colors,
        })
    finally:
        if conn is not None:
            conn.close()


def _part_color_img_url(part_num, color_id):
    if not part_num or color_id is None:
        return None
    cache_key = (part_num, int(color_id))
    if cache_key in PART_COLOR_IMAGE_CACHE:
        return PART_COLOR_IMAGE_CACHE[cache_key]

    img_url = None
    try:
        resp = requests.get(
            f"{RB_BASE}/lego/parts/{part_num}/colors/",
            params={"key": API_KEY, "page_size": 100},
            timeout=5,
        )
        if resp.status_code == 200:
            for color in resp.json().get("results", []):
                if color.get("color_id") == int(color_id):
                    img_url = color.get("part_img_url")
                    break
    except requests.exceptions.RequestException:
        img_url = None

    PART_COLOR_IMAGE_CACHE[cache_key] = img_url
    return img_url


@app.route("/api/partlists/<int:list_id>/parts/<part_num>/<int:color_id>")
def get_partlist_part(list_id, part_num, color_id):
    resp = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/{part_num}/{color_id}/",
        params={"key": API_KEY},
    )
    if resp.status_code == 404:
        return jsonify({"quantity": 0, "_exists": False}), 200
    data = resp.json()
    data["_exists"] = True
    return jsonify(data), resp.status_code


@app.route("/api/part/<part_num>")
def get_part(part_num):
    resp = rebrickable_get(
        f"/lego/parts/{part_num}/",
        params={"key": API_KEY},
    )
    if resp is None:
        return jsonify({"error": "Failed to fetch part"}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/part_colors/<part_num>")
def get_part_colors(part_num):
    # Prefer the offline catalog (no API quota); fall back to the live API.
    # Empty result → part isn't in any local inventory, so try the API instead.
    local = _local_part_colors(part_num)
    if local:
        return jsonify({"count": len(local), "results": local}), 200
    resp = rebrickable_get(
        f"/lego/parts/{part_num}/colors/",
        params={"key": API_KEY, "page_size": 100},
    )
    if resp is None:
        return jsonify({"error": "Failed to fetch part colors", "results": []}), 503
    return jsonify(resp.json()), resp.status_code


@app.route("/api/part_in_lists/<part_num>/<int:color_id>")
def get_part_in_lists(part_num, color_id):
    """Fetch all lists containing a specific part/color with quantities."""
    try:
        # Get all user's lists
        lists_resp = requests.get(
            f"{RB_BASE}/users/{USER_TOKEN}/partlists/",
            params={"key": API_KEY},
        )
        lists_data = lists_resp.json()
        lists = lists_data.get("results", [])

        # For each list, check if the part exists
        lists_with_part = []
        for lst in lists:
            part_resp = requests.get(
                f"{RB_BASE}/users/{USER_TOKEN}/partlists/{lst['id']}/parts/{part_num}/{color_id}/",
                params={"key": API_KEY},
                timeout=5,
            )
            if part_resp.status_code == 200:
                part_data = part_resp.json()
                lists_with_part.append({
                    "list_id": lst["id"],
                    "list_name": lst["name"],
                    "quantity": part_data.get("quantity", 0)
                })

        return jsonify({"results": lists_with_part}), 200
    except Exception as e:
        print(f"Error fetching part in lists: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@app.route("/api/add_part", methods=["POST"])
def add_part():
    data = request.json
    list_id = data["list_id"]
    part_num = data["part_num"]
    color_id = data["color_id"]
    quantity = int(data["quantity"])

    # Check if this part+color already exists: GET /parts/{part_num}/{color_id}/
    existing = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/{part_num}/{color_id}/",
        params={"key": API_KEY},
    )

    print(f"[add_part] list={list_id} part={part_num} color={color_id} qty={quantity}")

    if existing.status_code == 200:
        current_qty = existing.json().get("quantity", 0)
        new_qty = current_qty + quantity
        resp = requests.put(
            f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/{part_num}/{color_id}/",
            params={"key": API_KEY},
            data={"quantity": new_qty},
        )
        print(f"[add_part] PUT {resp.status_code}: {resp.text[:200]}")
        result = resp.json()
        result["_updated"] = True
        result["_previous_quantity"] = current_qty
        return jsonify(result), resp.status_code
    else:
        resp = requests.post(
            f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/",
            params={"key": API_KEY},
            data={"part_num": part_num, "color_id": color_id, "quantity": quantity},
        )
        print(f"[add_part] POST {resp.status_code}: {resp.text[:200]}")
        return jsonify(resp.json()), resp.status_code


@app.route("/api/remove_part_one", methods=["POST"])
def remove_part_one():
    data = request.json
    list_id = data["list_id"]
    part_num = data["part_num"]
    color_id = data["color_id"]

    item_url = f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/{part_num}/{color_id}/"
    existing = requests.get(item_url, params={"key": API_KEY})

    print(f"[remove_part_one] list={list_id} part={part_num} color={color_id}")

    if existing.status_code == 404:
        return jsonify({"error": "Part is not in this list.", "_previous_quantity": 0}), 404
    if existing.status_code != 200:
        return jsonify(existing.json()), existing.status_code

    current_qty = int(existing.json().get("quantity", 0))
    if current_qty <= 1:
        resp = requests.delete(item_url, params={"key": API_KEY})
        if resp.status_code == 204:
            return jsonify({
                "_deleted": True,
                "_previous_quantity": current_qty,
                "quantity": 0,
            }), 200
        return jsonify(resp.json()), resp.status_code

    new_qty = current_qty - 1
    resp = requests.put(item_url, params={"key": API_KEY}, data={"quantity": new_qty})
    if resp.status_code == 200:
        result = resp.json()
        result["_updated"] = True
        result["_previous_quantity"] = current_qty
        result["quantity"] = new_qty
        return jsonify(result), 200
    return jsonify(resp.json()), resp.status_code


@app.route("/api/partlists/<int:list_id>", methods=["DELETE"])
def delete_partlist(list_id):
    resp = requests.delete(
        f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/",
        params={"key": API_KEY},
    )
    if resp.status_code == 204:
        return '', 204
    return jsonify(resp.json()), resp.status_code


@app.route("/api/partlists", methods=["POST"])
def create_partlist():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    resp = requests.post(
        f"{RB_BASE}/users/{USER_TOKEN}/partlists/",
        params={"key": API_KEY},
        data={"name": name},
    )
    return jsonify(resp.json()), resp.status_code


@app.route("/api/minifig_sets/<set_num>")
def get_minifig_sets(set_num):
    # Prefer the offline catalog (no API quota); fall back to the live API.
    local = _local_minifig_sets(set_num)
    if local:
        return jsonify({"count": len(local), "results": local}), 200
    resp = requests.get(
        f"{RB_BASE}/lego/minifigs/{set_num}/sets/",
        params={"key": API_KEY, "page_size": 30},
    )
    return jsonify(resp.json()), resp.status_code


@app.route("/api/minifig/<minifig_id>")
def get_minifig(minifig_id):
    is_rebrickable_format = minifig_id.startswith('fig-')
    bl_error_status = None

    def try_bricklink(fig_id):
        nonlocal bl_error_status
        try:
            auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
            resp = requests.get(
                f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{fig_id}",
                auth=auth, timeout=8
            )
            bl_error_status = resp.status_code
            if resp.status_code == 200:
                bl_data = resp.json()
                if 'data' in bl_data:
                    item = bl_data['data']
                    img_url = item.get('image_url', '')
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    return {
                        'fig_num': item.get('no'),
                        'name': item.get('name'),
                        'fig_img_url': img_url,
                        'external_id': item.get('no'),
                        'source': 'bricklink'
                    }
            else:
                print(f"BrickLink {resp.status_code} for {fig_id}: {resp.text[:200]}", file=sys.stderr)
        except Exception as e:
            print(f"BrickLink error for {fig_id}: {e}", file=sys.stderr)
        return None

    if not is_rebrickable_format:
        result = try_bricklink(minifig_id)
        if result:
            return jsonify(result), 200

    # Try Rebrickable (primary for fig- format, fallback for BL format)
    try:
        resp = requests.get(
            f"{RB_BASE}/lego/minifigs/{minifig_id}/",
            params={"key": API_KEY},
        )
        if resp.status_code == 200:
            return jsonify(resp.json()), 200
    except Exception as e:
        print(f"Rebrickable lookup error: {e}")

    if is_rebrickable_format:
        result = try_bricklink(minifig_id)
        if result:
            return jsonify(result), 200

    # Fallback for BL-format IDs: return partial data using direct image URL
    # (works even when BrickLink API is blocked, e.g. on cloud hosting)
    if not is_rebrickable_format:
        img_url = f"https://img.bricklink.com/ML/{minifig_id}.jpg"
        print(f"BrickLink API unavailable (status={bl_error_status}), using image fallback for {minifig_id}", file=sys.stderr)
        return jsonify({
            'fig_num': minifig_id,
            'name': minifig_id,
            'fig_img_url': img_url,
            'external_id': minifig_id,
            'source': 'fallback'
        }), 200

    return jsonify({"error": "Minifig not found"}), 404


@app.route("/api/minifiglists")
def get_minifiglists():
    # Rebrickable has no separate minifig-lists API — only a single flat
    # collection at /users/{token}/minifigs/. Return a synthetic list so the
    # frontend list-picker works without changes.
    return jsonify({"count": 1, "results": [{"id": 0, "name": "My Minifigs"}]}), 200


@app.route("/api/minifiglists", methods=["POST"])
def create_minifiglist():
    return jsonify({"error": "Rebrickable does not support multiple minifig lists"}), 400


@app.route("/api/minifiglists/<int:list_id>", methods=["DELETE"])
def delete_minifiglist(list_id):
    return jsonify({"error": "Rebrickable does not support multiple minifig lists"}), 400


@app.route("/api/add_minifig", methods=["POST"])
def add_minifig():
    data = request.json
    set_num = data["set_num"]
    quantity = int(data["quantity"])

    print(f"[add_minifig] set_num={set_num} qty={quantity}")

    existing = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/minifigs/{set_num}/",
        params={"key": API_KEY},
    )

    if existing.status_code == 200:
        current_qty = existing.json().get("quantity", 0)
        new_qty = current_qty + quantity
        resp = requests.put(
            f"{RB_BASE}/users/{USER_TOKEN}/minifigs/{set_num}/",
            params={"key": API_KEY},
            data={"quantity": new_qty},
        )
        result = resp.json()
        result["_updated"] = True
        result["_previous_quantity"] = current_qty
        return jsonify(result), resp.status_code
    else:
        resp = requests.post(
            f"{RB_BASE}/users/{USER_TOKEN}/minifigs/",
            params={"key": API_KEY},
            data={"set_num": set_num, "quantity": quantity},
        )
        return jsonify(resp.json()), resp.status_code


def _bl_sold_price(item_type, item_no):
    """BrickLink last-6-months SOLD price guide for an item, both Used and New.
    item_type: MINIFIG | SET | PART. Returns {"U": {...}, "N": {...}} where each
    value is BrickLink's price 'data' (avg_price/min_price/max_price/unit_quantity
    /qty_avg_price). Empty/partial if BrickLink is unavailable (e.g. on cloud)."""
    out = {}
    if not (BL_CONSUMER_KEY and BL_TOKEN):
        return out
    auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
    for cond in ("U", "N"):
        try:
            r = requests.get(
                f"{BL_BASE}/items/{item_type}/{item_no}/price",
                params={"guide_type": "sold", "new_or_used": cond, "currency_code": "USD"},
                auth=auth, timeout=8,
            )
            if r.status_code == 200:
                out[cond] = r.json().get("data", {})
            else:
                print(f"[BL price] {item_type} {item_no} {cond} → {r.status_code}", file=sys.stderr)
        except Exception as e:
            print(f"[BL price] error: {e}", file=sys.stderr)
    return out


@app.route("/api/minifig_price/<fig_id>")
def get_minifig_price(fig_id):
    theme_map = {
        'sw': 'Star Wars', 'hp': 'Harry Potter', 'lor': 'Lord of the Rings',
        'loz': 'Legend of Zelda', 'dim': 'Dimensions', 'cmf': 'Collectible Minifigure',
        'coltlm': 'The LEGO Movie', 'colsh': 'Super Heroes',
        'col': 'Collectible Series', 'pm': 'Pirates of the Caribbean', 'njo': 'Ninjago',
    }
    prefix = (re.match(r'^([a-z]+)', fig_id.lower()) or re.match(r'', '')).group(0)
    results = {"category": theme_map.get(prefix, 'Minifigure')}
    results.update(_bl_sold_price("MINIFIG", fig_id))
    return jsonify(results)


@app.route("/api/set_price/<set_num>")
def get_set_price(set_num):
    """BrickLink last-6-months sold price (Used + New) for a set. BrickLink set
    ids carry the variant suffix (e.g. 75300-1), matching Rebrickable's set_num;
    a bare number defaults to '-1'."""
    bl_no = set_num if "-" in set_num else f"{set_num}-1"
    return jsonify(_bl_sold_price("SET", bl_no))


@app.route("/api/minifig_parts/<minifig_id>")
def get_minifig_parts(minifig_id):
    """Fetch parts that make up a minifigure (offline catalog first, API fallback)."""
    local = _local_minifig_parts(minifig_id)
    if local:
        return jsonify({"count": len(local), "results": local})
    try:
        parts_resp = requests.get(
            f"{RB_BASE}/lego/minifigs/{minifig_id}/parts/",
            params={"key": API_KEY},
            timeout=8,
        )
        if parts_resp.status_code == 200:
            return jsonify(parts_resp.json())
        else:
            return jsonify({"error": "Unable to fetch minifigure parts", "count": 0, "results": []}), 404
    except Exception as e:
        print(f"Error fetching minifig parts: {e}")
        return jsonify({"error": str(e), "count": 0, "results": []}), 500


def _api_search_fallback(kind, query, limit):
    """Search the live Rebrickable API when the offline DB is unavailable.

    Returns results in the same shape as the offline search so the frontend
    renders them identically (just with source='api'). Costs API quota.
    """
    if kind == "minifigs":
        resp = rebrickable_get("/lego/minifigs/", params={
            "key": API_KEY, "search": query, "page_size": limit,
        })
        rows = (resp.json().get("results", []) if resp and resp.status_code == 200 else [])
        return [{
            "type": "minifig",
            "fig_num": r.get("set_num"),       # Rebrickable uses set_num for fig id
            "name": r.get("set_name"),
            "num_parts": r.get("num_parts"),
            "img_url": r.get("set_img_url"),
        } for r in rows]

    if kind == "sets":
        resp = rebrickable_get("/lego/sets/", params={
            "key": API_KEY, "search": query, "page_size": limit, "ordering": "-year",
        })
        rows = (resp.json().get("results", []) if resp and resp.status_code == 200 else [])
        return [{
            "type": "set",
            "set_num": r.get("set_num"),
            "name": r.get("name"),
            "year": r.get("year"),
            "part_count": r.get("num_parts"),
            "theme": None,
            "img_url": r.get("set_img_url"),
        } for r in rows]

    # parts
    resp = rebrickable_get("/lego/parts/", params={
        "key": API_KEY, "search": query, "page_size": limit,
    })
    rows = (resp.json().get("results", []) if resp and resp.status_code == 200 else [])
    return [{
        "type": "part",
        "part_num": r.get("part_num"),
        "name": r.get("name"),
        "img_url": r.get("part_img_url"),
        "category": None,
    } for r in rows]


@app.route("/api/resolve_part/<part_id>")
def resolve_part(part_id):
    """Resolve a BrickLink (or Rebrickable) part id to a Rebrickable part via the
    local catalog: exact match → bl_aliases (authoritative BrickLink map) → mold
    heuristic. Used by the voice-add flow, where users speak BrickLink numbers
    (e.g. "3068" → 3068b). Returns {part_num, name, img_url} or 404."""
    p = _local_resolve_part(part_id)
    if p:
        return jsonify(p)
    return jsonify({"error": "not found"}), 404


@app.route("/api/local/search")
def local_search():
    """Catalog search. Prefers the offline SQLite DB (no Rebrickable quota);
    falls back to the live Rebrickable API if the DB is absent.

    Query params:
      q     — search term (matches name or catalog number)
      type  — 'parts' | 'minifigs' | 'sets'  (default 'parts')
      limit — max results (default 30, capped 100)

    Response includes "source": "offline" | "api" so the UI can show which
    data source served the results.
    """
    query = request.args.get("q", "").strip()
    kind = request.args.get("type", "parts").strip().lower()
    try:
        limit = min(int(request.args.get("limit", 30)), 100)
    except (TypeError, ValueError):
        limit = 30

    if not query:
        return jsonify({"error": "Please enter a search term", "results": []}), 400

    conn = local_db()
    if conn is None:
        # No offline catalog → fall back to the live Rebrickable API.
        try:
            results = _api_search_fallback(kind, query, limit)
            return jsonify({"results": results, "count": len(results), "source": "api"})
        except Exception as e:
            print(f"Error in API search fallback: {e}")
            return jsonify({"error": str(e), "results": [], "source": "api"}), 500

    like = f"%{query}%"
    prefix = f"{query}%"
    bl_match = None  # set when a BrickLink minifig id was translated to a name
    try:
        if kind == "minifigs":
            rows = conn.execute(
                """
                SELECT fig_num, name, num_parts, img_url FROM minifigs
                WHERE fig_num = :q OR fig_num LIKE :prefix OR name LIKE :like
                ORDER BY
                  CASE WHEN fig_num = :q THEN 0
                       WHEN fig_num LIKE :prefix THEN 1
                       WHEN name LIKE :prefix THEN 2
                       ELSE 3 END,
                  name
                LIMIT :limit
                """,
                {"q": query, "prefix": prefix, "like": like, "limit": limit},
            ).fetchall()
            results = [{
                "type": "minifig",
                "fig_num": r["fig_num"],
                "name": r["name"],
                "num_parts": r["num_parts"],
                "img_url": r["img_url"],
            } for r in rows]

            # BrickLink minifig id (e.g. sw0131) with no local hit: Rebrickable has
            # no BrickLink minifig ids, so translate the id → name via BrickLink and
            # surface the best-matching Rebrickable figs as candidates to choose from.
            if not results and re.match(r'^[a-z]{1,4}\d{2,5}[a-z]?$', query.lower()):
                bl_name = _bricklink_minifig_name(query)
                if bl_name:
                    bl_match = {"id": query, "name": bl_name}
                    rows2 = _local_minifig_search_by_name(bl_name, limit)
                    results = [{
                        "type": "minifig",
                        "fig_num": r["fig_num"],
                        "name": r["name"],
                        "num_parts": r["num_parts"],
                        "img_url": r["img_url"],
                    } for r in rows2]

        elif kind == "sets":
            rows = conn.execute(
                """
                SELECT s.set_num, s.name, s.year, s.num_parts, s.img_url, t.name AS theme
                FROM sets s LEFT JOIN themes t ON t.id = s.theme_id
                WHERE s.set_num = :q OR s.set_num LIKE :prefix OR s.name LIKE :like
                ORDER BY
                  CASE WHEN s.set_num = :q THEN 0
                       WHEN s.set_num LIKE :prefix THEN 1
                       WHEN s.name LIKE :prefix THEN 2
                       ELSE 3 END,
                  s.year DESC
                LIMIT :limit
                """,
                {"q": query, "prefix": prefix, "like": like, "limit": limit},
            ).fetchall()
            results = [{
                "type": "set",
                "set_num": r["set_num"],
                "name": r["name"],
                "year": r["year"],
                "part_count": r["num_parts"],
                "theme": r["theme"],
                "img_url": r["img_url"],
            } for r in rows]

        else:  # parts
            rows = conn.execute(
                """
                SELECT p.part_num, p.name, p.img_url, c.name AS category
                FROM parts p LEFT JOIN part_categories c ON c.id = p.part_cat_id
                WHERE p.part_num = :q OR p.part_num LIKE :prefix OR p.name LIKE :like
                ORDER BY
                  CASE WHEN p.part_num = :q THEN 0
                       WHEN p.part_num LIKE :prefix THEN 1
                       WHEN p.name LIKE :prefix THEN 2
                       ELSE 3 END,
                  p.name
                LIMIT :limit
                """,
                {"q": query, "prefix": prefix, "like": like, "limit": limit},
            ).fetchall()
            results = [{
                "type": "part",
                "part_num": r["part_num"],
                "name": r["name"],
                "img_url": r["img_url"],
                "category": r["category"],
            } for r in rows]

        resp = {"results": results, "count": len(results), "source": "offline"}
        if bl_match:
            resp["bl_match"] = bl_match
        return jsonify(resp)
    except Exception as e:
        print(f"Error in local_search: {e}")
        return jsonify({"error": str(e), "results": []}), 500
    finally:
        conn.close()


@app.route("/api/search_sets")
def search_sets():
    """Search for LEGO sets by number or keyword from Rebrickable API."""
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "Please enter a search term", "results": []}), 400

    try:
        # Search by set number or keyword
        sets_resp = requests.get(
            f"{RB_BASE}/lego/sets/",
            params={
                "key": API_KEY,
                "search": query,
                "page_size": 20,
                "ordering": "-year"
            },
            timeout=8,
        )

        if sets_resp.status_code == 200:
            data = sets_resp.json()
            results = data.get("results", [])

            # Format results for frontend
            formatted = []
            for set_info in results:
                formatted.append({
                    "set_num": set_info.get("set_num"),
                    "name": set_info.get("name"),
                    "year": set_info.get("year"),
                    "image_url": set_info.get("set_img_url"),
                    "part_count": set_info.get("num_parts"),
                    "theme": set_info.get("theme", {}).get("id") if set_info.get("theme") else None
                })

            return jsonify({"results": formatted, "count": len(formatted)})
        else:
            return jsonify({"error": f"Search failed: {sets_resp.status_code}", "results": []}), sets_resp.status_code
    except Exception as e:
        print(f"Error searching sets: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@app.route("/api/sets/<set_num>/parts")
def get_set_parts(set_num):
    """Fetch all parts in a specific LEGO set from Rebrickable API."""
    try:
        all_parts = []
        page = 1
        while True:
            parts_resp = requests.get(
                f"{RB_BASE}/lego/sets/{set_num}/parts/",
                params={
                    "key": API_KEY,
                    "page": page,
                    "page_size": 100,
                },
                timeout=8,
            )

            if parts_resp.status_code != 200:
                return jsonify({"error": f"Failed to fetch parts: {parts_resp.status_code}", "results": []}), parts_resp.status_code

            data = parts_resp.json()
            results = data.get("results", [])
            all_parts.extend(results)

            # Check if there are more pages
            if not data.get("next"):
                break
            page += 1

        # Format parts for frontend
        formatted = []
        for part in all_parts:
            formatted.append({
                "part_num": part.get("part", {}).get("part_num"),
                "part_name": part.get("part", {}).get("name"),
                "part_img_url": part.get("part", {}).get("part_img_url"),
                "color_id": part.get("color", {}).get("id"),
                "color_name": part.get("color", {}).get("name"),
                "color_rgb": part.get("color", {}).get("rgb"),
                "quantity": part.get("quantity", 0),
                "is_spare": part.get("is_spare", False)
            })

        return jsonify({"results": formatted, "count": len(formatted)})
    except Exception as e:
        print(f"Error fetching set parts: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@app.route("/api/sets/<set_num>/minifigs")
def get_set_minifigs(set_num):
    """Fetch all minifigs in a specific LEGO set from Rebrickable API."""
    try:
        all_figs = []
        page = 1
        while True:
            figs_resp = requests.get(
                f"{RB_BASE}/lego/sets/{set_num}/minifigs/",
                params={
                    "key": API_KEY,
                    "page": page,
                    "page_size": 100,
                },
                timeout=8,
            )

            if figs_resp.status_code != 200:
                return jsonify({"error": f"Failed to fetch minifigs: {figs_resp.status_code}", "results": []}), figs_resp.status_code

            data = figs_resp.json()
            results = data.get("results", [])
            all_figs.extend(results)

            # Check if there are more pages
            if not data.get("next"):
                break
            page += 1

        # Format minifigs for frontend
        formatted = []
        for fig in all_figs:
            formatted.append({
                "fig_num": fig.get("set_num"),  # Rebrickable uses "set_num" for fig ID
                "fig_name": fig.get("set_name"),  # Rebrickable uses "set_name" for minifig name
                "fig_img_url": fig.get("set_img_url"),  # Rebrickable uses "set_img_url" for image
                "quantity": fig.get("quantity", 0)
            })

        return jsonify({"results": formatted, "count": len(formatted)})
    except Exception as e:
        print(f"Error fetching set minifigs: {e}")
        return jsonify({"error": str(e), "results": []}), 500


@app.route("/api/import-csv", methods=["POST"])
def import_csv():
    """Import parts from CSV file: part_num, color, quantity"""
    try:
        import csv
        import io

        list_id = request.form.get("list_id")
        if not list_id:
            return jsonify({"error": "list_id is required"}), 400

        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400

        file = request.files["file"]
        if file.filename == "":
            return jsonify({"error": "No file selected"}), 400

        # Read and parse CSV
        stream = io.StringIO(file.stream.read().decode("UTF-8"), newline=None)
        csv_data = csv.DictReader(stream)

        # Fetch all colors for lookup (use cached colors to reduce API calls)
        import difflib
        colors = []
        color_name_to_id = {}
        color_names_list = []

        # Use cached colors first, only fetch if cache empty/expired
        import time
        now = time.time()
        if COLORS_CACHE["data"] and (now - COLORS_CACHE["timestamp"]) < COLORS_CACHE_DURATION:
            colors = COLORS_CACHE["data"]
            print(f"✓ Using cached colors ({len(colors)} colors)")
        else:
            # Fetch from Rebrickable only (to avoid rate limits)
            try:
                url = f"{RB_BASE}/lego/colors/"
                page_count = 0
                while url:
                    resp = requests.get(url, params={"key": API_KEY, "page_size": 200}, timeout=10)

                    # Check for rate limit
                    if resp.status_code == 429:
                        print("⚠ Rate limited (429), using fallback colors")
                        colors = FALLBACK_COLORS
                        break

                    resp.raise_for_status()
                    data = resp.json()
                    colors.extend(data.get("results", []))
                    url = data.get("next")
                    page_count += 1

                if colors:
                    print(f"✓ Fetched {len(colors)} colors from Rebrickable ({page_count} pages)")
                    # Cache the result
                    COLORS_CACHE["data"] = colors
                    COLORS_CACHE["timestamp"] = now
            except Exception as e:
                print(f"⚠ Rebrickable colors error: {e}, using fallback")
                colors = FALLBACK_COLORS

        # If still no colors, use fallback
        if len(colors) == 0:
            colors = FALLBACK_COLORS
            print(f"⚠ Using fallback color list ({len(colors)} colors)")

        # Build color lookup maps
        for c in colors:
            color_name_to_id[c["name"].lower()] = c["id"]
            color_names_list.append(c["name"])

        # Helper function for fuzzy color matching
        def resolve_color(color_input):
            """Resolve color name with fuzzy matching"""
            if not color_input:
                return None

            color_lower = color_input.lower()

            # 1. Try exact match (case-insensitive)
            if color_lower in color_name_to_id:
                return color_name_to_id[color_lower]

            # 2. Try variations: add/remove hyphens for Trans colors
            if "trans" in color_lower:
                # Try replacing spaces with hyphens
                variant = color_lower.replace(" ", "-")
                if variant in color_name_to_id:
                    return color_name_to_id[variant]
                # Try replacing hyphens with spaces
                variant = color_lower.replace("-", " ")
                if variant in color_name_to_id:
                    return color_name_to_id[variant]

            # 3. Try closest string match (difflib)
            matches = difflib.get_close_matches(color_input, color_names_list, n=1, cutoff=0.75)
            if matches:
                matched_color = matches[0]
                return color_name_to_id.get(matched_color.lower())

            return None

        results = {
            "imported": 0,
            "failed": 0,
            "errors": []
        }

        for row in csv_data:
            try:
                # Normalize column names to lowercase for case-insensitive matching
                row_lower = {k.lower(): v for k, v in row.items()}

                part_num = row_lower.get("part_num", "").strip()
                color_name = row_lower.get("color", "").strip()
                quantity = int(row_lower.get("quantity", 1))

                if not part_num:
                    results["failed"] += 1
                    results["errors"].append("Missing part_num in row")
                    continue

                # Resolve color name to ID (with fuzzy matching)
                color_id = resolve_color(color_name)
                if not color_id:
                    results["failed"] += 1
                    results["errors"].append(f"Unknown color '{color_name}' for part {part_num}")
                    continue

                # Add part using existing add_part logic
                existing = requests.get(
                    f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/{part_num}/{color_id}/",
                    params={"key": API_KEY},
                )

                if existing.status_code == 200:
                    # Update existing
                    current_qty = existing.json().get("quantity", 0)
                    new_qty = current_qty + quantity
                    requests.put(
                        f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/{part_num}/{color_id}/",
                        params={"key": API_KEY},
                        data={"quantity": new_qty},
                    )
                else:
                    # Create new
                    requests.post(
                        f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/",
                        params={"key": API_KEY},
                        data={"part_num": part_num, "color_id": color_id, "quantity": quantity},
                    )

                results["imported"] += 1

            except Exception as e:
                results["failed"] += 1
                results["errors"].append(f"Error importing row: {str(e)}")

        return jsonify(results)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    try:
        local_ip = socket.gethostbyname(hostname)
    except socket.gaierror:
        local_ip = "127.0.0.1"
    print(f"\n  Open on your phone: http://{local_ip}:5000\n")
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
