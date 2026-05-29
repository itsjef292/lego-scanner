#!/usr/bin/env python3
"""Build a local SQLite database from the Rebrickable CSV dump in "Brick Parts/".

This powers the app's OFFLINE search (parts / minifigs / sets) so we don't burn
the 60 req/min Rebrickable API quota just to look something up.

Usage:
    python3 build_brick_db.py            # builds ./brick_parts.db from ./Brick Parts
    python3 build_brick_db.py --src DIR --db PATH

The .db file and the CSV folder are git-ignored — this is a local dev tool. The
app degrades gracefully (offline search disabled) when the DB is absent, so
production is unaffected.
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

# Load .env so a local/nightly build can read REBRICKABLE_API_KEY for the
# BrickLink-alias harvest. On Render the key comes from the environment instead.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Allow very large CSV fields (inventory_parts img_url URLs are long)
csv.field_size_limit(10 * 1024 * 1024)

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SRC = os.path.join(HERE, "Brick Parts")
DEFAULT_DB = os.path.join(HERE, "brick_parts.db")

# table name -> (csv file, [(csv_col, db_col), ...])
# Only the columns we actually use are imported.
TABLES = {
    "colors": ("colors.csv", [
        ("id", "id"), ("name", "name"), ("rgb", "rgb"), ("is_trans", "is_trans"),
    ]),
    "part_categories": ("part_categories.csv", [
        ("id", "id"), ("name", "name"),
    ]),
    "parts": ("parts.csv", [
        ("part_num", "part_num"), ("name", "name"), ("part_cat_id", "part_cat_id"),
    ]),
    "themes": ("themes.csv", [
        ("id", "id"), ("name", "name"), ("parent_id", "parent_id"),
    ]),
    "sets": ("sets.csv", [
        ("set_num", "set_num"), ("name", "name"), ("year", "year"),
        ("theme_id", "theme_id"), ("num_parts", "num_parts"), ("img_url", "img_url"),
    ]),
    "minifigs": ("minifigs.csv", [
        ("fig_num", "fig_num"), ("name", "name"),
        ("num_parts", "num_parts"), ("img_url", "img_url"),
    ]),
    "inventories": ("inventories.csv", [
        ("id", "id"), ("version", "version"), ("set_num", "set_num"),
    ]),
    "inventory_parts": ("inventory_parts.csv", [
        ("inventory_id", "inventory_id"), ("part_num", "part_num"),
        ("color_id", "color_id"), ("quantity", "quantity"), ("img_url", "img_url"),
    ]),
    "inventory_minifigs": ("inventory_minifigs.csv", [
        ("inventory_id", "inventory_id"), ("fig_num", "fig_num"),
        ("quantity", "quantity"),
    ]),
}

SCHEMA = """
DROP TABLE IF EXISTS colors;
CREATE TABLE colors (id INTEGER PRIMARY KEY, name TEXT, rgb TEXT, is_trans TEXT);

DROP TABLE IF EXISTS part_categories;
CREATE TABLE part_categories (id INTEGER PRIMARY KEY, name TEXT);

DROP TABLE IF EXISTS parts;
CREATE TABLE parts (part_num TEXT PRIMARY KEY, name TEXT, part_cat_id INTEGER, img_url TEXT);

DROP TABLE IF EXISTS themes;
CREATE TABLE themes (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER);

DROP TABLE IF EXISTS sets;
CREATE TABLE sets (set_num TEXT PRIMARY KEY, name TEXT, year INTEGER, theme_id INTEGER, num_parts INTEGER, img_url TEXT);

DROP TABLE IF EXISTS minifigs;
CREATE TABLE minifigs (fig_num TEXT PRIMARY KEY, name TEXT, num_parts INTEGER, img_url TEXT);

DROP TABLE IF EXISTS inventories;
CREATE TABLE inventories (id INTEGER PRIMARY KEY, version INTEGER, set_num TEXT);

DROP TABLE IF EXISTS inventory_parts;
CREATE TABLE inventory_parts (inventory_id INTEGER, part_num TEXT, color_id INTEGER, quantity INTEGER, img_url TEXT);

DROP TABLE IF EXISTS inventory_minifigs;
CREATE TABLE inventory_minifigs (inventory_id INTEGER, fig_num TEXT, quantity INTEGER);

DROP TABLE IF EXISTS part_colors;
CREATE TABLE part_colors (part_num TEXT, color_id INTEGER, img_url TEXT, PRIMARY KEY (part_num, color_id));

