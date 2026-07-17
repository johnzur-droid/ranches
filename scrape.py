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


def nominatim_road_class(lat, lng):
    """
    Reverse geocode with Nominatim to get the road the property sits on
    and its OSM highway classification.
    Returns (road_name, highway_class) or (None, None) on failure.
    Rate limit: 1 req/sec — caller must manage spacing.
    """
    params = urllib.parse.urlencode({
        "lat":            lat,
        "lon":            lng,
        "format":         "jsonv2",
        "zoom":           16,
        "addressdetails": 1,
        "extratags":      1,
    })
    url = f"https://nominatim.openstreetmap.org/reverse?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "RanchFinder/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        road_name = data.get("address", {}).get("road")
        hw_class  = data.get("type")   # residential, tertiary, secondary, primary, trunk, motorway
        return road_name, hw_class
    except Exception as e:
        print(f"  Nominatim error: {e}", file=sys.stderr)
        return None, None

# OSM highway class → human-readable road label
ROAD_CLASS_LABEL = {
    "motorway":    "🚨 Highway/Motorway",
    "trunk":       "🚨 Major Highway",
    "primary":     "🔴 Primary Arterial",
    "secondary":   "🟠 Secondary Road",
    "tertiary":    "🟡 Minor Through-Road",
    "unclassified":"🏘️ Local Road",
    "residential": "🏘️ Residential Street",
    "service":     "🟡 Service/Access Road",
}

def check_location_risk(address, lat, lng):
    """
    Run location risk checks for a confirmed listing.
    Returns dict with keys:
      property_road:  str — human-readable road classification of property's street
      busy_road:      bool
      near_highway:   bool
      highway_roads:  list of {name, distance_miles} sorted by distance
    All empty/False if Maps key not set or geocoding fails.
    """
    result = {
        "property_road": "",
        "busy_road":     False,
        "near_highway":  False,
        "highway_roads": [],
    }

    if not MAPS_KEY:
        return result

    # Geocode if coordinates not already known
    if lat is None or lng is None:
        lat, lng = geocode_address(address)
    if lat is None:
        return result

    # Nominatim road classification — what road is the house actually on?
    import time
    road_name, hw_class = nominatim_road_class(lat, lng)
    if road_name and hw_class:
        label = ROAD_CLASS_LABEL.get(hw_class, f"🏘️ {hw_class.capitalize()}")
        result["property_road"] = f"{label} ({road_name})"
    elif road_name:
        result["property_road"] = f"🏘️ {road_name}"
    time.sleep(1)  # Nominatim rate limit

    latlng = f"{lat},{lng}"

    # Highway check — Places nearbysearch for routes within proximity radius
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
                if ploc:
                    dist = haversine_miles(lat, lng, ploc["lat"], ploc["lng"])
                else:
                    dist = None
                result["near_highway"] = True
                result["highway_roads"].append({
                    "name":           name,
                    "distance_miles": round(dist, 2) if dist is not None else None,
                })
            if any(kw in name_lower for kw in HIGHWAY_KEYWORDS):
                result["busy_road"] = True

    result["highway_roads"].sort(
        key=lambda r: r["distance_miles"] if r["distance_miles"] is not None else 999
    )

    return result

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def fmt_price(p):
    if p is None:
        return "N/A"
    # Unwrap Zillow price dict e.g. {'value': 749000, 'pricePerSquareFoot': 367}
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
    # Bare integer string — Redfin returns sqft as e.g. '11270'
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
    garage_spaces   = None
    garage_desc     = []

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
                elif name == "# Of Garage Spaces" and vals:
                    try:
                        garage_spaces = int(vals[0])
                    except (ValueError, TypeError):
                        pass
                elif name == "Garage Description" and vals:
                    garage_desc = [str(v) for v in (entry.get("amenityValues") or [])]

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

    # Basement label — stored on listing, shown as badge (not a hard filter)
    desc_has_basement = (
        any(kw in description for kw in BASEMENT_KEYWORDS) or
        any(kw in room_desc_text for kw in BASEMENT_KEYWORDS)
    )
    if basement_yn:
        val = room_desc_text + " " + description
        if "finish" in val:
            basement_label = "✅ Finished Basement"
        elif "unfinish" in val:
            basement_label = "✅ Unfinished Basement"
        else:
            basement_label = "✅ Basement"
    elif basement_filled:
        basement_label = "✅ Basement"
    elif desc_has_basement:
        basement_label = "⚠️ Basement Unconfirmed"
    elif basement_yn is False:
        basement_label = "❌ No Basement"
    else:
        basement_label = ""

    # Garage label — from Parking Information amenity group
    if garage_spaces is not None and garage_spaces > 0:
        desc_parts = ", ".join(garage_desc) if garage_desc else ""
        if desc_parts:
            garage_label = f"🚗 {garage_spaces}-car garage ({desc_parts})"
        else:
            garage_label = f"🚗 {garage_spaces}-car garage"
    elif garage_spaces == 0:
        garage_label = "🚗 No garage"
    elif any(kw in description for kw in ["garage", "carport"]):
        garage_label = "🚗 Garage (from description)"
    else:
        garage_label = "🚗 Unknown"

    return is_ranch, basement_label, garage_label


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

        # Pre-detail filters — saves API calls
        addr_info   = r.get("addressInfo") or {}
        addr_street = addr_info.get("formattedStreetLine") or ""
        addr_state  = addr_info.get("state") or addr_info.get("stateCode") or ""

        # NJ-only filter
        if addr_state and addr_state.upper() != "NJ":
            print(f"    skip (out of state: {addr_state}): {addr_street}")
            continue

        # Unit/condo filter — drop addresses with unit indicators
        if re.search(r'#\s*\d+|\bunit\b|\bapt\b|\bsuite\b|\bste\b', addr_street, re.IGNORECASE):
            print(f"    skip (unit/condo): {addr_street}")
            continue

        details = redfin_details(property_id, listing_id)
        is_ranch, basement_label, garage_label = redfin_is_ranch(details)

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

        lot_info = r.get("lotSize") or {}
        lot = lot_info.get("amount") if isinstance(lot_info, dict) else lot_info

        rel_url  = r.get("url") or r.get("href") or r.get("detailUrl") or ""
        full_url = f"https://www.redfin.com{rel_url}" if rel_url.startswith("/") else rel_url

        # Google Maps enrichment
        risk = check_location_risk(addr, None, None)

        photo_url = ""
        photo_urls = r.get("photoUrls") or {}
        medium = photo_urls.get("mediumRes") or []
        if medium:
            photo_url = medium[0] if isinstance(medium, list) else medium

        listings.append({
            "id":             f"redfin_{property_id}",
            "address":        addr,
            "price":          price,
            "beds":           beds,
            "baths":          baths,
            "lot_sqft":       lot,
            "url":            full_url,
            "source":         "Redfin",
            "photo_url":      photo_url,
            "basement_label": basement_label,
            "garage_label":   garage_label,
            "property_road":  risk["property_road"],
            "busy_road":      risk["busy_road"],
            "near_highway":   risk["near_highway"],
            "highway_roads":  risk["highway_roads"],
        })
        print(f"    PASS: {addr} {fmt_price(price)} | {basement_label or 'no basement data'} | {garage_label} | {risk['property_road'] or 'road unknown'}")

    return listings

