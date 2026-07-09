"use strict";

/* ─────────────────────────── Constants & helpers ─────────────────────────── */

const SLOTS = ["Breakfast", "Lunch", "Dinner", "Snack"];
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

const typeIcon = { feed: "🍼", sleep: "😴", meal: "🥣", diaper: "🧷", other: "⭐" };
const typeColor = { feed: "feed", sleep: "sleep", meal: "meal", diaper: "diaper", other: "plan" };

function uid() { return Math.random().toString(36).slice(2, 10); }

function fmtClock(d) {
  return new Date(d).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function fmtDate(d) {
  return new Date(d).toLocaleDateString([], { month: "short", day: "numeric" });
}
function isToday(d) {
  const a = new Date(d), b = new Date();
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}
function minsBetween(a, b) {
  return Math.max(0, Math.round((new Date(b) - new Date(a)) / 60000));
}
function fmtDur(mins) {
  const h = Math.floor(mins / 60), m = Math.round(mins % 60);
  if (h && m) return `${h}h ${m}m`;
  if (h) return `${h}h`;
  return `${m}m`;
}
function timeAgo(d) {
  const m = minsBetween(d, new Date());
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  return `${fmtDur(m)} ago`;
}
function dayFrac(d) {
  const t = new Date(d);
  return (t.getHours() * 60 + t.getMinutes()) / 1440;
}
function byLine(by) { return by ? ` · by ${escapeHtml(by)}` : ""; }
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ─────────────────────────── Connection setup ─────────────────────────── */

let API_BASE = "";

function resolveApiBase() {
  if (window.CONFIG && window.CONFIG.API_BASE) return window.CONFIG.API_BASE;
  if (location.protocol === "http:" || location.protocol === "https:") return "";
  return localStorage.getItem("serverBase") || null;
}

function isPackagedShell() {
  return !(location.protocol === "http:" || location.protocol === "https:") &&
    !(window.CONFIG && window.CONFIG.API_BASE);
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const code = localStorage.getItem("accessCode");
  if (code) headers["X-Access-Code"] = code;
  const res = await fetch(API_BASE + path, Object.assign({}, opts, { headers }));
  if (res.status === 401) {
    const err = new Error("unauthorized");
    err.code = 401;
    throw err;
  }
  if (!res.ok) {
    const err = new Error("request failed: " + res.status);
    err.code = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  return ct.includes("application/json") ? res.json() : null;
}

/* ─────────────────────────── App state ─────────────────────────── */

let data = null;          // server state blob
let now = new Date();
let tab = "today";
let histFilter = "all";
let growthMetric = "weightKg";
let pollTimer = null;
let clockTimer = null;

const caregiverName = () => localStorage.getItem("caregiverName") || "";

/* ─────────────────────────── Boot sequence ─────────────────────────── */

async function boot() {
  API_BASE = resolveApiBase();

  if (API_BASE === null) {
    showServerGate();
    return;
  }

  await tryConnect();
}

async function tryConnect() {
  try {
    await apiFetch("/api/health");
  } catch (e) {
    if (isPackagedShell()) {
      showServerGate("Couldn't reach that address.");
      return;
    }
  }
  try {
    data = await apiFetch("/api/state");
    onConnected();
  } catch (e) {
    if (e.code === 401) {
      showAccessGate();
    } else {
      // Same-origin server unreachable — nothing more we can do here.
      document.getElementById("gate-error")?.classList.remove("hidden");
      showAccessGate("Can't reach the server. Check it's running.");
    }
  }
}

function showServerGate(message) {
  document.getElementById("serverGate").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  document.getElementById("gate").classList.add("hidden");
  if (message) {
    const el = document.getElementById("server-error");
    el.textContent = message;
    el.classList.remove("hidden");
  }
  const saved = localStorage.getItem("serverBase");
  if (saved) document.getElementById("server-input").value = saved;
}

function showAccessGate(message) {
  document.getElementById("gate").classList.remove("hidden");
  document.getElementById("app").classList.add("hidden");
  document.getElementById("serverGate").classList.add("hidden");
  if (message) {
    const el = document.getElementById("gate-error");
    el.textContent = message;
    el.classList.remove("hidden");
  }
}

function onConnected() {
  document.getElementById("gate").classList.add("hidden");
  document.getElementById("serverGate").classList.add("hidden");
  document.getElementById("app").classList.remove("hidden");
  if (isPackagedShell() || (window.CONFIG && window.CONFIG.API_BASE)) {
    document.getElementById("syncBadge").classList.remove("hidden");
  }
  renderAll();
  startTimers();
}

function startTimers() {
  clearInterval(pollTimer);
  clearInterval(clockTimer);
  clockTimer = setInterval(() => { now = new Date(); renderAll(); }, 30000);
  pollTimer = setInterval(async () => {
    try {
      data = await apiFetch("/api/state");
      renderAll();
    } catch (e) {
      /* transient network hiccup — next poll will retry */
    }
  }, 8000);
}

async function reloadState() {
  data = await apiFetch("/api/state");
  renderAll();
}

/* ─────────────────────────── Gate wiring ─────────────────────────── */

document.getElementById("gate-submit").addEventListener("click", async () => {
  const val = document.getElementById("gate-input").value.trim();
  localStorage.setItem("accessCode", val);
  document.getElementById("gate-error").classList.add("hidden");
  await tryConnect();
});
document.getElementById("gate-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("gate-submit").click();
});

document.getElementById("server-submit").addEventListener("click", async () => {
  let val = document.getElementById("server-input").value.trim();
  if (!val) return;
  val = val.replace(/\/+$/, "");
  localStorage.setItem("serverBase", val);
  API_BASE = val;
  document.getElementById("server-error").classList.add("hidden");
  await tryConnect();
});
document.getElementById("server-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") document.getElementById("server-submit").click();
});

