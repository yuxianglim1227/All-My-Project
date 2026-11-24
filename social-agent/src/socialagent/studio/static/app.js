// Social Studio front end: no build step, no framework. All user/AI text is inserted with textContent (never innerHTML).

const $ = (id) => document.getElementById(id);
const state = { config: null, selected: new Set(), assets: [], editing: null, task: null, prefill: null,
  compose: null, month: null, posts: [], post: null };

// ---------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c !== null && c !== undefined) node.append(c);
  return node;
}

async function api(path, { method = "GET", body, form } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-Studio"] = "1";
  if (form) opts.body = form;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch("/studio/api" + path, opts);
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (res.status === 401 && path !== "/login") { showLogin(); throw new Error(data.error || "Please log in."); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied"); }
  catch {
    const ta = el("textarea", { class: "hidden" }); ta.value = text; document.body.append(ta);
    ta.classList.remove("hidden"); ta.select(); document.execCommand("copy"); ta.remove(); toast("Copied");
  }
}

function fillSelect(select, items, value) {
  select.replaceChildren(...items.map((i) => el("option", { value: i.key, text: i.emoji ? `${i.emoji} ${i.label}` : i.label })));
  if (value) select.value = value;
}

function fillChecks(box, items, checked) {
  box.replaceChildren(...items.map((p) => el("label", {}, el("input", { type: "checkbox", value: p.key, checked: checked.includes(p.key) }), p.label)));
}
const checkedValues = (box) => [...box.querySelectorAll("input:checked")].map((i) => i.value);

// ---------------------------------------------------------------- views / routing
const VIEWS = ["login", "library", "ai", "task", "calendar", "compose"];
function show(view) {
  for (const v of VIEWS) $(`view-${v}`).classList.toggle("hidden", v !== view);
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("active", a.dataset.tab === ({ task: "ai", compose: "library" }[view] || view));
  $("tabs").classList.toggle("hidden", view === "login");
  $("logout").classList.toggle("hidden", view === "login");
}

async function route() {
  const hash = location.hash.slice(1) || "library";
  if (hash.startsWith("task/")) return openTask(hash.slice(5));
  if (hash === "ai") { show("ai"); return loadTasks(); }
  if (hash === "calendar") { show("calendar"); return loadCalendar(); }
  if (hash === "compose") { if (!state.compose) { location.hash = "library"; return; } show("compose"); return renderCompose(); }
  show("library"); return loadLibrary();
}

// ---------------------------------------------------------------- login
async function showLogin() {
  show("login");
  try {
    const me = await (await fetch("/studio/api/me", { credentials: "same-origin" })).json();
    $("totp-row").classList.toggle("hidden", !me.totp);
    $("login-hint").textContent = me.configured ? "" :
      "No password set yet. On the server run:  python -m socialagent studio-password";
  } catch { /* offline */ }
  $("login-password").focus();
}

$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/login", { method: "POST", body: { password: $("login-password").value, totp: $("login-totp").value } });
    $("login-password").value = ""; $("login-totp").value = "";
    await boot();
  } catch (err) { $("login-error").textContent = err.message; }
});

$("logout").addEventListener("click", async () => { await api("/logout", { method: "POST" }).catch(() => {}); showLogin(); });

// ---------------------------------------------------------------- library
async function loadLibrary() {
  const params = new URLSearchParams({ folder: $("folder-filter").value || "", kind: $("kind-filter").value, q: $("search").value });
  const data = await api("/assets?" + params);
  state.assets = data.items;
  const current = $("folder-filter").value;
  $("folder-filter").replaceChildren(...data.folders.map((f) => el("option", { value: f.folder, text: `${f.folder} (${f.count})` })));
  $("folder-filter").value = current || "All media";
  $("folder-list").replaceChildren(...data.folders.map((f) => el("option", { value: f.folder })));
  renderGrid();
}

