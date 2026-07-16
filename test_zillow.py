"""
Zillow Test Run — S005
Tests the full Zillow pipeline end-to-end against test-specific output files.
Does NOT touch state.json or index.html.

Output:
  docs/state_test.json  — isolated test state
  docs/index_test.html  — isolated test HTML

API budget:
  1 Zillow search call
  Up to 3 Zillow detail calls
  Up to 3 Google Maps calls (geocode only on confirmed listings)
  Total RealtyAPI: max 4 calls

Verification targets:
  Search level:  price (plain or dict), beds, baths, location lat/lng, lotSizeWithUnit
  Detail level:  resoFacts.architecturalStyle, basementYN, basement, stories, levels, description
  Pipeline:      ranch+basement filter, geo post-filter, fmt_price, fmt_lot,
                 highway_roads, busy_road, near_highway, merge_into_state, run_date,
                 generate_html
"""

import urllib.request
import urllib.parse
import json
import os
import re
import sys
import math
from datetime import datetime, timezone
from collections import OrderedDict

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY  = os.environ.get("REALTYAPI_KEY", "")
MAPS_KEY = os.environ.get("GOOGLE_MAPS_KEY", "")

if not API_KEY:
    print("ERROR: REALTYAPI_KEY not set.", file=sys.stderr)
    sys.exit(1)

if not MAPS_KEY:
    print("WARNING: GOOGLE_MAPS_KEY not set — Maps enrichment disabled.", file=sys.stderr)

REPO_ROOT   = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR    = os.path.join(REPO_ROOT, "docs")
STATE_FILE  = os.path.join(DOCS_DIR, "state_test.json")   # TEST FILE — not state.json
OUTPUT_FILE = os.path.join(DOCS_DIR, "index_test.html")   # TEST FILE — not index.html

# Single town — Bridgewater only
TEST_AREA = {"name": "Bridgewater", "state": "NJ", "lat": 40.5887, "lng": -74.6040,
             "radius": 10, "zillow_location": "Bridgewater, NJ"}

SEARCH_AREAS = [
    {"name": "Bridgewater", "state": "NJ", "lat": 40.5887, "lng": -74.6040, "radius": 10,
     "zillow_location": "Bridgewater, NJ"},
    {"name": "Somerset",    "state": "NJ", "lat": 40.5007, "lng": -74.4882, "radius": 10,
     "zillow_location": "Somerset, NJ"},
    {"name": "Cranford",    "state": "NJ", "lat": 40.6579, "lng": -74.2982, "radius": 10,
     "zillow_location": "Cranford, NJ"},
]

MIN_BEDS      = 3
MIN_BATHS     = 2.0
MAX_PRICE     = 1_000_000
PROXIMITY_METERS = 402
MAX_DETAIL_CALLS = 3   # cap — stay within budget

HIGHWAY_KEYWORDS = {
    "interstate", "i-", "turnpike", "expressway", "freeway",
    "parkway", "highway", "hwy", "route", "rte", "us-", "nj-"
}

RANCH_KEYWORDS    = {"ranch", "single floor", "one level", "one-level", "1 story",
                     "one story", "one-story", "single story", "single-story",
                     "rambler", "one-floor"}
BASEMENT_KEYWORDS = {"basement", "full basement", "finished basement",
                     "unfinished basement", "partial basement", "walk-out basement",
                     "walkout basement", "walk out basement"}

_api_calls  = 0
_maps_calls = 0

# ---------------------------------------------------------------------------
# API helpers (identical to scrape.py)
# ---------------------------------------------------------------------------

ZILLOW_BASE = "https://zillow.realtyapi.io"

def api_get(base_url, path, params=None):
    global _api_calls
    url = base_url + path
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "x-realtyapi-key": API_KEY,
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
    })
    _api_calls += 1
    print(f"  [API call #{_api_calls}] {base_url}{path}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  API error: {e}", file=sys.stderr)
        return None

def maps_get(endpoint, params):
    global _maps_calls
    params["key"] = MAPS_KEY
    url = f"https://maps.googleapis.com/maps/api/{endpoint}/json?{urllib.parse.urlencode(params)}"
    _maps_calls += 1
    print(f"  [Maps call #{_maps_calls}] {endpoint}")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Maps error [{endpoint}]: {e}", file=sys.stderr)
        return None