/* ─────────────────────────── Bottom nav ─────────────────────────── */

document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});
document.addEventListener("click", (e) => {
  const gotoBtn = e.target.closest("[data-goto]");
  if (gotoBtn) setTab(gotoBtn.dataset.goto);
});

function setTab(id) {
  tab = id;
  document.querySelectorAll(".tab-panel").forEach((p) => p.classList.add("hidden"));
  document.getElementById("tab-" + id).classList.remove("hidden");
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === id));
  renderAll();
}

/* ─────────────────────────── Baby name editing ─────────────────────────── */

const babyNameDisplay = document.getElementById("babyNameDisplay");
const babyNameInput = document.getElementById("babyNameInput");
babyNameDisplay.addEventListener("click", () => {
  babyNameInput.value = data.babyName;
  babyNameDisplay.classList.add("hidden");
  babyNameInput.classList.remove("hidden");
  babyNameInput.focus();
});
async function commitBabyName() {
  const val = babyNameInput.value.trim() || "Baby";
  babyNameInput.classList.add("hidden");
  babyNameDisplay.classList.remove("hidden");
  if (val !== data.babyName) {
    data.babyName = val;
    renderAll();
    await apiFetch("/api/settings", { method: "PUT", body: JSON.stringify({ baby_name: val }) });
  }
}
babyNameInput.addEventListener("blur", commitBabyName);
babyNameInput.addEventListener("keydown", (e) => { if (e.key === "Enter") babyNameInput.blur(); });

/* ─────────────────────────── Derived data ─────────────────────────── */

function derived() {
  const feeds = data.feeds, naps = data.naps, meals = data.meals, diapers = data.diapers;
  const todayFeeds = feeds.filter((f) => isToday(f.time));
  const todayNaps = naps.filter((n) => isToday(n.start));
  const todayMeals = meals.filter((m) => isToday(m.time));
  const todayDiapers = diapers.filter((d) => isToday(d.time));
  const lastFeed = feeds[0];
  const lastDiaper = diapers[0];
  const napMins = todayNaps.reduce((s, n) => s + minsBetween(n.start, n.end), 0) +
    (data.activeNap && isToday(data.activeNap.start) ? minsBetween(data.activeNap.start, now) : 0);
  const totalMl = todayFeeds.reduce((s, f) => s + (f.amount_ml || 0), 0);
  const schedule = [...data.schedule].sort((a, b) => a.time.localeCompare(b.time));
  const nowHM = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const nextItem = schedule.find((s) => s.time >= nowHM);

  const allRows = [
    ...feeds.map((f) => ({
      cat: "feeds", key: "f" + f.id, time: f.time, icon: "🍼", cls: "feed-soft",
      title: f.type === "bottle" ? `Bottle · ${f.amount_ml} ml` : `Breast · ${f.side} · ${f.duration_min} min`,
      sub: (f.note || "") + byLine(f.by), del: () => removeItem("feeds", f.id),
    })),
    ...naps.map((n) => ({
      cat: "naps", key: "n" + n.id, time: n.start, icon: "😴", cls: "sleep-soft",
      title: `Nap · ${fmtDur(minsBetween(n.start, n.end))}`,
      sub: `${fmtClock(n.start)} – ${fmtClock(n.end)}${byLine(n.by)}`, del: () => removeItem("naps", n.id),
    })),
    ...meals.map((m) => ({
      cat: "meals", key: "m" + m.id, time: m.time,
      icon: m.reaction === "loved" ? "😋" : m.reaction === "refused" ? "🙅" : "😐", cls: "meal-soft",
      title: `${m.slot} · ${m.foods.join(", ")}`, sub: (m.note || "") + byLine(m.by), del: () => removeItem("meals", m.id),
    })),
    ...diapers.map((x) => ({
      cat: "diapers", key: "d" + x.id, time: x.time,
      icon: x.kind === "wet" ? "💧" : x.kind === "dirty" ? "💩" : "🌀", cls: "diaper-soft",
      title: x.kind === "wet" ? "Diaper · wet" : x.kind === "dirty" ? "Diaper · dirty" : "Diaper · wet + dirty",
      sub: byLine(x.by).replace(" · ", ""), del: () => removeItem("diapers", x.id),
    })),
  ].sort((a, b) => new Date(b.time) - new Date(a.time));

  return { todayFeeds, todayNaps, todayMeals, todayDiapers, lastFeed, lastDiaper, napMins, totalMl, schedule, nextItem, allRows };
}

