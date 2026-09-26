/* TwinStack Gate: society gate management front end (no framework). */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const API = "/api/soc";

async function api(path, opts = {}) {
  const o = { ...opts, headers: { ...(opts.headers || {}) } };
  if (o.body && typeof o.body !== "string" && !(o.body instanceof FormData)) {
    o.body = JSON.stringify(o.body);
    o.headers["Content-Type"] = "application/json";
  }
  const r = await fetch(path.startsWith("/") ? path : `${API}/${path}`, o);
  if (r.status === 401 && !path.includes("login")) { showLogin(); throw new Error("Please log in"); }
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : "Request failed");
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

const TZ = "Asia/Karachi";
const fmtTime = (iso) => new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: TZ });
const fmtDT = (iso) => new Date(iso).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", timeZone: TZ });
const fmtDay = (iso) => new Date(iso + (iso.length === 10 ? "T00:00:00+05:00" : "")).toLocaleDateString("en-GB", { day: "2-digit", month: "short", timeZone: TZ });
const todayISO = () => new Date().toLocaleDateString("en-CA", { timeZone: TZ });
const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00+05:00"); d.setDate(d.getDate() + n); return d.toLocaleDateString("en-CA", { timeZone: TZ }); };
const dur = (m) => (m >= 1440 ? `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h` : m >= 60 ? `${Math.floor(m / 60)}h ${m % 60}m` : `${m}m`);
const ago = (iso) => { const m = Math.round((Date.now() - new Date(iso)) / 60000); return m < 1 ? "just now" : m < 60 ? `${m} min ago` : fmtDT(iso); };
const img = (p) => (p ? `/api/soc/img/${p}` : "");
const plateTag = (p, big = false) => (p ? `<span class="plate${big ? " big" : ""}">${esc(p)}</span>` : `<span class="plate none">No plate read</span>`);
const CAT = { resident: "Resident", staff: "Staff", service: "Service", visitor: "Visitor", unknown: "Unknown", blacklist: "Blacklisted", unreadable: "Plate not read" };
const STATUS = { allowed: "Allowed", denied: "Denied", pending: "Needs decision", no_action: "No action" };
const catTag = (c) => `<span class="tag c-${c}">${CAT[c] || c}</span>`;
const statusTag = (s) => `<span class="status s-${s}">${STATUS[s] || s}</span>`;
const gateTag = (g) => `<span class="gate-tag">${g === "exit" ? "OUT" : "IN"}</span>`;

let ME = null;
let cleanup = [];
const onLeave = (fn) => cleanup.push(fn);

function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.hidden = false;
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.hidden = true), 3500);
}

/* ---------- modal ---------- */
function modal(title, html, onOpen) {
  $("#modal-title").textContent = title;
  $("#modal-body").innerHTML = html;
  $("#modal").hidden = false;
  onOpen && onOpen($("#modal-body"));
}
function closeModal() { $("#modal").hidden = true; $("#modal-body").innerHTML = ""; }
$("#modal-close").onclick = closeModal;
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

function formData(form) {
  const o = {};
  new FormData(form).forEach((v, k) => (o[k] = v));
  return o;
}
function bindForm(form, fn) {
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("button[type=submit], button:not([type])", form);
    btn && (btn.disabled = true);
    try { await fn(formData(form), form); } catch (err) { toast(err.message, true); } finally { btn && (btn.disabled = false); }
  };
}

/* ---------- tooltip ---------- */
const tip = $("#tip");
function showTip(e, html) { tip.innerHTML = html; tip.hidden = false; moveTip(e); }
function moveTip(e) {
  const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
  tip.style.left = x + "px";
  tip.style.top = e.clientY - tip.offsetHeight - 10 + "px";
}
function hideTip() { tip.hidden = true; }

/* ---------- bar chart (single series) ---------- */
function barChart(values, labels, tipFn, { height = 170, labelEvery = 1 } = {}) {
  const W = 600, H = height, pad = { l: 28, r: 4, t: 8, b: 22 };
  const max = Math.max(4, ...values);
  const step = max <= 10 ? 2 : max <= 40 ? 10 : max <= 100 ? 20 : Math.ceil(max / 5 / 10) * 10;
  const top = Math.ceil(max / step) * step;
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const bw = iw / values.length;
  const y = (v) => pad.t + ih - (v / top) * ih;
  let s = `<svg viewBox="0 0 ${W} ${H}" role="img">`;
  for (let v = 0; v <= top; v += step) {
    s += `<line class="grid-line" x1="${pad.l}" x2="${W - pad.r}" y1="${y(v)}" y2="${y(v)}"/><text class="axis" x="${pad.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  values.forEach((v, i) => {
    const x = pad.l + i * bw, w = Math.max(2, bw - 2 * Math.max(1, bw * 0.14));
    const bx = x + (bw - w) / 2, by = y(v), bh = Math.max(0, pad.t + ih - by);
    const r = Math.min(4, w / 2, bh);
    const path = bh > 0 ? `M${bx},${by + bh} V${by + r} Q${bx},${by} ${bx + r},${by} H${bx + w - r} Q${bx + w},${by} ${bx + w},${by + r} V${by + bh} Z` : "";
    s += `<g data-i="${i}"><rect class="hit" x="${x}" y="${pad.t}" width="${bw}" height="${ih}"/>${path ? `<path d="${path}" fill="var(--bar)" class="bar-p"/>` : ""}</g>`;
    if (i % labelEvery === 0) s += `<text class="axis" x="${x + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(labels[i])}</text>`;
  });
  s += "</svg>";
  const wrap = document.createElement("div");
  wrap.className = "chart";
  wrap.innerHTML = s;
  $$("g[data-i]", wrap).forEach((g) => {
    const i = +g.dataset.i;
    g.onmouseenter = (e) => { showTip(e, tipFn(i)); $(".bar-p", g)?.setAttribute("opacity", "0.75"); };
    g.onmousemove = moveTip;
    g.onmouseleave = () => { hideTip(); $(".bar-p", g)?.removeAttribute("opacity"); };
  });
  return wrap;
}

/* ---------- alert sound ---------- */
let audioCtx;
function beep(times = 3) {
  try {
    audioCtx = audioCtx || new AudioContext();
    for (let i = 0; i < times; i++) {
      const o = audioCtx.createOscillator(), g = audioCtx.createGain();
      o.type = "square"; o.frequency.value = 880;
      g.gain.value = 0.08;
      o.connect(g); g.connect(audioCtx.destination);
      const t = audioCtx.currentTime + i * 0.35;
      o.start(t); o.stop(t + 0.2);
    }
  } catch {}
}
document.addEventListener("pointerdown", () => { try { audioCtx = audioCtx || new AudioContext(); audioCtx.resume(); } catch {} }, { once: true });

/* ---------- blacklist alerts for staff, on every page ---------- */
let alertSince = 0;
async function alertPoll() {
  if (!ME || ME.user.role === "resident" || document.visibilityState !== "visible") return;
  try {
    if (!alertSince) {
      const last = await api("events?limit=1");
      alertSince = last[0]?.id || 1;
      return;
    }
    const rows = await api(`events?since=${alertSince}&limit=50`);
    if (rows.length) alertSince = Math.max(alertSince, ...rows.map((r) => r.id));
    const bl = rows.find((r) => r.category === "blacklist");
    if (bl) {
      const b = $("#alert-banner");
      b.innerHTML = `<span style="font-size:28px">⛔</span><div><b>Blacklisted vehicle at the gate</b><div>${esc(bl.plate)} · ${esc(bl.note || "")}</div><div class="ur">بلیک لسٹ گاڑی — گیٹ نہ کھولیں</div></div>`;
      b.hidden = false;
      b.onclick = () => { b.hidden = true; showEvent(bl); };
      clearTimeout(b._h);
      b._h = setTimeout(() => (b.hidden = true), 15000);
      beep(4);
    }
    document.dispatchEvent(new CustomEvent("gate-events", { detail: rows }));
  } catch {}
}
setInterval(alertPoll, 2500);