# ---------------------------------------------------------------------------
# Formatting helpers (identical to scrape.py)
# ---------------------------------------------------------------------------

def fmt_price(p):
    if p is None:
        return "N/A"
    if isinstance(p, dict):
        p = p.get("value") or p.get("amount") or p.get("price")
    if p is None:
        return "N/A"
    try:
        return f"${int(float(str(p).replace(',', ''))):,}"
    except Exception:
        return str(p)

def fmt_lot(sqft):
    if sqft is None:
        return "N/A"
    if isinstance(sqft, (int, float)):
        try:
            acres = round(float(sqft) / 43560, 2)
            return f"{int(sqft):,} sqft ({acres} ac)"
        except Exception:
            return str(sqft)
    s = str(sqft).strip()
    if re.match(r"^\d+$", s):
        try:
            sqft_val = int(s)
            acres = round(sqft_val / 43560, 2)
            return f"{sqft_val:,} sqft ({acres} ac)"
        except Exception:
            pass
    m = re.match(r"^([\d,]+\.?\d*)\s*acres?$", s, re.IGNORECASE)
    if m:
        try:
            acres = float(m.group(1).replace(",", ""))
            sqft_val = int(acres * 43560)
            return f"{sqft_val:,} sqft ({round(acres, 2)} ac)"
        except Exception:
            pass
    m = re.match(r"^([\d,]+)\s*sq\s*ft", s, re.IGNORECASE)
    if m:
        try:
            sqft_val = int(m.group(1).replace(",", ""))
            acres = round(sqft_val / 43560, 2)
            return f"{sqft_val:,} sqft ({acres} ac)"
        except Exception:
            pass
    return s

def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def within_area(listing_lat, listing_lng):
    MAX_DISTANCE_MILES = 12
    for area in SEARCH_AREAS:
        d = haversine_miles(listing_lat, listing_lng, area["lat"], area["lng"])
        if d <= MAX_DISTANCE_MILES:
            return True
    return False

def normalize_address(addr):
    if not addr:
        return ""
    a = addr.lower()
    a = re.sub(r"[^\w\s]", "", a)
    a = re.sub(r"\s+", " ", a).strip()
    for abbr, full in [("rd","road"),("dr","drive"),("ave?","avenue"),("st","street"),
                       ("ln","lane"),("ct","court"),("blvd","boulevard")]:
        a = re.sub(rf"\b{abbr}\b", full, a)
    for drop in ["twp","township","boro","borough"]:
        a = re.sub(rf"\b{drop}\b", "", a)
    return re.sub(r"\s+", " ", a).strip()

# ---------------------------------------------------------------------------
# Maps enrichment (identical to scrape.py)
# ---------------------------------------------------------------------------

def geocode_address(address):
    if not MAPS_KEY:
        return None, None
    data = maps_get("geocode", {"address": address})
    if data and data.get("status") == "OK" and data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    return None, None

def check_location_risk(address, lat, lng):
    result = {"busy_road": False, "near_highway": False, "highway_roads": []}
    if not MAPS_KEY:
        return result
    if lat is None or lng is None:
        lat, lng = geocode_address(address)
    if lat is None:
        return result
    latlng = f"{lat},{lng}"
    places_data = maps_get("place/nearbysearch", {
        "location": latlng,
        "radius":   PROXIMITY_METERS,
        "type":     "route",
    })
    if places_data and places_data.get("status") == "OK":
        for place in places_data.get("results", []):
            name = (place.get("name") or "").strip()
            if not name:
                continue
            name_lower = name.lower()
            if any(kw in name_lower for kw in HIGHWAY_KEYWORDS):
                ploc = place.get("geometry", {}).get("location", {})
                dist = haversine_miles(lat, lng, ploc["lat"], ploc["lng"]) if ploc else None
                result["near_highway"] = True
                result["highway_roads"].append({
                    "name":           name,
                    "distance_miles": round(dist, 2) if dist is not None else None,
                })
                result["busy_road"] = True
    result["highway_roads"].sort(
        key=lambda r: r["distance_miles"] if r["distance_miles"] is not None else 999
    )
    return result