async function removeItem(kind, id) {
  const endpoint = { feeds: "feeds", naps: "naps", meals: "meals", diapers: "diapers", growth: "growth", schedule: "schedule", prep: "prep" }[kind];
  await apiFetch(`/api/${endpoint}/${id}`, { method: "DELETE" });
  await reloadState();
}

/* ─────────────────────────── Render: master ─────────────────────────── */

function renderAll() {
  if (!data) return;
  renderHeader();
  const d = derived();
  if (tab === "today") renderToday(d);
  if (tab === "history") renderHistory(d);
  if (tab === "growth") renderGrowth();
  if (tab === "plan") renderPlan();
  if (tab === "family") renderFamily();
}

function renderHeader() {
  document.getElementById("greeting").textContent =
    (now.getHours() < 12 ? "Good morning" : now.getHours() < 18 ? "Good afternoon" : "Good evening") +
    (caregiverName() ? `, ${caregiverName()}` : "");
  if (!babyNameInput.classList.contains("hidden")) return;
  babyNameDisplay.innerHTML = `${escapeHtml(data.babyName)} <span class="edit-hint">✎</span>`;
}

/* ─────────────────────────── Render: Today ─────────────────────────── */

function renderToday(d) {
  document.getElementById("ribbonTitle").textContent = `${data.babyName}'s day`;
  document.getElementById("todayDate").textContent = now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });

  const ribbonNaps = [...d.todayNaps];
  if (data.activeNap && isToday(data.activeNap.start)) ribbonNaps.push({ ...data.activeNap, end: null });

  const ribbon = document.getElementById("ribbon");
  ribbon.innerHTML = "";
  ribbonNaps.forEach((n) => {
    const start = dayFrac(n.start) * 100, end = dayFrac(n.end || now) * 100;
    const el = document.createElement("div");
    el.className = "blk";
    el.style.left = start + "%";
    el.style.width = Math.max(end - start, 0.8) + "%";
    ribbon.appendChild(el);
  });
  d.todayFeeds.forEach((f) => ribbon.appendChild(dot(dayFrac(f.time) * 100, 5, "var(--feed)")));
  d.todayDiapers.forEach((x) => ribbon.appendChild(dot(dayFrac(x.time) * 100, 16, "var(--diaper)", true)));
  d.todayMeals.forEach((m) => ribbon.appendChild(dot(dayFrac(m.time) * 100, null, "var(--meal)", false, true)));
  const nowLine = document.createElement("div");
  nowLine.className = "now-line";
  nowLine.style.left = dayFrac(now) * 100 + "%";
  ribbon.appendChild(nowLine);

  document.getElementById("lastFeedSub").textContent = d.lastFeed ? timeAgo(d.lastFeed.time) : "none yet";
  document.getElementById("napIcon").textContent = data.activeNap ? "⏹" : "😴";
  document.getElementById("napLabel").textContent = data.activeNap ? "End nap" : "Start nap";
  document.getElementById("napSub").textContent = data.activeNap
    ? fmtDur(minsBetween(data.activeNap.start, now)) + " so far"
    : fmtDur(d.napMins) + " today";
  document.getElementById("lastDiaperSub").textContent = d.lastDiaper ? timeAgo(d.lastDiaper.time) : "none yet";
  document.getElementById("mealCountSub").textContent = `${d.todayMeals.length} today`;

  document.getElementById("statRow").innerHTML = [
    ["Feeds", `${d.todayFeeds.length}${d.totalMl ? ` · ${d.totalMl}ml` : ""}`, "var(--feed)"],
    ["Sleep", fmtDur(d.napMins), "var(--sleep)"],
    ["Diapers", d.todayDiapers.length, "var(--diaper)"],
    ["Meals", d.todayMeals.length, "var(--meal)"],
  ].map(([label, val, color]) => `<div class="stat"><b>${val}</b><span style="color:${color}">${label}</span></div>`).join("");

  const nextCard = document.getElementById("nextUpCard");
  if (d.nextItem) {
    nextCard.classList.remove("hidden");
    document.getElementById("nextIcon").textContent = typeIcon[d.nextItem.type] || "⭐";
    document.getElementById("nextLabel").textContent = `UP NEXT · ${d.nextItem.time}`;
    document.getElementById("nextTitle").textContent = d.nextItem.label;
  } else {
    nextCard.classList.add("hidden");
  }

  const todayRows = d.allRows.filter((r) => isToday(r.time)).slice(0, 8);
  document.getElementById("todayList").innerHTML = todayRows.length
    ? todayRows.map((r) => rowHtml(r, fmtClock(r.time))).join("")
    : `<div class="empty-note">Nothing logged yet today. Tap a button above to start.</div>`;
  wireRowDeletes("todayList", todayRows);
}

function dot(leftPct, top, color, square, bottom) {
  const el = document.createElement("div");
  el.className = "dot";
  el.style.left = leftPct + "%";
  el.style.background = color;
  if (square) { el.style.borderRadius = "2px"; el.style.width = "7px"; el.style.height = "7px"; }
  if (top != null) el.style.top = top + "px";
  if (bottom) el.style.bottom = "5px";
  return el;
}

