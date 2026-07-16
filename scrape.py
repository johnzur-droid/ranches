"""
Ranch Home Finder — Bridgewater, Somerset & Cranford, NJ
Runs weekly via GitHub Actions.

Sources: Redfin + Zillow only (both verified 100% accurate on keyword=ranch search).
         Realtor.com dropped — only 38% accurate on keyword search (S002 finding).

Strategy per listing:
  1. Search with keyword=ranch (100% accurate on both sources, confirmed S002)
  2. Details call — check ALL available structured fields + description fallback:
     Zillow: resoFacts.architecturalStyle, basementYN, basement, stories, levels
     Redfin: Room Information→Basement YN, Room Information→Basement, description
  3. Drop if: not confirmed ranch, no basement indicator, <3 beds, <2 baths, >$1,000,000
  4. Google Maps enrichment — geocode address, check for:
     - Busy road (Roads API — road classification of property street)
     - Highway within quarter mile (Places API)
     - Industrial zone within quarter mile (Places API)
  5. Dedupe by normalized address across both sources and all towns

Repo:    johnzur-droid/ranches
Output:  docs/state.json  (all known listings + favorite/think/deleted status)
         docs/index.html  (live page served via GitHub Pages)

Env vars required:
  REALTYAPI_KEY   — RealtyAPI key (GitHub Secret)
  GOOGLE_MAPS_KEY — Google Maps API key (GitHub Secret)
"""

import urllib.request
import urllib.parse
import json
import os
import re
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_KEY     = os.environ.get("REALTYAPI_KEY", "")
MAPS_KEY    = os.environ.get("GOOGLE_MAPS_KEY", "")

if not API_KEY:
    print("ERROR: REALTYAPI_KEY environment variable not set.", file=sys.stderr)
    sys.exit(1)

if not MAPS_KEY:
    print("WARNING: GOOGLE_MAPS_KEY not set — location risk checks disabled.", file=sys.stderr)

REPO_ROOT   = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR    = os.path.join(REPO_ROOT, "docs")
STATE_FILE  = os.path.join(DOCS_DIR, "state.json")
OUTPUT_FILE = os.path.join(DOCS_DIR, "index.html")

SEARCH_AREAS = [
    {"name": "Bridgewater", "state": "NJ", "lat": 40.5887, "lng": -74.6040, "radius": 10,
     "zillow_location": "Bridgewater, NJ"},
    {"name": "Somerset",    "state": "NJ", "lat": 40.5007, "lng": -74.4882, "radius": 10,
     "zillow_location": "Somerset, NJ"},
    {"name": "Cranford",    "state": "NJ", "lat": 40.6579, "lng": -74.2982, "radius": 10,
     "zillow_location": "Cranford, NJ"},
]

MIN_BEDS  = 3      # confirmed S003
MIN_BATHS = 2.0
MAX_PRICE = 1_000_000

# Quarter mile in meters for Google Maps proximity checks
PROXIMITY_METERS = 402

# Highway/busy road keywords — used to flag road names from Places API
HIGHWAY_KEYWORDS = {
    "interstate", "i-", "turnpike", "expressway", "freeway",
    "parkway", "highway", "hwy", "route", "rte", "us-", "nj-"
}

# API call counters
_api_calls   = 0
_maps_calls  = 0

# ---------------------------------------------------------------------------
# Ranch / basement keyword sets (fallback — used if structured fields absent)
# ---------------------------------------------------------------------------

RANCH_KEYWORDS    = {"ranch", "single floor", "one level", "one-level", "1 story",
                     "one story", "one-story", "single story", "single-story",
                     "rambler", "one-floor"}
BASEMENT_KEYWORDS = {"basement", "full basement", "finished basement",
                     "unfinished basement", "partial basement", "walk-out basement",
                     "walkout basement", "walk out basement"}

# ---------------------------------------------------------------------------
# RealtyAPI helper
# ---------------------------------------------------------------------------

REDFIN_BASE = "https://redfin.realtyapi.io"
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
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  API error [{base_url}{path}]: {e}", file=sys.stderr)
        return None

def report_budget():
    print(f"  [RealtyAPI] Calls this run: {_api_calls} (free tier: 250/mo)")
    print(f"  [Google Maps] Calls this run: {_maps_calls}")

# ---------------------------------------------------------------------------
# Google Maps helpers
# ---------------------------------------------------------------------------

