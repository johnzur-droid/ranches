# Real Estate Search — S002 Delta

**Session:** S002
**Date:** 2026-07-15
**Status:** CLOSED

---

## Summary

Verified that structured style/basement fields exist on all four RealtyAPI sources' details endpoints, superior to the free-text parsing the original PRD specified. Found Realtor.com's keyword search is unreliable (38% accuracy) while Redfin and Zillow are both 100% accurate. Built and deployed full GitHub-based infrastructure (public repo, GitHub Pages, GitHub Actions, Cloudflare Worker as a secure token proxy) replacing the original local-PC-plus-SMTP design. Consumed 244/250 monthly API quota without tracking a running total — a real process gap, flagged directly. The production script (`scrape.py`) still only queries Realtor.com and does not yet reflect the verified multi-source approach; that rewrite is the main item carried into S003.

---

## Actions Taken

### Structured Field Discovery
- **What:** Checked details endpoints on all 4 sources for structured style/basement fields, not just free-text description
- **Why:** Original PRD design relied on parsing description text for "ranch"/"basement" keywords — wanted to check if a more reliable method existed
- **Result:** All 4 sources have structured fields — Realtor.com (`details.styles`), Homes.com (`features[]`), Redfin (`amenities` → Style/Basement Description), Zillow (`resoFacts.architecturalStyle`/`basementYN`). Free-text parsing downgraded to fallback-only status.

### Endpoint Bug Fixes
- **What:** Found and corrected wrong/nonexistent endpoints in the original script for Redfin and Zillow details calls
- **Redfin:** Was calling `/basicDetails` with `propertyId` — actually needs `property_url`. Correct endpoint is `/detailsbyid`, requires BOTH `property_id` AND `listing_id`.
- **Zillow:** Was calling `/details/byid` — doesn't exist. Correct endpoint is `/pro/byzpid`.
- **Result:** Both sources now return real structured data via the correct endpoints, confirmed live.

### Search-Level Filter Discovery and Accuracy Verification
- **What:** Found `keyword`/`keywords` parameters on Realtor.com, Redfin, and Zillow search endpoints (not Homes.com). Verified each against the structured style field to check real accuracy, not just trust the keyword match.
- **Result:**
  - Redfin: 100% accurate (13/13 verified across Bridgewater + Somerset)
  - Zillow: 100% accurate (16/16 verified)
  - Realtor.com: only 38% accurate (8/21) — found via full verification pass, includes false positives like colonial and cape cod style homes
  - Homes.com: no keyword param exists at all on its search endpoint