/* ---------- event card & detail ---------- */
function verdictText(e) {
  if (e.category === "blacklist") return "Blacklisted: do not open";
  if (e.status === "pending") return e.category === "unreadable" ? "Plate not read: check the vehicle" : "Unknown vehicle: verify with the house";
  if (e.status === "denied") return "Entry refused";
  if (e.gate === "exit") return "Leaving";
  return "Allowed in";
}
const REQ = {
  waiting: (r) => `<div class="req req-wait">⏳ Asked House ${esc(r.house || "")}: waiting for reply…</div>`,
  approved: (r) => `<div class="req req-ok">✓ House ${esc(r.house || "")} said YES${r.answered_by_name ? ` (${esc(r.answered_by_name)})` : ""} <span class="ur">گھر والوں نے اجازت دی</span></div>`,
  rejected: (r) => `<div class="req req-bad">✗ House ${esc(r.house || "")} said NO <span class="ur">گھر والوں نے انکار کیا</span></div>`,
  expired: () => "",
};
function reqLine(e) {
  const r = e.request;
  if (!r) return "";
  return (REQ[r.status] || REQ.expired)({ ...r, house: e.house });
}
function evCard(e, { decide = false } = {}) {
  const pics = [e.plate_img, e.vehicle_img].filter(Boolean).map((p) => `<img src="${img(p)}" alt="" loading="lazy">`).join("") || `<div class="noimg">🚗</div>`;
  const who = [e.house ? `House ${esc(e.house)}` : "", e.owner && e.category === "resident" ? esc(e.owner) : "", e.visitor ? esc(e.visitor) : "", e.note ? esc(e.note) : ""].filter(Boolean).join(" · ");
  return `<div class="ev b-${e.category} ${e.status === "pending" && decide ? "pending" : ""}" data-id="${e.id}">
    <div class="pics">${pics}</div>
    <div>
      <div class="row1">${plateTag(e.plate)} ${catTag(e.category)} ${gateTag(e.gate)}</div>
      <div class="row2">${who || esc(e.vehicle || "Vehicle")}</div>
      <div class="row3">${fmtTime(e.at)} · ${esc(e.vehicle || "")} · ${statusTag(e.status)}${e.camera ? " · " + esc(e.camera) : e.source === "manual" ? " · typed by guard" : e.source === "upload" ? " · uploaded clip" : ""}</div>
      ${e.status === "pending" || e.request?.status !== "expired" ? reqLine(e) : ""}
      ${decide && e.status === "pending" ? `<div class="btns">
        ${ME.resident_approval && e.category !== "blacklist" && e.request?.status !== "waiting" ? `<button class="btn small ask" data-act="ask">📱 Ask resident</button>` : ""}
        <button class="btn ok small" data-act="visitor">Allow as visitor</button>
        <button class="btn small" data-act="allow">Allow</button>
        <button class="btn bad small" data-act="deny">Deny</button></div>` : ""}
    </div></div>`;
}
function bindEvCards(root, list, after) {
  $$(".ev", root).forEach((el) => {
    const e = list.find((x) => String(x.id) === el.dataset.id);
    el.onclick = (ev) => {
      const act = ev.target.closest("[data-act]")?.dataset.act;
      if (act === "allow") return decideEvent(e, "allowed", {}, after);
      if (act === "deny") return decideEvent(e, "denied", {}, after);
      if (act === "visitor") return visitorForm(e, after);
      if (act === "ask") return askForm(e, after);
      showEvent(e, after);
    };
  });
}
let gateCam = null;  // camera shown on the gate screen (its barrier opens for typed-in vehicles)
const barrierMsg = (r) => (r?.barrier ? ` · barrier opening (${r.barrier.camera})` : "");
async function decideEvent(e, status, extra, after) {
  try {
    const r = await api(`events/${e.id}/decide`, { method: "POST", body: { status, camera_id: gateCam, ...extra } });
    toast(status === "allowed" ? "Allowed" + barrierMsg(r) : "Denied");
    closeModal();
    after && after();
  } catch (err) { toast(err.message, true); }
}
function askForm(e, after) {
  modal("Ask the resident", `<form id="af" class="form-grid">
      <p class="full muted" style="margin:0">The house gets a notification on their phone with this vehicle. You still decide at the gate. <span class="ur">گھر والوں سے فون پر پوچھیں</span></p>
      ${ME.demo ? `<p class="full note-box" style="margin:0">Demo: house <b>A-12</b> has the resident account. Log in as <code>resident</code> on your phone to answer.</p>` : ""}
      <label>House they are visiting<input name="house" placeholder="e.g. A-12" value="${esc(e.house || (ME.demo ? "A-12" : ""))}" required></label>
      <label>Visitor name<input name="visitor" placeholder="Name they gave" value="${esc(e.visitor || "")}"></label>
      <div class="actions full"><button type="button" class="btn" id="af-cancel">Cancel</button><button class="btn primary">📱 Send to the house</button></div></form>`, (b) => {
    $("#af-cancel", b).onclick = closeModal;
    bindForm($("#af", b), async (d) => {
      const r = await api(`events/${e.id}/ask`, { method: "POST", body: d });
      closeModal();
      toast(`Sent to House ${r.house}${r.notified ? ` · ${r.notified} phone${r.notified > 1 ? "s" : ""} notified` : " · shown in their app"}`);
      after && after();
    });
  });
}
function visitorForm(e, after) {
  modal("Allow as visitor", `<form id="vf" class="form-grid">
      <label>House you are visiting<input name="house" placeholder="e.g. A-12" value="${esc(e.house || "")}" required></label>
      <label>Visitor name<input name="visitor" placeholder="Name" value="${esc(e.visitor || "")}" required></label>
      <label class="full">Plate<input name="plate" value="${esc(e.plate || "")}" placeholder="Plate if the camera missed it"></label>
      <label class="full">Note<input name="note" placeholder="e.g. Called the house, confirmed"></label>
      <div class="actions full"><button type="button" class="btn" id="vf-cancel">Cancel</button><button class="btn ok">Allow in</button></div></form>`, (b) => {
    $("#vf-cancel", b).onclick = closeModal;
    bindForm($("#vf", b), (d) => decideEvent(e, "allowed", d, after));
  });
}
function showEvent(e, after) {
  const staff = ME.user.role !== "resident";
  const pics = [e.plate_img, e.vehicle_img].filter(Boolean);
  modal(e.plate || "Vehicle", `
    ${pics.length ? `<div class="detail-pics">${pics.map((p) => `<img src="${img(p)}" alt="">`).join("")}</div>` : ""}
    <dl class="kv">
      <dt>Plate</dt><dd>${plateTag(e.plate)}</dd>
      <dt>Result</dt><dd>${catTag(e.category)} ${statusTag(e.status)}</dd>
      <dt>Time</dt><dd>${fmtDT(e.at)} · ${e.gate === "exit" ? "Exit" : "Entry"}</dd>
      <dt>Vehicle</dt><dd>${esc(e.vehicle || "–")}${e.confidence ? ` · read with ${Math.round(e.confidence * 100)}% confidence` : ""}</dd>
      ${e.house ? `<dt>House</dt><dd>${esc(e.house)} · ${esc(e.owner || "")} ${e.house_phone ? `· <a href="tel:${esc(e.house_phone)}">${esc(e.house_phone)}</a>` : ""}</dd>` : ""}
      ${e.visitor ? `<dt>Visitor</dt><dd>${esc(e.visitor)}</dd>` : ""}
      ${e.note ? `<dt>Note</dt><dd>${esc(e.note)}</dd>` : ""}
      <dt>Source</dt><dd>${e.camera ? esc(e.camera) : e.source === "manual" ? "Typed in by guard" : e.source === "upload" ? "Uploaded clip" : "Camera"}</dd>
      ${e.decided_by_name ? `<dt>Decided by</dt><dd>${esc(e.decided_by_name)} · ${fmtDT(e.decided_at)}</dd>` : ""}
      ${e.request && e.request.status !== "expired" ? `<dt>Resident</dt><dd>${reqLine(e)}</dd>` : ""}
    </dl>
    ${staff ? `<div class="actions">
      <button class="btn ok" data-a="visitor">Allow as visitor</button>
      <button class="btn" data-a="allow">Mark allowed</button>
      <button class="btn bad" data-a="deny">Mark denied</button></div>` : ""}`, (b) => {
    if (!staff) return;
    $("[data-a=visitor]", b).onclick = () => visitorForm(e, after);
    $("[data-a=allow]", b).onclick = () => decideEvent(e, "allowed", {}, after);
    $("[data-a=deny]", b).onclick = () => decideEvent(e, "denied", {}, after);
  });
}

/* ================= VIEWS ================= */
const VIEWS = {};

