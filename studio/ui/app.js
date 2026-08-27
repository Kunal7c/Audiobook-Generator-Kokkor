"use strict";
const $ = (s) => document.querySelector(s);

const state = {
  book: null,        // {name, chapters:[{index,title,words}]}
  snap: null,        // latest server snapshot
  renderedLog: 0,
  es: null,
  rowEls: {},
};
const selection = new Set();

/* ---------- helpers ---------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}
function fmtT(sec) {
  sec = Math.max(0, Math.round(sec));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return (h ? h + ":" : "") + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0");
}
let toastTimer;
function toast(msg, isErr) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, 3400);
}
function busy() {
  return state.snap && (state.snap.status === "running" || state.snap.status === "initializing");
}

/* ---------- rendering ---------- */
const PILL = {
  idle: ["IDLE", "dim"], initializing: ["INITIALIZING", "amber"],
  running: ["SYNTHESIZING", "cyan"], stopped: ["ABORTED", "amber"],
  done: ["COMPLETE", "green"], error: ["FAULT", "red"],
};
function renderStatus() {
  const s = state.snap;
  if (!s) return;
  const [txt, cls] = PILL[s.status] || PILL.idle;
  $("#pill").className = "pill " + cls;
  $("#pilltxt").textContent = txt;
  const btn = $("#startbtn");
  const b = busy();
  btn.textContent = b ? "ABORT TRANSMISSION" : "INITIATE SYNTHESIS";
  btn.disabled = b ? false : (!state.book || selection.size === 0);
  btn.classList.toggle("danger", b);
  $("#dropzone").classList.toggle("locked", b);
}

function renderChapters() {
  const wrap = $("#chlist");
  wrap.innerHTML = "";
  state.rowEls = {};
  if (!state.book) {
    wrap.innerHTML = '<div class="empty">NO BOOK LOADED</div>';
    updateSelCount();
    return;
  }
  for (const c of state.book.chapters) {
    const row = document.createElement("div");
    row.className = "ch" + (selection.has(c.index) ? " checked" : "");
    row.dataset.idx = c.index;
    row.innerHTML =
      '<span class="num">' + String(c.index + 1).padStart(3, "0") + "</span>" +
      '<span class="ttl" title="' + esc(c.title) + '">' + esc(c.title) + "</span>" +
      '<span class="words">' + c.words.toLocaleString() + "w</span>" +
      '<span class="badge">—</span>' +
      '<span class="cb"></span>';
    row.addEventListener("click", () => {
      if (busy()) return;
      if (selection.has(c.index)) selection.delete(c.index);
      else selection.add(c.index);
      row.classList.toggle("checked", selection.has(c.index));
      updateSelCount();
    });
    wrap.appendChild(row);
    state.rowEls[c.index] = row;
  }
  updateSelCount();
  applyStates();
}

const BADGE = {
  pending: "PENDING", active: "ACTIVE", done: "DONE",
  partial: "PARTIAL", skipped: "SKIPPED",
};
function applyStates() {
  const s = state.snap;
  if (!s) return;
  // reset badges
  for (const idx in state.rowEls) {
    const row = state.rowEls[idx];
    row.className = row.className.replace(/\b(active|done|partial|skipped)\b/g, "").trim();
    if (selection.has(Number(idx))) row.classList.add("checked");
    row.querySelector(".badge").textContent = "—";
  }
  for (const x of s.states) {
    const row = state.rowEls[x.index];
    if (!row) continue;
    row.classList.add(x.status);
    row.querySelector(".badge").textContent = BADGE[x.status] || x.status.toUpperCase();
    if (x.status === "active" && x.paras) {
      row.querySelector(".badge").textContent = "P " + x.para + "/" + x.paras;
    }
  }
}