# ---------------------------------------------------------------------------
# Zillow (identical to scrape.py)
# ---------------------------------------------------------------------------

def zillow_search(area):
    print(f"  Zillow search: {area['name']}...")
    data = api_get(ZILLOW_BASE, "/search/byaddress", {
        "location":         area["zillow_location"],
        "listing_status":   "For_Sale",
        "keywords":         "ranch",
        "bed_min":          MIN_BEDS,
        "bathrooms":        "TwoPlus",
        "home_type":        "Houses",
        "list_price_range": f"min:0, max:{MAX_PRICE}",
        "Basement":         "Yes_Finished,Yes_Unfinished,Yes_Both",
    })
    if not data:
        return []
    raw = data.get("searchResults") or []
    results = [item.get("property") or item for item in raw]
    print(f"    → {len(results)} raw hits")
    return results

def zillow_details(zpid):
    data = api_get(ZILLOW_BASE, "/pro/byzpid", {"zpid": zpid})
    return data

def zillow_is_ranch(details):
    if not details:
        return False, False
    pd   = details.get("propertyDetails") or details
    reso = pd.get("resoFacts") or {}
    desc = str(pd.get("description") or details.get("description") or "").lower()

    arch_style = reso.get("architecturalStyle") or []
    if isinstance(arch_style, str):
        arch_style = [arch_style]
    style_text = " ".join(str(s).lower() for s in arch_style)
    is_ranch = any(kw in style_text for kw in RANCH_KEYWORDS)

    stories = reso.get("stories") or reso.get("storiesDecimal") or reso.get("storiesTotal")
    levels  = reso.get("levels")
    if not is_ranch and stories:
        try:
            if float(str(stories)) <= 1.0:
                is_ranch = True
        except (ValueError, TypeError):
            pass
    if not is_ranch and levels:
        levels_str = str(levels).lower()
        if "one" in levels_str or "1" in levels_str:
            is_ranch = True
    if not is_ranch:
        is_ranch = any(kw in desc for kw in RANCH_KEYWORDS)

    basement_yn  = reso.get("basementYN")
    basement_str = str(reso.get("basement") or "").lower()
    if isinstance(basement_yn, bool):
        has_basement = basement_yn
    elif isinstance(basement_yn, str):
        has_basement = basement_yn.strip().lower() in ("yes", "true", "1")
    else:
        has_basement = False
    if not has_basement and basement_str and basement_str not in ("none", ""):
        has_basement = True
    if not has_basement:
        has_basement = any(kw in desc for kw in BASEMENT_KEYWORDS)

    return is_ranch, has_basement

def zillow_fmt_address(r):
    a = r.get("address") or {}
    if isinstance(a, dict):
        street = a.get("streetAddress") or a.get("line", "")
        city   = a.get("city", "")
        state  = a.get("state") or a.get("state_code", "")
        zip_   = a.get("zipcode") or a.get("zip") or a.get("postal_code", "")
        return f"{street} {city} {state} {zip_}".strip()
    return str(a)

# ---------------------------------------------------------------------------
# State + HTML (identical to scrape.py)
# ---------------------------------------------------------------------------

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {"listings": {}}

def save_state(state):
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)
    print(f"  Written: {STATE_FILE}")

def merge_into_state(state, fresh_listings):
    listings = state["listings"]
    new_ids  = []
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing_norm = {}
    for lid, data in listings.items():
        key = normalize_address(data.get("address", ""))
        if key:
            existing_norm[key] = lid

    for listing in fresh_listings:
        lid      = listing["id"]
        norm_key = normalize_address(listing.get("address", ""))
        if norm_key and norm_key in existing_norm and existing_norm[norm_key] != lid:
            existing_lid = existing_norm[norm_key]
            print(f"  dedup drop (cross-run dupe of {existing_lid}): {listing['address']}")
            continue
        if lid in listings:
            existing_status = listings[lid].get("status", "new")
            if existing_status in ("favorite", "think", "deleted"):
                listings[lid]["price"]         = listing["price"]
                listings[lid]["busy_road"]     = listing.get("busy_road", False)
                listings[lid]["near_highway"]  = listing.get("near_highway", False)
                listings[lid]["highway_roads"] = listing.get("highway_roads", [])
            else:
                listing["status"]     = "new"
                listing["first_seen"] = listings[lid].get("first_seen", today)
                listings[lid] = listing
        else:
            listing["status"]     = "new"
            listing["first_seen"] = today
            listing["run_date"]   = today
            listings[lid] = listing
            existing_norm[norm_key] = lid
            new_ids.append(lid)
            print(f"  NEW: {listing['address']} {fmt_price(listing['price'])}")

    state["listings"] = listings
    return new_ids