/* ---------- guard screen ---------- */
VIEWS.gate = async (v) => {
  v.innerHTML = `<div class="gate-layout">
    <div class="gate-left">
      <div class="cam-wrap"><div class="cam-tabs" id="cam-tabs"></div>
      <div class="cam" id="cam"><div class="cam-msg" id="cam-msg">Loading camera…</div><img id="cam-img" alt="" hidden><span class="cam-label" id="cam-label" hidden></span></div></div>
      <div class="barrier card" id="barrier" hidden>
        <div class="boom" aria-hidden="true"><span class="post"></span><span class="arm"></span></div>
        <div class="grow"><b id="b-state">Barrier closed</b><small id="b-sub" class="ur">گیٹ بند ہے</small></div>
        <button class="btn primary" id="b-open">Open barrier <span class="ur">گیٹ کھولیں</span></button>
      </div>
      <div class="card manual">
        <h3>Check a vehicle or pass <span class="ur">گاڑی یا پاس چیک کریں</span></h3>
        <form id="manual">
          <input name="q" placeholder="Plate (LEB-1234) or pass code (6 letters)" required autocomplete="off">
          <select name="gate" class="gate-sel"><option value="entry">Entry</option><option value="exit">Exit</option></select>
          <button class="btn primary">Check</button>
        </form>
        <p class="muted" style="margin:8px 0 0;font-size:13px">Use this when the camera cannot read a plate, or a visitor shows a pass code.</p>
        <div class="toolbar" style="margin:12px 0 0">
          <label class="btn small" style="flex-direction:row;cursor:pointer">⤒ Upload a gate clip<input type="file" id="clip" accept="video/*,image/*" hidden></label>
          <select id="clip-gate" style="padding:5px 10px"><option value="entry">as Entry</option><option value="exit">as Exit</option></select>
          <span class="muted" id="clip-status" style="font-size:13px"></span>
        </div>
      </div>
    </div>
    <div class="gate-right">
      <div id="verdict"></div>
      <div class="section-title">Needs decision <span class="tag c-unknown" id="pend-count">0</span></div>
      <div class="decide-list" id="pending"></div>
      <div class="section-title">Recent at the gate</div>
      <div class="feed" id="feed"></div>
    </div></div>`;

  // cameras
  const cams = await api("cameras");
  let camId = cams[0]?.id;
  const camImg = $("#cam-img"), camMsg = $("#cam-msg"), camLabel = $("#cam-label");
  function renderTabs() {
    $("#cam-tabs").innerHTML = cams.length > 1 ? cams.map((c) => `<button class="btn small ${c.id === camId ? "primary" : ""}" data-c="${c.id}">${esc(c.name)}</button>`).join("") : "";
    $$("#cam-tabs button").forEach((b) => (b.onclick = () => { camId = +b.dataset.c; renderTabs(); openCam(); }));
  }
  function openCam() {
    const c = cams.find((x) => x.id === camId);
    gateCam = c?.id || null;
    $("#barrier").hidden = !c || c.barrier_kind === "none";
    if (!c) { camMsg.innerHTML = `<b>No camera connected</b><span>Add a camera in Cameras, or upload a clip below.</span>`; return; }
    camLabel.innerHTML = `<span class="dot" id="cam-dot"></span>${esc(c.name)}`;
    camLabel.hidden = false;
    camMsg.hidden = false;
    camMsg.innerHTML = `<b>Connecting to ${esc(c.name)}…</b><span>${c.demo || c.source?.startsWith("Simulated") ? "The demo camera plays a Lahore traffic clip in real time." : ""}</span>`;
    camImg.hidden = true;
    camImg.onload = () => { camMsg.hidden = true; camImg.hidden = false; $("#cam-dot")?.classList.add("online"); };
    camImg.onerror = () => { camImg.hidden = true; camMsg.hidden = false; setTimeout(() => camImg.isConnected && openCam(), 4000); };
    camImg.src = `/api/soc/cameras/${c.id}/live?t=${Date.now()}`;
  }
  renderTabs();
  openCam();
  onLeave(() => { camImg.onerror = null; camImg.removeAttribute("src"); gateCam = null; });

  // barrier state (also keeps a simulated camera awake)
  function showBarrier(st) {
    const open = !!st.barrier_open;
    $("#barrier").classList.toggle("open", open);
    $("#b-state").textContent = open ? "Barrier open" : "Barrier closed";
    $("#b-sub").textContent = open ? "گیٹ کھلا ہے" : "گیٹ بند ہے";
  }
  async function pollBarrier() {
    if (!gateCam || $("#barrier").hidden) return;
    try { showBarrier(await api(`cameras/${gateCam}/wake`, { method: "POST" })); } catch {}
  }
  const bTimer = setInterval(pollBarrier, 1000);
  onLeave(() => clearInterval(bTimer));
  $("#b-open").onclick = async () => {
    try { showBarrier(await api(`cameras/${gateCam}/barrier`, { method: "POST", body: {} })); toast("Barrier opened by hand (logged)"); } catch (err) { toast(err.message, true); }
  };

  // events
  let feed = [], pending = [];
  const answered = new Map();
  async function load() {
    const [recent, pend] = await Promise.all([api("events?limit=25"), api(`events?status=pending&day_from=${todayISO()}&limit=30`)]);
    feed = recent; pending = pend;
    for (const e of pending) {
      const st = e.request?.status;
      if (st && answered.has(e.id) && answered.get(e.id) === "waiting" && (st === "approved" || st === "rejected")) {
        beep(st === "approved" ? 1 : 2);
        toast(`House ${e.house} said ${st === "approved" ? "YES" : "NO"} for ${e.plate || e.visitor || "the vehicle"}`, st === "rejected");
      }
      if (st) answered.set(e.id, st);
    }
    render();
  }
  function render() {
    $("#pend-count").textContent = pending.length;
    $("#pending").innerHTML = pending.length ? pending.map((e) => evCard(e, { decide: true })).join("") : `<p class="muted" style="margin:0">Nothing waiting. Unknown vehicles appear here.</p>`;
    bindEvCards($("#pending"), pending, load);
    const rest = feed.filter((e) => e.status !== "pending").slice(0, 15);
    $("#feed").innerHTML = rest.length ? rest.map((e) => evCard(e)).join("") : `<p class="muted">No vehicles yet today.</p>`;
    bindEvCards($("#feed"), rest, load);
    const last = feed[0];
    if (last && Date.now() - new Date(last.at) < 10 * 60000) {
      const cls = last.category === "blacklist" || last.status === "denied" ? "v-bad" : last.status === "pending" ? "v-warn" : "v-ok";
      const ico = cls === "v-bad" ? "⛔" : cls === "v-warn" ? "⚠️" : "✅";
      const ur = cls === "v-bad" ? "رکیں — گیٹ نہ کھولیں" : cls === "v-warn" ? "گھر سے تصدیق کریں" : "گیٹ کھولیں";
      $("#verdict").innerHTML = `<div class="big-verdict ${cls}"><span class="ico">${ico}</span><div>
        <b>${esc(verdictText(last))}</b>
        <small>${esc(last.plate || "No plate")} · ${CAT[last.category]}${last.house ? " · House " + esc(last.house) : ""} · ${fmtTime(last.at)}</small>
        <div class="ur">${ur}</div></div></div>`;
    } else $("#verdict").innerHTML = "";
  }
  await load();
  const onNew = (e) => { if (e.detail.length) load(); };
  document.addEventListener("gate-events", onNew);
  const timer = setInterval(() => document.visibilityState === "visible" && load(), 4000);
  onLeave(() => { document.removeEventListener("gate-events", onNew); clearInterval(timer); });

  bindForm($("#manual"), async (d, form) => {
    const q = d.q.trim().toUpperCase();
    const maybeCode = /^[A-Z0-9]{6}$/.test(q);  // a 6-character entry is tried as a pass code first, then as a plate
    const e = await api("events", { method: "POST", body: { plate: q, code: maybeCode ? q : "", gate: d.gate, camera_id: gateCam } });
    form.reset();
    await load();
    if (e.status === "pending") (ME.resident_approval ? askForm : visitorForm)(e, load);
    else toast(`${e.plate || e.visitor}: ${CAT[e.category]} · ${STATUS[e.status]}${barrierMsg(e)}`, e.status === "denied");
  });

  $("#clip").onchange = async (ev) => {
    const f = ev.target.files[0];
    if (!f) return;
    const st = $("#clip-status");
    const fd = new FormData();
    fd.append("file", f); fd.append("gate", $("#clip-gate").value); fd.append("soc", "1");
    st.textContent = "Uploading…";
    try {
      const j = await api("/api/jobs", { method: "POST", body: fd });
      const check = async () => {
        if (!st.isConnected) return;
        const r = await api(`/api/jobs/${j.id}`);
        if (r.job.status === "done") { st.textContent = "Done. Vehicles added to the gate log."; load(); return; }
        if (r.job.status === "failed") { st.textContent = "Could not process: " + (r.job.error || ""); return; }
        st.textContent = r.job.status === "queued" ? "Waiting in queue…" : `Analysing ${Math.round(r.job.progress * 100)}%`;
        setTimeout(check, 1500);
      };
      check();
    } catch (err) { st.textContent = err.message; }
    ev.target.value = "";
  };
};

/* ---------- dashboard ---------- */
VIEWS.dashboard = async (v) => {
  const d = await api("dashboard");
  const k = d.kpi;
  v.innerHTML = `
    <section class="kpis">
      <div class="kpi"><b>${k.entries}</b><span>entries today</span></div>
      <div class="kpi"><b>${k.exits}</b><span>exits today</span></div>
      <div class="kpi"><b>${d.inside_total}</b><span>vehicles inside now</span></div>
      <div class="kpi"><b>${d.inside_visitors}</b><span>visitors inside</span></div>
      <div class="kpi ${k.pending ? "warn" : ""}"><b>${k.pending}</b><span>waiting for guard</span></div>
      <div class="kpi ${k.alerts ? "bad" : ""}"><b>${k.alerts}</b><span>blacklist alerts today</span></div>
    </section>
    <div class="grid g2">
      <div class="card"><h3>Entries by hour, today</h3><div id="ch-hour"></div></div>
      <div class="card"><h3>Entries per day, last 7 days</h3><div id="ch-week"></div></div>
    </div>
    <div class="grid g3" style="margin-top:16px">
      <div class="card"><h3>Today's entries by type</h3><div id="by-cat"></div></div>
      <div class="card"><h3>Cameras <a class="right" href="#/cameras">Manage</a></h3><div id="cams"></div></div>
      <div class="card"><h3>Society</h3><div id="counts"></div></div>
    </div>
    <div class="grid g3" style="margin-top:16px">
      <div class="card"><h3>Alerts &amp; refused, last 7 days</h3><div id="alerts"></div></div>
      <div class="card"><h3>Visitors staying long</h3><div id="over"></div></div>
      <div class="card"><h3>Inside now <a class="right" href="#/inside">All ${d.inside_total}</a></h3><div id="inside"></div></div>
    </div>`;
  const hrs = [...Array(24).keys()];
  $("#ch-hour").append(barChart(d.hourly, hrs.map((h) => String(h).padStart(2, "0")), (i) => `<b>${String(i).padStart(2, "0")}:00–${String(i + 1).padStart(2, "0")}:00</b>${d.hourly[i]} entries`, { labelEvery: 3 }));
  const days = [...Array(7).keys()].map((i) => addDays(todayISO(), i - 6));
  const wk = days.map((dd) => Object.values(d.week[dd] || {}).reduce((a, b) => a + b, 0));
  $("#ch-week").append(barChart(wk, days.map((dd) => fmtDay(dd)), (i) => {
    const c = d.week[days[i]] || {};
    return `<b>${fmtDay(days[i])} · ${wk[i]} entries</b>` + Object.entries(c).sort((a, b) => b[1] - a[1]).map(([kk, n]) => `${CAT[kk]}: ${n}`).join("<br>");
  }));
  const cats = ["resident", "visitor", "service", "staff", "unknown", "blacklist", "unreadable"];
  const tot = Object.values(d.by_category).reduce((a, b) => a + b, 0) || 1;
  $("#by-cat").innerHTML = cats.map((c) => `<div class="mini">${catTag(c)}<span class="grow"></span><b>${d.by_category[c] || 0}</b><span class="muted" style="width:44px;text-align:right">${Math.round(((d.by_category[c] || 0) / tot) * 100)}%</span></div>`).join("");
  $("#cams").innerHTML = d.cameras.length ? d.cameras.map((c) => `<div class="mini"><b>${esc(c.name)}</b><span class="grow">${c.gate}</span><span class="state ${c.state}">${c.state}${c.fps ? ` · ${c.fps} fps` : ""}</span></div>`).join("") : `<p class="muted">No cameras yet.</p>`;
  const n = d.counts;
  $("#counts").innerHTML = `<div class="mini"><span class="grow">Houses</span><b>${n.houses}</b></div><div class="mini"><span class="grow">Registered vehicles</span><b>${n.vehicles}</b></div>
    <div class="mini"><span class="grow">Active visitor passes</span><b>${n.passes}</b></div><div class="mini"><span class="grow">Blacklisted vehicles</span><b>${n.blacklist}</b></div>`;
  const miniEv = (list, id, empty) => {
    $(id).innerHTML = list.length ? list.map((e) => `<div class="mini click" data-id="${e.id}" style="cursor:pointer">${plateTag(e.plate)}<span class="grow">${e.minutes != null ? `${esc(e.house ? "House " + e.house : e.visitor || CAT[e.category])} · inside ${dur(e.minutes)}` : esc(e.note || CAT[e.category])}</span><span class="muted">${ago(e.at)}</span></div>`).join("") : `<p class="muted">${empty}</p>`;
    $$(".mini[data-id]", $(id)).forEach((el) => (el.onclick = () => showEvent(list.find((x) => String(x.id) === el.dataset.id))));
  };
  miniEv(d.alerts, "#alerts", "No alerts this week.");
  miniEv(d.overstay, "#over", "No visitor has stayed longer than the limit.");
  miniEv(d.inside, "#inside", "No vehicles inside.");
  const t = setInterval(() => document.visibilityState === "visible" && route(true), 30000);
  onLeave(() => clearInterval(t));
};

