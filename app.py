from flask import Flask, render_template, request, jsonify
import os
import re
import sys
import requests
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
PART_COLOR_IMAGE_CACHE = {}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/partlists")
def get_partlists():
    resp = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/partlists/",
        params={"key": API_KEY},
    )
    return jsonify(resp.json()), resp.status_code


@app.route("/api/colors")
def get_colors():
    all_colors = []
    url = f"{RB_BASE}/lego/colors/"
    while url:
        resp = requests.get(url, params={"key": API_KEY, "page_size": 200})
        data = resp.json()
        all_colors.extend(data.get("results", []))
        url = data.get("next")
    all_colors.sort(key=lambda c: c["name"])
    return jsonify(all_colors)


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
    resp = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/partlists/{list_id}/parts/",
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
    resp = requests.get(
        f"{RB_BASE}/lego/parts/{part_num}/",
        params={"key": API_KEY},
    )
    return jsonify(resp.json()), resp.status_code


@app.route("/api/part_colors/<part_num>")
def get_part_colors(part_num):
    resp = requests.get(
        f"{RB_BASE}/lego/parts/{part_num}/colors/",
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
    # Determine which source to try first
    # If it starts with "fig-", it's a Rebrickable format - try that first
    # Otherwise, assume it's a BrickLink format (e.g., sw1094, col001, M123)

    is_rebrickable_format = minifig_id.startswith('fig-')

    if not is_rebrickable_format:
        # Try BrickLink first for non-Rebrickable formats
        try:
            # Use OAuth to access BrickLink API
            auth = OAuth1(
                BL_CONSUMER_KEY,
                BL_CONSUMER_SECRET,
                BL_TOKEN,
                BL_TOKEN_SECRET
            )

            resp = requests.get(
                f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{minifig_id}",
                auth=auth
            )

            if resp.status_code == 200:
                bl_data = resp.json()
                if 'data' in bl_data:
                    item = bl_data['data']
                    img_url = item.get('image_url', '')
                    # BrickLink returns relative URLs, fix them
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    return jsonify({
                        'fig_num': item.get('no'),
                        'name': item.get('name'),
                        'fig_img_url': img_url,
                        'external_id': item.get('no'),
                        'source': 'bricklink'
                    }), 200
        except Exception as e:
            print(f"BrickLink lookup error for {minifig_id}: {e}", file=sys.stderr)

    # Try Rebrickable (either as primary for fig- format, or as fallback)
    try:
        resp = requests.get(
            f"{RB_BASE}/lego/minifigs/{minifig_id}/",
            params={"key": API_KEY},
        )
        if resp.status_code == 200:
            return jsonify(resp.json()), 200
    except Exception as e:
        print(f"Rebrickable lookup error: {e}")

    # If BrickLink format and Rebrickable didn't work, try BrickLink one more time
    # (in case it's a format we didn't recognize)
    if is_rebrickable_format:
        try:
            auth = OAuth1(
                BL_CONSUMER_KEY,
                BL_CONSUMER_SECRET,
                BL_TOKEN,
                BL_TOKEN_SECRET
            )

            resp = requests.get(
                f"https://api.bricklink.com/api/store/v1/items/MINIFIG/{minifig_id}",
                auth=auth
            )

            if resp.status_code == 200:
                bl_data = resp.json()
                if 'data' in bl_data:
                    item = bl_data['data']
                    img_url = item.get('image_url', '')
                    # BrickLink returns relative URLs, fix them
                    if img_url.startswith('//'):
                        img_url = 'https:' + img_url
                    return jsonify({
                        'fig_num': item.get('no'),
                        'name': item.get('name'),
                        'fig_img_url': img_url,
                        'external_id': item.get('no'),
                        'source': 'bricklink'
                    }), 200
        except Exception as e:
            print(f"BrickLink fallback lookup error for {minifig_id}: {e}", file=sys.stderr)

    return jsonify({"error": "Minifig not found"}), 404


@app.route("/api/minifiglists")
def get_minifiglists():
    resp = requests.get(
        f"{RB_BASE}/users/{USER_TOKEN}/minifiglists/",
        params={"key": API_KEY},
    )
    return jsonify(resp.json()), resp.status_code


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