def risk_badges(listing):
    badges = ""
    highway_roads = listing.get("highway_roads") or []
    if highway_roads:
        for road in highway_roads:
            name     = road.get("name", "Highway")
            dist     = road.get("distance_miles")
            dist_str = f" — {dist} mi" if dist is not None else ""
            badges  += f'<span class="badge badge-highway">🛣️ {name}{dist_str}</span>'
    elif listing.get("near_highway"):
        badges += '<span class="badge badge-highway">🛣️ Near Highway</span>'
    if listing.get("busy_road"):
        badges += '<span class="badge badge-road">⚠️ Busy Road</span>'
    return badges

def map_buttons(listing):
    addr_enc = urllib.parse.quote(listing.get("address", ""))
    lat = listing.get("lat")
    lng = listing.get("lng")
    if lat and lng:
        sv_url  = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}"
        sat_url = f"https://www.google.com/maps/@{lat},{lng},18z/data=!3m1!1e3"
    else:
        sv_url  = f"https://www.google.com/maps/search/?api=1&query={addr_enc}&layer=streetview"
        sat_url = f"https://www.google.com/maps/search/?api=1&query={addr_enc}&t=k"
    return (
        f'<a href="{sv_url}" target="_blank" class="map-btn">📷 Street View</a>'
        f'<a href="{sat_url}" target="_blank" class="map-btn">🛰️ Satellite</a>'
    )

def listing_card_html(lid, listing, status):
    buttons = ""
    for btn_status, label, cls in [
        ("favorite", "Favorite",       "btn-favorite"),
        ("think",    "Think About It", "btn-think"),
        ("deleted",  "Delete",         "btn-delete"),
    ]:
        active   = "active" if status == btn_status else ""
        buttons += (
            f'<button class="btn {cls} {active}" '
            f'onclick="setStatus(\'{lid}\', \'{btn_status}\')">{label}</button>'
        )
    source_cls   = listing.get("source", "").lower()
    source_badge = f'<span class="source-badge source-{source_cls}">{listing.get("source","")}</span>'
    badges = risk_badges(listing)
    maps   = map_buttons(listing)
    return f"""
<div class="card" id="card-{lid}" data-status="{status}" data-id="{lid}">
  <div class="card-body">
    <h3 class="address"><a href="{listing.get('url','')}" target="_blank">{listing.get('address','')}</a></h3>
    <div class="stats">
      <span class="price">{fmt_price(listing.get('price'))}</span>
      <span class="stat">{listing.get('beds','?')} bd</span>
      <span class="stat">{listing.get('baths','?')} ba</span>
      <span class="stat">{fmt_lot(listing.get('lot_sqft'))}</span>
      {source_badge}
    </div>
    {f'<div class="badges">{badges}</div>' if badges else ''}
    <div class="map-btns">{maps}</div>
    <div class="btn-group">{buttons}</div>
  </div>
</div>"""