function rowHtml(r, rightText) {
  return `
    <div class="activity-row" data-key="${r.key}">
      <div class="row-icon" style="background:var(--${r.cls.replace("-soft", "")}-soft, var(--bg))">${r.icon}</div>
      <div class="row-body">
        <div class="row-title">${escapeHtml(r.title)}</div>
        ${r.sub ? `<div class="row-sub">${escapeHtml(r.sub)}</div>` : ""}
      </div>
      <div class="row-right">${escapeHtml(rightText)}</div>
      ${r.del ? `<button class="row-del" aria-label="Delete entry" data-del="${r.key}">🗑</button>` : ""}
    </div>`;
}

function wireRowDeletes(containerId, rows) {
  const container = document.getElementById(containerId);
  rows.forEach((r) => {
    const btn = container.querySelector(`[data-del="${r.key}"]`);
    if (btn) btn.addEventListener("click", r.del);
  });
}

document.getElementById("btnFeed").addEventListener("click", () => openSheet("feed"));
document.getElementById("btnDiaper").addEventListener("click", () => openSheet("diaper"));
document.getElementById("btnMeal").addEventListener("click", () => openSheet("meal"));
document.getElementById("btnNap").addEventListener("click", async () => {
  if (data.activeNap) {
    await apiFetch("/api/naps/end", { method: "POST" });
  } else {
    await apiFetch("/api/naps/start", { method: "POST", body: JSON.stringify({ by: caregiverName() || null }) });
  }
  await reloadState();
});
document.getElementById("endNapBtn2").addEventListener("click", async () => {
  await apiFetch("/api/naps/end", { method: "POST" });
  await reloadState();
});

/* ─────────────────────────── Render: History ─────────────────────────── */

function renderHistory(d) {
  const filters = [
    ["all", "All", "ink"], ["feeds", "🍼 Feeds", "feed"], ["naps", "😴 Sleep", "sleep"],
    ["diapers", "🧷 Diapers", "diaper"], ["meals", "🥣 Meals", "meal"],
  ];
  document.getElementById("historyFilters").innerHTML = filters.map(([id, label, color]) =>
    `<button class="chip ${histFilter === id ? "active" : ""}" style="${histFilter === id ? `background:var(--${color})` : ""}" data-filter="${id}">${label}</button>`
  ).join("");
  document.querySelectorAll("[data-filter]").forEach((btn) => {
    btn.addEventListener("click", () => { histFilter = btn.dataset.filter; renderAll(); });
  });

  const napCard = document.getElementById("activeNapCard");
  if (data.activeNap) {
    napCard.classList.remove("hidden");
    document.getElementById("activeNapText").textContent = `Nap in progress · ${fmtDur(minsBetween(data.activeNap.start, now))}`;
    document.getElementById("activeNapSince").textContent = `since ${fmtClock(data.activeNap.start)}`;
  } else {
    napCard.classList.add("hidden");
  }

  const rows = histFilter === "all" ? d.allRows : d.allRows.filter((r) => r.cat === histFilter);
  const shown = rows.slice(0, 40);
  document.getElementById("historyList").innerHTML = shown.length
    ? shown.map((r) => rowHtml(r, (isToday(r.time) ? "Today" : fmtDate(r.time)) + " · " + fmtClock(r.time))).join("")
    : `<div class="empty-note">No entries here yet.</div>`;
  wireRowDeletes("historyList", shown);
}

/* ─────────────────────────── Render: Growth ─────────────────────────── */

const metricInfo = {
  weightKg: { label: "Weight", unit: "kg", field: "weight_kg" },
  heightCm: { label: "Height", unit: "cm", field: "height_cm" },
  headCm: { label: "Head", unit: "cm", field: "head_cm" },
};

function renderGrowth() {
  document.getElementById("growthMetricChips").innerHTML = Object.entries(metricInfo).map(([k, m]) =>
    `<button class="chip ${growthMetric === k ? "active" : ""}" style="${growthMetric === k ? "background:var(--growth)" : ""}" data-metric="${k}">${m.label}</button>`
  ).join("");
  document.querySelectorAll("[data-metric]").forEach((btn) => {
    btn.addEventListener("click", () => { growthMetric = btn.dataset.metric; renderAll(); });
  });

  const field = metricInfo[growthMetric].field;
  const points = [...data.growth].filter((g) => g[field] != null)
    .sort((a, b) => new Date(a.date) - new Date(b.date))
    .map((g) => ({ date: g.date, value: g[field] }));

  drawChart(points);
  document.getElementById("growthEmpty").classList.toggle("hidden", points.length >= 2);

  document.getElementById("growthList").innerHTML = data.growth.length
    ? data.growth.map((g) => `
      <div class="activity-row">
        <div class="row-icon" style="background:var(--growth-soft)">📏</div>
        <div class="row-body">
          <div class="row-title">${[
            g.weight_kg != null ? `${g.weight_kg} kg` : null,
            g.height_cm != null ? `${g.height_cm} cm` : null,
            g.head_cm != null ? `head ${g.head_cm} cm` : null,
          ].filter(Boolean).join(" · ")}</div>
          ${g.by ? `<div class="row-sub">by ${escapeHtml(g.by)}</div>` : ""}
        </div>
        <div class="row-right">${fmtDate(g.date)}</div>
        <button class="row-del" data-growth-del="${g.id}">🗑</button>
      </div>`).join("")
    : `<div class="empty-note">No measurements yet.</div>`;
  data.growth.forEach((g) => {
    const btn = document.querySelector(`[data-growth-del="${g.id}"]`);
    if (btn) btn.addEventListener("click", () => removeItem("growth", g.id));
  });
}