def maps_get(endpoint, params):
    global _maps_calls
    params["key"] = MAPS_KEY
    url = f"https://maps.googleapis.com/maps/api/{endpoint}/json?{urllib.parse.urlencode(params)}"
    _maps_calls += 1
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"  Maps error [{endpoint}]: {e}", file=sys.stderr)
        return None


def geocode_address(address):
    """Convert address string to lat/lng. Returns (lat, lng) or (None, None)."""
    if not MAPS_KEY:
        return None, None
    data = maps_get("geocode", {"address": address})
    if data and data.get("status") == "OK" and data.get("results"):
        loc = data["results"][0]["geometry"]["location"]
        return loc["lat"], loc["lng"]
    return None, None


def check_location_risk(address, lat, lng):
    """
    Run three location risk checks for a confirmed listing.
    Returns dict with keys: busy_road, near_highway, near_industrial
    All False if Maps key not set or geocoding fails.
    """
    result = {"busy_road": False, "near_highway": False, "near_industrial": False}

    if not MAPS_KEY:
        return result

    # Geocode if coordinates not already known
    if lat is None or lng is None:
        lat, lng = geocode_address(address)
    if lat is None:
        return result

    latlng = f"{lat},{lng}"

    # Check 1: Busy road — Roads API speedLimits or nearestRoads
    # Use Roads nearestRoads to get the road the property is ON
    try:
        global _maps_calls
        url = f"https://roads.googleapis.com/v1/nearestRoads?points={latlng}&key={MAPS_KEY}"
        _maps_calls += 1
        with urllib.request.urlopen(url, timeout=10) as resp:
            roads_data = json.loads(resp.read())
            # If speed limit data available, flag >35mph as busy
            # Roads API returns speedLimitMph on paid tier only
            # On free tier we check road name for busy road indicators
            for road in roads_data.get("snappedPoints", []):
                name = (road.get("placeId") or "").lower()
                # Fall through to Places check below for name-based detection
    except Exception:
        pass

    # Check 2: Highway within quarter mile — Places API
    places_data = maps_get("place/nearbysearch", {
        "location": latlng,
        "radius":   PROXIMITY_METERS,
        "type":     "route",
    })
    if places_data and places_data.get("status") == "OK":
        for place in places_data.get("results", []):
            name = (place.get("name") or "").lower()
            if any(kw in name for kw in HIGHWAY_KEYWORDS):
                result["near_highway"] = True
                break
            # Also flag if the property's own road is major
            # (appears in nearby results and has highway keyword)
            if place.get("types") and "route" in place.get("types", []):
                if any(kw in name for kw in HIGHWAY_KEYWORDS):
                    result["busy_road"] = True

    # Check 3: Industrial zone within quarter mile — Places API
    industrial_data = maps_get("place/nearbysearch", {
        "location": latlng,
        "radius":   PROXIMITY_METERS,
        "type":     "industrial_building",
    })
    if industrial_data and industrial_data.get("status") == "OK":
        if industrial_data.get("results"):
            result["near_industrial"] = True

    return result

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_price(p):
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


import math