/* ---------- gate log ---------- */
VIEWS.log = async (v) => {
  const resident = ME.user.role === "resident";
  v.innerHTML = `<div class="toolbar">
      <input class="grow" id="q" type="search" placeholder="Search plate, house, owner or visitor">
      <input type="date" id="from"> <span class="muted">to</span> <input type="date" id="to">
      <select id="cat"><option value="">All types</option>${Object.entries(CAT).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
      <select id="gate"><option value="">In &amp; out</option><option value="entry">Entries</option><option value="exit">Exits</option></select>
      <select id="st"><option value="">Any result</option>${Object.entries(STATUS).map(([k, l]) => `<option value="${k}">${l}</option>`).join("")}</select>
      ${resident ? "" : `<a class="btn" id="csv" href="#">⤓ CSV</a>`}
    </div>
    <div class="table-wrap"><table><thead><tr><th>Time</th><th></th><th>Plate</th><th class="hide-sm">Photo</th><th>Type</th><th>House / visitor</th><th>Result</th><th class="hide-sm">Source</th></tr></thead><tbody id="rows"></tbody></table></div>
    <p class="muted" id="count" style="font-size:13px"></p>`;
  $("#from").value = addDays(todayISO(), -6);
  $("#to").value = todayISO();
  let tmr;
  const params = () => new URLSearchParams({ q: $("#q").value, day_from: $("#from").value, day_to: $("#to").value, category: $("#cat").value, gate: $("#gate").value, status: $("#st").value });
  async function load() {
    const p = params();
    if ($("#csv")) $("#csv").href = `${API}/events.csv?${p}`;
    const rows = await api(`events?${p}&limit=500`);
    $("#rows").innerHTML = rows.length ? rows.map((e) => `<tr class="click" data-id="${e.id}">
        <td style="white-space:nowrap">${fmtDT(e.at)}</td><td>${gateTag(e.gate)}</td><td>${plateTag(e.plate)}</td>
        <td class="hide-sm">${e.plate_img ? `<img class="crop" src="${img(e.plate_img)}" alt="" loading="lazy">` : ""}</td>
        <td>${catTag(e.category)}</td><td>${esc([e.house, e.category === "resident" ? e.owner : e.visitor].filter(Boolean).join(" · ") || e.note || "")}</td>
        <td>${statusTag(e.status)}</td><td class="hide-sm muted">${e.camera ? "Camera" : e.source === "manual" ? "Guard" : e.source === "upload" ? "Clip" : "Camera"}</td></tr>`).join("")
      : `<tr><td colspan="8" class="empty">No gate events for these filters.</td></tr>`;
    $("#count").textContent = rows.length >= 500 ? "Showing the latest 500. Narrow the dates to see more." : `${rows.length} events`;
    $$("#rows tr[data-id]").forEach((tr) => (tr.onclick = () => showEvent(rows.find((x) => String(x.id) === tr.dataset.id), load)));
  }
  $("#q").oninput = () => { clearTimeout(tmr); tmr = setTimeout(load, 250); };
  ["#from", "#to", "#cat", "#gate", "#st"].forEach((s) => ($(s).onchange = load));
  await load();
};

/* ---------- inside now ---------- */
VIEWS.inside = async (v) => {
  const rows = await api("inside");
  v.innerHTML = `<div class="note-box">Vehicles whose last recorded movement is an entry. Visitors and unknown vehicles inside longer than the limit in Settings are marked.</div>
    <div class="table-wrap"><table><thead><tr><th>Plate</th><th>Type</th><th>House / visitor</th><th>Came in</th><th>Inside for</th></tr></thead><tbody>
    ${rows.length ? rows.map((e) => `<tr class="click" data-id="${e.id}"><td>${plateTag(e.plate)}</td><td>${catTag(e.category)}</td>
      <td>${esc([e.house, e.category === "resident" ? e.owner : e.visitor].filter(Boolean).join(" · ") || e.note || "")}</td>
      <td>${fmtDT(e.at)}</td><td>${dur(e.minutes)} ${e.overstay ? `<span class="tag c-unknown">long stay</span>` : ""}</td></tr>`).join("") : `<tr><td colspan="5" class="empty">No vehicles inside.</td></tr>`}
    </tbody></table></div>`;
  $$("tr[data-id]", v).forEach((tr) => (tr.onclick = () => showEvent(rows.find((x) => String(x.id) === tr.dataset.id))));
};