function drawChart(points) {
  const svg = document.getElementById("growthChart");
  svg.innerHTML = "";
  if (points.length < 2) return;
  const W = 320, H = 200, PAD = 28;
  const vals = points.map((p) => p.value);
  const min = Math.min(...vals), max = Math.max(...vals);
  const range = max - min || 1;
  const xStep = (W - PAD * 2) / (points.length - 1);
  const xy = (i) => {
    const x = PAD + i * xStep;
    const y = H - PAD - ((points[i].value - min) / range) * (H - PAD * 2);
    return [x, y];
  };
  const ns = "http://www.w3.org/2000/svg";
  // gridlines
  for (let i = 0; i <= 3; i++) {
    const y = PAD + (i * (H - PAD * 2)) / 3;
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", PAD); line.setAttribute("x2", W - PAD);
    line.setAttribute("y1", y); line.setAttribute("y2", y);
    line.setAttribute("stroke", "#ECE9F4"); line.setAttribute("stroke-dasharray", "4 4");
    svg.appendChild(line);
  }
  // path
  const d = points.map((_, i) => { const [x, y] = xy(i); return `${i === 0 ? "M" : "L"}${x},${y}`; }).join(" ");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", "none");
  path.setAttribute("stroke", "#D9708C");
  path.setAttribute("stroke-width", "3");
  path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round");
  svg.appendChild(path);
  // dots + x labels
  points.forEach((p, i) => {
    const [x, y] = xy(i);
    const c = document.createElementNS(ns, "circle");
    c.setAttribute("cx", x); c.setAttribute("cy", y); c.setAttribute("r", 4);
    c.setAttribute("fill", "#D9708C");
    svg.appendChild(c);
    if (i === 0 || i === points.length - 1 || points.length <= 5) {
      const t = document.createElementNS(ns, "text");
      t.setAttribute("x", x); t.setAttribute("y", H - 8);
      t.setAttribute("font-size", "9"); t.setAttribute("fill", "#8B87A8");
      t.setAttribute("text-anchor", i === 0 ? "start" : i === points.length - 1 ? "end" : "middle");
      t.textContent = fmtDate(p.date);
      svg.appendChild(t);
    }
  });
}

document.getElementById("btnAddGrowth").addEventListener("click", () => openSheet("growth"));

/* ─────────────────────────── Render: Plan ─────────────────────────── */

function renderPlan() {
  const schedule = [...data.schedule].sort((a, b) => a.time.localeCompare(b.time));
  document.getElementById("scheduleList").innerHTML = schedule.length
    ? schedule.map((s) => `
      <div class="activity-row">
        <div class="row-icon" style="background:var(--${typeColor[s.type]}-soft)">${typeIcon[s.type] || "⭐"}</div>
        <div class="row-body"><div class="row-title">${escapeHtml(s.label)}</div></div>
        <div class="row-right">${s.time}</div>
        <button class="row-del" data-sched-del="${s.id}">🗑</button>
      </div>`).join("")
    : `<div class="empty-note">No routine items yet. Add feeds, naps, and meals to build the day.</div>`;
  schedule.forEach((s) => {
    const btn = document.querySelector(`[data-sched-del="${s.id}"]`);
    if (btn) btn.addEventListener("click", () => removeItem("schedule", s.id));
  });

  renderPlanTable();
  renderPrepList();
}

function renderPlanTable() {
  const table = document.getElementById("planTable");
  let html = `<thead><tr><th></th>${SLOTS.map((s) => `<th>${s.slice(0, 5)}</th>`).join("")}</tr></thead><tbody>`;
  DAYS.forEach((day) => {
    html += `<tr><td class="plan-day-label">${day}</td>`;
    SLOTS.forEach((slot) => {
      const key = `${day}-${slot}`;
      const val = data.mealPlan[key] || "";
      html += `<td><button class="plan-cell ${val ? "filled" : ""}" data-plan-key="${key}">${val ? escapeHtml(val) : "＋"}</button></td>`;
    });
    html += `</tr>`;
  });
  html += `</tbody>`;
  table.innerHTML = html;
  table.querySelectorAll("[data-plan-key]").forEach((btn) => {
    btn.addEventListener("click", () => openPlanEditor(btn.dataset.planKey));
  });
}