# ---------------------------------------------------------------------------
# Zillow
# ---------------------------------------------------------------------------

def zillow_search(area):
    """
    Zillow /search/byaddress — single search using keywords=ranch.
    Confirmed S005: singleStoryOnly=true tested but overlap with keywords=ranch
    is very high — only 1 additional listing found across Somerset test.
    Not worth the extra API call per town per run.
    bathrooms=TwoPlus confirmed working on byaddress (not bycoordinates).
    listing_status=For_Sale (not status=forSale) — confirmed S003.
    bed_min=3 — updated S003 per JZ requirement.
    Basement filter removed S005 — basement is a badge not a hard filter.
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

    # --- Basement label (not a hard filter — stored on listing, shown as badge) ---
    basement_yn  = reso.get("basementYN")
    basement_str = str(reso.get("basement") or "").lower()

    if isinstance(basement_yn, bool):
        yn_confirmed = basement_yn
    elif isinstance(basement_yn, str):
        yn_confirmed = basement_yn.strip().lower() in ("yes", "true", "1")
    else:
        yn_confirmed = None

    desc_has_basement = any(kw in desc for kw in BASEMENT_KEYWORDS)

    if yn_confirmed is True:
        if "finish" in basement_str and "unfinish" not in basement_str:
            basement_label = "✅ Finished Basement"
        elif "unfinish" in basement_str:
            basement_label = "✅ Unfinished Basement"
        elif basement_str and basement_str not in ("none", ""):
            basement_label = "✅ Basement"
        else:
            basement_label = "✅ Basement"
    elif basement_str and basement_str not in ("none", ""):
        basement_label = "✅ Basement"
    elif desc_has_basement:
        basement_label = "⚠️ Basement Unconfirmed"
    elif yn_confirmed is False:
        basement_label = "❌ No Basement"
    else:
        basement_label = ""

    # --- Garage label ---
    has_garage     = reso.get("hasGarage")
    has_attached   = reso.get("hasAttachedGarage")
    garage_cap     = reso.get("garageParkingCapacity")
    parking_feats  = reso.get("parkingFeatures") or []
    # Filter parkingFeatures to garage-relevant entries only
    garage_feats   = [f for f in parking_feats
                      if any(kw in f.lower() for kw in ["garage","attached","detached"])]

    if has_garage:
        parts = []
        if garage_cap:
            parts.append(f"{garage_cap}-car")
        if has_attached:
            parts.append("attached")
        elif garage_feats:
            # Strip trailing 'garage' from feature string to avoid double word
            feat = garage_feats[0].lower()
            feat_clean = re.sub(r'\bgarage\b', '', feat, flags=re.IGNORECASE).strip(" ,")
            if feat_clean:
                parts.append(feat_clean)
        if parts:
            garage_label = f"🚗 {' '.join(parts)} garage"
        else:
            garage_label = "🚗 Garage"
    elif has_garage is False:
        garage_label = "🚗 No garage"
    elif any(kw in desc for kw in ["garage", "carport"]):
        garage_label = "🚗 Garage (from description)"
    else:
        garage_label = "🚗 Unknown"

    return is_ranch, basement_label, garage_label


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

        # Pre-detail filters — saves API calls
        addr_info  = r.get("address") or {}
        z_street   = addr_info.get("streetAddress") or "" if isinstance(addr_info, dict) else ""
        z_state    = addr_info.get("state") or addr_info.get("state_code") or "" if isinstance(addr_info, dict) else ""

        # NJ-only filter
        if z_state and z_state.upper() != "NJ":
            print(f"    skip (out of state: {z_state}): {z_street}")
            continue

        # Unit/condo filter
        if re.search(r'#\s*\d+|\bunit\b|\bapt\b|\bsuite\b|\bste\b', z_street, re.IGNORECASE):
            print(f"    skip (unit/condo): {z_street}")
            continue

        details = zillow_details(zpid)
        is_ranch, basement_label, garage_label = zillow_is_ranch(details)

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

        lot_info = r.get("lotSizeWithUnit") or {}
        if isinstance(lot_info, dict):
            lot_val  = lot_info.get("lotSize")
            lot_unit = (lot_info.get("lotSizeUnit") or "").lower()
            lot = f"{lot_val} acres" if (lot_val and "acre" in lot_unit) else lot_val
        else:
            lot = r.get("lotAreaValue") or r.get("lot_sqft")

        # Google Maps enrichment
        risk = check_location_risk(addr, None, None)

        photo_url = ""
        media = r.get("media") or {}
        links = media.get("propertyPhotoLinks") or {}
        if links.get("mediumSizeLink"):
            photo_url = links["mediumSizeLink"]

        listings.append({
            "id":             f"zillow_{zpid}",
            "address":        addr,
            "price":          price,
            "beds":           beds,
            "baths":          baths,
            "lot_sqft":       lot,
            "url":            f"https://www.zillow.com/homedetails/{zpid}_zpid/",
            "source":         "Zillow",
            "photo_url":      photo_url,
            "basement_label": basement_label,
            "garage_label":   garage_label,
            "property_road":  risk["property_road"],
            "busy_road":      risk["busy_road"],
            "near_highway":   risk["near_highway"],
            "highway_roads":  risk["highway_roads"],
        })
        print(f"    PASS: {addr} {fmt_price(price)} | {basement_label or 'no basement data'} | {garage_label} | {risk['property_road'] or 'road unknown'}")

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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def merge_into_state(state, fresh_listings):
    listings = state["listings"]
    new_ids  = []
    today    = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build normalized address index of existing state to catch cross-run dupes
    existing_norm = {}
    for lid, data in listings.items():
        key = normalize_address(data.get("address", ""))
        if key:
            existing_norm[key] = lid

    for listing in fresh_listings:
        lid = listing["id"]

        # Check if same normalized address already exists under a different ID
        norm_key = normalize_address(listing.get("address", ""))
        if norm_key and norm_key in existing_norm and existing_norm[norm_key] != lid:
            existing_lid = existing_norm[norm_key]
            print(f"  dedup drop (cross-run dupe of {existing_lid}): {listing['address']}")
            continue

        if lid in listings:
            existing_status = listings[lid].get("status", "new")
            if existing_status in ("favorite", "think", "deleted"):
                # Preserve user decision — update price + risk flags + labels silently
                listings[lid]["price"]             = listing["price"]
                listings[lid]["property_road"]     = listing.get("property_road", "")
                listings[lid]["busy_road"]         = listing.get("busy_road", False)
                listings[lid]["near_highway"]      = listing.get("near_highway", False)
                listings[lid]["highway_roads"]     = listing.get("highway_roads", [])
                listings[lid]["basement_label"]    = listing.get("basement_label", "")
                listings[lid]["garage_label"]      = listing.get("garage_label", "🚗 Unknown")
                # Never overwrite Christine's decisions on re-scrape
                # christine_favorite and christine_pass preserved as-is
            else:
                listing["status"]     = "new"
                listing["first_seen"] = listings[lid].get("first_seen", today)
                listings[lid] = listing
        else:
            listing["status"]     = "new"
            listing["first_seen"] = today
            listing["run_date"]   = today
            listings[lid] = listing
            existing_norm[norm_key] = lid  # register so later dupes in same batch also caught
            new_ids.append(lid)
            print(f"  NEW: {listing['address']} {fmt_price(listing['price'])}")

    state["listings"] = listings
    return new_ids

# ---------------------------------------------------------------------------
# HTML generation — shell only, all rendering done client-side via JS
# ---------------------------------------------------------------------------

def _DELETED_risk_badges(listing):
    badges = ""

    # Basement badge
    bl = listing.get("basement_label") or ""
    if bl:
        if bl.startswith("✅"):
            badges += f'<span class="badge badge-basement-yes">{bl}</span>'
        elif bl.startswith("⚠️"):
            badges += f'<span class="badge badge-basement-maybe">{bl}</span>'
        elif bl.startswith("❌"):
            badges += f'<span class="badge badge-basement-no">{bl}</span>'

    # Highway badges
    highway_roads = listing.get("highway_roads") or []
    if highway_roads:
        for road in highway_roads:
            name = road.get("name", "Highway")
            dist = road.get("distance_miles")
            dist_str = f" — {dist} mi" if dist is not None else ""
            badges += f'<span class="badge badge-highway">🛣️ {name}{dist_str}</span>'
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
        # Street View — opens directly in Street View panorama mode per Google Maps URL spec
        sv_url  = f"https://www.google.com/maps/@?api=1&map_action=pano&viewpoint={lat},{lng}"
        # Satellite — forces aerial/satellite layer at high zoom
        sat_url = f"https://www.google.com/maps/@{lat},{lng},18z/data=!3m1!1e3"
    else:
        # Fallback — address-based; layer=streetview for Street View,
        # t=k satellite tile type for satellite view
        sv_url  = f"https://www.google.com/maps/search/?api=1&query={addr_enc}&layer=streetview"
        sat_url = f"https://www.google.com/maps/search/?api=1&query={addr_enc}&t=k"

    return (
        f'<a href="{sv_url}" target="_blank" class="map-btn">📷 Street View</a>'
        f'<a href="{sat_url}" target="_blank" class="map-btn">🛰️ Satellite</a>'
    )


def listing_card_html(lid, listing, status):
    # Action buttons — John controls all
    buttons = ""
    for btn_status, label, cls in [
        ("favorite", "❤️ Favorite",    "btn-favorite"),
        ("think",    "🤔 Maybe",        "btn-think"),
        ("deleted",  "🗑️ Delete",       "btn-delete"),
    ]:
        active   = "active" if status == btn_status else ""
        buttons += (
            f'<button class="btn {cls} {active}" '
            f'onclick="setStatus(\'{lid}\', \'{btn_status}\')">{label}</button>'
        )

    # Christine's buttons — only shown when John has favorited
    christine_fav  = listing.get("christine_favorite", False)
    christine_pass = listing.get("christine_pass", False)
    christine_btns = ""
    if status == "favorite":
        c_heart_active = "active" if christine_fav else ""
        c_pass_active  = "active" if christine_pass else ""
        c_heart_icon   = "❤️" if christine_fav else "🤍"
        christine_btns = (
            f'<button class="btn btn-christine {c_heart_active}" '
            f'onclick="toggleChristine(\'{lid}\')">{c_heart_icon} Christine</button>'
            f'<button class="btn btn-christine-pass {c_pass_active}" '
            f'onclick="christinePass(\'{lid}\')">👎 Not Interested</button>'
        )

    source_cls   = listing.get("source", "").lower()
    source_badge = f'<span class="source-badge source-{source_cls}">{listing.get("source","")}</span>'

    badges        = risk_badges(listing)
    maps          = map_buttons(listing)
    garage_label  = listing.get("garage_label") or ""
    property_road = listing.get("property_road") or ""

    # Christine's pass indicator — shown on card in favorites when she's not interested
    c_pass_indicator = ""
    if status == "favorite" and christine_pass:
        c_pass_indicator = '<div class="christine-pass-indicator">👎 Christine not interested</div>'

    return f"""
