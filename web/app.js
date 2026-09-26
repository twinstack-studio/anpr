const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return r.json();
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const file = (job, name) => `/api/jobs/${job}/file/${encodeURIComponent(name)}`;
const time = (iso) => new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Karachi" });
const plateTag = (p) => (p ? `<span class="plate">${esc(p)}</span>` : `<span class="plate none">plate not readable</span>`);

let current = null;       // job id on screen
let currentStatus = null;
let pollTimer = null;
let gate = "entry";
let jobsCache = [];

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 4000);
}

/* ---------- tabs ---------- */
function showTab(name) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $$(".tab").forEach((t) => t.classList.toggle("on", t.id === "tab-" + name));
  if (name === "log") loadLog();
  if (name === "parking") loadParking();
  if (name === "watch") loadWatch();
  history.replaceState(null, "", name === "monitor" ? location.pathname + (current ? "#job=" + current : "") : "#" + name);
}
$$("#tabs button").forEach((b) => (b.onclick = () => showTab(b.dataset.tab)));

$$("#gate button").forEach((b) => (b.onclick = () => {
  gate = b.dataset.v;
  $$("#gate button").forEach((x) => x.classList.toggle("on", x === b));
}));

/* ---------- stats ---------- */
async function loadStats() {
  try {
    const s = await api("/api/stats");
    $("#st-vehicles").textContent = s.vehicles;
    $("#st-plates").textContent = s.plates;
    $("#st-unique").textContent = s.unique_plates;
    $("#st-alerts").textContent = s.alerts;
    const secs = Math.round(s.videos.secs);
    $("#st-video").textContent = secs >= 60 ? `${Math.floor(secs / 60)}m ${secs % 60}s` : secs + "s";
  } catch {}
}