function computeProgress() {
  const s = state.snap;
  const st = s ? s.states : [];
  if (!st.length) return { pct: 0, cur: null };
  let done = 0;
  let cur = null;
  for (const x of st) {
    if (x.status === "done" || x.status === "partial") done++;
    if (x.status === "active") cur = x;
  }
  let pct = (done / st.length) * 100;
  if (cur && cur.paras) pct += (cur.para / cur.paras) * (100 / st.length);
  return { pct: Math.min(100, pct), cur };
}

function renderProgress() {
  const s = state.snap;
  if (!s) return;
  const { pct, cur } = computeProgress();
  $("#pfill").style.width = pct.toFixed(1) + "%";
  $("#ppct").textContent = pct.toFixed(1) + "%";
  $("#curch").textContent = cur
    ? "CH " + String(cur.index + 1).padStart(3, "0") + " · " + cur.title
    : s.status === "done" ? "ALL CHAPTERS COMPLETE"
    : s.status === "stopped" ? "TRANSMISSION ABORTED"
    : s.status === "error" ? "FAULT"
    : "STANDBY";
  $("#parafill").style.width = (cur && cur.paras) ? (cur.para / cur.paras * 100) + "%" : "0%";
  let done = 0;
  for (const x of s.states) if (x.status === "done" || x.status === "partial") done++;
  $("#stCh").textContent = done + "/" + s.states.length;
  $("#stTime").textContent = s.elapsed ? fmtT(s.elapsed) : "00:00";
  const totalMin = s.states.reduce((a, x) => a + (x.duration_min || 0), 0);
  $("#stAudio").textContent = totalMin ? totalMin.toFixed(1) + " MIN" : "—";
  $("#outAbs").textContent = s.output_abs ? "→ " + s.output_abs : "";
}

function renderConsole() {
  const s = state.snap;
  if (!s) return;
  const logArr = s.log;
  const box = $("#console");
  if (logArr.length < state.renderedLog) { box.innerHTML = ""; state.renderedLog = 0; }
  while (state.renderedLog < logArr.length) {
    const e = logArr[state.renderedLog++];
    const d = document.createElement("div");
    d.className = "line " + (e.level || "info");
    d.textContent = "[" + new Date(e.t * 1000).toTimeString().slice(0, 8) + "] " + e.msg;
    box.appendChild(d);
  }
  box.scrollTop = box.scrollHeight;
}

function renderBookMeta() {
  if (!state.book) { $("#bookmeta").classList.add("hidden"); return; }
  $("#bookmeta").classList.remove("hidden");
  $("#bookName").textContent = state.book.name;
  $("#bmCh").textContent = state.book.chapters.length;
  $("#bmWords").textContent = state.book.chapters.reduce((a, c) => a + c.words, 0).toLocaleString();
}

function updateSelCount() {
  const total = state.book ? state.book.chapters.length : 0;
  $("#selcount").textContent = selection.size + "/" + total;
  renderStatus();
}

function onSnap(s) {
  state.snap = s;
  renderStatus();
  renderProgress();
  applyStates();
  renderConsole();
}

/* ---------- actions ---------- */
function setSel(set) {
  selection.clear();
  for (const i of set) selection.add(i);
  renderChapters();
}
function allIdx() { return new Set(state.book ? state.book.chapters.map((c) => c.index) : []); }

async function upload(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".epub")) { toast("Only .epub files are supported", true); return; }
  const fd = new FormData();
  fd.append("file", file);
  $("#dzTitle").textContent = "TRANSMITTING…";
  $("#dzSub").textContent = file.name;
  try {
    const r = await fetch("/api/book", { method: "POST", body: fd });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) { toast(j.detail || "Upload failed", true); resetDz(); return; }
    state.book = j;
    selection.clear();
    j.chapters.forEach((c) => selection.add(c.index));
    $("#dzTitle").textContent = "BOOK LOADED";
    renderBookMeta();
    renderChapters();
    toast(j.chapters.length + " chapters found");
  } catch (e) {
    toast("Upload error: " + e, true);
    resetDz();
  }
}
function resetDz() {
  $("#dzTitle").textContent = "DROP .EPUB FILE";
  $("#dzSub").textContent = "or click to browse";
}