-- BrickLink part id -> Rebrickable part_num, harvested from Rebrickable's
-- external_ids. A BrickLink id can map to several Rebrickable molds (e.g. BL
-- 3068 -> 3068a/3068b); all rows are kept and resolved at query time by picking
-- the most-common part. Populated by harvest_bl_aliases(); may be empty if the
-- API key is absent or the harvest fails (the app then falls back to its
-- identity/mold heuristic + live API).
DROP TABLE IF EXISTS bl_aliases;
CREATE TABLE bl_aliases (bl_id TEXT, part_num TEXT);
"""


def load_table(conn, src_dir, table, csv_file, cols):
    path = os.path.join(src_dir, csv_file)
    if not os.path.exists(path):
        print(f"  ! skipping {table}: {csv_file} not found")
        return 0
    db_cols = [c[1] for c in cols]
    placeholders = ",".join("?" for _ in db_cols)
    sql = f"INSERT INTO {table} ({','.join(db_cols)}) VALUES ({placeholders})"

    n = 0
    batch = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            batch.append(tuple(row.get(src_col, "") for src_col, _ in cols))
            if len(batch) >= 20000:
                conn.executemany(sql, batch)
                n += len(batch)
                batch = []
    if batch:
        conn.executemany(sql, batch)
        n += len(batch)
    conn.commit()
    return n


def harvest_bl_aliases(conn):
    """Populate bl_aliases (bricklink_id -> rebrickable part_num) from Rebrickable's
    parts list endpoint, which includes external_ids inline (~63 throttled pages,
    ~1-2 min). Graceful: with no REBRICKABLE_API_KEY, no `requests`, or any error,
    it leaves the table empty and returns 0 — the build still succeeds and the app
    falls back to its identity/mold heuristic + live API.
    """
    api_key = os.environ.get("REBRICKABLE_API_KEY")
    if not api_key:
        print("  ! bl_aliases: REBRICKABLE_API_KEY not set — skipping (heuristic fallback remains)")
        return 0
    try:
        import requests
    except ImportError:
        print("  ! bl_aliases: 'requests' unavailable — skipping")
        return 0

    base = "https://rebrickable.com/api/v3/lego/parts/"
    n, page = 0, 1
    try:
        while True:
            r = requests.get(base, params={"key": api_key, "page_size": 1000, "page": page}, timeout=30)
            if r.status_code in (429, 503):       # rate limited / unavailable: back off
                time.sleep(2)
                continue
            r.raise_for_status()
            data = r.json()
            batch = []
            for p in data.get("results", []):
                pn = p.get("part_num")
                for bl in ((p.get("external_ids") or {}).get("BrickLink") or []):
                    if bl and pn:
                        batch.append((bl, pn))
            if batch:
                conn.executemany("INSERT INTO bl_aliases (bl_id, part_num) VALUES (?,?)", batch)
                n += len(batch)
            if not data.get("next"):
                break
            page += 1
            time.sleep(1)                          # ~1 req/sec to respect 60/min
        conn.commit()
        print(f"  harvested bl_aliases   {n:>9,} mappings ({page} pages)")
    except Exception as e:
        print(f"  ! bl_aliases harvest failed ({e}) — continuing with empty table")
        conn.rollback()
    return n


def build(src_dir, db_path):
    if not os.path.isdir(src_dir):
        print(f"ERROR: source folder not found: {src_dir}")
        sys.exit(1)

    if os.path.exists(db_path):
        os.remove(db_path)

    t0 = time.time()
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(SCHEMA)

    print(f"Building {db_path} from {src_dir}")
    for table, (csv_file, cols) in TABLES.items():
        ts = time.time()
        n = load_table(conn, src_dir, table, csv_file, cols)
        print(f"  loaded {table:20s} {n:>9,} rows  ({time.time()-ts:.1f}s)")

    print("Building indexes…")
    conn.executescript("""
        CREATE INDEX idx_parts_name ON parts(name);
        CREATE INDEX idx_minifigs_name ON minifigs(name);
        CREATE INDEX idx_sets_name ON sets(name);
        CREATE INDEX idx_inv_parts_part ON inventory_parts(part_num, color_id);
        CREATE INDEX idx_inv_parts_inv ON inventory_parts(inventory_id);
        CREATE INDEX idx_inv_minifigs_fig ON inventory_minifigs(fig_num);
        CREATE INDEX idx_inventories_set ON inventories(set_num);
    """)
    conn.commit()

    # Harvest the BrickLink->Rebrickable part-id mapping (Rebrickable API).
    print("Harvesting BrickLink aliases…")
    harvest_bl_aliases(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bl_aliases ON bl_aliases(bl_id)")
    conn.commit()

    # Derive distinct part/color combos (+ a representative image per combo)
    print("Deriving part colors…")
    conn.execute("""
        INSERT OR IGNORE INTO part_colors (part_num, color_id, img_url)
        SELECT part_num, color_id,
               MAX(CASE WHEN img_url IS NOT NULL AND img_url != '' THEN img_url END)
        FROM inventory_parts
        WHERE color_id IS NOT NULL
        GROUP BY part_num, color_id
    """)
    conn.commit()

    # Give each part a representative thumbnail (first non-empty color image)
    print("Deriving part thumbnails…")
    conn.execute("""
        UPDATE parts SET img_url = (
            SELECT img_url FROM part_colors
            WHERE part_colors.part_num = parts.part_num
              AND img_url IS NOT NULL AND img_url != ''
            LIMIT 1
        )
    """)
    conn.commit()

    conn.execute("ANALYZE")
    conn.commit()
    conn.close()

    size_mb = os.path.getsize(db_path) / (1024 * 1024)
    print(f"Done in {time.time()-t0:.1f}s — {db_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=DEFAULT_SRC, help="folder with Rebrickable CSVs")
    ap.add_argument("--db", default=DEFAULT_DB, help="output SQLite db path")
    args = ap.parse_args()
    build(args.src, args.db)
