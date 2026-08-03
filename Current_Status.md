# Real Estate Search — Current Status

**Session:** S006
**Date:** 2026-08-03

---

## 🚀 Current Environment State

**Live site:** ranches.johnzur.com
**Repo:** johnzur-droid/ranches (public)
**Cloudflare Worker:** ranches-proxy.johnzur.workers.dev (v2 — GET + delta POST + purge_deleted)
**Python:** 3.13.1, Windows 11, scripts at C:/Users/johnz/scripts
**RealtyAPI key:** rt_LdoGUUQL8l2FuKjwTjtWvPCC — ~94 calls remaining. New key needed next month (free plan = 250/month).
**Google Maps API key:** stored in memory slot 10
**Cloudflare API token / Account ID / PAT:** stored in memory slot 10
**Custom domain:** ranches.johnzur.com — Netlify DNS CNAME → johnzur-droid.github.io
**State:** 170 listings (new=0, fav=5, think=15, del=150) — queue cleared by JZ

---

## 📋 Open Work Queue

**Active — S007 priority:**
1. Revoke Actions write from GitHub-Ranches PAT (deferred since S003)
2. New RealtyAPI key next month before next run
3. Calculate exact call budget before triggering next run
4. Zillow photo field fix in scrape.py — use mediumImageLink/hiResImageLink not propertyPhotoLinks

**Known, deferred:**
- Stale listing ID detection — listings that get relisted under new ID stay as orphans
- Redfin bylocation region ID format never resolved — stays on bycoordinates
- 🚨 NEVER commit state.json directly — use GitHub API PUT with current SHA only (violated twice S006 — cost JZ hours of work)

---

## 📝 S006 Work Completed

**Filter bar (7 filters):**
- Town (dynamic checkboxes from data), Price (dollars), Lot (acres), Home Size (sqft min/max)
- Basement (confirmed/unconfirmed/none), Garage (has/no)
- Road Type (4 types: residential/minor/secondary/primary) — separate from Near Highway
- Near Highway (binary dropdown: Any / Near / Not Near)
- Filters apply across all tabs simultaneously, exempt Deleted tab
- Reset button, "X of Y shown" counter, filter button turns green when active

**Card display improvements:**
- Near Highway moved from badges row to dedicated info-line: 🛣️ Near Highway: [name] — [dist] mi
- Home sqft displayed: 🏠 1,298 sqft
- Placeholder hints on price (e.g. 400000) and lot (e.g. 0.25) filter inputs

**Bug fixes:**
- Removed busy_road entirely — was duplicating near_highway, caused contradictory display
- Fixed lot_sqft normalization — Zillow acres strings ("0.37 acres") converted to sqft
- Fixed malformed address Route202206 → US-202/206, re-geocoded + re-enriched
- Fixed deduplication — normalize_address now strips zip+4 suffixes
- Removed 2 duplicate 1280 Oxford Rd listings (Redfin + Zillow)
- Fixed stale banner firing on every save — now only fires on new scrape
- Fixed recurring hw-line template literal syntax error (switched to perl for emoji edits)
- Fixed apple-touch-icon path — absolute paths restored

**New features:**
- ? help dropdown menu: User Guide, Video Walkthrough, Slide Deck PDF, Infographic
- Public HTML user guide (guide.html) at ranches.johnzur.com/guide.html
- Private Word doc user guide with credentials (delivered to JZ)
- Purge All button in Deleted tab — strips data, keeps ID only, prevents re-appearance
- PWA manifest + favicon-192 — Chrome desktop install button enabled
- Town blacklist (15 towns): clark, monroe, spotswood, east brunswick, carteret, west orange, newark, avenel, edison, kendall park, linden, new brunswick, north brunswick, rahway
- MIN_SQFT=1500 hard filter in scrape.py — drops listings where sqft known and below 1500
- home_sqft captured in scrape.py for both Redfin (search result) and Zillow (resoFacts.livingArea)

**Scrape runs:**
- S006 Run 1: failed — syntax error in scrape.py (fixed immediately)
- S006 Run 2: success — 26 new listings, ~156 RealtyAPI calls, key rt_LdoGUUQL8l2FuKjwTjtWvPCC

**State.json incidents (critical lessons):**
- Incident 1: busy_road commit overwrote JZ work — recovered from b132fe8
- Incident 2: sqft commit overwrote JZ work — recovered from 49f08b81
- Root cause: pulling stale state.json from git and committing it over live worker state
- Fix: state.json is NEVER committed directly — use GitHub API PUT with current SHA only

**Infrastructure:**
- Worker redeployed with purge_deleted action
- Worker GITHUB_TOKEN secret fixed after accidental placeholder deployment
- DNS re-verification resolved TLS certificate failure on ranches.johnzur.com

---

## 📝 S005 Work Completed

**Architecture — full rewrite:**
- Worker v2: GET returns {listings, sha}, POST accepts {id, field, value} delta only
- Shell HTML — no embedded listings, all data fetched live from worker on page load
- 409 conflict retry logic (3 retries, 200ms delay)

**New features:**
- Photo thumbnails + lightbox (hi-res on tap)
- Deleted tab with Restore button
- Christine heart + Not Interested buttons
- Both Love It section in nav

*Updated: S006 — 2026-08-03*
