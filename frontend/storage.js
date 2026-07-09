"use strict";

/* ─────────────────────────────────────────────────────────────────────────
   Storage — the single thing app.js talks to for data. Everything else
   (which backend, tokens, retry-on-conflict) is hidden behind this.

   Backends implement:
     readRaw()          -> { json, ref }   ref = opaque version marker or null
     writeRaw(json,ref)  -> { ref }        throws { conflict:true } on version clash
     testConnection()   -> throws with a human message if config is bad
   ───────────────────────────────────────────────────────────────────────── */

const CONFIG_KEY = "babytracker:backendConfig";

const DEFAULT_STATE = () => ({
  babyName: "Baby",
  feeds: [],
  naps: [],
  activeNap: null,
  meals: [],
  diapers: [],
  growth: [],
  schedule: [
    { id: "s1", time: "07:00", label: "Wake up & morning feed", type: "feed" },
    { id: "s2", time: "09:30", label: "Morning nap", type: "sleep" },
    { id: "s3", time: "12:00", label: "Lunch (solids)", type: "meal" },
    { id: "s4", time: "13:00", label: "Afternoon nap", type: "sleep" },
    { id: "s5", time: "17:30", label: "Dinner (solids)", type: "meal" },
    { id: "s6", time: "19:00", label: "Bath & bedtime feed", type: "feed" },
  ],
  mealPlan: {},
  prepList: [],
});

const Storage = (() => {
  let backend = null; // { type, ...adapterInstance }

  function getConfig() {
    try {
      const raw = localStorage.getItem(CONFIG_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch (e) {
      return null;
    }
  }

  function setConfig(cfg) {
    localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg));
  }

  function clearConfig() {
    localStorage.removeItem(CONFIG_KEY);
    backend = null;
  }

  function isConfigured() {
    return !!getConfig();
  }

  function currentBackendType() {
    const cfg = getConfig();
    return cfg ? cfg.type : null;
  }

  function activate() {
    const cfg = getConfig();
    if (!cfg) throw new Error("No backend configured yet");
    if (cfg.type === "github") backend = window.GitHubBackend.create(cfg);
    else if (cfg.type === "drive") backend = window.DriveBackend.create(cfg);
    else throw new Error("Unknown backend type: " + cfg.type);
    return backend;
  }

  function ensureActive() {
    if (!backend) activate();
    return backend;
  }

  /** Plain read — used for the polling refresh. Returns default state if the
   *  remote file doesn't exist yet (first run). */
  async function load() {
    const b = ensureActive();
    const { json } = await b.readRaw();
    return json || DEFAULT_STATE();
  }

  /** Read-modify-write with retry on version conflicts (GitHub only —
   *  Drive's adapter has no conflict detection and just always succeeds). */
  async function mutate(mutatorFn, attempt = 0) {
    const b = ensureActive();
    const { json, ref } = await b.readRaw();
    const current = json || DEFAULT_STATE();
    const next = mutatorFn(current);
    try {
      await b.writeRaw(next, ref);
      return next;
    } catch (e) {
      if (e.conflict && attempt < 4) {
        await new Promise((r) => setTimeout(r, 300 + Math.random() * 400));
        return mutate(mutatorFn, attempt + 1);
      }
      throw e;
    }
  }

  /** First-time setup: write the default state if nothing exists yet. */
  async function ensureInitialized() {
    const b = ensureActive();
    const { json, ref } = await b.readRaw();
    if (!json) {
      await b.writeRaw(DEFAULT_STATE(), ref);
    }
  }

  return {
    getConfig, setConfig, clearConfig, isConfigured, currentBackendType,
    activate, load, mutate, ensureInitialized, DEFAULT_STATE,
  };
})();

/* ─────────────────────────── shared helpers used by both adapters ─────────────────────────── */

function utf8ToBase64(str) {
  const bytes = new TextEncoder().encode(str);
  let binary = "";
  bytes.forEach((b) => { binary += String.fromCharCode(b); });
  return btoa(binary);
}

function base64ToUtf8(b64) {
  const binary = atob(b64.replace(/\s/g, ""));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new TextDecoder().decode(bytes);
}

async function readErrorMessage(res) {
  try {
    const body = await res.json();
    return body.message || body.error?.message || `HTTP ${res.status}`;
  } catch (e) {
    return `HTTP ${res.status}`;
  }
}