async function start() {
  const body = {
    chapters: [...selection].sort((a, b) => a - b),
    voice: $("#voice").value,
    lang: $("#lang").value,
    output: $("#output").value.trim() || "./audiobook",
    keep_segments: $("#keepseg").checked,
  };
  const r = await fetch("/api/start", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) toast(j.detail || "Start failed", true);
}

async function stop() {
  const r = await fetch("/api/stop", { method: "POST" });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) toast(j.detail || "Stop failed", true);
}

/* ---------- folder picker (02 · SETTINGS) ---------- */
const dm = { dir: "", parent: null, exists: false };

function dmRow(name, path, isUp) {
  const row = document.createElement("div");
  row.className = "dm-row" + (isUp ? " up" : "");
  row.title = path;
  row.innerHTML = '<span class="ico">' + (isUp ? "↑" : "▸") + '</span><span class="nm"></span>';
  row.querySelector(".nm").textContent = name;
  row.addEventListener("click", () => dmLoad(path));
  return row;
}

function renderCrumbs() {
  const el = $("#dmCrumbs");
  el.innerHTML = "";
  const parts = dm.dir.split("/").filter(Boolean);
  const segs = [{ label: "/", path: "/" }];
  let acc = "";
  for (const p of parts) { acc += "/" + p; segs.push({ label: p, path: acc }); }
  segs.forEach((s, i) => {
    const here = i === segs.length - 1;
    const c = document.createElement("span");
    c.className = "crumb" + (here ? " here" : "") + (here && !dm.exists ? " missing" : "");
    c.textContent = s.label;
    c.title = s.path;
    if (!here) c.addEventListener("click", () => dmLoad(s.path));
    el.appendChild(c);
    if (!here) {
      const sep = document.createElement("span");
      sep.className = "csep";
      sep.textContent = " / ";
      el.appendChild(sep);
    }
  });
}

function renderDirList(j) {
  const el = $("#dmList");
  el.innerHTML = "";
  if (dm.parent) el.appendChild(dmRow("..", dm.parent, true));
  if (!j.exists) {
    el.insertAdjacentHTML("beforeend", '<div class="empty">PATH DOES NOT EXIST YET — SELECT WILL CREATE IT</div>');
  } else if (!j.dirs.length) {
    el.insertAdjacentHTML("beforeend", '<div class="empty">NO SUBDIRECTORIES</div>');
  } else {
    for (const d of j.dirs) el.appendChild(dmRow(d.name, d.path, false));
  }
}

async function dmLoad(path) {
  const q = path ? "?path=" + encodeURIComponent(path) : "";
  let j;
  try {
    const r = await fetch("/api/dirs" + q);
    j = await r.json().catch(() => ({}));
    if (!r.ok || typeof j.dir !== "string") throw new Error((j && j.detail) || "bad response");
  } catch (e) {
    toast("Folder picker error: " + e, true);
    return;
  }
  dm.dir = j.dir;
  dm.parent = j.parent;
  dm.exists = !!j.exists;
  renderCrumbs();
  renderDirList(j);
}

function openDirModal() {
  $("#dirmodal").classList.remove("hidden");
  dmLoad($("#output").value.trim());
}

function closeDirModal() {
  $("#dirmodal").classList.add("hidden");
}

/* ---------- voice preview (02 · SETTINGS) ---------- */
const vp = { url: null };  // current object URL

function vpToggle() {
  const btn = $("#vp-toggle");
  const panel = $("#vp-panel");
  const open = panel.classList.contains("hidden");
  panel.classList.toggle("hidden", !open);
  btn.setAttribute("aria-expanded", String(open));
  btn.textContent = open ? "ADVANCED ▲" : "ADVANCED";
}

