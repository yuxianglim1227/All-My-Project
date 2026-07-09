"use strict";

/* ─────────────────────────────────────────────────────────────────────────
   GitHub backend — stores the whole app state as one JSON file in a repo,
   via the Contents API. Every save is a real commit.

   Config shape: { type:'github', token, owner, repo, path, branch? }
   Auth: fine-grained Personal Access Token, scoped to just this repo,
   permission "Contents: Read and write". Stored in localStorage on this
   device only — never committed anywhere.
   ───────────────────────────────────────────────────────────────────────── */

window.GitHubBackend = (() => {
  function create(cfg) {
    const branch = cfg.branch || "main";
    const apiUrl = () =>
      `https://api.github.com/repos/${cfg.owner}/${cfg.repo}/contents/${encodeURIComponent(cfg.path)}`;

    function headers(extra) {
      const base = {
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      };
      if (cfg.token) base.Authorization = `Bearer ${cfg.token}`;
      return Object.assign(base, extra || {});
    }

    async function readRaw() {
      const res = await fetch(`${apiUrl()}?ref=${encodeURIComponent(branch)}`, { headers: headers() });
      if (res.status === 404) return { json: null, ref: null };
      if (!res.ok) {
        const msg = await readErrorMessage(res);
        throw new Error(`GitHub read failed: ${msg}`);
      }
      const body = await res.json();
      if (Array.isArray(body)) {
        throw new Error(`"${cfg.path}" is a folder, not a file — check the path.`);
      }
      let json = null;
      try {
        json = JSON.parse(base64ToUtf8(body.content));
      } catch (e) {
        throw new Error(`"${cfg.path}" doesn't contain valid JSON.`);
      }
      return { json, ref: body.sha };
    }

    async function writeRaw(json, ref) {
      const payload = {
        message: `Update baby tracker data — ${new Date().toISOString()}`,
        content: utf8ToBase64(JSON.stringify(json, null, 2)),
        branch,
      };
      if (ref) payload.sha = ref;
      const res = await fetch(apiUrl(), {
        method: "PUT",
        headers: headers({ "Content-Type": "application/json" }),
        body: JSON.stringify(payload),
      });
      if (res.status === 409) {
        const e = new Error("conflict"); e.conflict = true; throw e;
      }
      if (!res.ok) {
        const msg = await readErrorMessage(res);
        throw new Error(`GitHub write failed: ${msg}`);
      }
      const body = await res.json();
      return { ref: body.content.sha };
    }

    async function testConnection() {
      const res = await fetch(`https://api.github.com/repos/${cfg.owner}/${cfg.repo}`, { headers: headers() });
      if (res.status === 401 || res.status === 403) {
        throw new Error("That token was rejected — check it's valid and has Contents read/write access to this repo.");
      }
      if (res.status === 404) {
        throw new Error(`Repository "${cfg.owner}/${cfg.repo}" wasn't found (or this token can't see it).`);
      }
      if (!res.ok) {
        throw new Error(await readErrorMessage(res));
      }
    }

    return { readRaw, writeRaw, testConnection };
  }

  return { create };
})();
