from flask import Flask, render_template, request, jsonify
import os
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

API_KEY = os.environ.get("REBRICKABLE_API_KEY", "")
USER_TOKEN = os.environ.get("REBRICKABLE_USER_TOKEN", "")
RB_BASE = "https://rebrickable.com/api/v3"


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
            bl_id = raw_id.replace("part-", "").replace("minifig-", "")
            ext = next((e for e in ci.get("external_items", [])
                        if e.get("catalog_name") == "bricklink"), {})
            bl_external_id = ext.get("external_id", bl_id)

            # Resolve BrickLink ID → Rebrickable part_num (they differ, e.g. 3070 → 3070b)
            rb_part_num = bl_id
            try:
                rb_resp = requests.get(
                    f"{RB_BASE}/lego/parts/",
                    params={"key": API_KEY, "bricklink_id": bl_external_id},
                    timeout=5,
                )
                if rb_resp.status_code == 200:
                    results = rb_resp.json().get("results", [])
                    if results:
                        rb_part_num = results[0]["part_num"]
            except Exception:
                pass

            # Build candidate_colors with Rebrickable IDs
            rb_colors = []
            for c in ci.get("candidate_colors", []):
                rb_id = _brk_color_id(c.get("id", ""))
                if rb_id:
                    rb_colors.append({"id": rb_id, "name": c.get("name", "")})

            item = {
                "id": rb_part_num,
                "name": ci.get("name", ""),
                "img_url": f"https://storage.googleapis.com/brickognize-static/thumbnails/v2.22/part/{bl_id}/0.webp",
                "external_sites": [{"name": "bricklink", "url": ext.get("url", "")}] if ext else [],
                "type": ci.get("type", "part"),
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


if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print(f"\n  Open on your phone: http://{local_ip}:5000\n")
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