async function vpGenerate() {
  const text = $("#vp-text").value.trim();
  const btn = $("#vp-generate");
  const status = $("#vp-status");
  const setMsg = (msg, cls) => { status.textContent = msg; status.className = cls || ""; };
  if (!text) {
    setMsg("Type some text first — even one line is enough.", "warn");
    return;
  }
  btn.disabled = true;
  setMsg("Generating…");
  try {
    const res = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, voice: $("#voice").value, lang: $("#lang").value }),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      setMsg("Preview failed — " + (j.detail || res.status), "err");
      return;
    }
    const blob = await res.blob();
    if (vp.url) URL.revokeObjectURL(vp.url);
    vp.url = URL.createObjectURL(blob);
    const audio = $("#vp-audio");
    audio.src = vp.url;
    audio.classList.remove("hidden");
    audio.play().catch(() => {});
    setMsg("Preview ready — playing.", "ok");
  } catch (e) {
    setMsg("Network error — is the server up?", "err");
  } finally {
    btn.disabled = false;
  }
}

/* ---------- stream ---------- */
function openStream() {
  if (state.es) state.es.close();
  state.es = new EventSource("/api/stream");
  state.es.onmessage = (e) => onSnap(JSON.parse(e.data));
  state.es.onerror = () => { /* EventSource auto-reconnects */ };
}

/* ---------- wiring ---------- */
(async function init() {
  const dz = $("#dropzone");
  const fileInp = $("#file");

  dz.addEventListener("click", () => { if (!busy()) fileInp.click(); });
  fileInp.addEventListener("change", () => upload(fileInp.files[0]));
  ["dragenter", "dragover"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => {
    if (busy()) return;
    upload(e.dataTransfer.files[0]);
  });

  $("#bAll").addEventListener("click", () => { if (!busy()) setSel(allIdx()); });
  $("#bNone").addEventListener("click", () => { if (!busy()) setSel(new Set()); });
  $("#bRange").addEventListener("click", () => {
    if (busy() || !state.book) return;
    const total = state.book.chapters.length;
    let a = parseInt($("#rA").value, 10), b = parseInt($("#rB").value, 10);
    if (!a || !b || a < 1 || b < 1 || a > total || b > total) {
      toast("Enter a valid range 1–" + total, true); return;
    }
    const [lo, hi] = [Math.min(a, b), Math.max(a, b)];
    setSel(new Set(state.book.chapters.slice(lo - 1, hi).map((c) => c.index)));
  });
  $("#startbtn").addEventListener("click", () => {
    if (busy()) stop(); else start();
  });

  // folder picker
  $("#browseBtn").addEventListener("click", openDirModal);
  $("#dmClose").addEventListener("click", closeDirModal);
  $("#dmCancel").addEventListener("click", closeDirModal);
  $("#dmSelect").addEventListener("click", () => {
    $("#output").value = dm.dir;
    closeDirModal();
    toast("Output location set → " + dm.dir);
  });
  $("#dirmodal").addEventListener("click", (e) => {
    if (e.target.id === "dirmodal") closeDirModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("#dirmodal").classList.contains("hidden")) closeDirModal();
  });

  // voice preview (02 · SETTINGS)
  $("#vp-toggle").addEventListener("click", vpToggle);
  $("#vp-generate").addEventListener("click", vpGenerate);

  // populate voices
  try {
    const meta = await fetch("/api/meta").then((r) => r.json());
    const vs = $("#voice");
    meta.voices.forEach((v) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v;
      vs.appendChild(o);
    });
    vs.value = meta.default_voice;
  } catch (e) { /* ignore */ }

  // restore state (e.g. page refresh mid-job)
  try {
    const s = await fetch("/api/snapshot").then((r) => r.json());
    state.snap = s;
    if (s.chapters.length) {
      state.book = { name: s.epub_name, chapters: s.chapters };
      selection.clear();
      (s.selected || []).forEach((i) => selection.add(i));
      $("#dzTitle").textContent = "BOOK LOADED";
      renderBookMeta();
    }
    renderChapters();
    renderStatus();
    renderProgress();
    renderConsole();
  } catch (e) { /* ignore */ }

  openStream();
})();