def generate_html(state, new_ids):
    listings = state["listings"]
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except ImportError:
        eastern = timezone.utc
    run_time = datetime.now(eastern).strftime("%B %d, %Y at %I:%M %p %Z")
    today_dt = datetime.now(timezone.utc)

    def is_new_this_week(data):
        fs = data.get("first_seen") or data.get("run_date") or ""
        if not fs:
            return False
        try:
            fs_date = datetime.strptime(fs[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (today_dt - fs_date).days <= 7
        except Exception:
            return False

    groups = {"new_this_week": [], "unreviewed": [], "favorite": [], "think": []}
    for lid, data in listings.items():
        s = data.get("status", "new")
        if s == "deleted":
            continue
        if s == "favorite":
            groups["favorite"].append((lid, data))
        elif s == "think":
            groups["think"].append((lid, data))
        else:
            if is_new_this_week(data):
                groups["new_this_week"].append((lid, data))
            groups["unreviewed"].append((lid, data))

    groups["unreviewed"].sort(
        key=lambda x: (x[1].get("run_date",""), x[1].get("first_seen","")), reverse=True
    )
    unreviewed_by_date = OrderedDict()
    for lid, data in groups["unreviewed"]:
        rd = data.get("run_date") or data.get("first_seen") or "Unknown"
        unreviewed_by_date.setdefault(rd, []).append((lid, data))

    def unreviewed_section_html():
        if not groups["unreviewed"]:
            return '<section class="section hidden" id="section-unreviewed"><h2>Unreviewed</h2><p class="empty">No unreviewed listings.</p></section>'
        inner = ""
        for rd, items in unreviewed_by_date.items():
            group_ids = json.dumps([lid for lid, _ in items])
            inner += (
                f'<div class="run-group">'
                f'<div class="run-group-header">'
                f'<span class="run-date-label">Run: {rd}</span>'
                f'<button class="delete-all-btn" onclick="deleteAll({group_ids})">Delete All ({len(items)})</button>'
                f'</div>'
                f'<div class="grid">'
                + "".join(listing_card_html(lid, d, d.get("status","new")) for lid, d in items)
                + '</div></div>'
            )
        count = len(groups["unreviewed"])
        return (
            f'<section class="section hidden" id="section-unreviewed">'
            f'<h2>Unreviewed <span class="count" id="count-unreviewed">{count}</span></h2>'
            + inner + '</section>'
        )

    def simple_section(title, key, cards, collapsed=True):
        hidden = " hidden" if collapsed else ""
        count_id = f'id="count-{key}"'
        if not cards:
            return (
                f'<section class="section{hidden}" id="section-{key}">'
                f'<h2>{title} <span class="count" {count_id}>0</span></h2>'
                f'<p class="empty">No listings in this section.</p></section>'
            )
        html = "".join(listing_card_html(lid, d, d.get("status","new")) for lid, d in cards)
        return (
            f'<section class="section{hidden}" id="section-{key}">'
            f'<h2>{title} <span class="count" {count_id}>{len(cards)}</span></h2>'
            f'<div class="grid">{html}</div></section>'
        )

    ntw_count   = len(groups["new_this_week"])
    unrev_count = len(groups["unreviewed"])
    fav_count   = len(groups["favorite"])
    think_count = len(groups["think"])

    ntw_cards = "".join(listing_card_html(lid, d, d.get("status","new")) for lid, d in groups["new_this_week"])
    new_this_week_html = (
        f'<section class="section" id="section-new-this-week">'
        f'<h2>New This Week <span class="count" id="count-new-this-week">{ntw_count}</span></h2>'
        + (f'<div class="grid">{ntw_cards}</div>' if ntw_count else '<p class="empty">No new listings this week.</p>')
        + '</section>'
    )

    sections_html = (
        new_this_week_html +
        unreviewed_section_html() +
        simple_section("Favorites",      "favorite", groups["favorite"], collapsed=True) +
        simple_section("Think About It", "think",    groups["think"],    collapsed=True)
    )

    state_json = json.dumps({k: v for k, v in listings.items() if v.get("status") != "deleted"})
    worker_url = os.environ.get("WORKER_URL", "https://ranches-proxy.johnzur.workers.dev")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>⚠️ TEST — Ranch Finder Zillow Test Run</title>
<style>
  :root{{--bg:#f7f6f3;--surface:#fff;--border:#e2e0db;--text:#1a1a1a;--muted:#6b6b6b;--accent:#2d6a4f;--radius:10px;--shadow:0 2px 8px rgba(0,0,0,.08);}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5;}}
  .test-banner{{background:#b91c1c;color:#fff;text-align:center;padding:8px;font-weight:700;font-size:.9rem;}}
  .sticky-top{{position:sticky;top:0;z-index:100;}}
  header{{background:var(--accent);color:#fff;padding:14px 32px;}}
  header h1{{font-size:1.2rem;font-weight:700;}}
  header .meta{{font-size:.78rem;opacity:.8;margin-top:2px;}}
  nav{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 32px;display:flex;gap:2px;}}
  nav button{{background:none;border:none;padding:12px 12px;font-size:.85rem;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;font-weight:500;}}
  nav button.active{{color:var(--accent);border-bottom-color:var(--accent);}}
  main{{padding:24px 32px;max-width:1400px;margin:0 auto;}}
  .section{{margin-bottom:48px;}}
  .section h2{{font-size:1.05rem;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px;}}
  .count{{background:var(--accent);color:#fff;font-size:.72rem;padding:2px 8px;border-radius:20px;font-weight:600;}}
  .run-group{{margin-bottom:32px;}}
  .run-group-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border);}}
  .run-date-label{{font-size:.82rem;font-weight:600;color:var(--muted);}}
  .delete-all-btn{{font-size:.75rem;font-weight:600;padding:4px 12px;border-radius:6px;border:1px solid #fca5a5;background:#fef2f2;color:#dc2626;cursor:pointer;}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:20px;}}
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);transition:opacity .3s;}}
  .card-body{{padding:16px;}}
  .address{{font-size:.95rem;font-weight:600;margin-bottom:10px;line-height:1.3;}}
  .address a{{color:var(--text);text-decoration:none;}}
  .address a:hover{{color:var(--accent);text-decoration:underline;}}
  .stats{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:10px;}}
  .price{{font-size:1.1rem;font-weight:700;color:var(--accent);width:100%;}}
  .stat{{font-size:.82rem;color:var(--muted);background:var(--bg);padding:3px 8px;border-radius:4px;}}
  .source-badge{{font-size:.75rem;font-weight:600;padding:3px 8px;border-radius:4px;}}
  .source-zillow{{background:#fef9c3;color:#854d0e;}}
  .badges{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;}}
  .badge{{font-size:.75rem;font-weight:600;padding:3px 10px;border-radius:4px;}}
  .badge-highway{{background:#fef3c7;color:#92400e;}}
  .badge-road{{background:#fee2e2;color:#991b1b;}}
  .map-btns{{display:flex;gap:8px;margin-bottom:10px;}}
  .map-btn{{font-size:.78rem;font-weight:600;padding:5px 12px;border-radius:6px;background:#f0f9f4;color:var(--accent);text-decoration:none;border:1px solid #c6e8d5;}}
  .btn-group{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;}}
  .btn{{border:1px solid var(--border);background:var(--bg);color:var(--muted);padding:6px 14px;border-radius:6px;font-size:.8rem;font-weight:600;cursor:pointer;}}
  .btn.active{{color:#fff;border-color:transparent;}}
  .btn-favorite.active{{background:#2d6a4f;}}
  .btn-think.active{{background:#7b68ee;}}
  .btn-delete.active{{background:#c0392b;}}
  .empty{{color:var(--muted);font-size:.9rem;padding:12px 0;}}
  .hidden{{display:none!important;}}
  #toast{{position:fixed;bottom:24px;right:24px;background:#1a1a1a;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;}}
  #toast.show{{opacity:1;}}
</style>
</head>
<body>
<div class="test-banner">⚠️ TEST RUN — Zillow only, Bridgewater only — NOT PRODUCTION</div>
<div class="sticky-top">
  <header>
    <h1>Ranch Finder — Zillow Test Run</h1>
    <div class="meta">Run: {run_time} &nbsp;|&nbsp; {unrev_count} unreviewed</div>
  </header>
  <nav>
    <button class="active" onclick="showSection('new-this-week',this)">🆕 This Week (<span id="nav-new-this-week">{ntw_count}</span>)</button>
    <button onclick="showSection('unreviewed',this)">📋 Queue (<span id="nav-unreviewed">{unrev_count}</span>)</button>
    <button onclick="showSection('favorite',this)">⭐ Favorites (<span id="nav-favorite">{fav_count}</span>)</button>
    <button onclick="showSection('think',this)">🤔 Maybe (<span id="nav-think">{think_count}</span>)</button>
  </nav>
</div>
<main>{sections_html}</main>
<div id="toast"></div>
<script>
let state = {state_json};
function showSection(key, btn) {{
  document.querySelectorAll(".section").forEach(s => s.classList.add("hidden"));
  document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
  const sec = document.getElementById("section-" + key);
  if (sec) sec.classList.remove("hidden");
  if (btn) btn.classList.add("active");
}}
function countVisible(sectionId) {{
  const sec = document.getElementById("section-" + sectionId);
  if (!sec) return 0;
  return sec.querySelectorAll(".card:not([style*='display: none'])").length;
}}
function updateNavCounts() {{
  ["new-this-week","unreviewed","favorite","think"].forEach(key => {{
    const navEl   = document.getElementById("nav-" + key);
    const countEl = document.getElementById("count-" + key);
    const n = countVisible(key);
    if (navEl) navEl.textContent = n;
    if (countEl) countEl.textContent = n;
  }});
}}
function showToast(msg) {{
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2500);
}}
async function setStatus(lid, newStatus) {{
  if (!state[lid]) return;
  state[lid].status = newStatus;
  const card = document.getElementById("card-" + lid);
  if (card) {{
    card.dataset.status = newStatus;
    card.querySelectorAll(".btn").forEach(b => b.classList.remove("active"));
    const activeBtn = card.querySelector(".btn-" + (newStatus === "deleted" ? "delete" : newStatus));
    if (activeBtn) activeBtn.classList.add("active");
    if (newStatus === "deleted") {{
      card.style.opacity = "0.3";
      setTimeout(() => {{ card.style.display = "none"; updateNavCounts(); }}, 400);
    }} else {{
      updateNavCounts();
    }}
  }}
  showToast("(Test mode — state not saved to GitHub)");
}}
async function deleteAll(ids) {{
  for (const lid of ids) {{
    if (state[lid]) state[lid].status = "deleted";
    const card = document.getElementById("card-" + lid);
    if (card) {{
      card.style.opacity = "0.3";
      setTimeout(() => {{ card.style.display = "none"; }}, 400);
    }}
  }}
  setTimeout(updateNavCounts, 500);
  showToast("(Test mode — state not saved to GitHub)");
}}
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Zillow Test Run — test_zillow.py")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Output state:  {STATE_FILE}")
    print(f"Output HTML:   {OUTPUT_FILE}")
    print("=" * 60)

    state = load_state()
    confirmed_listings = []
    detail_calls = 0

    # Search
    print(f"\n--- Zillow search: {TEST_AREA['name']} ---")
    raw = zillow_search(TEST_AREA)

    if not raw:
        print("  No results returned from search. Check API key and endpoint.")
        return

    # Process each result up to detail call cap
    print(f"\n--- Processing {len(raw)} search results (detail cap: {MAX_DETAIL_CALLS}) ---")
    for r in raw:
        if detail_calls >= MAX_DETAIL_CALLS:
            print(f"  Detail call cap ({MAX_DETAIL_CALLS}) reached — stopping.")
            break

        zpid = r.get("zpid")
        if not zpid:
            print(f"  skip (no zpid): {zillow_fmt_address(r)}")
            continue

        # --- Print raw search-level fields for verification ---
        raw_price = r.get("price") or r.get("unformattedPrice") or r.get("list_price")
        loc       = r.get("location") or {}
        lot_info  = r.get("lotSizeWithUnit") or {}
        print(f"\n  zpid={zpid}")
        print(f"    raw price field:     {repr(raw_price)}")
        print(f"    beds:                {repr(r.get('bedrooms') or r.get('beds'))}")
        print(f"    baths:               {repr(r.get('bathrooms') or r.get('baths'))}")
        print(f"    location lat/lng:    {repr(loc.get('latitude'))}, {repr(loc.get('longitude'))}")
        print(f"    lotSizeWithUnit:     {repr(lot_info)}")

        # Price extraction (same logic as scrape.py process_zillow_area)
        if isinstance(raw_price, dict):
            price = raw_price.get("value") or raw_price.get("amount")
        else:
            price = raw_price

        baths = r.get("bathrooms") or r.get("baths")
        beds  = r.get("bedrooms")  or r.get("beds")

        # Pre-filter
        try:
            if price and float(str(price).replace(",", "")) > MAX_PRICE:
                print(f"    skip (over price): {fmt_price(price)}")
                continue
        except (TypeError, ValueError):
            pass
        try:
            if baths and float(str(baths).replace(",", "")) < MIN_BATHS:
                print(f"    skip (baths {baths} < {MIN_BATHS})")
                continue
        except (TypeError, ValueError):
            pass

        # Details call
        print(f"    → fetching details...")
        details = zillow_details(zpid)
        detail_calls += 1

        # --- Print raw detail fields for verification ---
        if details:
            pd   = details.get("propertyDetails") or details
            reso = pd.get("resoFacts") or {}
            print(f"    resoFacts.architecturalStyle: {repr(reso.get('architecturalStyle'))}")
            print(f"    resoFacts.basementYN:         {repr(reso.get('basementYN'))}")
            print(f"    resoFacts.basement:           {repr(reso.get('basement'))}")
            print(f"    resoFacts.stories:            {repr(reso.get('stories'))}")
            print(f"    resoFacts.levels:             {repr(reso.get('levels'))}")
            desc_snippet = str(pd.get("description") or "")[:120]
            print(f"    description (first 120):      {repr(desc_snippet)}")
        else:
            print(f"    details returned None")

        is_ranch, has_basement = zillow_is_ranch(details)
        addr = zillow_fmt_address(r)

        # Geo post-filter
        z_lat = loc.get("latitude")
        z_lng = loc.get("longitude")
        if z_lat and z_lng and not within_area(z_lat, z_lng):
            print(f"    skip (out of area): {addr}")
            continue

        if not is_ranch:
            print(f"    skip (not ranch): {addr}")
            continue
        if not has_basement:
            print(f"    skip (no basement): {addr}")
            continue

        # Lot formatting
        if isinstance(lot_info, dict):
            lot_val  = lot_info.get("lotSize")
            lot_unit = (lot_info.get("lotSizeUnit") or "").lower()
            lot = f"{lot_val} acres" if (lot_val and "acre" in lot_unit) else lot_val
        else:
            lot = r.get("lotAreaValue") or r.get("lot_sqft")

        print(f"    fmt_lot input:  {repr(lot)}")
        print(f"    fmt_lot output: {fmt_lot(lot)}")
        print(f"    fmt_price output: {fmt_price(price)}")

        # Maps enrichment
        risk = check_location_risk(addr, None, None)
        print(f"    highway_roads:  {risk['highway_roads']}")
        print(f"    busy_road:      {risk['busy_road']}")
        print(f"    near_highway:   {risk['near_highway']}")

        listing = {
            "id":            f"zillow_{zpid}",
            "address":       addr,
            "price":         price,
            "beds":          beds,
            "baths":         baths,
            "lot_sqft":      lot,
            "url":           f"https://www.zillow.com/homedetails/{zpid}_zpid/",
            "source":        "Zillow",
            "busy_road":     risk["busy_road"],
            "near_highway":  risk["near_highway"],
            "highway_roads": risk["highway_roads"],
        }
        confirmed_listings.append(listing)
        print(f"    ✅ PASS: {addr} {fmt_price(price)}")

    # Merge + write state
    print(f"\n--- Merging {len(confirmed_listings)} confirmed listings into test state ---")
    new_ids = merge_into_state(state, confirmed_listings)
    save_state(state)

    # Verify state_test.json fields
    print(f"\n--- state_test.json field verification ---")
    for lid, data in state["listings"].items():
        print(f"\n  {lid}:")
        for field in ["address","price","beds","baths","lot_sqft","url","source",
                      "busy_road","near_highway","highway_roads","status","first_seen","run_date"]:
            print(f"    {field}: {repr(data.get(field))}")

    # Generate HTML
    print(f"\n--- Generating test HTML ---")
    html = generate_html(state, set(new_ids))
    os.makedirs(DOCS_DIR, exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Written: {OUTPUT_FILE}")

    # Budget report
    print(f"\n{'='*60}")
    print(f"RealtyAPI calls this run: {_api_calls}  (key had 4 used, now has {4 + _api_calls})")
    print(f"Google Maps calls:        {_maps_calls}")
    print(f"Confirmed listings:       {len(confirmed_listings)}")
    print(f"New to test state:        {len(new_ids)}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