let openPlanKey = null;
function openPlanEditor(key) {
  openPlanKey = key;
  const editor = document.getElementById("planEditor");
  const input = document.getElementById("planEditorInput");
  editor.classList.remove("hidden");
  input.value = data.mealPlan[key] || "";
  input.placeholder = `${key.replace("-", " · ")} — what to serve?`;
  input.focus();
  document.querySelectorAll(".plan-cell").forEach((c) => c.classList.toggle("editing", c.dataset.planKey === key));
}
document.getElementById("planEditorSave").addEventListener("click", savePlanCell);
document.getElementById("planEditorInput").addEventListener("keydown", (e) => { if (e.key === "Enter") savePlanCell(); });
async function savePlanCell() {
  if (!openPlanKey) return;
  const val = document.getElementById("planEditorInput").value.trim();
  data.mealPlan[openPlanKey] = val;
  document.getElementById("planEditor").classList.add("hidden");
  const key = openPlanKey; openPlanKey = null;
  renderPlanTable();
  await apiFetch(`/api/meal-plan/${encodeURIComponent(key)}`, { method: "PUT", body: JSON.stringify({ value: val }) });
}

function renderPrepList() {
  const list = document.getElementById("prepList");
  list.innerHTML = data.prepList.length ? data.prepList.map((p) => `
    <div class="prep-row">
      <button class="prep-check ${p.done ? "done" : ""}" data-prep-toggle="${p.id}">${p.done ? "✓" : ""}</button>
      <div class="prep-text ${p.done ? "done" : ""}">${escapeHtml(p.text)}</div>
      <button class="row-del" data-prep-del="${p.id}">🗑</button>
    </div>`).join("") : `<div class="empty-note">Add prep tasks — purées to batch, bottles to sterilise, snacks to pack.</div>`;
  data.prepList.forEach((p) => {
    document.querySelector(`[data-prep-toggle="${p.id}"]`)?.addEventListener("click", async () => {
      await apiFetch(`/api/prep/${p.id}`, { method: "PATCH", body: JSON.stringify({ done: !p.done }) });
      await reloadState();
    });
    document.querySelector(`[data-prep-del="${p.id}"]`)?.addEventListener("click", () => removeItem("prep", p.id));
  });
}

