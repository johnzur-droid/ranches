# Real Estate Search — Current Status

**Session:** S003
**Date:** 2026-07-16

---

## 🚀 Current Environment State

**Live site:** johnzur-droid.github.io/ranches
**Repo:** johnzur-droid/ranches (public)
**Cloudflare Worker:** ranches-proxy.johnzur.workers.dev
**Python:** 3.13.1, Windows 11, scripts at C:/Users/johnz/scripts
**RealtyAPI key:** rt_gaFGHJcV7cqnARopuxIHJZFN — 232/250 used, resets Aug 14. DO NOT USE FOR TESTING.
**Third production key:** created, not yet activated — reserved for first clean production run
**Google Maps API key:** AIzaSyBeaCKQ_wC7zBiThr--xkcK4607pOGEDP4 (in GitHub Secrets as GOOGLE_MAPS_KEY)
**PAT:** stored in Claude memory slot 10 only — not stored in repo

---

## 📋 Open Work Queue

**Active — S004 priority (in order):**
1. Revoke Actions write from Github-Ranches PAT — deployment gate, prevents unauthorized runs
2. Remove Near Industrial detection code + badge entirely — satellite button covers it
3. Fix satellite button URL — force satellite layer
4. Fix nav counts — update dynamically after Favorite/Think/Delete clicks
5. Full page architecture rebuild:
   - Sticky header always visible
   - Option B nav — one section at a time
   - Sections: New This Week | Unreviewed (grouped by run_date, Delete All per group) | Favorites | Think About It
   - Floating scroll-to-top arrow
6. Near Highway redesign — show road names + classifications + distances, let user filter
7. First clean production run on third RealtyAPI key (after all fixes verified)

**Known, deferred:**
- Redfin bylocation region ID format never resolved — stays on bycoordinates for now
- basementTypes param on Redfin search untested — potential call savings if it works
- Deployment gate word with JZ — TBD next session

---

## 📝 S003 Work Completed

**scrape.py — complete rewrite:**
- Redfin + Zillow only (Realtor.com + Homes.com dropped permanently)
- 3 towns: Bridgewater + Somerset + Cranford
- MIN_BEDS = 3
- Zillow switched to /search/byaddress with bathrooms=TwoPlus + Basement filter
- Redfin stays on /search/bycoordinates (latitude/longitude params — NOT lat/lng)
- Both sources: correct base URLs, response nesting, field extraction confirmed via live calls
- 12-mile geography post-filter — drops out-of-area listings (Cranford radius was reaching Staten Island)
- Google Maps enrichment per listing: geocode + highway proximity + industrial zone (industrial to be removed S004)
- Street View + Satellite buttons on every card
- Price dict extraction fix (Zillow returns price as {value: N})
- Zillow propertyDetails.resoFacts nesting bug fixed
- normalize_address fix — strips township/borough qualifiers for cross-source dedup

**GitHub Actions workflow:**
- GOOGLE_MAPS_KEY secret added
- Two unauthorized runs triggered (S003 process failure) — burned 232/250 quota

**Confirmed via live API calls this session:**
- Redfin base URL: redfin.realtyapi.io
- Zillow base URL: zillow.realtyapi.io
- Both use latitude/longitude not lat/lng
- Redfin results nested under homeData
- Zillow results nested under property
- Zillow details at propertyDetails.resoFacts
- Redfin basement at amenities superGroups chain
- Zillow bathrooms=TwoPlus works on byaddress (not bycoordinates)
- Zillow Basement filter works on byaddress — confirmed 6 results returned
- Google Geocoding + Places API both confirmed working

**Page bugs identified (fix in S004):**
- Nav counts don't update after status changes
- Near Industrial flags every listing (invalid Places type)
- Satellite button opens road view not satellite view
- Near Highway accuracy uncertain — road name matching too broad

**Architecture decisions made:**
- Option B navigation (one section at a time)
- Sticky header
- Floating up arrow
- Unreviewed grouped by run_date with Delete All per group
- Near Highway to show road names/classifications/distances for user filtering

---

## 📝 S002 Work Completed

Verified Redfin + Zillow as sole sources (100% accurate on keyword=ranch).
Dropped Realtor.com (38% accurate) and Homes.com (no keyword filter).
Built live site on GitHub Pages with Cloudflare Worker for Favorite/Delete persistence.
state.json has 10 legacy entries (8 Realtor.com, 1 Redfin, 1 Zillow) — kept as test bed.

---

*Updated: S003 — 2026-07-16*
