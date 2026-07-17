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

        listings.append({
            "id":             f"redfin_{property_id}",
            "address":        addr,
            "price":          price,
            "beds":           beds,
            "baths":          baths,
            "lot_sqft":       lot,
            "url":            full_url,
            "source":         "Redfin",
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

        listings.append({
            "id":             f"zillow_{zpid}",
            "address":        addr,
            "price":          price,
            "beds":           beds,
            "baths":          baths,
            "lot_sqft":       lot,
            "url":            f"https://www.zillow.com/homedetails/{zpid}_zpid/",
            "source":         "Zillow",
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
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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
# HTML generation
# ---------------------------------------------------------------------------

def risk_badges(listing):
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
    listings = state["listings"]
    try:
        from zoneinfo import ZoneInfo
        eastern  = ZoneInfo("America/New_York")
    except ImportError:
        eastern = timezone.utc
    run_time = datetime.now(eastern).strftime("%B %d, %Y at %I:%M %p %Z")

    # Determine "this week" cutoff — listings first_seen within 7 days
    today_dt = datetime.now(timezone.utc)
    def is_new_this_week(data):
        fs = data.get("first_seen") or data.get("run_date") or ""
        if not fs:
            return False
        try:
            from datetime import date
            fs_date = datetime.strptime(fs[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
            return (today_dt - fs_date).days <= 7
        except Exception:
            return False

    groups = {"new_this_week": [], "unreviewed": [], "favorite": [], "both": [], "think": []}
    for lid, data in listings.items():
        s = data.get("status", "new")
        if s == "deleted":
            continue
        if s == "favorite":
            if data.get("christine_favorite"):
                groups["both"].append((lid, data))
            else:
                # Stays in favorites whether christine_pass is True or False
                groups["favorite"].append((lid, data))
        elif s == "think":
            groups["think"].append((lid, data))
        else:
            if is_new_this_week(data):
                groups["new_this_week"].append((lid, data))
            groups["unreviewed"].append((lid, data))

    # Sort unreviewed by run_date desc then first_seen desc
    groups["unreviewed"].sort(key=lambda x: (x[1].get("run_date",""), x[1].get("first_seen","")), reverse=True)

    # Group unreviewed by run_date for rendering
    from collections import OrderedDict
    unreviewed_by_date = OrderedDict()
    for lid, data in groups["unreviewed"]:
        rd = data.get("run_date") or data.get("first_seen") or "Unknown"
        unreviewed_by_date.setdefault(rd, []).append((lid, data))

    # Build unreviewed section HTML — grouped by run_date with Delete All per group
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
            + inner +
            '</section>'
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
    both_count  = len(groups["both"])
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
        simple_section("⭐ Favorites",   "favorite", groups["favorite"], collapsed=True) +
        simple_section("💑 Both Love It","both",     groups["both"],     collapsed=True) +
        simple_section("🤔 Maybe",       "think",    groups["think"],    collapsed=True)
    )

    state_json   = json.dumps({k: v for k, v in listings.items() if v.get("status") != "deleted"})
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
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);font-size:15px;line-height:1.5;padding-top:0;}}
  /* Sticky header + nav */
  .sticky-top{{position:sticky;top:0;z-index:100;}}
  header{{background:var(--accent);color:#fff;padding:14px 32px;}}
  .header-title{{display:flex;align-items:center;gap:10px;}}
  .logo{{width:24px;height:24px;border-radius:5px;}}
  header h1{{font-size:1.2rem;font-weight:700;}}
  header .meta{{font-size:.78rem;opacity:.8;margin-top:2px;}}
  nav{{background:var(--surface);border-bottom:1px solid var(--border);padding:0 32px;display:flex;gap:2px;}}
  nav button{{background:none;border:none;padding:12px 12px;font-size:.85rem;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;font-weight:500;}}
  nav button.active{{color:var(--accent);border-bottom-color:var(--accent);}}
  /* Main content */
  main{{padding:24px 32px;max-width:1400px;margin:0 auto;}}
  .section{{margin-bottom:48px;}}
  .section h2{{font-size:1.05rem;font-weight:700;margin-bottom:16px;display:flex;align-items:center;gap:8px;}}
  .count{{background:var(--accent);color:#fff;font-size:.72rem;padding:2px 8px;border-radius:20px;font-weight:600;}}
  /* Run date groups */
  .run-group{{margin-bottom:32px;}}
  .run-group-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid var(--border);}}
  .run-date-label{{font-size:.82rem;font-weight:600;color:var(--muted);}}
  .delete-all-btn{{font-size:.75rem;font-weight:600;padding:4px 12px;border-radius:6px;border:1px solid #fca5a5;background:#fef2f2;color:#dc2626;cursor:pointer;}}
  .delete-all-btn:hover{{background:#fee2e2;}}
  /* Cards */
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
  .badge-basement-yes{{background:#dcfce7;color:#166534;}}
  .badge-basement-maybe{{background:#fef9c3;color:#854d0e;}}
  .badge-basement-no{{background:#f3f4f6;color:#6b7280;}}
  .garage-line{{font-size:.8rem;color:var(--muted);margin-bottom:6px;}}
  .road-line{{font-size:.8rem;color:var(--muted);margin-bottom:8px;}}
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
  .btn-christine{{border-color:#f9a8d4;color:#9d174d;}}
  .btn-christine.active{{background:#e11d48;border-color:transparent;color:#fff;}}
  .btn-christine-pass{{border-color:#d1d5db;color:#6b7280;}}
  .btn-christine-pass.active{{background:#6b7280;border-color:transparent;color:#fff;}}
  .christine-pass-indicator{{font-size:.78rem;color:#6b7280;margin-bottom:6px;font-style:italic;}}
  .empty{{color:var(--muted);font-size:.9rem;padding:12px 0;}}
  .hidden{{display:none!important;}}
  /* Toast */
  #toast{{position:fixed;bottom:24px;right:24px;background:#1a1a1a;color:#fff;padding:10px 18px;border-radius:8px;font-size:.85rem;opacity:0;transition:opacity .3s;pointer-events:none;z-index:999;}}
  #toast.show{{opacity:1;}}
  /* Scroll-to-top */
  #scroll-top{{position:fixed;bottom:72px;right:24px;width:40px;height:40px;border-radius:50%;background:var(--accent);color:#fff;border:none;font-size:1.1rem;cursor:pointer;display:none;align-items:center;justify-content:center;box-shadow:0 2px 8px rgba(0,0,0,.2);z-index:998;}}
  #scroll-top.visible{{display:flex;}}
  @media(max-width:600px){{main{{padding:16px;}}header{{padding:12px 16px;}}.grid{{grid-template-columns:1fr;}}nav{{padding:0 4px;gap:0;}}nav button{{padding:10px 6px;font-size:.72rem;}}}}
</style>
</head>
<body>
<div class="sticky-top">
  <header>
    <div class="header-title">
      <img src="/ranches/favicon-32.png?v=2" alt="" class="logo">
      <h1>Ranch Finder — Bridgewater, Somerset &amp; Cranford, NJ</h1>
    </div>
    <div class="meta">Last run: {run_time} &nbsp;|&nbsp; {unrev_count} unreviewed listing{"s" if unrev_count != 1 else ""}</div>
  </header>
  <nav>
    <button class="active" onclick="showSection('new-this-week',this)">🆕 This Week (<span id="nav-new-this-week">{ntw_count}</span>)</button>
    <button onclick="showSection('unreviewed',this)">📋 Queue (<span id="nav-unreviewed">{unrev_count}</span>)</button>
    <button onclick="showSection('favorite',this)">⭐ Favorites (<span id="nav-favorite">{fav_count}</span>)</button>
    <button onclick="showSection('both',this)">💑 (<span id="nav-both">{both_count}</span>)</button>
    <button onclick="showSection('think',this)">🤔 Maybe (<span id="nav-think">{think_count}</span>)</button>
  </nav>
</div>
<main>{sections_html}</main>
<div id="toast"></div>
<button id="scroll-top" onclick="window.scrollTo({{top:0,behavior:'smooth'}})">↑</button>
<script>
const WORKER_URL = "{worker_url}";
let state = {state_json};

// Section nav — Option B: one section at a time
function showSection(key, btn) {{
  document.querySelectorAll(".section").forEach(s => s.classList.add("hidden"));
  document.querySelectorAll("nav button").forEach(b => b.classList.remove("active"));
  const sec = document.getElementById("section-" + key);
  if (sec) sec.classList.remove("hidden");
  if (btn) btn.classList.add("active");
}}

// Count a section's visible (non-deleted, non-hidden) cards
function countVisible(sectionId) {{
  const sec = document.getElementById("section-" + sectionId);
  if (!sec) return 0;
  return sec.querySelectorAll(".card:not([style*='display: none'])").length;
}}

// Update all nav counts from live DOM
function updateNavCounts() {{
  const sections = ["new-this-week", "unreviewed", "favorite", "both", "think"];
  sections.forEach(key => {{
    const navEl   = document.getElementById("nav-" + key);
    const countEl = document.getElementById("count-" + key);
    const n = countVisible(key);
    if (navEl)   navEl.textContent   = n;
    if (countEl) countEl.textContent = n;
  }});
}}

async function setStatus(lid, newStatus) {{
  if (!state[lid]) return;
  state[lid].status = newStatus;
  // Un-Christine when unfavoriting
  if (newStatus !== "favorite") {{
    state[lid].christine_favorite = false;
    state[lid].christine_pass     = false;
  }}
  const card = document.getElementById("card-" + lid);
  if (card) {{
    card.dataset.status = newStatus;
    card.querySelectorAll(".btn:not(.btn-christine)").forEach(b => b.classList.remove("active"));
    const activeBtn = card.querySelector(".btn-" + (newStatus === "deleted" ? "delete" : newStatus));
    if (activeBtn) activeBtn.classList.add("active");
    // Show/hide Christine heart based on favorite status
    const cBtn = card.querySelector(".btn-christine");
    if (cBtn) cBtn.style.display = newStatus === "favorite" ? "" : "none";
    if (newStatus === "deleted") {{
      card.style.opacity = "0.3";
      setTimeout(() => {{
        card.style.display = "none";
        updateNavCounts();
      }}, 400);
    }} else {{
      updateNavCounts();
    }}
  }}
  showToast("Saving...");
  await commitStateToGitHub();
}}

async function toggleChristine(lid) {{
  if (!state[lid]) return;
  const current = state[lid].christine_favorite || false;
  state[lid].christine_favorite = !current;
  // Clicking heart always clears pass
  state[lid].christine_pass = false;
  const card = document.getElementById("card-" + lid);
  if (card) {{
    card.dataset.christine = String(!current);
    card.dataset.christinePass = "false";
    const cBtn = card.querySelector(".btn-christine");
    if (cBtn) {{
      cBtn.classList.toggle("active", !current);
      cBtn.textContent = (!current ? "❤️" : "🤍") + " Christine";
    }}
    // Clear pass button active state
    const pBtn = card.querySelector(".btn-christine-pass");
    if (pBtn) pBtn.classList.remove("active");
    // Remove pass indicator if present
    const ind = card.querySelector(".christine-pass-indicator");
    if (ind) ind.remove();
    // Move card between Favorites and Both Love It sections
    const favSection  = document.getElementById("section-favorite");
    const bothSection = document.getElementById("section-both");
    if (!current) {{
      // Christine just favorited — move to Both Love It
      const bothGrid = bothSection ? (bothSection.querySelector(".grid") || (() => {{
        const g = document.createElement("div"); g.className = "grid";
        bothSection.appendChild(g); return g;
      }})()) : null;
      if (bothGrid) bothGrid.appendChild(card);
    }} else {{
      // Christine un-favorited — move back to Favorites
      const favGrid = favSection ? (favSection.querySelector(".grid") || (() => {{
        const g = document.createElement("div"); g.className = "grid";
        favSection.appendChild(g); return g;
      }})()) : null;
      if (favGrid) favGrid.appendChild(card);
    }}
    updateNavCounts();
  }}
  showToast("Saving...");
  await commitStateToGitHub();
}}

async function christinePass(lid) {{
  if (!state[lid]) return;
  const current = state[lid].christine_pass || false;
  state[lid].christine_pass = !current;
  // Clicking pass always clears heart
  state[lid].christine_favorite = false;
  const card = document.getElementById("card-" + lid);
  if (card) {{
    card.dataset.christinePass = String(!current);
    card.dataset.christine = "false";
    // Update pass button
    const pBtn = card.querySelector(".btn-christine-pass");
    if (pBtn) pBtn.classList.toggle("active", !current);
    // Reset heart button
    const cBtn = card.querySelector(".btn-christine");
    if (cBtn) {{
      cBtn.classList.remove("active");
      cBtn.textContent = "🤍 Christine";
    }}
    // Show/hide pass indicator on card
    let ind = card.querySelector(".christine-pass-indicator");
    if (!current) {{
      // Just passed — add indicator if not present
      if (!ind) {{
        ind = document.createElement("div");
        ind.className = "christine-pass-indicator";
        ind.textContent = "👎 Christine not interested";
        const mapBtns = card.querySelector(".map-btns");
        if (mapBtns) card.querySelector(".card-body").insertBefore(ind, mapBtns);
      }}
    }} else {{
      // Un-passed — remove indicator
      if (ind) ind.remove();
    }}
    // If card was in Both Love It, move back to Favorites
    const favSection  = document.getElementById("section-favorite");
    const bothSection = document.getElementById("section-both");
    if (card.closest("#section-both")) {{
      const favGrid = favSection ? (favSection.querySelector(".grid") || (() => {{
        const g = document.createElement("div"); g.className = "grid";
        favSection.appendChild(g); return g;
      }})()) : null;
      if (favGrid) favGrid.appendChild(card);
    }}
    updateNavCounts();
  }}
  showToast("Saving...");
  await commitStateToGitHub();
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

// Floating scroll-to-top visibility
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