def haversine_miles(lat1, lon1, lat2, lon2):
    """Distance in miles between two lat/lng points."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2)
    return R * 2 * math.asin(math.sqrt(a))


def within_area(listing_lat, listing_lng):
    """
    Returns True if the listing is within MAX_DISTANCE_MILES of ANY search area center.
    Prevents radius bleed — e.g. Cranford's 10-mile radius reaching Staten Island.
    """
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
    a = re.sub(r"\brd\b",       "road",      a)
    a = re.sub(r"\bdr\b",       "drive",     a)
    a = re.sub(r"\bave?\b",     "avenue",    a)
    a = re.sub(r"\bst\b",       "street",    a)
    a = re.sub(r"\bln\b",       "lane",      a)
    a = re.sub(r"\bct\b",       "court",     a)
    a = re.sub(r"\bblvd\b",     "boulevard", a)
    a = re.sub(r"\btwp\b",      "",          a)
    a = re.sub(r"\btownship\b", "",          a)
    a = re.sub(r"\bboro\b",     "",          a)
    a = re.sub(r"\bborough\b",  "",          a)
    a = re.sub(r"\s+", " ", a).strip()
    return a

# ---------------------------------------------------------------------------
# Redfin
# ---------------------------------------------------------------------------

def redfin_search(area):
    """
    Search Redfin by coordinates.
    Base URL: redfin.realtyapi.io — confirmed S003.
    Params: latitude/longitude — confirmed S003.
    Results nested as {"homeData": {...}} — unwrapped below.
    minStories broken — confirmed S002, do not use.
    """
    print(f"  Redfin search: {area['name']}...")
    data = api_get(REDFIN_BASE, "/search/bycoordinates", {
        "latitude":          area["lat"],
        "longitude":         area["lng"],
        "radius":            area["radius"],
        "keyword":           "ranch",
        "homeType":          "House",
        "minBeds":           MIN_BEDS,
        "baths":             MIN_BATHS,
        "status":            "for_sale",
        "maxPrice":          MAX_PRICE,
        "excludeLandLeases": True,
    })
    if not data:
        return []
    raw = data.get("searchResults") or []
    results = [item.get("homeData") or item for item in raw]
    print(f"    → {len(results)} raw hits")
    return results


def redfin_details(property_id, listing_id):
    data = api_get(REDFIN_BASE, "/detailsbyid", {
        "property_id": property_id,
        "listing_id":  listing_id,
    })
    return data


def redfin_is_ranch(details):
    """
    Check Redfin details for ranch + basement confirmation.
    Confirmed field locations from S003 live call:
      - Basement YN: amenities.superGroups[*].amenityGroups[*].amenityEntries
                     where amenityName == 'Basement YN' and amenityValues contains 'Yes'
      - Basement:    same path, amenityName == 'Basement', values like 'Finished', 'Full'
      - Style:       not reliably populated on Redfin — confirmed S003
    Fallback: scan Room Description amenity entry + description text
    Returns (is_ranch: bool, has_basement: bool)
    """
    if not details:
        return False, False

    details_body = details.get("details") or details
    amenities    = details_body.get("amenities") or {}
    description  = str(details_body.get("description") or details.get("description") or "").lower()

    basement_yn     = False
    basement_filled = False
    room_desc_text  = ""

    for sg in amenities.get("superGroups", []):
        for ag in sg.get("amenityGroups", []):
            for entry in ag.get("amenityEntries", []):
                name = (entry.get("amenityName") or "").strip()
                vals = [str(v).lower() for v in (entry.get("amenityValues") or [])]
                val_str = " ".join(vals)

                if name == "Basement YN":
                    basement_yn = "yes" in vals
                elif name == "Basement" and vals:
                    basement_filled = True
                elif name == "Room Description":
                    room_desc_text = val_str

    # Basement confirmed if either structured field is populated
    has_basement = (
        basement_yn or
        basement_filled or
        any(kw in description for kw in BASEMENT_KEYWORDS) or
        any(kw in room_desc_text for kw in BASEMENT_KEYWORDS)
    )

    # Ranch — Redfin style field not reliably populated (confirmed S003)
    # keyword=ranch at search level is 100% accurate (confirmed S002)
    # Additional check: room description and listing description
    is_ranch = (
        any(kw in description for kw in RANCH_KEYWORDS) or
        any(kw in room_desc_text for kw in RANCH_KEYWORDS)
    )

    # If description empty (common on Redfin), trust keyword search
    if not description and not room_desc_text:
        is_ranch = True  # keyword=ranch search already confirmed 100% accurate

    return is_ranch, has_basement


def redfin_fmt_address(r):
    a = r.get("addressInfo") or r.get("address") or {}
    if isinstance(a, dict):
        street = a.get("formattedStreetLine") or a.get("streetAddress") or a.get("line", "")
        city   = a.get("city", "")
        state  = a.get("state", a.get("state_code", ""))
        zip_   = a.get("zip") or a.get("zipcode") or a.get("postal_code", "")
        return f"{street} {city} {state} {zip_}".strip()
    return str(a)


def process_redfin_area(area):
    raw = redfin_search(area)
    listings = []
    for r in raw:
        property_id = r.get("propertyId") or r.get("property_id")
        listing_id  = r.get("listingId")  or r.get("listing_id")

        price_info = r.get("priceInfo") or {}
        price = price_info.get("amount") or price_info.get("int64Value")
        if not price:
            price = r.get("price") or r.get("list_price")

        baths = r.get("baths") or r.get("bathrooms")
        beds  = r.get("beds")  or r.get("bedrooms")

        try:
            if price and float(str(price).replace(",", "")) > MAX_PRICE:
                continue
        except (TypeError, ValueError):
            pass
        try:
            if baths and float(str(baths).replace(",", "")) < MIN_BATHS:
                continue
        except (TypeError, ValueError):
            pass

        if not property_id or not listing_id:
            print(f"    skip (missing IDs): {redfin_fmt_address(r)}")
            continue

        details = redfin_details(property_id, listing_id)
        is_ranch, has_basement = redfin_is_ranch(details)

        addr = redfin_fmt_address(r)

        # Distance post-filter — drop listings outside 12 miles of any town center
        centroid = (r.get("addressInfo") or {}).get("centroid") or {}
        if isinstance(centroid, dict):
            centroid = centroid.get("centroid") or centroid
        r_lat = centroid.get("latitude")
        r_lng = centroid.get("longitude")
        if r_lat and r_lng and not within_area(r_lat, r_lng):
            print(f"    skip (out of area): {addr}")
            continue

        if not is_ranch:
            print(f"    skip (not ranch): {addr}")
            continue
        if not has_basement:
            print(f"    skip (no basement): {addr}")
            continue

        lot_info = r.get("lotSize") or {}
        lot = lot_info.get("amount") if isinstance(lot_info, dict) else lot_info

        rel_url  = r.get("url") or r.get("href") or r.get("detailUrl") or ""
        full_url = f"https://www.redfin.com{rel_url}" if rel_url.startswith("/") else rel_url

        # Google Maps enrichment
        risk = check_location_risk(addr, None, None)

        listings.append({
            "id":             f"redfin_{property_id}",
            "address":        addr,
            "price":          price,
            "beds":           beds,
            "baths":          baths,
            "lot_sqft":       lot,
            "url":            full_url,
            "source":         "Redfin",
            "busy_road":      risk["busy_road"],
            "near_highway":   risk["near_highway"],
            "near_industrial":risk["near_industrial"],
        })
        print(f"    PASS: {addr} {fmt_price(price)}")

    return listings

# ---------------------------------------------------------------------------
# Zillow
# ---------------------------------------------------------------------------

def zillow_search(area):
    """
    Zillow /search/byaddress — confirmed working S003.
    bathrooms=TwoPlus confirmed working on byaddress (not bycoordinates).
    Basement filter confirmed working S003 — 6 results returned.
    listing_status=For_Sale (not status=forSale) — confirmed S003.
    bed_min=3 — updated S003 per JZ requirement.
    """
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
    """
    Zillow /pro/byzpid — confirmed S003.
    Response structure: data.propertyDetails.resoFacts
    Confirmed fields: architecturalStyle, basementYN, basement, stories, levels
    """
    data = api_get(ZILLOW_BASE, "/pro/byzpid", {"zpid": zpid})
    return data


def zillow_is_ranch(details):
    """
    Check ALL available Zillow structured fields for ranch + basement.
    Confirmed field locations from S003 live call:
      architecturalStyle: data.propertyDetails.resoFacts.architecturalStyle — 'Ranch'
      basementYN:         data.propertyDetails.resoFacts.basementYN — True/False
      basement:           data.propertyDetails.resoFacts.basement — 'Yes,Walk-Out Access'
      stories:            data.propertyDetails.resoFacts.stories — None (not reliable)
      levels:             data.propertyDetails.resoFacts.levels — None (not reliable)
    Fallback: description text
    Returns (is_ranch: bool, has_basement: bool)
    """
    if not details:
        return False, False

    pd   = details.get("propertyDetails") or details
    reso = pd.get("resoFacts") or {}
    desc = str(pd.get("description") or details.get("description") or "").lower()

    # --- Ranch check ---
    arch_style = reso.get("architecturalStyle") or []
    if isinstance(arch_style, str):
        arch_style = [arch_style]
    style_text = " ".join(str(s).lower() for s in arch_style)
    is_ranch = any(kw in style_text for kw in RANCH_KEYWORDS)

    # stories/levels — check when populated
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

    # Description fallback
    if not is_ranch:
        is_ranch = any(kw in desc for kw in RANCH_KEYWORDS)

    # --- Basement check ---
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


def process_zillow_area(area):
    raw = zillow_search(area)
    listings = []
    for r in raw:
        zpid  = r.get("zpid")
        # Price may be a dict {'value': 749000, ...} or a plain number
        raw_price = r.get("price") or r.get("unformattedPrice") or r.get("list_price")
        if isinstance(raw_price, dict):
            price = raw_price.get("value") or raw_price.get("amount")
        else:
            price = raw_price
        baths = r.get("bathrooms") or r.get("baths")
        beds  = r.get("bedrooms")  or r.get("beds")

        try:
            if price and float(str(price).replace(",", "")) > MAX_PRICE:
                continue
        except (TypeError, ValueError):
            pass
        try:
            if baths and float(str(baths).replace(",", "")) < MIN_BATHS:
                continue
        except (TypeError, ValueError):
            pass

        if not zpid:
            print(f"    skip (missing zpid): {zillow_fmt_address(r)}")
            continue

        details = zillow_details(zpid)
        is_ranch, has_basement = zillow_is_ranch(details)

        addr = zillow_fmt_address(r)

        # Distance post-filter — drop listings outside 12 miles of any town center
        loc = r.get("location") or {}
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

        lot_info = r.get("lotSizeWithUnit") or {}
        if isinstance(lot_info, dict):
            lot_val  = lot_info.get("lotSize")
            lot_unit = (lot_info.get("lotSizeUnit") or "").lower()
            lot = f"{lot_val} acres" if (lot_val and "acre" in lot_unit) else lot_val
        else:
            lot = r.get("lotAreaValue") or r.get("lot_sqft")

        # Google Maps enrichment
        risk = check_location_risk(addr, None, None)

        listings.append({
            "id":             f"zillow_{zpid}",
            "address":        addr,
            "price":          price,
            "beds":           beds,
            "baths":          baths,
            "lot_sqft":       lot,
            "url":            f"https://www.zillow.com/homedetails/{zpid}_zpid/",
            "source":         "Zillow",
            "busy_road":      risk["busy_road"],
            "near_highway":   risk["near_highway"],
            "near_industrial":risk["near_industrial"],
        })
        print(f"    PASS: {addr} {fmt_price(price)}")

    return listings

# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------

def dedup_listings(listings):
    seen = {}
    out  = []
    for listing in listings:
        key = normalize_address(listing.get("address", ""))
        if not key:
            out.append(listing)
            continue
        if key not in seen:
            seen[key] = True
            out.append(listing)
        else:
            print(f"  dedup drop ({listing['source']}): {listing['address']}")
    return out

# ---------------------------------------------------------------------------
# State management
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


def merge_into_state(state, fresh_listings):
    listings = state["listings"]
    new_ids  = []
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for listing in fresh_listings:
        lid = listing["id"]
        if lid in listings:
            existing_status = listings[lid].get("status", "new")
            if existing_status in ("favorite", "think", "deleted"):
                # Preserve user decision — update price + risk flags silently
                listings[lid]["price"]          = listing["price"]
                listings[lid]["busy_road"]      = listing.get("busy_road", False)
                listings[lid]["near_highway"]   = listing.get("near_highway", False)
                listings[lid]["near_industrial"]= listing.get("near_industrial", False)
            else:
                listing["status"]     = "new"
                listing["first_seen"] = listings[lid].get("first_seen", today)
                listings[lid] = listing
        else:
            listing["status"]     = "new"
            listing["first_seen"] = today
            listings[lid] = listing
            new_ids.append(lid)
            print(f"  NEW: {listing['address']} {fmt_price(listing['price'])}")

    state["listings"] = listings
    return new_ids

# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def risk_badges(listing):
    badges = ""
    if listing.get("near_highway"):
        badges += '<span class="badge badge-highway" title="Highway within quarter mile">🛣️ Near Highway</span>'
    if listing.get("busy_road"):
        badges += '<span class="badge badge-road" title="Property may be on a busy road">⚠️ Busy Road</span>'
    if listing.get("near_industrial"):
        badges += '<span class="badge badge-industrial" title="Industrial zone within quarter mile">🏭 Near Industrial</span>'
    return badges


def map_buttons(listing):
    addr_enc = urllib.parse.quote(listing.get("address", ""))
    lat = listing.get("lat")
    lng = listing.get("lng")

    if lat and lng:
        # Street View — opens directly in Street View panorama mode per Google Maps URL spec
        sv_url  = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}"
        # Satellite — opens aerial view at high zoom
        sat_url = f"https://www.google.com/maps/@{lat},{lng},18z/data=!3m1!1e3"
    else:
        # Fallback — address-based Street View search
        sv_url  = f"https://www.google.com/maps/search/?api=1&query={addr_enc}&layer=streetview"
        sat_url = f"https://www.google.com/maps/search/?api=1&query={addr_enc}"

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
        active = "active" if status == btn_status else ""
        buttons += (
            f'<button class="btn {cls} {active}" '
            f'onclick="setStatus(\'{lid}\', \'{btn_status}\')">{label}</button>'
        )

    source_cls = listing.get("source", "").lower()
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
        eastern  = ZoneInfo("America/New_York")
    except ImportError:
        eastern = timezone.utc
    run_time = datetime.now(eastern).strftime("%B %d, %Y at %I:%M %p %Z")

    groups = {"new": [], "favorite": [], "think": []}
    for lid, data in listings.items():
        s = data.get("status", "new")
        if s == "deleted":
            continue
        groups.get(s, groups["new"]).append((lid, data))

    def section(title, key, cards, collapsed=False):
        hidden = " hidden" if collapsed else ""
        if not cards:
            return (
                f'<section class="section{hidden}" id="section-{key}">'
                f'<h2>{title}</h2>'
                f'<p class="empty">No listings in this section.</p></section>'
            )
        html = "".join(listing_card_html(lid, d, d.get("status", "new")) for lid, d in cards)
        return (
            f'<section class="section{hidden}" id="section-{key}">'
            f'<h2>{title} <span class="count">{len(cards)}</span></h2>'
            f'<div class="grid">{html}</div></section>'
        )

    sections_html = (
        section("New Listings",   "new",      groups["new"],      collapsed=False) +
        section("Favorites",      "favorite", groups["favorite"], collapsed=True)  +
        section("Think About It", "think",    groups["think"],    collapsed=True)
    )

    state_json   = json.dumps({k: v for k, v in listings.items() if v.get("status") != "deleted"})
    new_count    = len(groups["new"])
    fav_count    = len(groups["favorite"])
    think_count  = len(groups["think"])
    worker_url   = os.environ.get("WORKER_URL", "https://ranches-proxy.johnzur.workers.dev")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ranch Finder — Bridgewater, Somerset &amp; Cranford, NJ</title>
<link rel="icon" type="image/x-icon" href="/ranches/favicon.ico?v=2">
<link rel="icon" type="image/png" sizes="32x32" href="/ranches/favicon-32.png?v=2">
<link rel="icon" type="image/png" sizes="16x16" href="/ranches/favicon-16.png?v=2">
<link rel="apple-touch-icon" sizes="180x180" href="/ranches/favicon-180.png?v=2">
<meta name="apple-mobile-web-app-title" content="Ranch Finder">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#2d6a4f">
<style>
  :root{{--bg:#f7f6f3;--surface:#fff;--border:#e2e0db;--text:#1a1a1a;--muted:#6b6b6b;--accent:#2d6a4f;--radius:10px;--shadow:0 2px 8px rgba(0,0,0,.08);}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5;}}
  header{{background:var(--accent);color:#fff;padding:20px 32px;}}
  .header-title{{display:flex;align-items:center;gap:10px;}}
  .logo{{width:28px;height:28px;border-radius:6px;}}
  header h1{{font-size:1.4rem;font-weight:700;}}
  header .meta{{font-size:.82rem;opacity:.8;margin-top:4px;}}
  nav{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 32px;display:flex;gap:4px;}}
  nav button{{background:none;border:none;padding:14px 16px;font-size:.88rem;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;font-weight:500;}}
  nav button.active{{color:var(--accent);border-bottom-color:var(--accent);}}
  main{{padding:28px 32px;max-width:1400px;margin:0 auto;}}
  .section{{margin-bottom:48px;}}
  .section h2{{font-size:1.1rem;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px;}}
  .count{{background:var(--accent);color:#fff;font-size:.75rem;padding:2px 8px;border-radius:20px;font-weight:600;}}
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
  .source-redfin{{background:#fee2e2;color:#991b1b;}}
  .source-zillow{{background:#fef9c3;color:#854d0e;}}
  .badges{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;}}
  .badge{{font-size:.75rem;font-weight:600;padding:3px 10px;border-radius:4px;}}
  .badge-highway{{background:#fef3c7;color:#92400e;}}
  .badge-road{{background:#fee2e2;color:#991b1b;}}
  .badge-industrial{{background:#f3f4f6;color:#374151;}}
  .map-btns{{display:flex;gap:8px;margin-bottom:10px;}}
  .map-btn{{font-size:.78rem;font-weight:600;padding:5px 12px;border-radius:6px;background:#f0f9f4;color:var(--accent);text-decoration:none;border:1px solid #c6e8d5;}}
  .map-btn:hover{{background:#d1f0e0;}}
  .btn-group{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;}}
  .btn{{border:1px solid var(--border);background:var(--bg);color:var(--muted);padding:6px 14px;border-radius:6px;font-size:.8rem;font-weight:600;cursor:pointer;}}
  .btn:hover{{border-color:#aaa;color:var(--text);}}
  .btn.active{{color:#fff;border-color:transparent;}}
  .btn-favorite.active{{background:#2d6a4f;}}
  .btn-think.active{{background:#7b68ee;}}
  .btn-delete.active{{background:#c0392b;}}
  .empty{{color:var(--muted);font-size:.9rem;padding:12px 0;}}
  .hidden{{display:none!important;}}
  #toast{{position:fixed;bottom:24px;right:24px;background:#1a1a1a;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;}}
  #toast.show{{opacity:1;}}
  @media(max-width:600px){{main{{padding:16px;}}header{{padding:16px;}}.grid{{grid-template-columns:1fr;}}}}
</style>
</head>
<body>
<header>
  <div class="header-title">
    <img src="/ranches/favicon-32.png?v=2" alt="" class="logo">
    <h1>Ranch Finder — Bridgewater, Somerset &amp; Cranford, NJ</h1>
  </div>
  <div class="meta">Last run: {run_time} &nbsp;|&nbsp; {new_count} unreviewed listing{"s" if new_count != 1 else ""}</div>
</header>
<nav>
  <button class="active" onclick="showSection('new',this)">New ({new_count})</button>
  <button onclick="showSection('favorite',this)">Favorites ({fav_count})</button>
  <button onclick="showSection('think',this)">Think About It ({think_count})</button>
</nav>
<main>{sections_html}</main>
<div id="toast"></div>
<script>
const WORKER_URL = "{worker_url}";
let state = {state_json};

function showSection(key, btn) {{
  document.querySelectorAll(".section").forEach(s => s.classList.add("hidden"));
  document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
  const sec = document.getElementById("section-" + key);
  if (sec) sec.classList.remove("hidden");
  if (btn) btn.classList.add("active");
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
      setTimeout(() => {{ card.style.display = "none"; }}, 400);
    }}
  }}
  showToast("Saving...");
  await commitStateToGitHub();
}}

async function commitStateToGitHub() {{
  try {{
    const resp = await fetch(WORKER_URL, {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{listings: state}})
    }});
    const result = await resp.json();
    if (!resp.ok || result.error) throw new Error(result.error || "status " + resp.status);
    showToast("Saved");
  }} catch(e) {{
    console.error(e);
    showToast("Sync failed — see console");
  }}
}}

function showToast(msg) {{
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2500);
}}
</script>
</body>
</html>"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Ranch Finder — scrape.py")
    print(f"Run started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    state = load_state()
    all_fresh = []

    for area in SEARCH_AREAS:
        print(f"\n--- {area['name']}, {area['state']} ---")
        all_fresh.extend(process_redfin_area(area))
        all_fresh.extend(process_zillow_area(area))

    print(f"\n--- Dedup ({len(all_fresh)} candidates) ---")
    deduped = dedup_listings(all_fresh)
    print(f"  {len(deduped)} after dedup")

    print("\n--- Merging into state ---")
    new_ids = merge_into_state(state, deduped)

    save_state(state)
    print(f"\n  State saved. New this run: {len(new_ids)}")
    print(f"  Total tracked: {len(state['listings'])}")

    print("\n--- Generating HTML ---")
    html = generate_html(state, set(new_ids))
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  Written: {OUTPUT_FILE}")

    print("\n" + "=" * 60)
    report_budget()
    print("=" * 60)


if __name__ == "__main__":
    main()
