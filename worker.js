/**
 * GitHub State Proxy Worker
 * Sits between the Bridgewater Ranch Finder page and the GitHub API.
 * Holds the GitHub token as a Cloudflare secret — never exposed to the browser.
 *
 * Deploy this in the Cloudflare dashboard: Workers & Pages → Create → paste this code.
 * Then add the secret: Settings → Variables and Secrets → Add
 *   Name: GITHUB_TOKEN
 *   Value: (a fresh fine-grained PAT, Contents read/write, "ranches" repo only)
 *
 * The page calls this Worker's URL instead of GitHub's API directly.
 */

const GH_OWNER = "johnzur-droid";
const GH_REPO  = "ranches";
const STATE_PATH = "docs/state.json";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  "Access-Control-Max-Age": "86400",
};

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    if (request.method !== "POST") {
      return new Response("Method not allowed", { status: 405, headers: corsHeaders });
    }

    try {
      const body = await request.json();
      const newListings = body.listings;
      if (!newListings || typeof newListings !== "object") {
        return json({ error: "Missing 'listings' object in request body" }, 400);
      }

      const ghHeaders = {
        "Authorization": `Bearer ${env.GITHUB_TOKEN}`,
        "Accept": "application/vnd.github+json",
        "User-Agent": "ranches-worker",
      };

      // 1. Get current state.json (need its sha to update it)
      const getUrl = `https://api.github.com/repos/${GH_OWNER}/${GH_REPO}/contents/${STATE_PATH}`;
      const getResp = await fetch(getUrl, { headers: ghHeaders });
      if (!getResp.ok) {
        return json({ error: `Could not read state.json (status ${getResp.status})` }, 502);
      }
      const getData = await getResp.json();
      const sha = getData.sha;

      // 2. Build new content and commit it
      const newContent = btoa(unescape(encodeURIComponent(
        JSON.stringify({ listings: newListings }, null, 2)
      )));

      const putUrl = getUrl;
      const putResp = await fetch(putUrl, {
        method: "PUT",
        headers: { ...ghHeaders, "Content-Type": "application/json" },
        body: JSON.stringify({
          message: "Update listing status via web UI",
          content: newContent,
          sha: sha,
        }),
      });

      if (!putResp.ok) {
        const errText = await putResp.text();
        return json({ error: `Commit failed (status ${putResp.status})`, detail: errText }, 502);
      }

      return json({ ok: true });
    } catch (err) {
      return json({ error: String(err) }, 500);
    }
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}
