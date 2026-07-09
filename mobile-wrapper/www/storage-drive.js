"use strict";

/* ─────────────────────────────────────────────────────────────────────────
   Google Drive backend — stores the whole app state as one JSON file in
   the signed-in user's Drive, addressed by file ID. Uses Google Identity
   Services (GIS) for auth — no server, no client secret.

   Config shape: { type:'drive', fileId }

   Scope used is the full 'drive' scope (not the narrower 'drive.file')
   specifically so a SECOND caregiver's Google account — different from
   whoever created the file — can read/write it once it's been shared,
   without needing the Google Picker UI. This does mean the OAuth consent
   screen shows a broad "See, edit, create, and delete all of your Google
   Drive files" warning; that's the trade-off for simpler multi-account
   sharing. See README.md if you'd rather tighten this to drive.file+Picker.

   Known limitation: Google's sign-in popup needs a real https:// origin,
   so this backend works in a browser tab or an installed PWA, but is NOT
   reliable inside a wrapped Capacitor/APK WebView. Use GitHub for the APK.
   ───────────────────────────────────────────────────────────────────────── */

window.DriveBackend = (() => {
  const DRIVE_FILENAME = "Baby Tracker Data.json";
  const SCOPE = "https://www.googleapis.com/auth/drive";

  let tokenClient = null;
  let cachedToken = null;
  let cachedExpiry = 0;
  let gisReady = null;

  function loadGis() {
    if (gisReady) return gisReady;
    gisReady = new Promise((resolve, reject) => {
      if (window.google && window.google.accounts && window.google.accounts.oauth2) {
        resolve();
        return;
      }
      const script = document.createElement("script");
      script.src = "https://accounts.google.com/gsi/client";
      script.onload = () => resolve();
      script.onerror = () => reject(new Error("Couldn't load Google's sign-in library — check your connection."));
      document.head.appendChild(script);
    });
    return gisReady;
  }

  function clientId() {
    const id = window.CONFIG && window.CONFIG.GOOGLE_CLIENT_ID;
    if (!id) throw new Error("Google Drive isn't configured yet — GOOGLE_CLIENT_ID is blank in config.js. See README.md.");
    return id;
  }

  async function ensureTokenClient() {
    const id = clientId(); // throws early if GOOGLE_CLIENT_ID is blank — no need to touch the network first
    await loadGis();
    if (!tokenClient) {
      tokenClient = google.accounts.oauth2.initTokenClient({
        client_id: id,
        scope: SCOPE,
        callback: () => {}, // overridden per-call below
      });
    }
    return tokenClient;
  }

  /** Must be called from a real user click (button press) — browsers block
   *  popups triggered any other way, and Google's flow is popup-based. */
  async function signIn() {
    const client = await ensureTokenClient();
    return new Promise((resolve, reject) => {
      client.callback = (resp) => {
        if (resp.error) { reject(new Error(resp.error)); return; }
        cachedToken = resp.access_token;
        cachedExpiry = Date.now() + (resp.expires_in * 1000) - 60000;
        resolve(cachedToken);
      };
      client.requestAccessToken({ prompt: "" });
    });
  }

  /** Used by background operations (polling). Throws NeedsSignIn if there's
   *  no valid cached token — caller should surface a "Reconnect" button
   *  rather than trying to pop a dialog outside a user gesture. */
  async function tokenOrThrow() {
    if (cachedToken && Date.now() < cachedExpiry) return cachedToken;
    const e = new Error("Google Drive session expired — reconnect to continue.");
    e.needsSignIn = true;
    throw e;
  }

  function create(cfg) {
    async function authHeader() {
      const token = await tokenOrThrow();
      return { Authorization: `Bearer ${token}` };
    }

    async function readRaw() {
      if (!cfg.fileId) return { json: null, ref: null };
      const res = await fetch(
        `https://www.googleapis.com/drive/v3/files/${cfg.fileId}?alt=media`,
        { headers: await authHeader() }
      );
      if (res.status === 404) return { json: null, ref: null };
      if (res.status === 401) { const e = new Error("Google Drive session expired — reconnect to continue."); e.needsSignIn = true; throw e; }
      if (!res.ok) throw new Error(`Drive read failed: ${await readErrorMessage(res)}`);
      const text = await res.text();
      let json = null;
      try { json = text ? JSON.parse(text) : null; } catch (e) { json = null; }
      return { json, ref: null }; // Drive has no convenient conditional-write; best-effort only
    }

    async function writeRaw(json) {
      if (!cfg.fileId) throw new Error("No Drive file selected yet.");
      const res = await fetch(
        `https://www.googleapis.com/upload/drive/v3/files/${cfg.fileId}?uploadType=media`,
        {
          method: "PATCH",
          headers: Object.assign(await authHeader(), { "Content-Type": "application/json" }),
          body: JSON.stringify(json, null, 2),
        }
      );
      if (res.status === 401) { const e = new Error("Google Drive session expired — reconnect to continue."); e.needsSignIn = true; throw e; }
      if (!res.ok) throw new Error(`Drive write failed: ${await readErrorMessage(res)}`);
      return { ref: null };
    }

    async function testConnection() {
      if (!cfg.fileId) throw new Error("No Drive file selected yet.");
      const res = await fetch(
        `https://www.googleapis.com/drive/v3/files/${cfg.fileId}?fields=id,name,capabilities`,
        { headers: await authHeader() }
      );
      if (!res.ok) throw new Error(`Couldn't access that file: ${await readErrorMessage(res)}`);
    }

    return { readRaw, writeRaw, testConnection };
  }

  /** Setup-flow helpers (not part of the Storage adapter interface, called
   *  directly by the setup screen and the Family tab). */

  async function createNewFile() {
    const res = await fetch("https://www.googleapis.com/drive/v3/files", {
      method: "POST",
      headers: Object.assign(await (async () => ({ Authorization: `Bearer ${cachedToken}` }))(), { "Content-Type": "application/json" }),
      body: JSON.stringify({ name: DRIVE_FILENAME }),
    });
    if (!res.ok) throw new Error(`Couldn't create the Drive file: ${await readErrorMessage(res)}`);
    const meta = await res.json();
    const uploadRes = await fetch(
      `https://www.googleapis.com/upload/drive/v3/files/${meta.id}?uploadType=media`,
      {
        method: "PATCH",
        headers: { Authorization: `Bearer ${cachedToken}`, "Content-Type": "application/json" },
        body: JSON.stringify(Storage.DEFAULT_STATE(), null, 2),
      }
    );
    if (!uploadRes.ok) throw new Error(`Created the file but couldn't write to it: ${await readErrorMessage(uploadRes)}`);
    return meta.id;
  }

  async function findSharedFile() {
    const q = encodeURIComponent(`sharedWithMe and name = '${DRIVE_FILENAME}' and trashed = false`);
    const res = await fetch(
      `https://www.googleapis.com/drive/v3/files?q=${q}&fields=files(id,name,owners(displayName,emailAddress))`,
      { headers: { Authorization: `Bearer ${cachedToken}` } }
    );
    if (!res.ok) throw new Error(`Couldn't search Drive: ${await readErrorMessage(res)}`);
    const data = await res.json();
    return (data.files && data.files[0]) || null;
  }

  async function getFileMeta(fileId) {
    const res = await fetch(
      `https://www.googleapis.com/drive/v3/files/${fileId}?fields=id,name,owners(displayName,emailAddress)`,
      { headers: { Authorization: `Bearer ${cachedToken}` } }
    );
    if (!res.ok) throw new Error(`Couldn't find that file: ${await readErrorMessage(res)}`);
    return res.json();
  }

  async function shareWith(fileId, email) {
    const res = await fetch(`https://www.googleapis.com/drive/v3/files/${fileId}/permissions`, {
      method: "POST",
      headers: { Authorization: `Bearer ${cachedToken}`, "Content-Type": "application/json" },
      body: JSON.stringify({ role: "writer", type: "user", emailAddress: email }),
    });
    if (!res.ok) throw new Error(`Couldn't share the file: ${await readErrorMessage(res)}`);
  }

  return { create, signIn, createNewFile, findSharedFile, getFileMeta, shareWith, DRIVE_FILENAME };
})();
