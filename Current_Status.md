# Real Estate Search — Current Status

**Session:** S005
**Date:** 2026-07-17

---

## 🚀 Current Environment State

**Live site:** ranches.johnzur.com (also johnzur-droid.github.io/ranches)
**Repo:** johnzur-droid/ranches (public)
**Cloudflare Worker:** ranches-proxy.johnzur.workers.dev (v2 — GET + delta POST)
**Python:** 3.13.1, Windows 11, scripts at C:/Users/johnz/scripts
**RealtyAPI key:** ***REALTYAPI_KEY*** — ~216 calls used, ~34 remaining. NEW KEY NEEDED before next run.
**Google Maps API key:** ***MAPS_KEY***
**Cloudflare API token:** cfut_***REDACTED***
**Cloudflare Account ID:** ***CF_ACCOUNT_ID***
**PAT:** stored in memory slot 10
**Custom domain:** ranches.johnzur.com — Netlify DNS CNAME → johnzur-droid.github.io

---

## 📋 Open Work Queue

**Active — S006 priority:**
1. New RealtyAPI key — get and update GitHub Secret REALTYAPI_KEY before next run
2. Calculate exact call budget before triggering next run (production run costs ~180 calls)
3. JZ reviews 124 listings — provide feedback on display/categorization issues
4. Town whitelist/blacklist — JZ to determine after reviewing (Edison, Clark, Spotswood candidates)
5. Revoke Actions write from Github-Ranches PAT (deferred since S003)
6. Stale Realtor.com + Staten Island listings — JZ deletes manually
7. Verify ranches.johnzur.com HTTPS provisioned by GitHub Pages

**Known, deferred:**
- Stale listing ID detection — listings that get relisted under new ID stay as orphans forever
- Redfin bylocation region ID format never resolved — stays on bycoordinates
- Min beds filter (3) may be too restrictive — revisit after JZ reviews listings

---

## 📝 S005 Work Completed

**Architecture — full rewrite:**
- Worker v2: GET returns {listings, sha}, POST accepts {id, field, value} delta only
- Worker merges one field on one listing — scraper fields never touched by browser
- 409 conflict retry logic (3 retries, 200ms delay) — handles simultaneous saves
- Shell HTML — no embedded listings, all data fetched live from worker on page load
- All card rendering moved to client-side JavaScript
- Save queue — serialized, no concurrent saves, optimistic UI update
- Stale page banner when SHA changes after save

**New features:**
- Photo thumbnails on every card (medium res) — click to open lightbox (high-res)
- Deleted tab with Restore button
- Christine heart + Not Interested buttons (mutually exclusive)
- Both Love It section in nav
- Nav counts driven from state object, never from DOM
- New This Week = client-side filter of Unreviewed, not separate section

**scrape.py changes:**
- Basement is badge not filter (both sources)
- Basement filter removed from Zillow search parameters
- Garage label extracted from Redfin amenities + Zillow resoFacts
- Property road classification via Nominatim reverse geocode (OSM highway type)
- NJ-only filter — drops out-of-state listings pre-detail-call
- Unit/condo filter — drops #NNN addresses pre-detail-call
- Photo URLs extracted from search response (photo_url + photo_url_hires)
- save_state fixed with ensure_ascii=False — prevents emoji double-encoding
- generate_html produces shell only — no embedded state

**Bugs found and fixed:**
- Emoji double-encoding in state.json (latin-1/utf-8 mismatch in GitHub Actions)
- Worker readState using atob() without TextDecoder — corrupted emojis
- Zillow Basement filter still in search params after removal from detail processing
- Favicon paths used /ranches/ prefix — broken on custom domain

**Infrastructure:**
- Cloudflare API token obtained — worker now deployable programmatically
- Custom domain ranches.johnzur.com live via Netlify DNS
- Cron disabled — manual trigger only until JZ approves next run

**Production run (S005):**
- 131 total listings, 55 new this run
- 176 RealtyAPI calls — over budget due to basement filter removal impact not calculated
- State.json emoji corruption repaired post-run

---

## 📝 S004 Work Completed

**scrape.py fixes:**
- Near Industrial removed entirely
- Satellite URL fixed
- fmt_price unwraps Zillow price dict
- fmt_lot fixed for bare integer strings
- check_location_risk rewritten — returns highway_roads list
- merge_into_state dedupes cross-run duplicates
- run_date added to new listings

**Page architecture:**
- Sticky header + nav, 4-section Option B nav
- New This Week, Unreviewed, Favorites, Think About It sections
- Dynamic nav counts, floating scroll-to-top

**Testing:**
- test_run.py written — Redfin only, 4 calls on third key

---

## 📝 S003 Work Completed

Complete scrape.py rewrite — Redfin + Zillow only, 3 towns, confirmed endpoints via live calls, 12-mile geo filter, Google Maps enrichment, GitHub Actions workflow.

---

## 📝 S002 Work Completed

Verified Redfin + Zillow as sole sources. Dropped Realtor.com + Homes.com. Built live site on GitHub Pages with Cloudflare Worker. 10 legacy Realtor.com/Redfin/Zillow entries remain in state.json.

---

*Updated: S005 — 2026-07-17*