<div class="card" id="card-{lid}" data-status="{status}" data-id="{lid}" data-christine="{str(christine_fav).lower()}" data-christine-pass="{str(christine_pass).lower()}">
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
    {f'<div class="garage-line">{garage_label}</div>' if garage_label else ''}
    {f'<div class="road-line">{property_road}</div>' if property_road else ''}
    {c_pass_indicator}
    <div class="map-btns">{maps}</div>
    <div class="btn-group">{buttons}{christine_btns}</div>
  </div>
</div>"""


def generate_html(state, new_ids):
    """
    Generate the shell HTML page. No listings embedded — all data fetched
    live from the worker on page load. Scraper owns state.json; browser
    owns only status/christine fields via delta saves.
    """
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except ImportError:
        eastern = timezone.utc
    run_time = datetime.now(eastern).strftime("%B %d, %Y at %I:%M %p %Z")

    worker_url = os.environ.get("WORKER_URL", "https://ranches-proxy.johnzur.workers.dev")
    return _shell_html(run_time, worker_url)


def _shell_html(run_time, worker_url):
    """Pure shell — no listings embedded. All data fetched live from worker."""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Ranch Finder — Bridgewater, Somerset &amp; Cranford, NJ</title>
<style>
  :root{{--bg:#f7f6f3;--surface:#fff;--border:#e2e0db;--text:#1a1a1a;--muted:#6b6b6b;--accent:#2d6a4f;--radius:10px;--shadow:0 2px 8px rgba(0,0,0,.08);}}
  *{{box-sizing:border-box;margin:0;padding:0;}}
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5;}}
  .sticky-top{{position:sticky;top:0;z-index:100;}}
  header{{background:var(--accent);color:#fff;padding:14px 32px;}}
  header h1{{font-size:1.2rem;font-weight:700;}}
  header .meta{{font-size:.78rem;opacity:.8;margin-top:2px;}}
  nav{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 32px;display:flex;gap:2px;overflow-x:auto;}}
  nav button{{background:none;border:none;padding:12px 10px;font-size:.85rem;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;font-weight:500;white-space:nowrap;}}
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
  .source-redfin{{background:#fee2e2;color:#991b1b;}}
  .source-realtor-com{{background:#e0f2fe;color:#0c4a6e;}}
  .badges{{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;}}
  .badge{{font-size:.75rem;font-weight:600;padding:3px 10px;border-radius:4px;}}
  .badge-basement-yes{{background:#dcfce7;color:#166534;}}
  .badge-basement-maybe{{background:#fef9c3;color:#854d0e;}}
  .badge-basement-no{{background:#f3f4f6;color:#6b7280;}}
  .badge-highway{{background:#fef3c7;color:#92400e;}}
  .badge-road{{background:#fee2e2;color:#991b1b;}}
  .info-line{{font-size:.8rem;color:var(--muted);margin-bottom:6px;}}
  .christine-pass-indicator{{font-size:.78rem;color:#6b7280;margin-bottom:6px;font-style:italic;}}
  .map-btns{{display:flex;gap:8px;margin-bottom:10px;}}
  .map-btn{{font-size:.78rem;font-weight:600;padding:5px 12px;border-radius:6px;background:#f0f9f4;color:var(--accent);text-decoration:none;border:1px solid #c6e8d5;}}
  .map-btn:hover{{background:#d1f0e0;}}
  .btn-group{{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px;}}
  .btn{{border:1px solid var(--border);background:var(--bg);color:var(--muted);padding:6px 14px;border-radius:6px;font-size:.8rem;font-weight:600;cursor:pointer;transition:all .15s;}}
  .btn:hover{{border-color:#aaa;color:var(--text);}}
  .btn.active{{color:#fff;border-color:transparent;}}
  .btn-favorite.active{{background:#2d6a4f;}}
  .btn-think.active{{background:#7b68ee;}}
  .btn-delete.active{{background:#c0392b;}}
  .btn-restore{{border-color:#86efac;color:#166534;}}
  .btn-restore:hover{{background:#dcfce7;}}
  .btn-christine.active{{background:#e11d48;border-color:transparent;color:#fff;}}
  .btn-christine-pass{{border-color:#d1d5db;color:#6b7280;}}
  .btn-christine-pass.active{{background:#6b7280;border-color:transparent;color:#fff;}}
  .empty{{color:var(--muted);font-size:.9rem;padding:12px 0;}}
  .hidden{{display:none!important;}}
  .card-photo{{width:100%;height:180px;object-fit:cover;cursor:pointer;display:block;border-bottom:1px solid var(--border);}}
  .card-photo-placeholder{{width:100%;height:60px;background:var(--bg);display:flex;align-items:center;justify-content:center;color:var(--muted);font-size:.8rem;}}
  #lightbox{{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);z-index:9999;align-items:center;justify-content:center;cursor:pointer;}}
  #lightbox.open{{display:flex;}}
  #lightbox img{{max-width:95vw;max-height:95vh;object-fit:contain;border-radius:6px;}}
  #error-banner{{background:#fef2f2;border:1px solid #fca5a5;color:#991b1b;padding:12px 32px;font-size:.9rem;display:none;}}
  #stale-banner{{background:#fef9c3;border-bottom:1px solid #fde68a;color:#92400e;padding:10px 32px;font-size:.85rem;text-align:center;display:none;cursor:pointer;}}
  #toast{{position:fixed;bottom:24px;right:24px;background:#1a1a1a;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;}}
  #toast.show{{opacity:1;}}
  #toast.error{{background:#c0392b;}}
  #scroll-top{{position:fixed;bottom:72px;right:24px;width:40px;height:40px;border-radius:50%;background:var(--accent);color:#fff;border:none;font-size:1.1rem;cursor:pointer;display:none;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.2);z-index:998;}}
  #scroll-top.visible{{display:flex;}}
  @media(max-width:600px){{main{{padding:16px;}}header{{padding:12px 16px;}}.grid{{grid-template-columns:1fr;}}nav{{padding:0 4px;}}nav button{{padding:10px 6px;font-size:.72rem;}}}}
</style>
</head>
<body>
<div id="stale-banner" onclick="location.reload()">🔄 New listings available — tap to refresh</div>
<div class="sticky-top">
  <header>
    <h1>Ranch Finder — Bridgewater, Somerset &amp; Cranford, NJ</h1>
    <div class="meta" id="header-meta">Last scrape: {run_time}</div>
  </header>
  <nav id="main-nav">
    <button class="active" onclick="showTab('new-this-week',this)">🆕 This Week (<span id="nav-new-this-week">0</span>)</button>
    <button onclick="showTab('unreviewed',this)">📋 Queue (<span id="nav-unreviewed">0</span>)</button>
    <button onclick="showTab('favorite',this)">⭐ Favorites (<span id="nav-favorite">0</span>)</button>
    <button onclick="showTab('both',this)">💑 (<span id="nav-both">0</span>)</button>
    <button onclick="showTab('think',this)">🤔 Maybe (<span id="nav-think">0</span>)</button>
    <button onclick="showTab('deleted',this)">🗑️ Deleted (<span id="nav-deleted">0</span>)</button>
  </nav>
</div>
<div id="error-banner"></div>
<main>
  <div id="loading">Loading listings…</div>
  <div id="tab-new-this-week" class="tab-pane hidden"></div>
  <div id="tab-unreviewed"    class="tab-pane hidden"></div>
  <div id="tab-favorite"      class="tab-pane hidden"></div>
  <div id="tab-both"          class="tab-pane hidden"></div>
  <div id="tab-think"         class="tab-pane hidden"></div>
  <div id="tab-deleted"       class="tab-pane hidden"></div>
</main>
<div id="toast"></div>
<div id="lightbox" onclick="closeLightbox()"><img id="lightbox-img" src="" alt="Property photo"></div>
<button id="scroll-top" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑</button>

<script>
// ── Constants ────────────────────────────────────────────────────────────────
const WORKER_URL = "{worker_url}";
const DAYS_NEW   = 7;   // listings first_seen within N days appear in "This Week"

// ── State ────────────────────────────────────────────────────────────────────
let state       = {{}};  // {{id: listing, ...}}
let loadedSha   = null;  // SHA of state.json at load time
let activeTab   = "new-this-week";
let saveQueue   = null;  // pending save timeout handle
let saving      = false; // save in flight

// ── Boot ─────────────────────────────────────────────────────────────────────
(async function boot() {{
  try {{
    const resp = await fetch(WORKER_URL, {{method:"GET"}});
    if (!resp.ok) throw new Error("Worker returned " + resp.status);
    const data = await resp.json();
    if (data.error) throw new Error(data.error);
    state     = data.listings || {{}};
    loadedSha = data.sha || null;
    document.getElementById("loading").style.display = "none";
    renderAll();
    showTab("new-this-week", document.querySelector("nav button"));
  }} catch(err) {{
    document.getElementById("loading").style.display = "none";
    const eb = document.getElementById("error-banner");
    eb.textContent = "⚠️ Could not load listings: " + err.message + " — try refreshing.";
    eb.style.display = "block";
  }}
}})();

// ── Rendering ────────────────────────────────────────────────────────────────
function fmtPrice(p) {{
  if (p == null) return "N/A";
  if (typeof p === "object") p = p.value || p.amount || p.price;
  if (p == null) return "N/A";
  const n = parseFloat(String(p).replace(/,/g,""));
  if (isNaN(n)) return String(p);
  return "$" + n.toLocaleString();
}}

function fmtLot(v) {{
  if (v == null) return "";
  const s = String(v).trim();
  if (!s || s === "null") return "";
  // Already formatted string — return as-is
  if (isNaN(parseFloat(s.replace(/,/g,"")))) return s;
  // Raw sqft number
  const sqft = parseFloat(s.replace(/,/g,""));
  if (isNaN(sqft)) return s;
  const acres = (sqft / 43560).toFixed(2);
  return sqft.toLocaleString() + " sqft (" + acres + " ac)";
}}

function isNewThisWeek(listing) {{
  const fs = listing.first_seen || listing.run_date || "";
  if (!fs) return false;
  const d = new Date(fs + "T00:00:00Z");
  return (Date.now() - d.getTime()) / 86400000 <= DAYS_NEW;
}}

function groupByStatus() {{
  const groups = {{unreviewed:[], favorite:[], both:[], think:[], deleted:[]}};
  for (const [id, L] of Object.entries(state)) {{
    const s = L.status || "new";
    if (s === "deleted") {{
      groups.deleted.push([id,L]);
      continue;
    }}
    if (s === "favorite") {{
      if (L.christine_favorite) groups.both.push([id,L]);
      else groups.favorite.push([id,L]);
    }} else if (s === "think") {{
      groups.think.push([id,L]);
    }} else {{
      groups.unreviewed.push([id,L]);
    }}
  }}
  // Sort unreviewed: run_date desc, then first_seen desc, then last_modified desc
  groups.unreviewed.sort((a,b) => {{
    const rd = (b[1].run_date||b[1].first_seen||"").localeCompare(a[1].run_date||a[1].first_seen||"");
    if (rd !== 0) return rd;
    const lm = (b[1].last_modified||"").localeCompare(a[1].last_modified||"");
    return lm !== 0 ? lm : (b[1].first_seen||"").localeCompare(a[1].first_seen||"");
  }});
  return groups;
}}

function renderCard(id, L) {{
  const status   = L.status || "new";
  const srcCls   = (L.source||"").toLowerCase().replace(/[.]/g,"-");
  const srcLabel = L.source || "";

  // Badges
  let badges = "";
  const bl = L.basement_label || "";
  if (bl.startsWith("✅")) badges += `<span class="badge badge-basement-yes">${{bl}}</span>`;
  else if (bl.startsWith("⚠️")) badges += `<span class="badge badge-basement-maybe">${{bl}}</span>`;
  else if (bl.startsWith("❌")) badges += `<span class="badge badge-basement-no">${{bl}}</span>`;
  const hw = L.highway_roads || [];
  if (hw.length) {{
    hw.forEach(r => {{
      const dist = r.distance_miles != null ? ` — ${{r.distance_miles}} mi` : "";
      badges += `<span class="badge badge-highway">🛣️ ${{r.name}}${{dist}}</span>`;
    }});
  }} else if (L.near_highway) {{
    badges += `<span class="badge badge-highway">🛣️ Near Highway</span>`;
  }}
  if (L.busy_road) badges += `<span class="badge badge-road">⚠️ Busy Road</span>`;

  // Map buttons — address-based fallback (no lat/lng stored yet)
  const addrEnc = encodeURIComponent(L.address || "");
  const svUrl   = `https://www.google.com/maps/search/?api=1&query=${{addrEnc}}&layer=streetview`;
  const satUrl  = `https://www.google.com/maps/search/?api=1&query=${{addrEnc}}&t=k`;

  // John's action buttons — active state + toggle
  const favActive  = status === "favorite" ? " active" : "";
  const thinkActive= status === "think"    ? " active" : "";
  const delActive  = status === "deleted"  ? " active" : "";

  // Christine buttons — only rendered when John has favorited
  let christineBtns = "";
  if (status === "favorite") {{
    const cFav  = L.christine_favorite || false;
    const cPass = L.christine_pass     || false;
    const cHeart= cFav  ? "❤️" : "🤍";
    christineBtns = `
      <button class="btn btn-christine${{cFav?" active":""}}" onclick="clickChristineHeart('${{id}}')">${{cHeart}} Christine</button>
      <button class="btn btn-christine-pass${{cPass?" active":""}}" onclick="clickChristinePass('${{id}}')">👎 Not Interested</button>`;
  }}

  // Christine pass indicator
  const passInd = (status === "favorite" && L.christine_pass)
    ? `<div class="christine-pass-indicator">👎 Christine not interested</div>` : "";

  const photoHtml = L.photo_url
    ? `<img class="card-photo" src="${{L.photo_url}}" alt="Property photo" onclick="openLightbox('${{L.photo_url}}')" loading="lazy">`
    : `<div class="card-photo-placeholder">No photo available</div>`;

  return `
<div class="card" id="card-${{id}}" data-id="${{id}}" data-status="${{status}}">
  ${{photoHtml}}
  <div class="card-body">
    <h3 class="address"><a href="${{L.url||"#"}}" target="_blank">${{L.address||""}}</a></h3>
    <div class="stats">
      <span class="price">${{fmtPrice(L.price)}}</span>
      <span class="stat">${{L.beds||"?"}} bd</span>
      <span class="stat">${{L.baths||"?"}} ba</span>
      ${{L.lot_sqft ? `<span class="stat">${{fmtLot(L.lot_sqft)}}</span>` : ""}}
      <span class="source-badge source-${{srcCls}}">${{srcLabel}}</span>
    </div>
    ${{badges ? `<div class="badges">${{badges}}</div>` : ""}}
    ${{L.garage_label  ? `<div class="info-line">${{L.garage_label}}</div>`  : ""}}
    ${{L.property_road ? `<div class="info-line">${{L.property_road}}</div>` : ""}}
    ${{passInd}}
    <div class="map-btns">
      <a href="${{svUrl}}"  target="_blank" class="map-btn">📷 Street View</a>
      <a href="${{satUrl}}" target="_blank" class="map-btn">🛰️ Satellite</a>
    </div>
    <div class="btn-group">
      <button class="btn btn-favorite${{favActive}}"  onclick="setStatus('${{id}}','favorite')">❤️ Favorite</button>
      <button class="btn btn-think${{thinkActive}}"   onclick="setStatus('${{id}}','think')">🤔 Maybe</button>
      <button class="btn btn-delete${{delActive}}"    onclick="setStatus('${{id}}','deleted')">🗑️ Delete</button>
      ${{status === "deleted" ? `<button class="btn btn-restore" onclick="restoreListing('${{id}}')">↩️ Restore</button>` : ""}}
      ${{christineBtns}}
    </div>
  </div>
</div>`;
}}

function renderAll() {{
  const groups = groupByStatus();
  const newThisWeek = groups.unreviewed.filter(([,L]) => isNewThisWeek(L));

  // Tab: New This Week — filtered view, cards NOT moved out of unreviewed
  const ntwPane = document.getElementById("tab-new-this-week");
  if (newThisWeek.length === 0) {{
    ntwPane.innerHTML = '<p class="empty">No new listings this week.</p>';
  }} else {{
    ntwPane.innerHTML = `<div class="grid">${{newThisWeek.map(([id,L]) => renderCard(id,L)).join("")}}</div>`;
  }}

  // Tab: Unreviewed — grouped by run_date
  const unrevPane = document.getElementById("tab-unreviewed");
  if (groups.unreviewed.length === 0) {{
    unrevPane.innerHTML = '<p class="empty">No unreviewed listings.</p>';
  }} else {{
    const byDate = {{}};
    groups.unreviewed.forEach(([id,L]) => {{
      const rd = L.run_date || L.first_seen || "Unknown";
      if (!byDate[rd]) byDate[rd] = [];
      byDate[rd].push([id,L]);
    }});
    unrevPane.innerHTML = Object.entries(byDate).map(([rd,items]) => `
      <div class="run-group">
        <div class="run-group-header">
          <span class="run-date-label">Run: ${{rd}}</span>
          <button class="delete-all-btn" onclick="deleteAll(${{JSON.stringify(items.map(([id])=>id))}})">Delete All (${{items.length}})</button>
        </div>
        <div class="grid">${{items.map(([id,L]) => renderCard(id,L)).join("")}}</div>
      </div>`).join("");
  }}

  // Tab: Favorites
  renderSimpleTab("tab-favorite", groups.favorite, "No favorites yet.");

  // Tab: Both Love It
  renderSimpleTab("tab-both", groups.both, "Nothing in Both Love It yet.");

  // Tab: Maybe
  renderSimpleTab("tab-think", groups.think, "Nothing in Maybe yet.");

  // Tab: Deleted
  renderSimpleTab("tab-deleted", groups.deleted, "No deleted listings.");

  updateNavCounts(groups, newThisWeek.length);
}}

function renderSimpleTab(paneId, items, emptyMsg) {{
  const pane = document.getElementById(paneId);
  if (items.length === 0) {{
    pane.innerHTML = `<p class="empty">${{emptyMsg}}</p>`;
  }} else {{
    pane.innerHTML = `<div class="grid">${{items.map(([id,L]) => renderCard(id,L)).join("")}}</div>`;
  }}
}}

// ── Nav ──────────────────────────────────────────────────────────────────────
function showTab(key, btn) {{
  document.querySelectorAll(".tab-pane").forEach(p => p.classList.add("hidden"));
  document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
  const pane = document.getElementById("tab-" + key);
  if (pane) pane.classList.remove("hidden");
  if (btn)  btn.classList.add("active");
  activeTab = key;
}}

function updateNavCounts(groups, ntwCount) {{
  if (!groups) {{
    groups = groupByStatus();
    ntwCount = groups.unreviewed.filter(([,L]) => isNewThisWeek(L)).length;
  }}
  document.getElementById("nav-new-this-week").textContent = ntwCount;
  document.getElementById("nav-unreviewed").textContent    = groups.unreviewed.length;
  document.getElementById("nav-favorite").textContent      = groups.favorite.length;
  document.getElementById("nav-both").textContent          = groups.both.length;
  document.getElementById("nav-think").textContent         = groups.think.length;
  document.getElementById("nav-deleted").textContent       = groups.deleted.length;
}}

// ── Button handlers ───────────────────────────────────────────────────────────
async function restoreListing(id) {{
  if (!state[id]) return;
  state[id].status        = "new";
  state[id].last_modified = new Date().toISOString();
  renderAll();
  showTab(activeTab, document.querySelector("nav button.active"));
  await enqueueSave(id, "status", "new");
}}

async function setStatus(id, newStatus) {{
  if (!state[id]) return;
  const L = state[id];
  const oldStatus = L.status || "new";

  // Toggle: clicking active button returns card to unreviewed
  const effectiveStatus = (oldStatus === newStatus && newStatus !== "deleted")
    ? "new" : newStatus;

  // Delete is one-way from the main buttons — use Restore button in Deleted tab to undo

  // Apply to state
  L.status = effectiveStatus;
  if (effectiveStatus !== "favorite") {{
    L.christine_favorite = false;
    L.christine_pass     = false;
  }}
  L.last_modified = new Date().toISOString();

  // Re-render everything and restore tab position
  renderAll();
  showTab(activeTab, document.querySelector(`nav button.active`));

  // Save — field by field (status first, then clear christine fields if needed)
  await enqueueSave(id, "status", effectiveStatus);
  if (effectiveStatus !== "favorite" && (oldStatus === "favorite")) {{
    await enqueueSave(id, "christine_favorite", false);
    await enqueueSave(id, "christine_pass",     false);
  }}
}}

async function clickChristineHeart(id) {{
  if (!state[id]) return;
  const L = state[id];
  const current = L.christine_favorite || false;
  L.christine_favorite = !current;
  L.christine_pass     = false;  // mutually exclusive
  renderAll();
  showTab(activeTab, document.querySelector("nav button.active"));
  await enqueueSave(id, "christine_favorite", !current);
  await enqueueSave(id, "christine_pass",     false);
}}

async function clickChristinePass(id) {{
  if (!state[id]) return;
  const L = state[id];
  const current = L.christine_pass || false;
  L.christine_pass     = !current;
  L.christine_favorite = false;  // mutually exclusive
  renderAll();
  showTab(activeTab, document.querySelector("nav button.active"));
  await enqueueSave(id, "christine_pass",     !current);
  await enqueueSave(id, "christine_favorite", false);
}}

async function deleteAll(ids) {{
  ids.forEach(id => {{
    if (state[id] && state[id].status !== "deleted") {{
      state[id].status        = "deleted";
      state[id].last_modified = new Date().toISOString();
    }}
  }});
  renderAll();
  showTab(activeTab, document.querySelector("nav button.active"));
  // Save each deletion sequentially
  for (const id of ids) {{
    await enqueueSave(id, "status", "deleted");
  }}
}}

// ── Save queue ────────────────────────────────────────────────────────────────
// Saves are serialized — each waits for previous to complete
const _saveQueue = [];
let   _saveBusy  = false;

async function enqueueSave(id, field, value) {{
  _saveQueue.push({{id, field, value}});
  if (!_saveBusy) drainQueue();
}}

async function drainQueue() {{
  _saveBusy = true;
  while (_saveQueue.length > 0) {{
    const job = _saveQueue.shift();
    await doSave(job.id, job.field, job.value);
  }}
  _saveBusy = false;
}}

async function doSave(id, field, value) {{
  showToast("Saving…");
  try {{
    const resp = await fetch(WORKER_URL, {{
      method:  "POST",
      headers: {{"Content-Type":"application/json"}},
      body:    JSON.stringify({{id, field, value}}),
    }});
    const result = await resp.json().catch(() => ({{}}));
    if (!resp.ok || result.error) throw new Error(result.error || "status " + resp.status);

    showToast("Saved ✓");

    // Check if a new scrape has landed since we loaded
    if (result.sha && loadedSha && result.sha !== loadedSha) {{
      document.getElementById("stale-banner").style.display = "block";
    }}
    loadedSha = result.sha || loadedSha;

  }} catch(err) {{
    showToast("Save failed — please retry", true);
    console.error("Save error:", err);
  }}
}}

// ── Utilities ─────────────────────────────────────────────────────────────────
function openLightbox(url) {{
  document.getElementById("lightbox-img").src = url;
  document.getElementById("lightbox").classList.add("open");
}}
function closeLightbox() {{
  document.getElementById("lightbox").classList.remove("open");
  document.getElementById("lightbox-img").src = "";
}}
document.addEventListener("keydown", e => {{ if(e.key==="Escape") closeLightbox(); }});

function showToast(msg, isError=false) {{
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className   = "show" + (isError ? " error" : "");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.className = "", 2500);
}}

window.addEventListener("scroll", () => {{
  const btn = document.getElementById("scroll-top");
  if (window.scrollY > 400) btn.classList.add("visible");
  else btn.classList.remove("visible");
}});
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

