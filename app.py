from flask import Flask, render_template, request, jsonify
import os
import re
import sys
import requests
import time
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

# ── Rate Limiter for Rebrickable API (60 req/min = 1 req/sec) ──────────────────
RB_RATE_LIMITER = {
    "last_request_time": 0,
    "min_interval": 1.0,  # 1 second minimum between requests (60 req/min)
    "requests_made": 0,
    "reset_time": time.time() + 60
}

def throttle_rebrickable_request():
    """Enforce rate limiting: max 1 request per second to Rebrickable"""
    global RB_RATE_LIMITER

    now = time.time()

    # Reset counter every minute
    if now > RB_RATE_LIMITER["reset_time"]:
        RB_RATE_LIMITER["requests_made"] = 0
        RB_RATE_LIMITER["reset_time"] = now + 60

    # Calculate time to wait
    time_since_last = now - RB_RATE_LIMITER["last_request_time"]
    wait_time = max(0, RB_RATE_LIMITER["min_interval"] - time_since_last)

    if wait_time > 0:
        print(f"⏳ Rate limit: waiting {wait_time:.2f}s before next Rebrickable request ({RB_RATE_LIMITER['requests_made']}/60 requests used)")
        time.sleep(wait_time)

    RB_RATE_LIMITER["last_request_time"] = time.time()
    RB_RATE_LIMITER["requests_made"] += 1

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
                try:
                    # Rebrickable doesn't support bricklink_id filtering for minifigs.
                    # Strip color/variant suffix (after " - " or "(") for a cleaner search term,
                    # then pick the result with the most word overlap against the full name.
                    name = ci.get("name", "")
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
                img_url = ""
                if rb_part and rb_part.get("part_img_url"):
                    img_url = rb_part["part_img_url"]
                else:
                    img_url = f"https://img.bricklink.com/ItemImage/PN/0/{bl_external_id}.png"

            # Build candidate_colors with Rebrickable IDs (parts only)
            rb_colors = []
            if item_type != "minifig":
                for c in ci.get("candidate_colors", []):
                    c_rb_id = _brk_color_id(c.get("id", ""))
                    if c_rb_id:
                        rb_colors.append({"id": c_rb_id, "name": c.get("name", "")})

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
    resp = rebrickable_get(
        f"/lego/parts/{part_num}/colors/",
        params={"key": API_KEY, "page_size": 100},
    )
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
    try:
        resp = rebrickable_get(
            f"/users/{USER_TOKEN}/minifiglists/",
            params={"key": API_KEY}
        )
        # If rate limited, preserve the 429 status for frontend to detect
        if resp.status_code in [429, 503]:
            return jsonify({"results": [], "error": "Rate limited or service unavailable"}), resp.status_code
        resp.raise_for_status()
        return jsonify(resp.json()), resp.status_code
    except Exception as e:
        print(f"⚠ Error fetching minifiglists: {e}")
        # Return 503 for other errors so frontend knows something went wrong
        return jsonify({"results": [], "error": str(e)}), 503


@app.route("/api/minifiglists", methods=["POST"])
def create_minifiglist():
    name = (request.json or {}).get("name", "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    resp = requests.post(
        f"{RB_BASE}/users/{USER_TOKEN}/minifiglists/",
        params={"key": API_KEY},
        data={"name": name},
    )
    return jsonify(resp.json()), resp.status_code


@app.route("/api/minifiglists/<int:list_id>", methods=["DELETE"])
def delete_minifiglist(list_id):
    resp = requests.delete(
        f"{RB_BASE}/users/{USER_TOKEN}/minifiglists/{list_id}/",
        params={"key": API_KEY},
    )
    if resp.status_code == 204:
        return '', 204
    return jsonify(resp.json()), resp.status_code


@app.route("/api/add_minifig", methods=["POST"])
def add_minifig():
    data = request.json
    list_id = data["list_id"]
    set_num = data["set_num"]
    quantity = int(data["quantity"])

    existing = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/minifiglists/{list_id}/minifigs/{set_num}/",
        params={"key": API_KEY},
    )

    print(f"[add_minifig] list={list_id} set_num={set_num} qty={quantity}")

    if existing.status_code == 200:
        current_qty = existing.json().get("quantity", 0)
        new_qty = current_qty + quantity
        resp = requests.put(
            f"{RB_BASE}/users/{USER_TOKEN}/minifiglists/{list_id}/minifigs/{set_num}/",
            params={"key": API_KEY},
            data={"quantity": new_qty},
        )
        result = resp.json()
        result["_updated"] = True
        result["_previous_quantity"] = current_qty
        return jsonify(result), resp.status_code
    else:
        resp = requests.post(
            f"{RB_BASE}/users/{USER_TOKEN}/minifiglists/{list_id}/minifigs/",
            params={"key": API_KEY},
            data={"set_num": set_num, "quantity": quantity},
        )
        return jsonify(resp.json()), resp.status_code


@app.route("/api/minifig_price/<fig_id>")
def get_minifig_price(fig_id):
    auth = OAuth1(BL_CONSUMER_KEY, BL_CONSUMER_SECRET, BL_TOKEN, BL_TOKEN_SECRET)
    results = {}
    category = None

    # Extract theme from minifigure ID prefix (e.g., "sw1094" = Star Wars)
    # Common prefixes: sw=Star Wars, hp=Harry Potter, lor=LOTR, dim=Dimensions, cmf=Collectible, etc.
    theme_prefix_match = re.match(r'^([a-z]+)', fig_id.lower() if fig_id else '')
    theme_prefix = theme_prefix_match.group(1) if theme_prefix_match else ''

    theme_map = {
        'sw': 'Star Wars',
        'hp': 'Harry Potter',
        'lor': 'Lord of the Rings',
        'loz': 'Legend of Zelda',
        'dim': 'Dimensions',
        'cmf': 'Collectible Minifigure',
        'coltlm': 'The LEGO Movie',
        'colsh': 'Super Heroes',
        'col': 'Collectible Series',
        'pm': 'Pirates of the Caribbean',
        'njo': 'Ninjago',
    }

    category = theme_map.get(theme_prefix, 'Minifigure')

    # Fetch pricing for each condition
    for condition in ("U", "N"):
        # Get pricing data
        price_resp = requests.get(
            f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{fig_id}/price",
            params={"guide_type": "sold", "new_or_used": condition, "currency_code": "USD"},
            auth=auth,
            timeout=8,
        )
        if price_resp.status_code == 200:
            results[condition] = price_resp.json().get("data", {})

    # Add category to results
    if category:
        results["category"] = category

    return jsonify(results)


@app.route("/api/minifig_parts/<minifig_id>")
def get_minifig_parts(minifig_id):
    """Fetch parts that make up a minifigure from Rebrickable API."""
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