/* ---------- residents ---------- */
VIEWS.residents = async (v) => {
  const admin = ME.user.role === "admin";
  v.innerHTML = `<div class="toolbar">
      <div class="seg" id="tabs"><button data-t="houses" class="on">Houses</button><button data-t="other">Staff &amp; service vehicles</button></div>
      <input class="grow" id="q" type="search" placeholder="Search house, owner, phone or plate">
      ${admin ? `<button class="btn" id="import">⤒ Import CSV</button><button class="btn primary" id="add">+ Add house</button>` : ""}
    </div><div id="list"></div>`;
  let tab = "houses", houses = [], vehicles = [];
  async function load() {
    [houses, vehicles] = await Promise.all([api("houses"), api("vehicles")]);
    render();
  }
  function render() {
    const q = $("#q").value.trim().toUpperCase().replace(/\s|-/g, "");
    if (tab === "houses") {
      const list = houses.filter((h) => !q || (h.label + h.owner + h.phone + h.vehicles.map((x) => x.plate).join("")).toUpperCase().replace(/\s|-/g, "").includes(q));
      $("#list").innerHTML = `<div class="table-wrap"><table><thead><tr><th>House</th><th>Owner / tenant</th><th class="hide-sm">Phone</th><th>Vehicles</th><th class="hide-sm">App login</th></tr></thead><tbody>
        ${list.map((h) => `<tr class="click" data-id="${h.id}"><td><b>${esc(h.label)}</b></td><td>${esc(h.owner)} ${h.status !== "owner" ? `<span class="muted">(${h.status})</span>` : ""}</td>
          <td class="hide-sm">${esc(h.phone)}</td><td><div class="chips">${h.vehicles.map((x) => plateTag(x.plate)).join("") || `<span class="muted">none</span>`}</div></td>
          <td class="hide-sm muted">${h.account ? esc(h.account) : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="empty">No houses found.</td></tr>`}
        </tbody></table></div><p class="muted" style="font-size:13px">${list.length} houses · ${list.reduce((a, h) => a + h.vehicles.length, 0)} vehicles</p>`;
      $$("tr[data-id]", $("#list")).forEach((tr) => (tr.onclick = () => houseModal(houses.find((h) => String(h.id) === tr.dataset.id))));
    } else {
      const list = vehicles.filter((x) => x.kind !== "resident" && (!q || (x.plate + x.owner_name + x.make).toUpperCase().replace(/\s|-/g, "").includes(q)));
      $("#list").innerHTML = `${admin ? `<div class="toolbar"><button class="btn primary" id="add-other">+ Add staff or service vehicle</button></div>` : ""}
        <div class="table-wrap"><table><thead><tr><th>Plate</th><th>Type</th><th>Who</th><th>Vehicle</th>${admin ? "<th></th>" : ""}</tr></thead><tbody>
        ${list.map((x) => `<tr><td>${plateTag(x.plate)}</td><td>${catTag(x.kind)}</td><td>${esc(x.owner_name || x.make)}</td><td>${esc(x.vehicle)}</td>
          ${admin ? `<td><button class="btn small ghost" data-del="${x.id}">Remove</button></td>` : ""}</tr>`).join("") || `<tr><td colspan="5" class="empty">No staff or service vehicles.</td></tr>`}
        </tbody></table></div>`;
      $("#add-other") && ($("#add-other").onclick = () => vehicleModal({ kind: "service" }, load));
      $$("[data-del]", $("#list")).forEach((b) => (b.onclick = async () => {
        if (!confirm("Remove this vehicle? It will be treated as unknown at the gate.")) return;
        await api(`vehicles/${b.dataset.del}`, { method: "DELETE" }); load();
      }));
    }
  }
  function houseModal(h = {}) {
    modal(h.id ? `House ${h.label}` : "Add house", `
      <form id="hf" class="form-grid">
        <input type="hidden" name="id" value="${h.id || ""}">
        <label>Block<input name="block" value="${esc(h.block || "")}" required ${admin ? "" : "disabled"}></label>
        <label>House no.<input name="number" value="${esc(h.number || "")}" required ${admin ? "" : "disabled"}></label>
        <label>Owner / tenant name<input name="owner" value="${esc(h.owner || "")}" required ${admin ? "" : "disabled"}></label>
        <label>Phone<input name="phone" value="${esc(h.phone || "")}" ${admin ? "" : "disabled"}></label>
        <label>Status<select name="status" ${admin ? "" : "disabled"}>${["owner", "tenant", "vacant"].map((s) => `<option ${h.status === s ? "selected" : ""}>${s}</option>`).join("")}</select></label>
        <label>Note<input name="note" value="${esc(h.note || "")}" ${admin ? "" : "disabled"}></label>
        ${admin ? `<div class="actions full">${h.id ? `<button type="button" class="btn bad ghost" id="hdel" style="margin-right:auto">Delete house</button>` : ""}<button class="btn primary">Save</button></div>` : ""}
      </form>
      ${h.id ? `<div class="section-title" style="margin-top:18px">Vehicles</div>
        <div>${h.vehicles.map((x) => `<div class="mini">${plateTag(x.plate)}<span class="grow">${esc([x.vehicle, x.colour, x.make].filter(Boolean).join(" · "))}</span>${admin ? `<button class="btn small ghost" data-vdel="${x.id}">Remove</button>` : ""}</div>`).join("") || `<p class="muted">No vehicles.</p>`}</div>
        ${admin ? `<button class="btn" id="vadd" style="margin-top:10px">+ Add vehicle</button>` : ""}` : ""}`, (b) => {
      if (!admin) return;
      bindForm($("#hf", b), async (d) => {
        const r = await api("houses", { method: "POST", body: { ...d, id: d.id ? +d.id : null } });
        toast("Saved");
        await load();
        if (!d.id) houseModal(houses.find((x) => x.id === r.id)); else closeModal();
      });
      $("#hdel", b) && ($("#hdel", b).onclick = async () => {
        if (!confirm(`Delete house ${h.label} and its ${h.vehicles.length} vehicles? This cannot be undone.`)) return;
        try { await api(`houses/${h.id}`, { method: "DELETE" }); closeModal(); load(); } catch (err) { toast(err.message, true); }
      });
      $("#vadd", b) && ($("#vadd", b).onclick = () => vehicleModal({ kind: "resident", house_id: h.id }, async () => { await load(); houseModal(houses.find((x) => x.id === h.id)); }));
      $$("[data-vdel]", b).forEach((x) => (x.onclick = async () => {
        if (!confirm("Remove this vehicle?")) return;
        await api(`vehicles/${x.dataset.vdel}`, { method: "DELETE" }); await load(); houseModal(houses.find((y) => y.id === h.id));
      }));
    });
  }
  $$("#tabs button").forEach((b) => (b.onclick = () => { tab = b.dataset.t; $$("#tabs button").forEach((x) => x.classList.toggle("on", x === b)); render(); }));
  $("#q").oninput = render;
  if (admin) {
    $("#add").onclick = () => houseModal();
    $("#import").onclick = () => modal("Import residents from Excel / CSV", `
      <p>Save your residents sheet as CSV with these columns: <code>block, house, owner, phone, status, plates</code>. Put several plates in one cell separated by <code>;</code>. Existing houses are updated.</p>
      <p><a href="${API}/houses/template.csv">⤓ Download a sample file</a></p>
      <form id="imp"><input type="file" name="file" accept=".csv,text/csv" required><div class="actions"><button class="btn primary">Import</button></div></form><div id="imp-res"></div>`, (b) => {
      $("#imp", b).onsubmit = async (e) => {
        e.preventDefault();
        const fd = new FormData(e.target);
        try {
          const r = await api("houses/import", { method: "POST", body: fd });
          $("#imp-res", b).innerHTML = `<div class="note-box">${r.rows} rows read · ${r.houses_added} new houses · ${r.vehicles_added} new vehicles${r.errors.length ? "<br>" + r.errors.map(esc).join("<br>") : ""}</div>`;
          load();
        } catch (err) { toast(err.message, true); }
      };
    });
  }
  await load();
};

function vehicleModal(x, after) {
  const resident = x.kind === "resident";
  modal(resident ? "Add vehicle" : "Add staff or service vehicle", `<form id="vf" class="form-grid">
    <label>Plate number<input name="plate" placeholder="LEB-1234" required></label>
    <label>Type<select name="vehicle">${["Car", "Bike", "Van", "Truck", "Bus"].map((t) => `<option>${t}</option>`).join("")}</select></label>
    ${resident ? `<label>Make / model<input name="make" placeholder="Toyota Corolla"></label><label>Colour<input name="colour" placeholder="White"></label>`
      : `<label>Category<select name="kind"><option value="service">Service (tanker, milk, garbage…)</option><option value="staff">Society staff</option></select></label>
         <label>Who / company<input name="owner_name" placeholder="e.g. Water tanker, Al-Madina" required></label>`}
    <div class="actions full"><button class="btn primary">Save vehicle</button></div></form>`, (b) => {
    bindForm($("#vf", b), async (d) => {
      const r = await api("vehicles", { method: "POST", body: { ...d, kind: d.kind || x.kind, house_id: x.house_id } });
      toast(`${r.plate} registered`);
      closeModal();
      after && after();
    });
  });
}

/* ---------- visitor passes ---------- */
function passModal(after, fixedHouse) {
  const t = todayISO();
  modal("New visitor pass", `<form id="pf" class="form-grid">
    ${fixedHouse ? "" : `<label>House<input name="house" placeholder="e.g. A-12" required></label>`}
    <label>Visitor name<input name="visitor" required></label>
    <label>Visitor phone<input name="phone" placeholder="optional"></label>
    <label>Vehicle plate<input name="plate" placeholder="optional: gate opens automatically"></label>
    <label>Purpose<input name="purpose" placeholder="Family visit, plumber…"></label>
    <label>From<input type="date" name="valid_from" value="${t}" required></label>
    <label>To<input type="date" name="valid_to" value="${t}" required></label>
    <label>Entries allowed<input type="number" name="max_entries" value="1" min="1" max="100"></label>
    <div class="actions full"><button class="btn primary">Create pass</button></div></form>`, (b) => {
    bindForm($("#pf", b), async (d) => {
      const r = await api("passes", { method: "POST", body: d });
      after && after();
      modal("Pass created", `<div class="ticket"><div class="muted">Pass code</div><div class="code">${esc(r.code)}</div><pre>${esc(r.message)}</pre></div>
        <div class="actions"><button class="btn" id="copy">Copy text</button><a class="btn ok" target="_blank" rel="noopener" href="https://wa.me/?text=${encodeURIComponent(r.message)}">Share on WhatsApp</a></div>`, (bb) => {
        $("#copy", bb).onclick = () => navigator.clipboard.writeText(r.message).then(() => toast("Copied"));
      });
    });
  });
}
VIEWS.visitors = async (v) => {
  const resident = ME.user.role === "resident";
  v.innerHTML = `<div class="toolbar"><div class="seg" id="scope"><button data-s="active" class="on">Active &amp; upcoming</button><button data-s="all">All</button></div>
    <input class="grow" id="q" type="search" placeholder="Search visitor, house, plate or code"><button class="btn primary" id="new">+ New visitor pass</button></div>
    <div class="note-box">${resident ? "Create a pass before your guest arrives. If you add their car's plate, the gate recognises it automatically. Otherwise they show the code to the guard." : "Passes created by residents or the office. A visitor's car with a pass is allowed automatically; without a plate, the guard types the pass code on the Gate screen."}</div>
    <div class="table-wrap"><table><thead><tr><th>Code</th><th>Visitor</th>${resident ? "" : "<th>House</th>"}<th>Vehicle</th><th>Valid</th><th>Used</th><th>State</th><th></th></tr></thead><tbody id="rows"></tbody></table></div>`;
  let scope = "active", rows = [];
  async function load() { rows = await api(`passes?scope=${scope}`); render(); }
  function render() {
    const q = $("#q").value.trim().toUpperCase();
    const list = rows.filter((p) => !q || [p.code, p.visitor, p.house, p.plate, p.purpose].join(" ").toUpperCase().includes(q));
    const stTag = { valid: "c-resident", upcoming: "c-visitor", used: "c-unreadable", expired: "c-unreadable", cancelled: "c-blacklist" };
    $("#rows").innerHTML = list.map((p) => `<tr><td><code>${esc(p.code)}</code></td><td><b>${esc(p.visitor)}</b><div class="muted" style="font-size:12.5px">${esc(p.purpose)}${p.phone ? " · " + esc(p.phone) : ""}</div></td>
      ${resident ? "" : `<td>${esc(p.house)}</td>`}<td>${p.plate ? plateTag(p.plate) : `<span class="muted">code only</span>`}</td>
      <td style="white-space:nowrap">${fmtDay(p.valid_from)}${p.valid_to !== p.valid_from ? " – " + fmtDay(p.valid_to) : ""}</td><td>${p.uses}/${p.max_entries}</td>
      <td><span class="tag ${stTag[p.state]}">${p.state}</span></td>
      <td>${["valid", "upcoming"].includes(p.state) ? `<button class="btn small ghost" data-c="${p.id}">Cancel</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="8" class="empty">No passes.</td></tr>`;
    $$("[data-c]", $("#rows")).forEach((b) => (b.onclick = async () => { await api(`passes/${b.dataset.c}/cancel`, { method: "POST" }); toast("Pass cancelled"); load(); }));
  }
  $$("#scope button").forEach((b) => (b.onclick = () => { scope = b.dataset.s; $$("#scope button").forEach((x) => x.classList.toggle("on", x === b)); load(); }));
  $("#q").oninput = render;
  $("#new").onclick = () => passModal(load, resident);
  await load();
};

/* ---------- blacklist ---------- */
VIEWS.blacklist = async (v) => {
  const admin = ME.user.role === "admin";
  v.innerHTML = `${admin ? `<form id="bf" class="card toolbar" style="margin-bottom:14px"><input name="plate" placeholder="Plate, e.g. LEB-1234" required>
      <input name="reason" class="grow" placeholder="Reason (stolen, banned by management…)"><button class="btn bad">Add to blacklist</button></form>` : ""}
    <div class="note-box">When a blacklisted vehicle is read at any gate, the guard screen shows a red alert with a sound, and it is logged. Plates match even with one wrong character.</div>
    <div class="table-wrap"><table><thead><tr><th>Plate</th><th>Reason</th><th>Added</th><th>Seen at gate</th>${admin ? "<th></th>" : ""}</tr></thead><tbody id="rows"></tbody></table></div>`;
  async function load() {
    const rows = await api("blacklist");
    $("#rows").innerHTML = rows.map((b) => `<tr><td>${plateTag(b.plate)}</td><td>${esc(b.reason)}</td><td>${fmtDay(b.created_at.slice(0, 10))}</td>
      <td>${b.times ? `${b.times}× · last ${fmtDT(b.last_seen)}` : `<span class="muted">never</span>`}</td>
      ${admin ? `<td><button class="btn small ghost" data-p="${esc(b.plate)}">Remove</button></td>` : ""}</tr>`).join("") || `<tr><td colspan="5" class="empty">No blacklisted vehicles.</td></tr>`;
    $$("[data-p]", $("#rows")).forEach((x) => (x.onclick = async () => {
      if (!confirm(`Remove ${x.dataset.p} from the blacklist?`)) return;
      await api(`blacklist/${encodeURIComponent(x.dataset.p)}`, { method: "DELETE" }); load();
    }));
  }
  if (admin) bindForm($("#bf"), async (d, f) => { const r = await api("blacklist", { method: "POST", body: d }); toast(`${r.plate} blacklisted`); f.reset(); load(); });
  await load();
};

/* ---------- cameras ---------- */
VIEWS.cameras = async (v) => {
  const cams = await api("cameras");
  v.innerHTML = `${ME.demo ? `<div class="note-box">In this online demo the camera is simulated: a Lahore traffic clip plays in real time and is analysed live. At your society we connect your own CCTV / IP cameras (Hikvision, Dahua and others, over RTSP), one per gate lane.</div>` : ""}
    <div class="grid" id="list"></div>
    <div class="card" style="margin-top:16px"><h3>Add a camera</h3>
      <form id="cf" class="form-grid">
        <label>Name<input name="name" placeholder="Main gate · Entry" required></label>
        <label>Gate<select name="gate"><option value="entry">Entry lane</option><option value="exit">Exit lane</option><option value="both">Both (towards camera = entry)</option></select></label>
        <label class="full">Camera address (RTSP)<input name="source" placeholder="rtsp://user:password@192.168.1.64:554/Streaming/Channels/101" required></label>
        <div class="actions full"><button class="btn primary" ${ME.demo ? "disabled" : ""}>Add camera</button></div>
      </form>
      <p class="muted" style="font-size:13px;margin:8px 0 0">Tip: mount the camera at 3–6 m in front of the lane at plate height (not high above the road), 1080p or better, with IR for night.</p></div>
    <div class="card" style="margin-top:16px"><h3>Barrier openings</h3>
      <p class="muted" style="font-size:13.5px;margin:0 0 10px">The barrier opens by itself for vehicles the system allows, and when a guard allows a vehicle. A guard can also open it by hand; every opening is recorded here. At your society a small network relay is wired to the barrier's "open" input.</p>
      <div class="table-wrap"><table><thead><tr><th>Time</th><th>Lane</th><th>Why</th><th>Vehicle</th><th>By</th><th></th></tr></thead><tbody id="blog"></tbody></table></div></div>`;
  api("barrier/log?limit=25").then((rows) => {
    const why = { auto: "Automatic", guard: "Guard allowed", manual: "Opened by hand" };
    $("#blog").innerHTML = rows.length ? rows.map((r) => `<tr><td>${fmtDT(r.at)}</td><td>${esc(r.camera || "")}</td><td>${why[r.reason] || r.reason}${r.note ? ` · ${esc(r.note)}` : ""}</td>
      <td>${r.plate ? plateTag(r.plate) : "–"}</td><td>${esc(r.user_name || (r.reason === "auto" ? "System" : ""))}</td><td>${r.ok ? "" : `<span class="tag c-blacklist">failed</span> <span class="muted">${esc(r.error)}</span>`}</td></tr>`).join("")
      : `<tr><td colspan="6" class="empty">No openings yet.</td></tr>`;
  }).catch(() => {});
  $("#list").innerHTML = cams.map((c) => `<div class="card cam-card">
      <div class="thumb">${c.state === "online" ? `<img src="/api/soc/cameras/${c.id}/live" alt="">` : ""}</div>
      <div><h3 style="margin-bottom:4px">${esc(c.name)}</h3>
        <div class="muted" style="font-size:13.5px">${c.gate === "both" ? "Entry and exit" : c.gate === "exit" ? "Exit lane" : "Entry lane"} · ${esc(c.source)}</div>
        <div style="margin-top:6px"><span class="state ${c.state}">● ${c.state}${c.fps ? ` · ${c.fps} fps` : ""}</span> ${c.error ? `<span class="muted">${esc(c.error)}</span>` : ""}</div>
        ${c.demo ? `<p class="muted" style="font-size:13px;margin:6px 0 0">Simulated cameras pause when nobody is watching. Open the Gate screen to start it.</p>` : ""}
        <div class="barrier-cfg"><b>Barrier</b>
          <select data-bk="${c.id}"><option value="none" ${c.barrier_kind === "none" ? "selected" : ""}>No barrier</option><option value="sim" ${c.barrier_kind === "sim" ? "selected" : ""}>Simulated (demo)</option><option value="relay" ${c.barrier_kind === "relay" ? "selected" : ""}>Relay${c.barrier ? ` · ${esc(c.barrier)}` : ""}</option></select>
          ${c.barrier_kind !== "none" ? `<span class="state ${c.barrier_open ? "online" : "paused"}">● ${c.barrier_open ? "open" : "closed"}</span>` : ""}</div>
        <div class="actions" style="justify-content:flex-start">
          <select data-g="${c.id}">${["entry", "exit", "both"].map((g) => `<option value="${g}" ${c.gate === g ? "selected" : ""}>${g}</option>`).join("")}</select>
          ${c.demo ? "" : `<button class="btn small" data-t="${c.id}">${c.enabled ? "Turn off" : "Turn on"}</button><button class="btn small ghost" data-d="${c.id}">Remove</button>`}
        </div></div></div>`).join("") || `<p class="muted">No cameras yet.</p>`;
  $$("[data-g]", v).forEach((s) => (s.onchange = async () => { const c = cams.find((x) => x.id === +s.dataset.g); await api("cameras", { method: "POST", body: { id: c.id, name: c.name, gate: s.value, enabled: c.enabled } }); toast("Saved"); route(true); }));
  $$("[data-bk]", v).forEach((s) => (s.onchange = async () => {
    const c = cams.find((x) => x.id === +s.dataset.bk);
    let barrier = s.value === "none" ? "" : s.value;
    if (s.value === "relay") {
      barrier = prompt("Relay address that opens the barrier (called once per opening), for example\nhttp://192.168.1.50/relay/0?turn=on&timer=2", "http://");
      if (!barrier) return route(true);
    }
    try { await api("cameras", { method: "POST", body: { id: c.id, name: c.name, gate: c.gate, enabled: c.enabled, barrier } }); toast("Saved"); } catch (e) { toast(e.message, true); }
    route(true);
  }));
  $$("[data-t]", v).forEach((b) => (b.onclick = async () => { const c = cams.find((x) => x.id === +b.dataset.t); await api("cameras", { method: "POST", body: { id: c.id, name: c.name, gate: c.gate, enabled: !c.enabled } }); route(true); }));
  $$("[data-d]", v).forEach((b) => (b.onclick = async () => { if (!confirm("Remove this camera? Its past gate events are kept.")) return; await api(`cameras/${b.dataset.d}`, { method: "DELETE" }); route(true); }));
  bindForm($("#cf"), async (d) => { await api("cameras", { method: "POST", body: d }); toast("Camera added"); route(true); });
  onLeave(() => $$(".thumb img", v).forEach((i) => i.removeAttribute("src")));
};

/* ---------- users ---------- */
VIEWS.users = async (v) => {
  const rows = await api("users");
  const roleName = { admin: "Management", guard: "Guard", resident: "Resident" };
  v.innerHTML = `<div class="toolbar"><span class="muted">Management sees everything, guards use the gate screen, residents see only their own house.</span><button class="btn primary spacer" id="add">+ Add account</button></div>
    <div class="table-wrap"><table><thead><tr><th>Username</th><th>Name</th><th>Role</th><th>House</th><th></th></tr></thead><tbody>
    ${rows.map((u) => `<tr><td><code>${esc(u.username)}</code></td><td>${esc(u.name)}</td><td>${roleName[u.role]}</td><td>${esc(u.house || "")}</td>
      <td style="white-space:nowrap">${u.locked ? `<span class="muted">demo account</span>` : `<button class="btn small" data-pw="${u.id}">Reset password</button> <button class="btn small ghost" data-del="${u.id}">Delete</button>`}</td></tr>`).join("")}
    </tbody></table></div>`;
  $("#add").onclick = () => modal("Add account", `<form id="uf" class="form-grid">
      <label>Role<select name="role"><option value="guard">Guard</option><option value="resident">Resident</option><option value="admin">Management</option></select></label>
      <label>House (residents)<input name="house" placeholder="e.g. B-7"></label>
      <label>Full name<input name="name" required></label>
      <label>Username<input name="username" required autocomplete="off"></label>
      <label class="full">Password (min 8)<input name="password" type="text" minlength="8" required autocomplete="off"></label>
      <div class="actions full"><button class="btn primary">Create</button></div></form>`, (b) => bindForm($("#uf", b), async (d) => { await api("users", { method: "POST", body: d }); toast("Account created"); closeModal(); route(true); }));
  $$("[data-pw]", v).forEach((b) => (b.onclick = () => modal("Reset password", `<form id="rf"><label>New password (min 8)<input name="password" type="text" minlength="8" required></label><div class="actions"><button class="btn primary">Save</button></div></form>`,
    (m) => bindForm($("#rf", m), async (d) => { await api(`users/${b.dataset.pw}/password`, { method: "POST", body: d }); toast("Password changed"); closeModal(); }))));
  $$("[data-del]", v).forEach((b) => (b.onclick = async () => { if (!confirm("Delete this account?")) return; try { await api(`users/${b.dataset.del}`, { method: "DELETE" }); route(true); } catch (e) { toast(e.message, true); } }));
};

/* ---------- settings ---------- */
VIEWS.settings = async (v) => {
  const admin = ME.user.role === "admin";
  const s = admin ? await api("settings") : {};
  v.innerHTML = `<div class="grid g2">
    ${admin ? `<div class="card"><h3>Society settings</h3><form id="sf" class="form-grid">
      <label class="full">Society name<input name="society_name" value="${esc(s.society_name)}" required></label>
      <label>Visitor long-stay alert after (hours)<input type="number" name="visitor_hours" value="${esc(s.visitor_hours)}" min="1" max="72"></label>
      <label>Unanswered vehicles expire after (minutes)<input type="number" name="pending_minutes" value="${esc(s.pending_minutes)}" min="1" max="600"></label>
      <label>Ignore repeat reads of the same plate for (minutes)<input type="number" name="dedupe_minutes" value="${esc(s.dedupe_minutes)}" min="0" max="120"></label>
      <label>Log vehicles whose plate cannot be read<select name="log_unreadable"><option value="1" ${s.log_unreadable === "1" ? "selected" : ""}>Yes, guard checks them</option><option value="0" ${s.log_unreadable === "0" ? "selected" : ""}>No</option></select></label>
      <label>Open the barrier automatically<select name="barrier_auto"><option value="1" ${s.barrier_auto === "1" ? "selected" : ""}>Yes, for vehicles the system allows</option><option value="0" ${s.barrier_auto === "0" ? "selected" : ""}>No, the guard opens it</option></select></label>
      <label>…also for visitors with a pass<select name="barrier_visitors"><option value="1" ${s.barrier_visitors === "1" ? "selected" : ""}>Yes</option><option value="0" ${s.barrier_visitors === "0" ? "selected" : ""}>No, guard checks visitors</option></select></label>
      <label class="full">Guards can ask residents to approve visitors on their phone<select name="resident_approval"><option value="1" ${s.resident_approval === "1" ? "selected" : ""}>Yes (the guard still makes the final decision)</option><option value="0" ${s.resident_approval === "0" ? "selected" : ""}>No, guards call the house</option></select></label>
      <div class="actions full"><button class="btn primary">Save settings</button></div></form></div>` : ""}
    <div class="card"><h3>Change your password</h3><form id="pw" class="form-grid">
      <label class="full">Current password<input type="password" name="old" required autocomplete="current-password"></label>
      <label class="full">New password (min 8)<input type="password" name="new" minlength="8" required autocomplete="new-password"></label>
      <div class="actions full"><button class="btn primary">Change password</button></div></form></div></div>`;
  if (admin) bindForm($("#sf"), async (d) => { await api("settings", { method: "POST", body: d }); toast("Settings saved"); await loadMe(); });
  bindForm($("#pw"), async (d, f) => { await api("me/password", { method: "POST", body: d }); toast("Password changed"); f.reset(); });
};

/* ---------- resident home ---------- */
VIEWS.home = async (v) => {
  const [houses, passes, events, inside] = await Promise.all([api("my-house").catch(() => null), api("passes?scope=active"), api("events?limit=30"), api("inside")]);
  const h = houses;
  v.innerHTML = `<div id="req-box"></div><div id="push-box"></div><div class="grid g2">
    <div class="card"><h3>House ${esc(ME.house_label)} <span class="right muted">${esc(h?.owner || "")}</span></h3>
      <div class="section-title">Your vehicles</div><div id="veh"></div>
      <button class="btn" id="vadd" style="margin-top:10px">+ Add vehicle</button>
      <div class="section-title" style="margin-top:18px">Inside the society now</div>
      <div>${inside.length ? inside.map((e) => `<div class="mini">${plateTag(e.plate)}<span class="grow">${esc(e.visitor || CAT[e.category])}</span><span class="muted">since ${fmtTime(e.at)}</span></div>`).join("") : `<p class="muted">None of your vehicles or visitors are inside.</p>`}</div>
    </div>
    <div class="card"><h3>Visitor passes <button class="btn primary small right" id="pnew" style="margin-left:auto">+ New pass</button></h3>
      <div>${passes.length ? passes.slice(0, 8).map((p) => `<div class="mini"><code>${esc(p.code)}</code><span class="grow"><b>${esc(p.visitor)}</b> · ${fmtDay(p.valid_from)}${p.plate ? " · " + esc(p.plate) : ""}</span><span class="tag ${p.state === "valid" ? "c-resident" : p.state === "upcoming" ? "c-visitor" : "c-unreadable"}">${p.state}</span></div>`).join("") : `<p class="muted">No active passes. Create one before a guest arrives.</p>`}</div>
      <a href="#/visitors" style="font-size:13.5px">All passes →</a>
    </div></div>
    <div class="section-title" style="margin-top:20px">Recent gate activity for your house</div>
    <div class="feed" id="feed"></div>`;
  const veh = h?.vehicles || [];
  $("#veh").innerHTML = veh.length ? veh.map((x) => `<div class="mini">${plateTag(x.plate)}<span class="grow">${esc([x.vehicle, x.colour, x.make].filter(Boolean).join(" · "))}</span><button class="btn small ghost" data-d="${x.id}">Remove</button></div>`).join("") : `<p class="muted">No vehicles registered.</p>`;
  $$("[data-d]", $("#veh")).forEach((b) => (b.onclick = async () => { if (!confirm("Remove this vehicle? The gate will treat it as unknown.")) return; await api(`vehicles/${b.dataset.d}`, { method: "DELETE" }); route(true); }));
  $("#vadd").onclick = () => vehicleModal({ kind: "resident" }, () => route(true));
  $("#pnew").onclick = () => passModal(() => {}, true);
  $("#feed").innerHTML = events.length ? events.slice(0, 15).map((e) => evCard(e)).join("") : `<p class="muted">No activity yet.</p>`;
  bindEvCards($("#feed"), events);
  renderRequests(lastRequests);
  pushCard();
};

/* ---------- resident: visitor approval requests ---------- */
let lastRequests = [], currentView = "";
const seenReq = new Set(), poppedReq = new Set();
function reqCard(r) {
  const pics = [r.vehicle_img, r.plate_img].filter(Boolean).map((p) => `<img src="${img(p)}" alt="">`).join("");
  const done = r.status !== "waiting";
  return `<div class="req-card ${done ? "done s-" + r.status : ""}" data-r="${r.id}">
    ${pics ? `<div class="req-pics">${pics}</div>` : ""}
    <div class="req-body">
      <b>${esc(r.visitor)} is at the ${r.gate === "exit" ? "exit" : "gate"}</b>
      <div class="row1">${plateTag(r.plate)} <span class="muted">${r.vehicle ? esc(r.vehicle) + " · " : ""}asked ${fmtTime(r.asked_at)}${r.asked_by_name ? ` by ${esc(r.asked_by_name)}` : ""}</span></div>
      ${done ? `<div class="req-ans">${r.status === "approved" ? "✓ You let them in" : r.status === "rejected" ? "✗ You refused" : "Expired: the guard decided"}</div>`
        : `<div class="ur" style="margin:4px 0">کیا یہ آپ کے مہمان ہیں؟</div><div class="btns"><button class="btn ok" data-ans="approved">✓ Let in <span class="ur">اجازت</span></button><button class="btn bad" data-ans="rejected">✗ Refuse <span class="ur">انکار</span></button></div>`}
    </div></div>`;
}
function bindReq(root, after) {
  $$("[data-ans]", root).forEach((b) => (b.onclick = async () => {
    const id = b.closest("[data-r]").dataset.r;
    try {
      await api(`requests/${id}/answer`, { method: "POST", body: { answer: b.dataset.ans } });
      toast(b.dataset.ans === "approved" ? "The guard has been told to let them in" : "The guard has been told to refuse");
    } catch (e) { toast(e.message, true); }
    after && after();
  }));
}
function renderRequests(list) {
  const box = $("#req-box");
  if (!box) return;
  const show = list.filter((r) => r.status === "waiting" || Date.now() - new Date(r.answered_at || r.asked_at) < 30 * 60000)
    .sort((a, b) => (b.status === "waiting") - (a.status === "waiting")).slice(0, 4);
  box.innerHTML = show.length ? `<div class="section-title">Visitors at the gate</div>${show.map(reqCard).join("")}` : "";
  bindReq(box, pollRequests);
}
async function pollRequests() {
  if (!ME || ME.user.role !== "resident") return;
  try { lastRequests = await api("requests"); } catch { return; }
  renderRequests(lastRequests);
  const shown = $("#modal-body [data-r]");  // close a pop-up that was answered elsewhere or expired
  if (shown && lastRequests.find((r) => String(r.id) === shown.dataset.r)?.status !== "waiting") closeModal();
  const waiting = lastRequests.filter((r) => r.status === "waiting");
  if (waiting.some((r) => !seenReq.has(r.id))) beep(2);
  waiting.forEach((r) => seenReq.add(r.id));
  const pop = waiting.find((r) => !poppedReq.has(r.id));
  if (pop && currentView && currentView !== "home" && $("#modal").hidden) {  // not on the home page: pop it up once
    poppedReq.add(pop.id);
    modal("Visitor at the gate", reqCard(pop), (b) => bindReq(b, () => { closeModal(); pollRequests(); }));
  }
}
setInterval(() => document.visibilityState === "visible" && pollRequests(), 4000);

/* ---------- phone notifications ---------- */
const b64key = (s) => { const p = "=".repeat((4 - (s.length % 4)) % 4); const r = atob((s + p).replace(/-/g, "+").replace(/_/g, "/")); return Uint8Array.from(r, (c) => c.charCodeAt(0)); };
async function pushCard() {
  const box = $("#push-box");
  if (!box) return;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  const supported = "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
  let on = false;
  if (supported && Notification.permission === "granted") {
    const reg = await navigator.serviceWorker.getRegistration("/society/");
    on = !!(reg && (await reg.pushManager.getSubscription()));
  }
  if (on) { box.innerHTML = `<div class="push-card on">🔔 <span class="grow">Phone notifications are on. You will be asked here when a visitor for your house is at the gate.</span><button class="btn small" id="push-test">Test</button></div>`; }
  else if (!supported && ios && !standalone) { box.innerHTML = `<div class="push-card">🔔 <span class="grow"><b>Get gate notifications on iPhone:</b> tap Share, then “Add to Home Screen”, open TwinStack Gate from the home screen and turn notifications on.</span></div>`; }
  else if (!supported) { box.innerHTML = `<div class="push-card">🔔 <span class="grow">This browser cannot show notifications. Keep this page open, or use Chrome on your phone.</span></div>`; }
  else if (Notification.permission === "denied") { box.innerHTML = `<div class="push-card">🔕 <span class="grow">Notifications are blocked for this site. Allow them in your browser's site settings.</span></div>`; }
  else { box.innerHTML = `<div class="push-card">🔔 <span class="grow"><b>Turn on notifications</b> so the gate can ask you when a visitor arrives, even when this page is closed. <span class="ur">مہمان آنے پر فون پر اطلاع</span></span><button class="btn primary small" id="push-on">Turn on</button></div>`; }
  $("#push-on") && ($("#push-on").onclick = async () => {
    try {
      const reg = await navigator.serviceWorker.register("/society/sw.js", { scope: "/society/" });
      await navigator.serviceWorker.ready;
      if ((await Notification.requestPermission()) !== "granted") return pushCard();
      const { key } = await api("push/key");
      const sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64key(key) });
      await api("push/subscribe", { method: "POST", body: sub.toJSON() });
      toast("Notifications are on");
    } catch (e) { toast("Could not turn on notifications: " + e.message, true); }
    pushCard();
  });
  $("#push-test") && ($("#push-test").onclick = async () => { const r = await api("push/test", { method: "POST" }); toast(r.sent ? "Test notification sent" : "No phone registered yet"); });
}

/* ================= ROUTER ================= */
const MENU = {
  admin: [["dashboard", "▦", "Dashboard"], ["gate", "◉", "Gate screen"], ["log", "☰", "Gate log"], ["inside", "⌂", "Inside now"], ["residents", "⌂", "Residents & vehicles"],
    ["visitors", "✉", "Visitor passes"], ["blacklist", "⛔", "Blacklist"], ["cameras", "◎", "Cameras"], ["users", "👤", "Accounts"], ["settings", "⚙", "Settings"]],
  guard: [["gate", "◉", "Gate screen"], ["log", "☰", "Gate log"], ["inside", "⌂", "Inside now"], ["visitors", "✉", "Visitor passes"], ["residents", "⌂", "Residents"], ["blacklist", "⛔", "Blacklist"], ["settings", "⚙", "My account"]],
  resident: [["home", "⌂", "My house"], ["visitors", "✉", "Visitor passes"], ["log", "☰", "Gate history"], ["settings", "⚙", "My account"]],
};
const TITLES = { dashboard: "Dashboard", gate: "Gate screen", log: "Gate log", inside: "Inside now", residents: "Residents & vehicles", visitors: "Visitor passes", blacklist: "Blacklist", cameras: "Cameras", users: "Accounts", settings: "Settings", home: "My house" };

async function route(keep = false) {
  if (!ME) return;
  const menu = MENU[ME.user.role];
  let name = location.hash.replace(/^#\/?/, "");
  if (!menu.some((m) => m[0] === name)) name = menu[0][0];
  $$("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.v === name));
  $("#page-title").textContent = ME.user.role === "resident" && name === "log" ? "Gate history" : TITLES[name];
  cleanup.forEach((f) => { try { f(); } catch {} });
  cleanup = [];
  hideTip();
  const req = $("#modal-body [data-r]");  // a visitor pop-up closed by navigation pops up again on the next page
  if (req) poppedReq.delete(+req.dataset.r);
  closeModal();
  currentView = name;
  const v = $("#view");
  if (!keep) v.innerHTML = `<p class="muted">Loading…</p>`;
  try { await VIEWS[name](v); } catch (e) { if (e.message !== "Please log in") v.innerHTML = `<p class="err">${esc(e.message)}</p>`; }
  $("#side").classList.remove("open");
  $("#scrim").hidden = true;
}
addEventListener("hashchange", () => route());

async function loadMe() {
  ME = await api("me");
  $("#soc-name").textContent = ME.society;
  document.title = `${ME.society} · TwinStack Gate`;
  $("#who-name").textContent = ME.user.name;
  $("#who-role").textContent = { admin: "Management", guard: "Gate guard", resident: `Resident · ${ME.house_label}` }[ME.user.role];
  $("#demo-pill").hidden = !ME.demo;
  $("#nav").innerHTML = MENU[ME.user.role].map(([k, i, l]) => `<a href="#/${k}" data-v="${k}"><i>${i}</i>${l}</a>`).join("");
}

function showLogin() {
  ME = null;
  $("#app").hidden = true;
  $("#login").hidden = false;
}
async function start() {
  api("public").then((p) => ($("#demo-box").hidden = !p.demo)).catch(() => {});
  try {
    await loadMe();
    $("#login").hidden = true;
    $("#app").hidden = false;
    alertSince = 0;
    route();
    if (ME.user.role === "resident") pollRequests();
  } catch { showLogin(); }
}

bindForm($("#login-form"), async (d) => {
  $("#login-err").hidden = true;
  try {
    await api("login", { method: "POST", body: d });
    location.hash = "";
    await start();
  } catch (e) { $("#login-err").textContent = e.message; $("#login-err").hidden = false; }
});
$$(".demo-accounts button").forEach((b) => (b.onclick = () => {
  const f = $("#login-form");
  f.username.value = b.dataset.u;
  f.password.value = "demo1234";
  f.requestSubmit();
}));
$("#logout").onclick = async () => { await api("logout", { method: "POST" }).catch(() => {}); location.hash = ""; showLogin(); };
$("#menu").onclick = () => { $("#side").classList.add("open"); $("#scrim").hidden = false; };
$("#scrim").onclick = () => { $("#side").classList.remove("open"); $("#scrim").hidden = true; };
setInterval(() => ($("#clock").textContent = new Date().toLocaleString("en-GB", { weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: TZ })), 1000);
start();

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/society/sw.js", { scope: "/society/" }).catch(() => {});