document.getElementById("prepAddBtn").addEventListener("click", addPrep);
document.getElementById("prepInput").addEventListener("keydown", (e) => { if (e.key === "Enter") addPrep(); });
async function addPrep() {
  const input = document.getElementById("prepInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  await apiFetch("/api/prep", { method: "POST", body: JSON.stringify({ text }) });
  await reloadState();
}

document.getElementById("btnAddSchedule").addEventListener("click", () => openSheet("schedule"));

/* ─────────────────────────── Render: Family ─────────────────────────── */

function renderFamily() {
  document.getElementById("babyNameSetting").value = data.babyName;
  document.getElementById("caregiverInput").value = caregiverName();
  document.getElementById("serverAddrDisplay").textContent =
    API_BASE || (location.origin + " (this device)");
  document.getElementById("syncFooter").textContent =
    "New entries from other devices appear automatically, usually within a few seconds.";
}

document.getElementById("babyNameSetting").addEventListener("change", async (e) => {
  const val = e.target.value.trim() || "Baby";
  data.babyName = val;
  renderHeader();
  await apiFetch("/api/settings", { method: "PUT", body: JSON.stringify({ baby_name: val }) });
});
document.getElementById("caregiverInput").addEventListener("change", (e) => {
  localStorage.setItem("caregiverName", e.target.value.trim());
  renderHeader();
});
document.getElementById("btnChangeServer").addEventListener("click", () => {
  document.getElementById("app").classList.add("hidden");
  showServerGate();
});

/* ─────────────────────────── Sheets (modals) ─────────────────────────── */

function openSheet(kind) {
  const overlay = document.getElementById("sheetOverlay");
  const content = document.getElementById("sheetContent");
  content.innerHTML = sheetTemplates[kind]();
  overlay.classList.remove("hidden");
  wireSheet(kind);
}
function closeSheet() {
  document.getElementById("sheetOverlay").classList.add("hidden");
}
document.getElementById("sheetOverlay").addEventListener("click", (e) => {
  if (e.target.id === "sheetOverlay") closeSheet();
});

function sheetHead(title) {
  return `<div class="sheet-handle"></div>
    <div class="sheet-head"><div class="sheet-title">${title}</div>
    <button class="sheet-close" id="sheetCloseBtn">✕</button></div>`;
}

const sheetTemplates = {
  feed: () => `
    ${sheetHead("Log a feed")}
    <div class="field">
      <div class="field-label">Type</div>
      <div class="chip-field-row">
        <button class="chip active" style="background:var(--feed)" data-feed-type="bottle">🍼 Bottle</button>
        <button class="chip" data-feed-type="breast">🤱 Breast</button>
      </div>
    </div>
    <div id="feedBottleFields" class="field">
      <div class="field-label">Amount</div>
      <div class="chip-field-row">
        <input id="feedAmount" class="text-input" style="flex:1" type="number" value="120" />
        <button class="chip active" style="background:var(--feed)" data-feed-unit="ml">ml</button>
        <button class="chip" data-feed-unit="oz">oz</button>
      </div>
    </div>
    <div id="feedBreastFields" class="field hidden">
      <div class="field-label">Side</div>
      <div class="chip-field-row">
        <button class="chip active" style="background:var(--feed)" data-feed-side="left">Left</button>
        <button class="chip" data-feed-side="right">Right</button>
        <button class="chip" data-feed-side="both">Both</button>
      </div>
      <div class="field-label" style="margin-top:12px">Duration (minutes)</div>
      <input id="feedDuration" class="text-input" type="number" value="15" />
    </div>
    <div class="field">
      <div class="field-label">Note (optional)</div>
      <input id="feedNote" class="text-input" placeholder="e.g. spit up a little" />
    </div>
    <button class="primary-btn" style="background:var(--feed)" id="feedSaveBtn">Save feed</button>
  `,
  diaper: () => `
    ${sheetHead("Log a diaper change")}
    <div class="diaper-grid">
      <button class="diaper-opt" data-diaper-kind="wet"><span class="diaper-opt-icon">💧</span><span class="diaper-opt-label">Wet</span></button>
      <button class="diaper-opt" data-diaper-kind="dirty"><span class="diaper-opt-icon">💩</span><span class="diaper-opt-label">Dirty</span></button>
      <button class="diaper-opt" data-diaper-kind="mixed"><span class="diaper-opt-icon">🌀</span><span class="diaper-opt-label">Both</span></button>
    </div>
    <div class="empty-note">One tap saves it with the current time.</div>
  `,
  meal: () => `
    ${sheetHead("Log a meal")}
    <div class="field">
      <div class="field-label">Mealtime</div>
      <div class="chip-field-row">
        ${SLOTS.map((s, i) => `<button class="chip ${i === 0 ? "active" : ""}" style="${i === 0 ? "background:var(--meal)" : ""}" data-meal-slot="${s}">${s}</button>`).join("")}
      </div>
    </div>
    <div class="field">
      <div class="field-label">Foods (comma separated)</div>
      <input id="mealFoods" class="text-input" placeholder="e.g. banana, oat porridge" />
    </div>
    <div class="field">
      <div class="field-label">How did it go?</div>
      <div class="chip-field-row">
        <button class="chip active" style="background:var(--meal)" data-meal-reaction="loved">😋 Loved it</button>
        <button class="chip" data-meal-reaction="some">😐 Some</button>
        <button class="chip" data-meal-reaction="refused">🙅 Refused</button>
      </div>
    </div>
    <div class="field">
      <div class="field-label">Note (optional)</div>
      <input id="mealNote" class="text-input" placeholder="e.g. first time trying avocado" />
    </div>
    <button class="primary-btn" style="background:var(--meal)" id="mealSaveBtn" disabled>Save meal</button>
  `,
  growth: () => `
    ${sheetHead("Add a measurement")}
    <div class="field"><div class="field-label">Weight (kg)</div><input id="gWeight" class="text-input" type="number" placeholder="e.g. 7.4" /></div>
    <div class="field"><div class="field-label">Height (cm)</div><input id="gHeight" class="text-input" type="number" placeholder="e.g. 66" /></div>
    <div class="field"><div class="field-label">Head circumference (cm)</div><input id="gHead" class="text-input" type="number" placeholder="e.g. 42" /></div>
    <div class="empty-note" style="padding:0 0 8px;text-align:left">Fill in whichever you measured — leave the rest blank.</div>
    <button class="primary-btn" style="background:var(--growth)" id="growthSaveBtn" disabled>Save measurement</button>
  `,
  schedule: () => `
    ${sheetHead("Add routine item")}
    <div class="field"><div class="field-label">Time</div><input id="schedTime" class="text-input" type="time" value="08:00" /></div>
    <div class="field"><div class="field-label">What happens</div><input id="schedLabel" class="text-input" placeholder="e.g. Morning nap" /></div>
    <div class="field">
      <div class="field-label">Type</div>
      <div class="chip-field-row">
        <button class="chip active" style="background:var(--feed)" data-sched-type="feed">Feed</button>
        <button class="chip" data-sched-type="sleep">Sleep</button>
        <button class="chip" data-sched-type="meal">Meal</button>
        <button class="chip" data-sched-type="diaper">Diaper</button>
        <button class="chip" data-sched-type="other">Other</button>
      </div>
    </div>
    <button class="primary-btn" id="schedSaveBtn" disabled>Add to routine</button>
  `,
};

function wireSheet(kind) {
  document.getElementById("sheetCloseBtn").addEventListener("click", closeSheet);

  if (kind === "feed") {
    let type = "bottle", unit = "ml", side = "left";
    const bottleFields = document.getElementById("feedBottleFields");
    const breastFields = document.getElementById("feedBreastFields");
    document.querySelectorAll("[data-feed-type]").forEach((btn) => btn.addEventListener("click", () => {
      type = btn.dataset.feedType;
      document.querySelectorAll("[data-feed-type]").forEach((b) => { b.classList.toggle("active", b === btn); b.style.background = b === btn ? "var(--feed)" : ""; });
      bottleFields.classList.toggle("hidden", type !== "bottle");
      breastFields.classList.toggle("hidden", type !== "breast");
    }));
    document.querySelectorAll("[data-feed-unit]").forEach((btn) => btn.addEventListener("click", () => {
      unit = btn.dataset.feedUnit;
      document.querySelectorAll("[data-feed-unit]").forEach((b) => { b.classList.toggle("active", b === btn); b.style.background = b === btn ? "var(--feed)" : ""; });
    }));
    document.querySelectorAll("[data-feed-side]").forEach((btn) => btn.addEventListener("click", () => {
      side = btn.dataset.feedSide;
      document.querySelectorAll("[data-feed-side]").forEach((b) => { b.classList.toggle("active", b === btn); b.style.background = b === btn ? "var(--feed)" : ""; });
    }));
    document.getElementById("feedSaveBtn").addEventListener("click", async () => {
      const note = document.getElementById("feedNote").value.trim();
      const body = type === "bottle"
        ? { type, amount_ml: Math.round(parseFloat(document.getElementById("feedAmount").value || 0) * (unit === "oz" ? 29.57 : 1)), note, by: caregiverName() || null }
        : { type, side, duration_min: parseInt(document.getElementById("feedDuration").value || 0, 10), note, by: caregiverName() || null };
      closeSheet();
      await apiFetch("/api/feeds", { method: "POST", body: JSON.stringify(body) });
      await reloadState();
    });
  }

  if (kind === "diaper") {
    document.querySelectorAll("[data-diaper-kind]").forEach((btn) => btn.addEventListener("click", async () => {
      closeSheet();
      await apiFetch("/api/diapers", { method: "POST", body: JSON.stringify({ kind: btn.dataset.diaperKind, by: caregiverName() || null }) });
      await reloadState();
    }));
  }

  if (kind === "meal") {
    let slot = SLOTS[0], reaction = "loved";
    const saveBtn = document.getElementById("mealSaveBtn");
    const foodsInput = document.getElementById("mealFoods");
    const checkValid = () => { saveBtn.disabled = !foodsInput.value.trim(); };
    foodsInput.addEventListener("input", checkValid);
    document.querySelectorAll("[data-meal-slot]").forEach((btn) => btn.addEventListener("click", () => {
      slot = btn.dataset.mealSlot;
      document.querySelectorAll("[data-meal-slot]").forEach((b) => { b.classList.toggle("active", b === btn); b.style.background = b === btn ? "var(--meal)" : ""; });
    }));
    document.querySelectorAll("[data-meal-reaction]").forEach((btn) => btn.addEventListener("click", () => {
      reaction = btn.dataset.mealReaction;
      document.querySelectorAll("[data-meal-reaction]").forEach((b) => { b.classList.toggle("active", b === btn); b.style.background = b === btn ? "var(--meal)" : ""; });
    }));
    saveBtn.addEventListener("click", async () => {
      const foods = foodsInput.value.split(",").map((s) => s.trim()).filter(Boolean);
      if (!foods.length) return;
      const note = document.getElementById("mealNote").value.trim();
      closeSheet();
      await apiFetch("/api/meals", { method: "POST", body: JSON.stringify({ slot, foods, reaction, note, by: caregiverName() || null }) });
      await reloadState();
    });
  }

  if (kind === "growth") {
    const wEl = document.getElementById("gWeight"), hEl = document.getElementById("gHeight"), headEl = document.getElementById("gHead");
    const saveBtn = document.getElementById("growthSaveBtn");
    const check = () => { saveBtn.disabled = !(wEl.value || hEl.value || headEl.value); };
    [wEl, hEl, headEl].forEach((el) => el.addEventListener("input", check));
    saveBtn.addEventListener("click", async () => {
      const body = {
        weight_kg: wEl.value ? parseFloat(wEl.value) : null,
        height_cm: hEl.value ? parseFloat(hEl.value) : null,
        head_cm: headEl.value ? parseFloat(headEl.value) : null,
        by: caregiverName() || null,
      };
      closeSheet();
      await apiFetch("/api/growth", { method: "POST", body: JSON.stringify(body) });
      await reloadState();
    });
  }

  if (kind === "schedule") {
    let type = "feed";
    const labelEl = document.getElementById("schedLabel");
    const saveBtn = document.getElementById("schedSaveBtn");
    labelEl.addEventListener("input", () => { saveBtn.disabled = !labelEl.value.trim(); });
    document.querySelectorAll("[data-sched-type]").forEach((btn) => btn.addEventListener("click", () => {
      type = btn.dataset.schedType;
      document.querySelectorAll("[data-sched-type]").forEach((b) => {
        b.classList.toggle("active", b === btn);
        b.style.background = b === btn ? `var(--${typeColor[type]})` : "";
      });
    }));
    saveBtn.addEventListener("click", async () => {
      const label = labelEl.value.trim();
      if (!label) return;
      const time = document.getElementById("schedTime").value;
      closeSheet();
      await apiFetch("/api/schedule", { method: "POST", body: JSON.stringify({ time, label, type }) });
      await reloadState();
    });
  }
}

/* ─────────────────────────── Go ─────────────────────────── */

boot();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("service-worker.js").catch(() => {});
  });
}