/* ---------- jobs ---------- */
async function loadJobs() {
  jobsCache = await api("/api/jobs");
  const el = $("#jobs");
  if (!jobsCache.length) { el.innerHTML = `<p class="muted">No clips yet.</p>`; return; }
  el.innerHTML = jobsCache.map((j) => {
    const status = j.status === "done" ? (j.kind === "video" ? `${Math.round(j.duration_s || 0)}s clip` : "photo")
      : j.status === "processing" ? `analysing ${Math.round(j.progress * 100)}%` : j.status;
    const thumb = j.status === "done" ? `style="background-image:url('${file(j.id, "thumb.jpg")}')"` : "";
    const tag = j.sample ? "Sample" : j.gate === "none" ? "Street" : j.gate === "exit" ? "Exit gate" : "Entry gate";
    return `<button class="job ${j.id === current ? "on" : ""}" data-id="${j.id}">
      <div class="thumb" ${thumb}><span class="tag">${tag}</span></div>
      <div class="meta"><b>${esc(j.filename)}</b><small class="st-${j.status}">${esc(status)}</small></div></button>`;
  }).join("");
  $$(".job", el).forEach((b) => (b.onclick = () => {
    openJob(b.dataset.id);
    $("#screen").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
}

async function openJob(id) {
  current = id;
  currentStatus = null;
  $$(".job").forEach((b) => b.classList.toggle("on", b.dataset.id === id));
  history.replaceState(null, "", "#job=" + id);
  clearTimeout(pollTimer);
  await poll();
}

async function poll() {
  const id = current;
  let data;
  try { data = await api(`/api/jobs/${id}`); } catch { return; }
  if (id !== current) return;
  const j = data.job;
  renderScreen(j);
  renderFeed(j, data.detections);
  if (j.status === "queued" || j.status === "processing") {
    pollTimer = setTimeout(poll, 1000);
  } else if (currentStatus && currentStatus !== j.status) {
    loadJobs(); loadStats();
    if (j.status === "done") toast(`Done: ${data.detections.filter((d) => d.plate).length} plates read`);
  }
  if (j.status !== currentStatus && (j.status === "processing" || currentStatus === null)) loadJobs();
  currentStatus = j.status;
}

function renderScreen(j) {
  const live = $("#live"), player = $("#player"), still = $("#still"), prog = $("#progress");
  $("#screen-empty").hidden = true;
  if (j.status === "done") {
    prog.hidden = true;
    live.hidden = true;
    if (live.src) live.removeAttribute("src");
    if (j.kind === "video") {
      still.hidden = true;
      const src = file(j.id, "annotated.mp4");
      if (!player.src.endsWith(src)) { player.src = src; player.play().catch(() => {}); }
      player.hidden = false;
    } else {
      player.hidden = true; player.pause();
      still.src = file(j.id, "annotated.jpg");
      still.hidden = false;
    }
  } else if (j.status === "failed") {
    prog.hidden = false; live.hidden = true; player.hidden = true; still.hidden = true;
    $("#progress-bar").style.width = "0";
    $("#progress-text").textContent = "Could not process this file: " + (j.error || "unknown error");
  } else {
    player.hidden = true; player.pause(); still.hidden = true;
    prog.hidden = false;
    $("#progress-bar").style.width = Math.round(j.progress * 100) + "%";
    $("#progress-text").textContent = j.status === "queued"
      ? `Waiting in queue${j.queue_position ? ` (position ${j.queue_position})` : ""}…`
      : `Analysing live… ${Math.round(j.progress * 100)}%`;
    if (j.status === "processing" && j.kind === "video") {
      const src = `/api/jobs/${j.id}/live`;
      if (!live.src.endsWith(src)) live.src = src;
      live.hidden = false;
    }
  }
}

function detCard(d, jobId) {
  const alert = d.watch_kind ? ` alert-${d.watch_kind}` : "";
  const crops = [d.plate_img, d.vehicle_img].filter(Boolean).map((n) => `<img src="${file(jobId, n)}" alt="" loading="lazy">`).join("");
  const conf = Math.round((d.confidence || 0) * 100);
  const when = d.t_first != null && d.direction ? `at ${d.t_first.toFixed(1)}s · ${esc(d.direction)}` : "";
  return `<div class="det${alert}" data-t="${d.t_first || 0}">
    <div class="crops">${crops || ""}</div>
    <div class="info">
      ${d.watch_kind ? `<span class="flag ${d.watch_kind}">${d.watch_kind}${d.watch_note ? " · " + esc(d.watch_note) : ""}</span>` : ""}
      <div>${plateTag(d.plate)}</div>
      <div>${esc(d.vehicle || "Vehicle")} ${d.readings > 1 ? `· read ${d.readings}×` : ""}</div>
      <div>${when}</div>
      ${d.plate ? `<div class="conf" title="confidence ${conf}%"><i style="width:${conf}%"></i></div>` : ""}
    </div></div>`;
}

function renderFeed(j, dets) {
  const el = $("#feed");
  const withPlate = dets.filter((d) => d.plate);
  const rest = dets.filter((d) => !d.plate);
  $("#feed-count").textContent = withPlate.length;
  if (!dets.length) {
    el.innerHTML = `<p class="muted">${j.status === "done" ? "No vehicles found in this clip." : "Vehicles will appear here when the clip is finished."}</p>`;
    return;
  }
  el.innerHTML = withPlate.map((d) => detCard(d, j.id)).join("") +
    (rest.length ? `<p class="muted">${rest.length} more vehicle${rest.length > 1 ? "s" : ""} tracked where the plate was not visible.</p>` : "");
  $$(".det", el).forEach((c) => (c.onclick = () => {
    const p = $("#player");
    if (!p.hidden) { p.currentTime = Math.max(0, +c.dataset.t - 0.3); p.play().catch(() => {}); }
  }));
}

/* ---------- upload ---------- */
async function upload(f) {
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  fd.append("gate", gate);
  const drop = $("#drop");
  const label = $("b", drop);
  label.textContent = "Uploading…";
  try {
    const xhr = new XMLHttpRequest();
    const res = await new Promise((resolve, reject) => {
      xhr.open("POST", "/api/jobs");
      xhr.upload.onprogress = (e) => e.lengthComputable && (label.textContent = `Uploading ${Math.round((e.loaded / e.total) * 100)}%`);
      xhr.onload = () => {
        let body = {};
        try { body = JSON.parse(xhr.responseText); } catch {}
        xhr.status < 300 ? resolve(body) : reject(new Error(body.detail || "Upload failed"));
      };
      xhr.onerror = () => reject(new Error("Upload failed. Check your connection."));
      xhr.send(fd);
    });
    await loadJobs();
    openJob(res.id);
    showTab("monitor");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } catch (e) {
    toast(e.message, true);
  } finally {
    label.textContent = "Upload video or photo";
    $("#file").value = "";
  }
}
$("#file").onchange = (e) => upload(e.target.files[0]);
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => upload(e.dataTransfer.files[0]));

/* ---------- log ---------- */
let searchTimer;
$("#search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadLog, 250); };
async function loadLog() {
  const rows = await api("/api/detections?q=" + encodeURIComponent($("#search").value));
  $("#log").innerHTML = rows.length ? rows.map((d) => `<tr>
      <td>${plateTag(d.plate)} ${d.watch_kind ? `<span class="flag ${d.watch_kind}">${d.watch_kind}</span>` : ""}</td>
      <td>${d.plate_img ? `<img src="${file(d.job_id, d.plate_img)}" alt="">` : ""}</td>
      <td>${esc(d.vehicle || "")}</td>
      <td>${time(d.seen_at)}</td>
      <td>${esc(d.gate)}</td>
      <td>${Math.round(d.confidence * 100)}%</td>
      <td><button class="link" data-job="${d.job_id}">${esc(d.filename)}</button></td></tr>`).join("")
    : `<tr><td colspan="7" class="muted">No plates found.</td></tr>`;
  $$("#log .link").forEach((b) => (b.onclick = () => { showTab("monitor"); openJob(b.dataset.job); }));
}

/* ---------- parking ---------- */
["#rate1", "#rate2"].forEach((s) => ($(s).oninput = loadParking));
async function loadParking() {
  const p = await api(`/api/parking?rate_first=${+$("#rate1").value || 0}&rate_next=${+$("#rate2").value || 0}`);
  $("#inside-count").textContent = p.inside.length;
  $("#inside").innerHTML = p.inside.length ? p.inside.map((v) => `<div class="mini">${plateTag(v.plate)}<span>${esc(v.vehicle || "")} · in since ${time(v.entry)}</span></div>`).join("")
    : `<p class="muted">No vehicles inside. Upload a clip as <b>Entry gate</b>.</p>`;
  const dur = (m) => (m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`);
  $("#sessions").innerHTML = p.sessions.length ? p.sessions.map((s) => `<tr><td>${plateTag(s.plate)}</td><td>${time(s.entry)}</td><td>${time(s.exit)}</td><td>${dur(s.minutes)}</td><td><b>Rs ${s.fee}</b></td></tr>`).join("")
    : `<tr><td colspan="5" class="muted">No completed sessions yet. Upload a later clip of the same cars as <b>Exit gate</b>.</td></tr>`;
}

/* ---------- watchlist ---------- */
async function loadWatch() {
  const rows = await api("/api/watchlist");
  $("#watch").innerHTML = rows.length ? rows.map((w) => `<div class="w-item">${plateTag(w.plate)}<span class="flag ${w.kind}">${w.kind}</span><span class="note">${esc(w.note)}</span><button data-p="${esc(w.plate)}" title="Remove">×</button></div>`).join("")
    : `<p class="muted">Watchlist is empty.</p>`;
  $$("#watch button").forEach((b) => (b.onclick = async () => {
    await api("/api/watchlist/" + encodeURIComponent(b.dataset.p), { method: "DELETE" });
    loadWatch(); loadStats();
  }));
}
$("#watch-form").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await api("/api/watchlist", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ plate: f.plate.value, kind: f.kind.value, note: f.note.value }) });
    toast(`${r.plate} added to watchlist`);
    f.reset();
    loadWatch(); loadStats();
  } catch (err) { toast(err.message, true); }
};

/* ---------- start ---------- */
(async function init() {
  loadStats();
  await loadJobs();
  const hash = location.hash.slice(1);
  if (hash.startsWith("job=")) openJob(hash.slice(4));
  else if (["log", "parking", "watch", "about"].includes(hash)) showTab(hash);
  else if (jobsCache.length) openJob(jobsCache.find((j) => j.status === "done")?.id || jobsCache[0].id);
  setInterval(() => { if (document.visibilityState === "visible") { loadJobs(); loadStats(); } }, 15000);
})();
