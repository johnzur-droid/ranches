/**
 * GitHub State Proxy Worker — v2
 * Architecture: shell page, live data fetch, delta saves
 *
 * GET  → reads state.json, returns {listings, sha}
 * POST → accepts {id, field, value} delta, merges one field on one listing, writes back
 *
 * Writable fields (POST only): status, christine_favorite, christine_pass
 * All other fields are scraper-owned and never touched by this worker.
 *
 * Retry logic: on 409 conflict, re-fetch SHA and retry up to 3 times.
 */

const GH_OWNER   = "johnzur-droid";
const GH_REPO    = "ranches";
const STATE_PATH  = "docs/state.json";
const ALLOWED_FIELDS = new Set(["status", "christine_favorite", "christine_pass"]);
const VALID_STATUSES  = new Set(["new", "favorite", "think", "deleted"]);

const corsHeaders = {
  "Access-Control-Allow-Origin":  "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age":       "86400",
};

export default {
  async fetch(request, env) {

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // ── GET ── return current state to browser
    if (request.method === "GET") {
      try {
        const { listings, sha } = await readState(env);
        return json({ listings, sha });
      } catch (err) {
        return json({ error: String(err) }, 502);
      }
    }

    // ── POST ── apply a single-field delta
    if (request.method === "POST") {
      try {
        const body = await request.json();
        const { id, field, value } = body;

        // Input validation
        if (!id || typeof id !== "string") {
          return json({ error: "Missing or invalid 'id'" }, 400);
        }
        if (!field || !ALLOWED_FIELDS.has(field)) {
          return json({ error: `Field '${field}' is not writable` }, 400);
        }
        if (field === "status" && !VALID_STATUSES.has(value)) {
          return json({ error: `Invalid status value '${value}'` }, 400);
        }
        if ((field === "christine_favorite" || field === "christine_pass")
            && typeof value !== "boolean") {
          return json({ error: `Field '${field}' must be boolean` }, 400);
        }

        // Retry loop — handles 409 conflicts (simultaneous saves)
        const MAX_RETRIES = 3;
        const RETRY_DELAY_MS = 200;

        for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
          const { listings, sha } = await readState(env);

          if (!listings[id]) {
            return json({ error: `Listing '${id}' not found` }, 404);
          }

          // Apply ONLY the one field — scraper fields never touched
          listings[id][field] = value;

          // Business rule: un-favoriting clears both Christine fields
          if (field === "status" && value !== "favorite") {
            listings[id]["christine_favorite"] = false;
            listings[id]["christine_pass"]     = false;
          }

          // Business rule: christine fields are mutually exclusive
          if (field === "christine_favorite" && value === true) {
            listings[id]["christine_pass"] = false;
          }
          if (field === "christine_pass" && value === true) {
            listings[id]["christine_favorite"] = false;
          }

          const result = await writeState(env, listings, sha);

          if (result.status === 409 && attempt < MAX_RETRIES) {
            // Conflict — another save landed between our read and write
            // Wait and retry with fresh SHA
            await sleep(RETRY_DELAY_MS * (attempt + 1));
            continue;
          }

          if (!result.ok) {
            const errText = await result.text().catch(() => "");
            return json({ error: `Commit failed (${result.status})`, detail: errText }, 502);
          }

          const resultData = await result.json();
          const newSha = resultData?.content?.sha || sha;
          return json({ ok: true, sha: newSha });
        }

        return json({ error: "Max retries exceeded — please try again" }, 503);

      } catch (err) {
        return json({ error: String(err) }, 500);
      }
    }

    return new Response("Method not allowed", { status: 405, headers: corsHeaders });
  },
};

// ── Helpers ──────────────────────────────────────────────────────────────────

async function readState(env) {
  const url  = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${STATE_PATH}`;
  const resp = await fetch(url, { headers: ghHeaders(env) });
  if (!resp.ok) {
    throw new Error(`Could not read state.json (${resp.status})`);
  }
  const data    = await resp.json();
  // Decode base64 → Uint8Array → UTF-8 string (handles emojis correctly)
  const b64     = data.content.replace(/\n/g, "");
  const binary  = atob(b64);
  const bytes   = Uint8Array.from(binary, c => c.charCodeAt(0));
  const raw     = new TextDecoder("utf-8").decode(bytes);
  const state   = JSON.parse(raw);
  return { listings: state.listings || {}, sha: data.sha };
}

async function writeState(env, listings, sha) {
  const url     = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${STATE_PATH}`;
  const content = btoa(unescape(encodeURIComponent(
    JSON.stringify({ listings }, null, 2)
  )));
  return fetch(url, {
    method:  "PUT",
    headers: { ...ghHeaders(env), "Content-Type": "application/json" },
    body:    JSON.stringify({
      message: "Update listing status via web UI",
      content,
      sha,
    }),
  });
}

function ghHeaders(env) {
  return {
    "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
    "Accept":        "application/vnd.github+json",
    "User-Agent":    "ranches-worker",
  };
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}