function renderGrid() {
  const grid = $("grid");
  grid.replaceChildren(...state.assets.map((a) => {
    const tile = el("button", { class: "tile" + (state.selected.has(a.id) ? " selected" : ""), type: "button", title: a.name,
      onclick: () => toggleSelect(a.id) },
      a.thumb_url ? el("img", { src: a.thumb_url, alt: a.name, loading: "lazy" }) : el("span", { text: a.name }),
      el("span", { class: "badge", text: a.kind === "video" ? `▶ ${Math.round(a.duration)}s` : (a.tags[0] || "photo") }));
    return tile;
  }));
  $("empty-library").classList.toggle("hidden", state.assets.length > 0);
  const n = state.selected.size;
  $("selection-bar").classList.toggle("hidden", n === 0);
  $("selection-count").textContent = `${n} selected`;
  $("sel-edit").disabled = n !== 1;
}

function toggleSelect(id) {
  if (state.selected.has(id)) state.selected.delete(id); else state.selected.add(id);
  renderGrid();
}

for (const id of ["folder-filter", "kind-filter"]) $(id).addEventListener("change", loadLibrary);
let searchTimer;
$("search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadLibrary, 300); });
$("sel-clear").addEventListener("click", () => { state.selected.clear(); renderGrid(); });
$("sel-compose").addEventListener("click", () => startCompose([...state.selected]));
$("prefill-clear").addEventListener("click", () => { state.prefill = null; renderPrefill(); });

function renderPrefill() {
  $("prefill-banner").classList.toggle("hidden", !state.prefill);
  if (state.prefill) $("prefill-text").textContent = `Pick media for: ${state.prefill.title}, then tap 📅 Compose.`;
}

$("file-input").addEventListener("change", async (e) => {
  const files = [...e.target.files];
  if (!files.length) return;
  const status = $("upload-status");
  let done = 0; const errors = [];
  for (const f of files) {  // one request per file: progress you can see, and one bad file never sinks the rest
    status.textContent = `Uploading ${done + 1} of ${files.length}: ${f.name}…`;
    const form = new FormData();
    form.append("file", f, f.name);
    const folder = $("folder-filter").value;
    if (folder && folder !== "All media") form.append("folder", folder);
    try { const r = await api("/assets", { method: "POST", form }); errors.push(...r.errors); }
    catch (err) { errors.push(`${f.name}: ${err.message}`); }
    done++;
  }
  status.textContent = errors.length ? `Uploaded ${done - errors.length} of ${files.length}. ${errors.join(" ")}` : `Uploaded ${done} file(s).`;
  e.target.value = "";
  loadLibrary();
});

$("sel-delete").addEventListener("click", async () => {
  const n = state.selected.size;
  if (!confirm(`Delete ${n} item(s) from the library? This cannot be undone.`)) return;
  for (const id of state.selected) await api(`/assets/${id}`, { method: "DELETE" }).catch((e) => toast(e.message));
  state.selected.clear(); loadLibrary();
});

$("sel-edit").addEventListener("click", () => {
  const a = state.assets.find((x) => state.selected.has(x.id));
  if (!a) return;
  state.editing = a.id;
  $("edit-name").value = a.name; $("edit-folder").value = a.folder; $("edit-tags").value = a.tags.join(", "); $("edit-note").value = a.note;
  $("edit-dialog").showModal();
});

$("edit-dialog").addEventListener("close", async () => {
  if ($("edit-dialog").returnValue !== "ok" || !state.editing) return;
  try {
    await api(`/assets/${state.editing}`, { method: "PATCH", body: { name: $("edit-name").value, folder: $("edit-folder").value,
      tags: $("edit-tags").value, note: $("edit-note").value } });
    toast("Saved"); loadLibrary();
  } catch (err) { toast(err.message); }
});

$("sel-captions").addEventListener("click", () => {
  const c = state.config;
  $("captions-count").textContent = `${state.selected.size} item(s) selected`;
  fillChecks($("captions-platforms"), c.platforms, ["instagram_post", "tiktok"]);
  fillSelect($("captions-tone"), c.tones, c.defaults.tone);
  fillSelect($("captions-lang"), c.languages, c.defaults.language);
  $("captions-dialog").showModal();
});

$("captions-dialog").addEventListener("close", async () => {
  if ($("captions-dialog").returnValue !== "ok") return;
  try {
    const t = await api("/tasks", { method: "POST", body: { kind: "captions", inputs: {
      asset_ids: [...state.selected], platforms: checkedValues($("captions-platforms")), tone: $("captions-tone").value,
      language: $("captions-lang").value, instruction: $("captions-instruction").value } } });
    state.selected.clear();
    location.hash = `task/${t.id}`;
  } catch (err) { toast(err.message); }
});

// ---------------------------------------------------------------- AI tasks
async function loadTasks() {
  const c = state.config;
  $("ai-mode").textContent = c.ai.gemini ? `Gemini free tier ready (${c.ai.model}) · copy-paste always available` :
    "Copy-paste mode (add GEMINI_API_KEY on the server for automatic runs)";
  const data = await api("/tasks");
  $("task-list").replaceChildren(...data.items.map((t) => el("button", { class: "task", type: "button",
    onclick: () => { location.hash = `task/${t.id}`; } },
    el("span", {}, el("strong", { text: t.title }), el("br"), el("span", { class: "muted", text: new Date(t.created_at).toLocaleString() })),
    el("span", { class: `status ${t.status}`, text: t.status === "waiting" ? "needs reply" : t.status }))));
  $("empty-tasks").classList.toggle("hidden", data.items.length > 0);
}

$("new-ideas").addEventListener("click", () => {
  const c = state.config;
  fillChecks($("ideas-platforms"), c.platforms, ["instagram_post", "instagram_story", "tiktok"]);
  fillSelect($("ideas-tone"), c.tones, c.defaults.tone);
  fillSelect($("ideas-lang"), c.languages, c.defaults.language);
  $("ideas-dialog").showModal();
});

$("ideas-dialog").addEventListener("close", async () => {
  if ($("ideas-dialog").returnValue !== "ok") return;
  try {
    const t = await api("/tasks", { method: "POST", body: { kind: "ideas", inputs: {
      count: Number($("ideas-count").value), days: Number($("ideas-days").value), platforms: checkedValues($("ideas-platforms")),
      tone: $("ideas-tone").value, language: $("ideas-lang").value, focus: $("ideas-focus").value } } });
    location.hash = `task/${t.id}`;
  } catch (err) { toast(err.message); }
});

async function openTask(id) {
  show("task");
  try { state.task = await api(`/tasks/${id}`); }
  catch (err) { toast(err.message); location.hash = "ai"; return; }
  renderTask();
}

function renderTask() {
  const t = state.task;
  $("task-title").textContent = t.title;
  $("task-meta").textContent = `${t.status === "done" ? `Answered via ${t.provider === "gemini" ? "Gemini" : "copy-paste"}` : "Waiting for an answer"} · ${new Date(t.created_at).toLocaleString()}`;
  $("task-error").textContent = t.error || "";
  const open = t.status !== "done";
  $("task-actions").classList.toggle("hidden", !open);
  $("run-gemini").classList.toggle("hidden", !(open && state.config.ai.gemini));
  $("share-step").classList.toggle("hidden", !(t.kind === "captions" && navigator.canShare));
  $("reply").value = "";
  renderResult(t);
}

$("back-to-ai").addEventListener("click", () => { location.hash = "ai"; });
$("copy-prompt").addEventListener("click", () => copyText(state.task.bridge_text));

$("share-media").addEventListener("click", async () => {
  try {
    const files = [];
    for (const [i, id] of state.task.inputs.asset_ids.entries()) {
      const a = state.assets.find((x) => x.id === id);
      const res = await fetch(`/studio/api/assets/${id}/file`, { credentials: "same-origin" });
      const blob = await res.blob();
      files.push(new File([blob], `${i + 1}-${a ? a.name : id}`, { type: blob.type }));
    }
    if (!navigator.canShare({ files })) throw new Error("This browser can't share these files; attach them in the chat app instead.");
    await navigator.share({ files, text: "Media for the prompt (in order)" });
  } catch (err) { if (err.name !== "AbortError") toast(err.message); }
});

$("submit-reply").addEventListener("click", async () => {
  $("task-error").textContent = "";
  try { state.task = { ...state.task, ...(await api(`/tasks/${state.task.id}/reply`, { method: "POST", body: { reply: $("reply").value } })) }; renderTask(); toast("Saved"); }
  catch (err) { $("task-error").textContent = err.message; }
});

$("run-gemini").addEventListener("click", async () => {
  const btn = $("run-gemini"); btn.disabled = true; btn.textContent = "⏳ Gemini is working…";
  $("task-error").textContent = "";
  try { state.task = { ...state.task, ...(await api(`/tasks/${state.task.id}/run`, { method: "POST" })) }; renderTask(); }
  catch (err) { $("task-error").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "⚡ Run with Gemini (free tier)"; }
});

function captionBlock(platformLabel, text) {
  return el("div", { class: "caption" },
    el("header", {}, el("span", { text: platformLabel }), el("button", { class: "ghost", type: "button", text: "Copy", onclick: () => copyText(text) })),
    el("div", { text }));
}

function renderResult(t) {
  const box = $("task-result");
  box.replaceChildren();
  if (!t.result) return;
  const label = (k) => (state.config.platforms.find((p) => p.key === k) || { label: k }).label;
  const res = el("div", { class: "result" });
  if (t.kind === "captions") {
    for (const item of t.result.items) {
      const a = state.assets.find((x) => x.id === item.asset_id);
      const right = el("div", { class: "stack" }, el("div", { class: "muted", text: item.what_i_see + (item.suggested_look ? ` · look: ${item.suggested_look}` : "") }));
      for (const c of item.captions) right.append(captionBlock(label(c.platform), [c.caption, c.hashtags.map((h) => h.startsWith("#") ? h : `#${h}`).join(" ")].filter(Boolean).join("\n\n")));
      right.append(el("button", { type: "button", text: "📅 Schedule this", onclick: () => {
        state.prefill = { title: item.what_i_see, captions: Object.fromEntries(item.captions.map((c) => [c.platform, c])),
                          look: item.suggested_look };
        startCompose([item.asset_id]);
      } }));
      res.append(el("div", { class: "result-item" }, a && a.thumb_url ? el("img", { src: a.thumb_url, alt: "" }) : el("span", { text: `#${item.asset}` }), right));
    }
  } else {
    for (const idea of t.result.ideas) {
      const text = [idea.hook, idea.caption, (idea.hashtags || []).map((h) => h.startsWith("#") ? h : `#${h}`).join(" ")].filter(Boolean).join("\n\n");
      res.append(el("div", { class: "idea" },
        el("strong", { text: idea.title }),
        el("div", {}, el("span", { class: "tag", text: label(idea.platform) }), el("span", { class: "tag", text: idea.format }),
          idea.pillar ? el("span", { class: "tag", text: idea.pillar }) : null,
          idea.day_offset !== undefined ? el("span", { class: "tag", text: idea.day_offset === 0 ? "today" : `in ${idea.day_offset} day(s)` }) : null),
        el("div", { class: "muted", text: idea.why }),
        el("div", {}, el("strong", { text: "Shot list" }), el("ul", {}, ...idea.shot_list.map((s) => el("li", { text: s })))),
        captionBlock("Caption", text),
        el("button", { type: "button", class: "ghost", text: "🖼 Use this idea: pick media", onclick: () => {
          state.prefill = { title: idea.title, captions: { [idea.platform]: { caption: [idea.hook, idea.caption].filter(Boolean).join("\n\n"),
            hashtags: idea.hashtags || [] } } };
          location.hash = "library"; renderPrefill();
        } })));
    }
  }
  box.append(res);
}

// ---------------------------------------------------------------- time helpers (brand time zone)
function tz() { return (state.config && state.config.timezone) || "UTC"; }
function parts(iso) {
  const f = new Intl.DateTimeFormat("en-CA", { timeZone: tz(), year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit",
    minute: "2-digit", hourCycle: "h23" });
  return Object.fromEntries(f.formatToParts(new Date(iso)).map((p) => [p.type, p.value]));
}
const localInput = (iso) => { const p = parts(iso); return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`; };
const dayKey = (iso) => { const p = parts(iso); return `${p.year}-${p.month}-${p.day}`; };
const timeOf = (iso) => { const p = parts(iso); return `${p.hour}:${p.minute}`; };
const pad = (n) => String(n).padStart(2, "0");
const ymd = (d) => `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;

// AI platform keys -> composer target keys
const AI_TO_TARGET = { instagram_post: ["instagram:photo", "instagram:carousel"], instagram_story: ["instagram:story"],
  instagram_reel: ["instagram:reel"], tiktok: ["tiktok:photo", "tiktok:video"], youtube_short: [] };

// ---------------------------------------------------------------- composer
async function startCompose(assetIds) {
  if (!assetIds.length) return;
  try {
    const opts = await api("/compose/options", { method: "POST", body: { asset_ids: assetIds } });
    const pre = state.prefill || { captions: {} };
    const look = pre.look && opts.looks.some((l) => l.key === pre.look) ? pre.look : opts.default_look;
    const noPrefill = !Object.keys(pre.captions).length;
    const targets = opts.targets.map((t, i) => {
      const aiKey = Object.keys(AI_TO_TARGET).find((k) => AI_TO_TARGET[k].includes(t.key) && pre.captions[k]);
      const c = aiKey ? pre.captions[aiKey] : null;
      return { ...t, on: !!c || (noPrefill && i === 0), caption: c ? c.caption : "",
        hashtags: c ? (c.hashtags || []).join(" ") : "", when: "", custom: "" };
    });
    state.compose = { assetIds, opts, look, targets };
    state.selected.clear();
    if (location.hash === "#compose") route(); else location.hash = "compose";
  } catch (err) { toast(err.message); }
}

function renderCompose() {
  const c = state.compose;
  $("compose-error").textContent = "";
  $("compose-media").replaceChildren(...c.assetIds.map((id) => el("img", { src: `/studio/api/assets/${id}/thumb`, alt: "" })));
  $("compose-looks").replaceChildren(...c.opts.looks.map((l) => el("button", { type: "button", class: l.key === c.look ? "on" : "",
    text: `${l.emoji} ${l.label}`, onclick: () => { c.look = l.key; renderCompose(); } })));
  $("compose-preview").src = `/studio/api/assets/${c.assetIds[0]}/preview?look=${encodeURIComponent(c.look)}`;
  $("compose-targets").replaceChildren(...c.targets.map((t) => {
    const counter = el("div", { class: "counter" });
    const caption = el("textarea", { rows: 4, placeholder: t.has_caption ? "Caption" : "Text sticker idea (Stories have no caption)" });
    caption.value = t.caption;
    const tags = el("input", { placeholder: "Hashtags, e.g. coffee latte", value: t.hashtags });
    const update = () => {
      t.caption = caption.value; t.hashtags = tags.value;
      const n = caption.value.length + tags.value.split(/[\s,]+/).filter(Boolean).reduce((a, h) => a + h.length + 2, 0);
      counter.textContent = `${n} / ${t.caption_limit}`; counter.classList.toggle("over", n > t.caption_limit);
    };
    caption.addEventListener("input", update); tags.addEventListener("input", update); update();
    const custom = el("input", { type: "datetime-local", class: t.when === "custom" ? "" : "hidden", value: t.custom || localInput(t.suggested) });
    custom.addEventListener("change", () => { t.custom = custom.value; });
    const when = el("select", {}, el("option", { value: "", text: `Next free slot (${new Date(t.suggested).toLocaleString([], { timeZone: tz(), weekday: "short", hour: "2-digit", minute: "2-digit" })})` }),
      el("option", { value: "now", text: "As soon as possible (after the undo window)" }), el("option", { value: "custom", text: "Pick a time…" }));
    when.value = t.when;
    when.addEventListener("change", () => { t.when = when.value; custom.classList.toggle("hidden", when.value !== "custom"); });
    const box = el("div", { class: "target" + (t.on ? "" : " off") },
      el("label", {}, el("input", { type: "checkbox", checked: t.on, onchange: (e) => { t.on = e.target.checked; box.classList.toggle("off", !t.on); } }),
        el("strong", { text: t.label }), el("span", { class: "muted small", text: t.method === "manual" ? " · you post it (I send a package)" : "" })),
      el("div", { class: "fields" }, caption, t.has_caption ? tags : null, t.has_caption ? counter : null, when, custom));
    return box;
  }));
}

$("back-to-library").addEventListener("click", () => { location.hash = "library"; });
$("compose-submit").addEventListener("click", async () => {
  const c = state.compose;
  const targets = c.targets.filter((t) => t.on).map((t) => ({ key: t.key, caption: t.caption,
    hashtags: t.hashtags.split(/[\s,]+/).filter(Boolean), when: t.when === "custom" ? (t.custom || localInput(t.suggested)) : t.when }));
  const btn = $("compose-submit"); btn.disabled = true; btn.textContent = "⏳ Preparing media…";
  try {
    const r = await api("/compose", { method: "POST", body: { asset_ids: c.assetIds, look: c.look, targets } });
    toast(`Scheduled ${r.posts.length} post(s)` + (r.notes.length ? `. ${r.notes.join(" ")}` : ""));
    state.compose = null; state.prefill = null; renderPrefill();
    state.month = null;
    location.hash = "calendar";
  } catch (err) { $("compose-error").textContent = err.message; }
  finally { btn.disabled = false; btn.textContent = "📅 Schedule"; }
});

// ---------------------------------------------------------------- calendar
const PLATFORM_ICON = { instagram: "📸", tiktok: "🎵", youtube: "▶️" };
function monthStart() {
  if (!state.month) { const p = parts(new Date().toISOString()); state.month = new Date(Date.UTC(+p.year, +p.month - 1, 1)); }
  return state.month;
}
$("cal-prev").addEventListener("click", () => { const m = monthStart(); state.month = new Date(Date.UTC(m.getUTCFullYear(), m.getUTCMonth() - 1, 1)); loadCalendar(); });
$("cal-next").addEventListener("click", () => { const m = monthStart(); state.month = new Date(Date.UTC(m.getUTCFullYear(), m.getUTCMonth() + 1, 1)); loadCalendar(); });
$("cal-today").addEventListener("click", () => { state.month = null; loadCalendar(); });

async function loadCalendar() {
  const m = monthStart();
  const first = new Date(m); first.setUTCDate(1 - ((m.getUTCDay() + 6) % 7));            // Monday on or before the 1st
  const last = new Date(first); last.setUTCDate(first.getUTCDate() + 41);                  // 6 weeks
  $("cal-title").textContent = m.toLocaleString([], { month: "long", year: "numeric", timeZone: "UTC" });
  $("cal-tz").textContent = tz();
  $("cal-weekdays").replaceChildren(...["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => el("div", { text: d })));
  const data = await api(`/posts?from=${ymd(first)}&to=${ymd(last)}`);
  state.posts = data.items;
  const byDay = {};
  for (const p of state.posts) (byDay[dayKey(p.publish_at)] ||= []).push(p);
  const today = dayKey(new Date().toISOString());
  const cells = [];
  for (let i = 0; i < 42; i++) {
    const d = new Date(first); d.setUTCDate(first.getUTCDate() + i);
    const key = ymd(d), posts = byDay[key] || [];
    const cell = el("div", { class: `day${d.getUTCMonth() !== m.getUTCMonth() ? " other" : ""}${key === today ? " today" : ""}${posts.length ? "" : " empty"}`,
      "data-day": key },
      el("h3", { text: d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }) }),
      ...posts.map((p) => el("button", { type: "button", class: `post st-${p.status}`, draggable: p.status === "scheduled" ? "true" : "false",
        "data-id": p.id, onclick: () => openPost(p.id) },
        p.thumb_url ? el("img", { src: p.thumb_url, alt: "" }) : el("span", { text: PLATFORM_ICON[p.platform] || "•" }),
        el("span", { class: "line", text: `${timeOf(p.publish_at)} ${PLATFORM_ICON[p.platform] || ""} ${p.kind} · ${p.status.replace("_", " ")}` }))));
    cell.addEventListener("dragover", (e) => { e.preventDefault(); cell.classList.add("drop"); });
    cell.addEventListener("dragleave", () => cell.classList.remove("drop"));
    cell.addEventListener("drop", (e) => { e.preventDefault(); cell.classList.remove("drop"); movePost(e.dataTransfer.getData("text/plain"), key); });
    cells.push(cell);
  }
  $("calendar").replaceChildren(...cells);
  for (const b of document.querySelectorAll(".post[draggable=true]")) b.addEventListener("dragstart", (e) => e.dataTransfer.setData("text/plain", b.dataset.id));
}

async function movePost(id, day) {
  const p = state.posts.find((x) => x.id === id);
  if (!p || dayKey(p.publish_at) === day) return;
  try { await api(`/posts/${id}`, { method: "PATCH", body: { when: `${day}T${timeOf(p.publish_at)}` } }); toast("Moved"); }
  catch (err) { toast(err.message); }
  loadCalendar();
}

function openPost(id) {
  const p = state.posts.find((x) => x.id === id);
  if (!p) return;
  state.post = p;
  const editable = p.status === "scheduled";
  $("post-title").textContent = `${PLATFORM_ICON[p.platform] || ""} ${p.platform} ${p.kind}`;
  $("post-meta").textContent = `${p.status.replace("_", " ")} · ${new Date(p.publish_at).toLocaleString([], { timeZone: tz() })}` +
    (p.method === "manual" ? " · you post it" : "") + (p.note ? ` · ${p.note}` : "");
  $("post-error").textContent = p.error;
  $("post-when").value = localInput(p.publish_at); $("post-when").disabled = !editable;
  $("post-caption").value = p.caption; $("post-caption").disabled = !editable;
  $("post-files").replaceChildren(...Array.from({ length: p.files }, (_, i) =>
    el("a", { class: "button ghost", href: `/studio/api/posts/${p.id}/files/${i}`, download: "", text: `⬇ File ${i + 1}` })));
  $("post-link").classList.toggle("hidden", !p.permalink); if (p.permalink) $("post-link").href = p.permalink;
  $("post-save").classList.toggle("hidden", !editable); $("post-cancel").classList.toggle("hidden", !editable);
  $("post-retry").classList.toggle("hidden", p.status !== "failed"); $("post-posted").classList.toggle("hidden", p.status !== "handed_off");
  $("post-dialog").showModal();
}

async function postAction(action) {
  try { await api(`/posts/${state.post.id}/${action}`, { method: "POST" }); $("post-dialog").close(); toast("Done"); loadCalendar(); }
  catch (err) { $("post-error").textContent = err.message; }
}
$("post-cancel").addEventListener("click", () => { if (confirm("Cancel this post?")) postAction("cancel"); });
$("post-retry").addEventListener("click", () => postAction("retry"));
$("post-posted").addEventListener("click", () => postAction("posted"));
$("post-save").addEventListener("click", async () => {
  const p = state.post, body = {};
  if ($("post-when").value !== localInput(p.publish_at)) body.when = $("post-when").value;
  if ($("post-caption").value !== p.caption) body.caption = $("post-caption").value;
  if (!Object.keys(body).length) { $("post-dialog").close(); return; }
  try { await api(`/posts/${p.id}`, { method: "PATCH", body }); $("post-dialog").close(); toast("Saved"); loadCalendar(); }
  catch (err) { $("post-error").textContent = err.message; }
});

// ---------------------------------------------------------------- boot
async function boot() {
  try { state.config = await api("/config"); } catch { return; }
  $("brand").textContent = state.config.account || "Social Studio";
  $("dryrun").classList.toggle("hidden", !state.config.dry_run);
  if (location.hash === "#login") location.hash = "library";
  await route();
}

window.addEventListener("hashchange", () => { if (state.config) route(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/studio/sw.js", { scope: "/studio/" }).catch(() => {});
boot();
