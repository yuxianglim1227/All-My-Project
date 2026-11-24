// Social Studio front end: no build step, no framework. All user/AI text is inserted with textContent (never innerHTML).

const $ = (id) => document.getElementById(id);
const state = { config: null, selected: new Set(), assets: [], editing: null, task: null };

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
const VIEWS = ["login", "library", "ai", "task"];
function show(view) {
  for (const v of VIEWS) $(`view-${v}`).classList.toggle("hidden", v !== view);
  for (const a of document.querySelectorAll("#tabs a")) a.classList.toggle("active", a.dataset.tab === (view === "task" ? "ai" : view));
  $("tabs").classList.toggle("hidden", view === "login");
  $("logout").classList.toggle("hidden", view === "login");
}

async function route() {
  const hash = location.hash.slice(1) || "library";
  if (hash.startsWith("task/")) return openTask(hash.slice(5));
  if (hash === "ai") { show("ai"); return loadTasks(); }
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
        captionBlock("Caption", text)));
    }
  }
  box.append(res);
}

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