### GitHub Infrastructure Build
- **What:** Migrated from planned local-PC + Task Scheduler + SMTP design to GitHub-hosted infrastructure per JZ's direction
- **Repo:** `johnzur-droid/ranches`, made public (GitHub Pages requires public repo on free tier — verified against GitHub's own docs)
- **GitHub Pages:** Serves from `/docs` on `main` branch — live at `https://johnzur-droid.github.io/ranches/`
- **GitHub Actions:** `.github/workflows/weekly-scrape.yml` — cron trigger + manual dispatch option. Currently calls `scrape.py`, which is Realtor.com-only (not yet rewritten for multi-source).
- **Cloudflare Worker:** `ranches-proxy.johnzur.workers.dev` — holds a GitHub PAT server-side (as a Cloudflare secret), proxies the live page's Favorite/Think/Delete button clicks into commits against `docs/state.json`. This avoids exposing any token in the browser/page source, which was the original design's security gap.
- **Result:** Tested end-to-end — sent a real POST to the Worker, confirmed the commit landed in the actual repo, reverted cleanly. Confirmed working.

### Favicon
- **What:** Built a custom favicon (green rounded-square background, white house silhouette) and wired it into the page
- **Issue found:** Displayed correctly in tab, Edge, and phone home-screen, but not in Chrome's bookmark bar specifically
- **Root cause:** Chrome caches bookmark-bar favicons by exact URL, independent of normal page cache — the URL never changed even after clearing browsing data, so Chrome kept serving its original (no-icon) cached entry
- **Fix:** Added `?v=2` cache-busting query parameter to all favicon references, forcing Chrome to treat them as new URLs
- **Result:** Confirmed by JZ as fixed after this change (pending final confirmation)

### Bug Fixes From Manual QA
- Stray test-favorite (1280 Oxford Rd) left in state from Claude's own end-to-end Worker test — reverted
- Timestamp was hardcoded UTC — switched to `America/New_York` via `zoneinfo` (auto-handles EDT/EST)
- 2 listings under the 2-bathroom minimum (264 Vanderveer Rd, 18 North Ave) were live on the page — root cause: Zillow's `bathrooms=2plus` API parameter broke when combined with `keywords=ranch` (500 error), so it was dropped weeks ago without a Python-side replacement check ever being added. Both listings removed from live state.
- Price filter added (`priceRange: max:1000000`, confirmed inclusive) — 3 listings over $1M removed from a previously-built state file

### Multi-Source Verified Dataset (Built Manually In-Chat)
- **Bridgewater:** 10 confirmed ranches (Redfin + Zillow, after removing 2 sub-2-bath and 2 over-$1M)
- **Somerset:** 4 confirmed ranches
- **Bonus (Zillow radius spillover from Somerset search):** 2 Piscataway, 5 Franklin Twp, 1 Milltown — all individually verified, all genuine, kept per JZ's direction ("keep those, that's good bonus information")
- **Not yet coded into `scrape.py`** — this was assembled by hand through repeated live API testing this session, not by running the production script

### Self-Corrected Error
- **What:** Cited 4 properties (68 Highland Ave, 1930 Mountain Top Rd, 14 Edgewood Ter, 55 Stella Dr) as evidence the pipeline was missing real active ranch listings, based on unverified search snippets
- **Correction:** JZ asked for verification. Live web search confirmed all 4 were invalid — 2 sold and off-market, 1 was vacant land (not a house at all), 1 had also sold. Retracted the claim, corrected the record.

---

## Decisions Made

| Decision | Rationale | Alternatives Considered |
|---|---|---|
| Redfin + Zillow only, drop Realtor.com | Realtor's keyword search only 38% accurate vs 100% for the other two; also the most expensive to verify | Keep all 3, verify everything (140 calls/run — too expensive) |
| Public repo, not private | Private repo requires GitHub Pages Pro ($4/mo); JZ confirmed comfortable with public exposure | Paying for Pro |
| Cloudflare Worker for token security | Static GitHub Pages has no backend; Worker holds the GitHub PAT server-side so browser never sees it | Embedding token directly in page (JZ initially wanted this, reconsidered after risk was explained) |
| Bridgewater + Somerset, not all 7 pilot towns | Cost: 3 towns/3 sources = 140 calls/run, 240% over monthly budget if weekly. 2 towns/2 sources = 34 calls/run, sustainable | 7-town pilot as originally scoped in S001 |
| Keep bonus out-of-town finds (Piscataway/Franklin/Milltown) | JZ confirmed he knows and likes these towns | Strict town-boundary filtering only |

---

## Deferred / Not Done

- `scrape.py` rewrite to match verified Redfin+Zillow multi-source approach — **top priority for S003**
- `fmt_lot()` fix for Zillow's string-formatted lot sizes
- Cost recalculation and run-cadence decision post-rewrite, given 244/250 quota already consumed this cycle

---

## Open Going Into S003

- Rewrite `scrape.py`: Redfin + Zillow, structured-field verification per hit, dedupe, Bridgewater + Somerset
- Confirm favicon fix held (Chrome bookmark bar)
- Track running API call totals explicitly and report to JZ as they accumulate — process fix following this session's quota gap
- Decide run cadence given quota reset date (Aug 13)

---

*Delta closed: S002 — 2026-07-15*
