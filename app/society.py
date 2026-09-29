"""TwinStack Gate: housing-society gate management on top of the ANPR pipeline.

Residents' vehicles, visitor passes, blacklist, live cameras, guard decisions, reports and a resident portal.
Every vehicle read at a gate (live camera, uploaded clip or typed in by the guard) becomes a gate event that is
classified as resident / staff / service / visitor / unknown / blacklist / unreadable.
"""
import csv
import io
import os
import secrets
import shutil
import threading
import time
import urllib.request
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import psycopg
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from psycopg.rows import dict_row

from . import society_push
from .gate_rules import check_pw, clean_plate, hash_pw, same_plate  # noqa: F401  (hash_pw used by society_demo)

ROOT = Path(__file__).resolve().parent.parent
SOC_DIR = ROOT / "runtime" / "society"
SOC_DIR.mkdir(parents=True, exist_ok=True)
DSN = os.environ.get("ANPR_DSN", "postgresql:///anpr?host=/var/run/postgresql")
DEMO = os.environ.get("SOC_DEMO", "1") == "1"  # public demo: simulated cameras only, demo accounts locked
PKT = timezone(timedelta(hours=5))
TZ = "Asia/Karachi"
COOKIE = "gate_session"
CATEGORIES = ("resident", "staff", "service", "visitor", "unknown", "blacklist", "unreadable")
DEFAULT_SETTINGS = {"society_name": "Green Valley Residencia", "visitor_hours": "4", "pending_minutes": "20",
                    "log_unreadable": "1", "dedupe_minutes": "5", "barrier_auto": "1", "barrier_visitors": "1",
                    "resident_approval": "1"}

router = APIRouter(prefix="/api/soc")
engine = None  # set by server.py (the shared ANPR instance)


def db():
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


SCHEMA = """
create table if not exists soc_settings (key text primary key, value text not null);
create table if not exists soc_houses (
  id serial primary key,
  block text not null,
  number text not null,
  owner text not null,
  phone text not null default '',
  status text not null default 'owner',   -- owner | tenant | vacant
  note text not null default '',
  created_at timestamptz not null default now(),
  unique (block, number)
);
create table if not exists soc_users (
  id serial primary key,
  username text not null unique,
  name text not null,
  password text not null,
  role text not null,                      -- admin | guard | resident
  house_id int references soc_houses(id) on delete cascade,
  locked boolean not null default false,   -- demo accounts cannot be changed
  created_at timestamptz not null default now()
);
create table if not exists soc_sessions (
  token text primary key,
  user_id int not null references soc_users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create table if not exists soc_vehicles (
  id serial primary key,
  plate text not null unique,
  kind text not null default 'resident',   -- resident | staff | service
  house_id int references soc_houses(id) on delete cascade,
  vehicle text not null default 'Car',
  make text not null default '',
  colour text not null default '',
  owner_name text not null default '',
  created_at timestamptz not null default now()
);
create table if not exists soc_passes (
  id serial primary key,
  code text not null unique,
  house_id int not null references soc_houses(id) on delete cascade,
  visitor text not null,
  phone text not null default '',
  plate text,
  purpose text not null default '',
  valid_from date not null,
  valid_to date not null,
  max_entries int not null default 1,
  uses int not null default 0,
  cancelled boolean not null default false,
  created_by int references soc_users(id) on delete set null,
  created_at timestamptz not null default now()
);
create table if not exists soc_blacklist (
  plate text primary key,
  reason text not null default '',
  created_at timestamptz not null default now()
);
create table if not exists soc_cameras (
  id serial primary key,
  name text not null,
  gate text not null default 'entry',      -- entry | exit | both (towards camera = entry)
  source text not null,
  enabled boolean not null default true,
  demo boolean not null default false,
  created_at timestamptz not null default now()
);
create table if not exists soc_events (
  id bigserial primary key,
  at timestamptz not null,
  gate text not null,                      -- entry | exit
  source text not null,                    -- camera | upload | manual
  camera_id int references soc_cameras(id) on delete set null,
  plate text,
  confidence real,
  vehicle text,
  category text not null,
  status text not null,                    -- allowed | denied | pending | no_action
  house_id int references soc_houses(id) on delete set null,
  pass_id int references soc_passes(id) on delete set null,
  visitor text,
  note text not null default '',
  decided_by int references soc_users(id) on delete set null,
  decided_at timestamptz,
  plate_img text,
  vehicle_img text,
  demo boolean not null default false
);
create index if not exists soc_events_at on soc_events(at desc);
alter table soc_cameras add column if not exists barrier text not null default '';  -- '' none | 'sim' | relay URL
create table if not exists soc_barrier_log (
  id bigserial primary key,
  at timestamptz not null default now(),
  camera_id int references soc_cameras(id) on delete set null,
  event_id bigint references soc_events(id) on delete set null,
  reason text not null,                    -- auto | guard | manual
  user_id int references soc_users(id) on delete set null,
  note text not null default '',
  ok boolean not null,
  error text not null default ''
);
create table if not exists soc_requests (
  id bigserial primary key,
  event_id bigint not null references soc_events(id) on delete cascade,
  house_id int not null references soc_houses(id) on delete cascade,
  visitor text not null default '',
  asked_by int references soc_users(id) on delete set null,
  asked_at timestamptz not null default now(),
  status text not null default 'waiting',  -- waiting | approved | rejected | expired
  answered_by int references soc_users(id) on delete set null,
  answered_at timestamptz
);
create index if not exists soc_requests_event on soc_requests(event_id);
create table if not exists soc_push (
  id serial primary key,
  user_id int not null references soc_users(id) on delete cascade,
  endpoint text not null unique,
  keys jsonb not null,
  created_at timestamptz not null default now()
);
create index if not exists soc_events_plate on soc_events(plate);
"""


# ---------------- small helpers ----------------
def settings(c=None) -> dict:
    own = c is None
    c = c or db()
    try:
        rows = c.execute("select key, value from soc_settings").fetchall()
    finally:
        if own:
            c.close()
    s = dict(DEFAULT_SETTINGS)
    s.update({r["key"]: r["value"] for r in rows})
    return s


def iso(v):
    if isinstance(v, datetime):
        return v.astimezone(PKT).isoformat()
    if isinstance(v, date):
        return v.isoformat()
    return v


def js(row) -> dict:
    return {k: iso(v) for k, v in dict(row).items()} if row else None


def today_pkt() -> date:
    return datetime.now(PKT).date()


def house_label(h) -> str:
    return f"{h['block']}-{h['number']}" if h else ""


# ---------------- sessions ----------------
def current_user(request: Request) -> dict:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Please log in")
    with db() as c:
        u = c.execute("""select u.id, u.username, u.name, u.role, u.house_id, u.locked from soc_sessions s
                         join soc_users u on u.id=s.user_id
                         where s.token=%s and s.created_at > now() - interval '30 days'""", (token,)).fetchone()
    if not u:
        raise HTTPException(401, "Please log in")
    return u


def need(*roles):
    def dep(u: dict = Depends(current_user)) -> dict:
        if u["role"] not in roles:
            raise HTTPException(403, "Not allowed for your account")
        return u
    return dep


ADMIN = need("admin")
STAFF = need("admin", "guard")
ANY = need("admin", "guard", "resident")

login_tries: dict[str, deque] = defaultdict(deque)


def client_ip(req: Request) -> str:
    return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "?")


@router.post("/login")
async def login(request: Request, response: Response):
    ip = client_ip(request)
    tries = login_tries[ip]
    now = time.time()
    while tries and now - tries[0] > 600:
        tries.popleft()
    if len(tries) >= 10:
        raise HTTPException(429, "Too many attempts. Try again in 10 minutes.")
    body = await request.json()
    with db() as c:
        u = c.execute("select * from soc_users where lower(username)=lower(%s)", (str(body.get("username", "")).strip(),)).fetchone()
        if not u or not check_pw(str(body.get("password", "")), u["password"]):
            tries.append(now)
            raise HTTPException(401, "Wrong username or password")
        token = secrets.token_urlsafe(32)
        c.execute("insert into soc_sessions (token, user_id) values (%s,%s)", (token, u["id"]))
        c.execute("delete from soc_sessions where created_at < now() - interval '30 days'")
    secure = request.headers.get("x-forwarded-proto") == "https" or "cf-ray" in request.headers
    response.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="lax", secure=secure)
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        with db() as c:
            c.execute("delete from soc_sessions where token=%s", (token,))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.get("/public")
def public():
    return {"demo": DEMO, "society": settings()["society_name"]}


@router.get("/my-house")
def my_house(u: dict = Depends(need("resident"))):
    with db() as c:
        h = c.execute("select * from soc_houses where id=%s", (u["house_id"],)).fetchone()
        v = c.execute("select * from soc_vehicles where house_id=%s order by id", (u["house_id"],)).fetchall()
    return dict(js(h), vehicles=[js(x) for x in v])


@router.get("/me")
def me(u: dict = Depends(ANY)):
    with db() as c:
        s = settings(c)
        house = c.execute("select * from soc_houses where id=%s", (u["house_id"],)).fetchone() if u["house_id"] else None
    return {"user": dict(u), "house": js(house), "house_label": house_label(house), "society": s["society_name"], "demo": DEMO,
            "resident_approval": s["resident_approval"] == "1"}


@router.post("/me/password")
async def change_password(request: Request, u: dict = Depends(ANY)):
    body = await request.json()
    if u["locked"]:
        raise HTTPException(400, "Demo accounts cannot change their password.")
    new = str(body.get("new", ""))
    if len(new) < 8:
        raise HTTPException(400, "Use at least 8 characters.")
    with db() as c:
        row = c.execute("select password from soc_users where id=%s", (u["id"],)).fetchone()
        if not check_pw(str(body.get("old", "")), row["password"]):
            raise HTTPException(400, "Current password is wrong.")
        c.execute("update soc_users set password=%s where id=%s", (hash_pw(new), u["id"]))
    return {"ok": True}


# ---------------- classification ----------------
def classify(c, plate: str | None, gate: str, at: datetime, use_pass: bool = True) -> dict:
    """Decides what a vehicle at the gate is. Returns category, status, house_id, pass_id, visitor, note."""
    out = {"category": "unreadable", "status": "pending", "house_id": None, "pass_id": None, "visitor": None, "note": ""}
    if not plate:
        return out
    for b in c.execute("select * from soc_blacklist").fetchall():
        if same_plate(plate, b["plate"]):
            return dict(out, category="blacklist", status="denied", note=b["reason"] or "Blacklisted vehicle", plate=b["plate"])
    for v in c.execute("select * from soc_vehicles").fetchall():
        if same_plate(plate, v["plate"]):
            return dict(out, category=v["kind"], status="allowed", house_id=v["house_id"], plate=v["plate"],
                        note=" ".join(x for x in (v["colour"], v["make"]) if x))
    day = at.astimezone(PKT).date()
    if gate == "entry":
        for p in c.execute("""select * from soc_passes where plate is not null and not cancelled and valid_from<=%s and valid_to>=%s
                              and uses < max_entries order by created_at desc""", (day, day)).fetchall():
            if same_plate(plate, p["plate"]):
                if use_pass:
                    c.execute("update soc_passes set uses=uses+1 where id=%s", (p["id"],))
                return dict(out, category="visitor", status="allowed", house_id=p["house_id"], pass_id=p["id"], visitor=p["visitor"],
                            note=p["purpose"], plate=p["plate"])
        return dict(out, category="unknown")
    # exit: link it to the visitor's entry if there was one
    for e in c.execute("""select * from soc_events where gate='entry' and plate is not null and at > %s - interval '2 days'
                          and at <= %s order by at desc limit 400""", (at, at)).fetchall():
        if same_plate(plate, e["plate"]):
            if e["category"] in ("visitor", "unknown") and e["status"] == "allowed":
                return dict(out, category="visitor", status="allowed", house_id=e["house_id"], pass_id=e["pass_id"], visitor=e["visitor"])
            break
    return dict(out, category="unknown", status="allowed", note="Leaving")


def _store_img(src: Path | None, event_id: int, kind: str) -> str | None:
    if not src or not src.exists():
        return None
    sub = datetime.now(PKT).strftime("%Y-%m")
    (SOC_DIR / sub).mkdir(exist_ok=True)
    name = f"{sub}/{event_id}_{kind}.jpg"
    shutil.copyfile(src, SOC_DIR / name)
    return name


def ingest(rec: dict, gate: str, at: datetime, source: str, crops_dir: Path | None = None, camera_id: int | None = None,
           dedupe: bool = True) -> int | None:
    """Turns one vehicle record from the pipeline into a gate event. Returns the event id (None when skipped)."""
    plate = rec.get("plate")
    with db() as c:
        s = settings(c)
        if not plate and s["log_unreadable"] != "1":
            return None
        if dedupe and plate:
            recent = c.execute("""select id, plate from soc_events where gate=%s and plate is not null
                                  and at > %s - %s * interval '1 minute' and at <= %s + interval '1 minute'""",
                               (gate, at, int(s["dedupe_minutes"]), at)).fetchall()
            if any(same_plate(plate, r["plate"]) for r in recent):
                return None
        d = classify(c, plate, gate, at)
        plate = d.get("plate") or plate  # registered spelling (LEA-20-4060 even if the camera read LEA-4060)
        ev = c.execute("""insert into soc_events (at, gate, source, camera_id, plate, confidence, vehicle, category, status, house_id,
                          pass_id, visitor, note) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                       (at, gate, source, camera_id, plate, rec.get("confidence"), rec.get("vehicle"), d["category"], d["status"],
                        d["house_id"], d["pass_id"], d["visitor"], d["note"])).fetchone()["id"]
        pi = _store_img(crops_dir / rec["plate_img"], ev, "p") if crops_dir and rec.get("plate_img") else None
        vi = _store_img(crops_dir / rec["vehicle_img"], ev, "v") if crops_dir and rec.get("vehicle_img") else None
        c.execute("update soc_events set plate_img=%s, vehicle_img=%s where id=%s", (pi, vi, ev))
        auto = camera_id and d["status"] == "allowed" and s["barrier_auto"] == "1" and \
            (d["category"] != "visitor" or s["barrier_visitors"] == "1")
    if auto:
        open_barrier(camera_id, "auto", event_id=ev)
    return ev


def refresh_event(ev: int, rec: dict, crops_dir: Path):
    """A live-camera event was created early; when the vehicle has left, update it with the final reading and crops."""
    with db() as c:
        e = c.execute("select * from soc_events where id=%s", (ev,)).fetchone()
        if not e:
            return
        if rec.get("plate") and rec["plate"] != e["plate"] and not e["decided_by"]:
            d = classify(c, rec["plate"], e["gate"], e["at"], use_pass=e["pass_id"] is None)
            c.execute("""update soc_events set plate=%s, confidence=%s, category=%s, status=%s, house_id=%s, pass_id=coalesce(pass_id, %s),
                         visitor=%s, note=%s where id=%s""",
                      (d.get("plate") or rec["plate"], rec["confidence"], d["category"], d["status"], d["house_id"], d["pass_id"], d["visitor"], d["note"], ev))
        for key, kind in (("plate_img", "p"), ("vehicle_img", "v")):
            if rec.get(key):
                c.execute(f"update soc_events set {key}=%s where id=%s", (_store_img(crops_dir / rec[key], ev, kind), ev))


def ingest_job(job: dict, records: list[dict], crops_dir: Path):
    """Called by server.py when a clip uploaded from the society app is finished."""
    if job["gate"] not in ("entry", "exit"):
        return
    for r in records:
        if not r.get("plate") and r.get("direction") == "stationary":
            continue
        ingest(r, job["gate"], job["created_at"] + timedelta(seconds=r.get("t_first") or 0), "upload", crops_dir)


def expire_pending():
    with db() as c:
        m = int(settings(c)["pending_minutes"])
        c.execute("update soc_events set status='no_action' where status='pending' and at < now() - %s * interval '1 minute'", (m,))
        c.execute("""update soc_requests r set status='expired' from soc_events e
                     where e.id=r.event_id and r.status='waiting' and e.status<>'pending'""")


# ---------------- events ----------------
EVENT_SQL = """select e.*, h.block, h.number, h.owner, h.phone as house_phone, cam.name as camera, u.name as decided_by_name
               from soc_events e left join soc_houses h on h.id=e.house_id left join soc_cameras cam on cam.id=e.camera_id
               left join soc_users u on u.id=e.decided_by"""


def _event_json(e) -> dict:
    d = js(e)
    d["house"] = f"{e['block']}-{e['number']}" if e.get("block") else None
    return d


def with_requests(c, events: list[dict]) -> list[dict]:
    """Adds the latest resident-approval request (if any) to each event."""
    ids = [e["id"] for e in events]
    if ids:
        rows = c.execute("""select distinct on (r.event_id) r.*, u.name as answered_by_name from soc_requests r
                            left join soc_users u on u.id=r.answered_by where r.event_id = any(%s)
                            order by r.event_id, r.id desc""", (ids,)).fetchall()
        by = {r["event_id"]: js(r) for r in rows}
        for e in events:
            e["request"] = by.get(e["id"])
    return events


@router.get("/events")
def list_events(request: Request, since: int = 0, q: str = "", category: str = "", gate: str = "", status: str = "",
                day_from: str = "", day_to: str = "", limit: int = 100, u: dict = Depends(ANY)):
    where, args = ["true"], []
    if u["role"] == "resident":
        where.append("e.house_id=%s")
        args.append(u["house_id"])
    if since:
        where.append("e.id > %s")
        args.append(since)
    if category in CATEGORIES:
        where.append("e.category=%s")
        args.append(category)
    if gate in ("entry", "exit"):
        where.append("e.gate=%s")
        args.append(gate)
    if status in ("allowed", "denied", "pending", "no_action"):
        where.append("e.status=%s")
        args.append(status)
    if day_from:
        where.append(f"(e.at at time zone '{TZ}')::date >= %s")
        args.append(day_from)
    if day_to:
        where.append(f"(e.at at time zone '{TZ}')::date <= %s")
        args.append(day_to)
    qq = "".join(ch for ch in q.upper() if ch.isalnum())
    if qq:
        where.append("(replace(coalesce(e.plate,''),'-','') like %s or upper(coalesce(e.visitor,'')) like %s or upper(h.owner) like %s "
                     "or upper(h.block||h.number) like %s)")
        args += [f"%{qq}%", f"%{q.upper()}%", f"%{q.upper()}%", f"%{qq}%"]
    expire_pending()
    with db() as c:
        rows = c.execute(f"{EVENT_SQL} where {' and '.join(where)} order by e.at desc, e.id desc limit %s", (*args, min(limit, 1000))).fetchall()
        return with_requests(c, [_event_json(r) for r in rows])


@router.get("/events.csv")
def export_events(request: Request, q: str = "", category: str = "", gate: str = "", status: str = "", day_from: str = "", day_to: str = "",
                  u: dict = Depends(STAFF)):
    rows = list_events(request, 0, q, category, gate, status, day_from, day_to, 1000, u)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Time", "Gate", "Plate", "Vehicle", "Category", "Status", "House", "Owner", "Visitor", "Note", "Source", "Decided by"])
    for r in rows:
        w.writerow([r["at"][:19].replace("T", " "), r["gate"], r["plate"] or "", r["vehicle"] or "", r["category"], r["status"],
                    r["house"] or "", r.get("owner") or "", r["visitor"] or "", r["note"], r["source"], r.get("decided_by_name") or ""])
    name = f"gate-log-{day_from or 'all'}-{day_to or today_pkt().isoformat()}.csv"
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.post("/events")
async def manual_event(request: Request, u: dict = Depends(STAFF)):
    """Guard types in a plate (camera missed it) or a visitor's pass code."""
    body = await request.json()
    gate = body.get("gate") if body.get("gate") in ("entry", "exit") else "entry"
    plate = clean_plate(body.get("plate", "")) or None
    code = str(body.get("code", "")).strip().upper()
    now = datetime.now(PKT)
    with db() as c:
        p = c.execute("select * from soc_passes where code=%s", (code,)).fetchone() if code else None
        if code and not p and not plate:
            raise HTTPException(404, "No pass with this code")
        if p:
            if p["cancelled"] or not (p["valid_from"] <= now.date() <= p["valid_to"]):
                raise HTTPException(400, "This pass is not valid today")
            if gate == "entry" and p["uses"] >= p["max_entries"]:
                raise HTTPException(400, "This pass has already been used")
            if gate == "entry":
                c.execute("update soc_passes set uses=uses+1 where id=%s", (p["id"],))
            d = {"category": "visitor", "status": "allowed", "house_id": p["house_id"], "pass_id": p["id"], "visitor": p["visitor"],
                 "note": p["purpose"]}
            plate = plate or p["plate"]
        else:
            if not plate:
                raise HTTPException(400, "Enter a plate number or a pass code")
            d = classify(c, plate, gate, now)
            plate = d.get("plate") or plate
        note = " · ".join(x for x in (d["note"], str(body.get("note", ""))[:200]) if x)
        ev = c.execute("""insert into soc_events (at, gate, source, plate, vehicle, category, status, house_id, pass_id, visitor, note)
                          values (now(),%s,'manual',%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                       (gate, plate, body.get("vehicle") or None, d["category"], d["status"], d["house_id"], d["pass_id"], d["visitor"],
                        note)).fetchone()["id"]
        if d["status"] != "pending":
            c.execute("update soc_events set decided_by=%s, decided_at=now() where id=%s", (u["id"], ev))
        row = c.execute(f"{EVENT_SQL} where e.id=%s", (ev,)).fetchone()
    out = _event_json(row)
    if out["status"] == "allowed":
        out["barrier"] = open_for_gate(gate, "guard", ev, u["id"], body.get("camera_id"))
    return out


@router.post("/events/{ev}/decide")
async def decide(ev: int, request: Request, u: dict = Depends(STAFF)):
    body = await request.json()
    status = body.get("status")
    if status not in ("allowed", "denied"):
        raise HTTPException(400, "status must be allowed or denied")
    with db() as c:
        e = c.execute("select * from soc_events where id=%s", (ev,)).fetchone()
        if not e:
            raise HTTPException(404)
        house_id, category, visitor = e["house_id"], e["category"], e["visitor"]
        if body.get("house"):
            h = find_house(c, body["house"])
            if not h:
                raise HTTPException(400, f"House {body['house']} not found")
            house_id = h["id"]
        if status == "allowed" and category in ("unknown", "unreadable") and (body.get("visitor") or body.get("house") or (visitor and house_id)):
            category = "visitor"  # the guard named the house, or asked it first
            visitor = str(body.get("visitor") or visitor or "Visitor")[:80]
        plate = e["plate"]
        if body.get("plate"):
            plate = clean_plate(body["plate"])
        note = str(body.get("note", "") or e["note"])[:200]
        c.execute("""update soc_events set status=%s, category=%s, house_id=%s, visitor=%s, plate=%s, note=%s, decided_by=%s, decided_at=now()
                     where id=%s""", (status, category, house_id, visitor, plate, note, u["id"], ev))
        c.execute("update soc_requests set status='expired' where event_id=%s and status='waiting'", (ev,))
        row = c.execute(f"{EVENT_SQL} where e.id=%s", (ev,)).fetchone()
    out = _event_json(row)
    # open the barrier only for a vehicle that is at the gate now (not for old or uploaded events)
    if status == "allowed" and e["status"] == "pending" and e["source"] != "upload" and body.get("open", True):
        out["barrier"] = (open_barrier(e["camera_id"], "guard", ev, u["id"]) if e["camera_id"]
                          else open_for_gate(e["gate"], "guard", ev, u["id"], body.get("camera_id")))
    return out


def find_house(c, label: str):
    s = str(label).strip().upper().replace(" ", "")
    for h in c.execute("select * from soc_houses").fetchall():
        if f"{h['block']}-{h['number']}".upper().replace(" ", "") == s or f"{h['block']}{h['number']}".upper().replace(" ", "") == s:
            return h
    return None


@router.get("/img/{sub}/{name}")
def event_img(sub: str, name: str, u: dict = Depends(ANY)):
    if not sub.replace("-", "").isdigit() or "/" in name or ".." in name:
        raise HTTPException(404)
    p = SOC_DIR / sub / name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "private, max-age=86400"})


# ---------------- inside now & dashboard ----------------
def inside_now(c, house_id: int | None = None) -> list[dict]:
    """Vehicles whose last allowed gate event is an entry."""
    rows = c.execute(f"""{EVENT_SQL} where e.status='allowed' and e.plate is not null and e.at > now() - interval '14 days'
                         order by e.at""").fetchall()
    last: dict[str, dict] = {}
    for r in rows:
        key = next((k for k in last if same_plate(k, r["plate"])), r["plate"])
        last.pop(key, None)
        last[r["plate"]] = r
    out = []
    now = datetime.now(PKT)
    hours = float(settings(c)["visitor_hours"])
    for r in last.values():
        if r["gate"] != "entry" or (house_id and r["house_id"] != house_id):
            continue
        d = _event_json(r)
        d["minutes"] = int((now - r["at"]).total_seconds() // 60)
        d["overstay"] = r["category"] in ("visitor", "unknown") and d["minutes"] > hours * 60
        out.append(d)
    return sorted(out, key=lambda d: d["at"], reverse=True)


@router.get("/inside")
def get_inside(u: dict = Depends(ANY)):
    with db() as c:
        return inside_now(c, u["house_id"] if u["role"] == "resident" else None)


@router.get("/dashboard")
def dashboard(u: dict = Depends(STAFF)):
    expire_pending()
    today = f"(at at time zone '{TZ}')::date = (now() at time zone '{TZ}')::date"
    with db() as c:
        by_cat = c.execute(f"select category, count(*) n from soc_events where gate='entry' and {today} group by category").fetchall()
        k = c.execute(f"""select count(*) filter (where gate='entry') entries, count(*) filter (where gate='exit') exits,
                          count(*) filter (where status='pending') pending, count(*) filter (where category='blacklist') alerts,
                          count(*) filter (where status='denied') denied
                          from soc_events where {today}""").fetchone()
        hourly = c.execute(f"""select extract(hour from at at time zone '{TZ}')::int h, count(*) n from soc_events
                               where gate='entry' and {today} group by h""").fetchall()
        week = c.execute(f"""select (at at time zone '{TZ}')::date d, category, count(*) n from soc_events
                             where gate='entry' and at > now() - interval '7 days' group by d, category order by d""").fetchall()
        alerts = c.execute(f"""{EVENT_SQL} where (e.category='blacklist' or e.status='denied') and e.at > now() - interval '7 days'
                               order by e.at desc limit 8""").fetchall()
        inside = inside_now(c)
        counts = c.execute("""select (select count(*) from soc_houses) houses, (select count(*) from soc_vehicles) vehicles,
                              (select count(*) from soc_passes where not cancelled and valid_to >= current_date) passes,
                              (select count(*) from soc_blacklist) blacklist""").fetchone()
        cams = c.execute("select id, name, gate from soc_cameras order by id").fetchall()
    h = [0] * 24
    for r in hourly:
        h[r["h"]] = r["n"]
    days = {}
    for r in week:
        days.setdefault(r["d"].isoformat(), {})[r["category"]] = r["n"]
    return {"kpi": dict(k), "by_category": {r["category"]: r["n"] for r in by_cat}, "hourly": h, "week": days,
            "alerts": [_event_json(a) for a in alerts], "inside": inside[:12], "inside_total": len(inside),
            "inside_visitors": sum(1 for i in inside if i["category"] in ("visitor", "unknown")),
            "overstay": [i for i in inside if i["overstay"]][:8], "counts": dict(counts),
            "cameras": [dict(cm, **cameras.status(cm["id"])) for cm in cams]}


# ---------------- houses & vehicles ----------------
@router.get("/houses")
def list_houses(q: str = "", u: dict = Depends(STAFF)):
    with db() as c:
        houses = c.execute("select * from soc_houses order by block, length(number), number").fetchall()
        vehicles = c.execute("select * from soc_vehicles where house_id is not null order by id").fetchall()
        users = c.execute("select id, username, house_id from soc_users where role='resident'").fetchall()
    by_house = defaultdict(list)
    for v in vehicles:
        by_house[v["house_id"]].append(js(v))
    acct = {x["house_id"]: x["username"] for x in users}
    out = []
    qq = q.strip().upper()
    for h in houses:
        d = js(h)
        d["label"] = house_label(h)
        d["vehicles"] = by_house[h["id"]]
        d["account"] = acct.get(h["id"])
        if qq and qq not in (d["label"] + d["owner"] + d["phone"] + " ".join(v["plate"] for v in d["vehicles"])).upper().replace(" ", "") \
                and qq not in (d["owner"]).upper():
            continue
        out.append(d)
    return out


@router.post("/houses")
async def save_house(request: Request, u: dict = Depends(ADMIN)):
    b = await request.json()
    block, number, owner = str(b.get("block", "")).strip().upper()[:10], str(b.get("number", "")).strip()[:10], str(b.get("owner", "")).strip()[:80]
    if not block or not number or not owner:
        raise HTTPException(400, "Block, house number and owner are required")
    status = b.get("status") if b.get("status") in ("owner", "tenant", "vacant") else "owner"
    with db() as c:
        try:
            if b.get("id"):
                c.execute("update soc_houses set block=%s, number=%s, owner=%s, phone=%s, status=%s, note=%s where id=%s",
                          (block, number, owner, str(b.get("phone", ""))[:30], status, str(b.get("note", ""))[:200], b["id"]))
                hid = b["id"]
            else:
                hid = c.execute("insert into soc_houses (block, number, owner, phone, status, note) values (%s,%s,%s,%s,%s,%s) returning id",
                                (block, number, owner, str(b.get("phone", ""))[:30], status, str(b.get("note", ""))[:200])).fetchone()["id"]
        except psycopg.errors.UniqueViolation:
            raise HTTPException(400, f"House {block}-{number} already exists") from None
    return {"id": hid}


@router.delete("/houses/{hid}")
def delete_house(hid: int, u: dict = Depends(ADMIN)):
    with db() as c:
        if c.execute("select 1 from soc_users where house_id=%s and locked", (hid,)).fetchone():
            raise HTTPException(400, "This house belongs to a demo account and cannot be deleted.")
        c.execute("delete from soc_houses where id=%s", (hid,))
    return {"ok": True}


@router.get("/vehicles")
def list_vehicles(u: dict = Depends(STAFF)):
    with db() as c:
        rows = c.execute("""select v.*, h.block, h.number, h.owner from soc_vehicles v left join soc_houses h on h.id=v.house_id
                            order by v.kind, h.block, h.number, v.plate""").fetchall()
    return [dict(js(r), house=f"{r['block']}-{r['number']}" if r["block"] else None) for r in rows]


@router.post("/vehicles")
async def save_vehicle(request: Request, u: dict = Depends(ANY)):
    b = await request.json()
    plate = clean_plate(b.get("plate", ""))
    if len(plate.replace("-", "")) < 3:
        raise HTTPException(400, "Enter a plate number like LEB-1234")
    kind = b.get("kind") if b.get("kind") in ("resident", "staff", "service") else "resident"
    house_id = b.get("house_id") or None
    if u["role"] == "resident":
        kind, house_id = "resident", u["house_id"]
    elif u["role"] != "admin":
        raise HTTPException(403, "Only management can register vehicles")
    if kind == "resident" and not house_id:
        raise HTTPException(400, "Choose the house for a resident vehicle")
    vtype = b.get("vehicle") if b.get("vehicle") in ("Car", "Bike", "Van", "Truck", "Bus") else "Car"
    with db() as c:
        if b.get("id"):
            q = "update soc_vehicles set plate=%s, kind=%s, house_id=%s, vehicle=%s, make=%s, colour=%s, owner_name=%s where id=%s"
            args = [plate, kind, house_id, vtype, str(b.get("make", ""))[:40], str(b.get("colour", ""))[:20], str(b.get("owner_name", ""))[:80], b["id"]]
            if u["role"] == "resident":
                q += " and house_id=%s"
                args.append(u["house_id"])
            c.execute(q, args)
        else:
            try:
                c.execute("insert into soc_vehicles (plate, kind, house_id, vehicle, make, colour, owner_name) values (%s,%s,%s,%s,%s,%s,%s)",
                          (plate, kind, house_id, vtype, str(b.get("make", ""))[:40], str(b.get("colour", ""))[:20], str(b.get("owner_name", ""))[:80]))
            except psycopg.errors.UniqueViolation:
                raise HTTPException(400, f"{plate} is already registered") from None
    return {"plate": plate}


@router.delete("/vehicles/{vid}")
def delete_vehicle(vid: int, u: dict = Depends(ANY)):
    with db() as c:
        if u["role"] == "resident":
            c.execute("delete from soc_vehicles where id=%s and house_id=%s", (vid, u["house_id"]))
        elif u["role"] == "admin":
            c.execute("delete from soc_vehicles where id=%s", (vid,))
        else:
            raise HTTPException(403)
    return {"ok": True}


@router.get("/houses/template.csv")
def house_template(u: dict = Depends(ADMIN)):
    txt = "block,house,owner,phone,status,plates\nA,12,Ahmed Raza,0300-1234567,owner,LEB-1234; LEA-20-4060\nB,7,Sana Iqbal,0321-7654321,tenant,ICT-AB-123\n"
    return Response(txt, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="residents-template.csv"'})


@router.post("/houses/import")
async def import_houses(file: UploadFile = File(...), u: dict = Depends(ADMIN)):
    raw = (await file.read(2_000_000)).decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    added_h = added_v = 0
    errors = []
    with db() as c:
        for i, r in enumerate(rows, start=2):
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            block, number, owner = r.get("block", "").upper(), r.get("house") or r.get("number", ""), r.get("owner", "")
            if not block or not number or not owner:
                errors.append(f"Row {i}: block, house and owner are required")
                continue
            status = r.get("status") if r.get("status") in ("owner", "tenant", "vacant") else "owner"
            h = c.execute("""insert into soc_houses (block, number, owner, phone, status) values (%s,%s,%s,%s,%s)
                             on conflict (block, number) do update set owner=excluded.owner, phone=excluded.phone, status=excluded.status
                             returning id, (xmax = 0) as inserted""", (block, number, owner, r.get("phone", ""), status)).fetchone()
            added_h += h["inserted"]
            for p in filter(None, (x.strip() for x in r.get("plates", "").replace(",", ";").split(";"))):
                plate = clean_plate(p)
                res = c.execute("""insert into soc_vehicles (plate, kind, house_id, owner_name) values (%s,'resident',%s,%s)
                                   on conflict (plate) do nothing returning id""", (plate, h["id"], owner)).fetchone()
                added_v += bool(res)
    return {"rows": len(rows), "houses_added": added_h, "vehicles_added": added_v, "errors": errors[:20]}


# ---------------- visitor passes ----------------
def _pass_code(c) -> str:
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        if not c.execute("select 1 from soc_passes where code=%s", (code,)).fetchone():
            return code


@router.get("/passes")
def list_passes(scope: str = "active", u: dict = Depends(ANY)):
    where, args = ["true"], []
    if u["role"] == "resident":
        where.append("p.house_id=%s")
        args.append(u["house_id"])
    if scope == "active":
        where.append("not p.cancelled and p.valid_to >= %s")
        args.append(today_pkt())
    with db() as c:
        rows = c.execute(f"""select p.*, h.block, h.number, h.owner, u.name as created_by_name from soc_passes p
                             join soc_houses h on h.id=p.house_id left join soc_users u on u.id=p.created_by
                             where {' and '.join(where)} order by p.valid_from desc, p.id desc limit 300""", args).fetchall()
    today = today_pkt()
    out = []
    for r in rows:
        d = js(r)
        d["house"] = f"{r['block']}-{r['number']}"
        d["state"] = ("cancelled" if r["cancelled"] else "expired" if r["valid_to"] < today else "used" if r["uses"] >= r["max_entries"]
                      else "upcoming" if r["valid_from"] > today else "valid")
        out.append(d)
    return out


@router.post("/passes")
async def create_pass(request: Request, u: dict = Depends(ANY)):
    b = await request.json()
    visitor = str(b.get("visitor", "")).strip()[:80]
    if not visitor:
        raise HTTPException(400, "Enter the visitor's name")
    try:
        vf = date.fromisoformat(b.get("valid_from") or today_pkt().isoformat())
        vt = date.fromisoformat(b.get("valid_to") or vf.isoformat())
    except ValueError:
        raise HTTPException(400, "Invalid date") from None
    if vt < vf or (vt - vf).days > 90:
        raise HTTPException(400, "A pass can be valid for up to 90 days")
    plate = clean_plate(b.get("plate", "")) if str(b.get("plate", "")).strip() else None
    with db() as c:
        if u["role"] == "resident":
            house_id = u["house_id"]
        else:
            h = find_house(c, b.get("house", ""))
            if not h:
                raise HTTPException(400, "Choose a valid house, e.g. A-12")
            house_id = h["id"]
        code = _pass_code(c)
        pid = c.execute("""insert into soc_passes (code, house_id, visitor, phone, plate, purpose, valid_from, valid_to, max_entries, created_by)
                           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id""",
                        (code, house_id, visitor, str(b.get("phone", ""))[:30], plate, str(b.get("purpose", ""))[:80], vf, vt,
                         max(1, min(100, int(b.get("max_entries") or 1))), u["id"])).fetchone()["id"]
        s = settings(c)
        h = c.execute("select * from soc_houses where id=%s", (house_id,)).fetchone()
    when = vf.strftime("%d %b") + ("" if vt == vf else " – " + vt.strftime("%d %b"))
    msg = (f"Gate pass for {s['society_name']}\nVisitor: {visitor}\nHouse: {house_label(h)}\nValid: {when}\n"
           + (f"Vehicle: {plate}\n" if plate else "") + f"Pass code: {code}\nShow this code at the gate.")
    return {"id": pid, "code": code, "message": msg}


@router.post("/passes/{pid}/cancel")
def cancel_pass(pid: int, u: dict = Depends(ANY)):
    with db() as c:
        if u["role"] == "resident":
            c.execute("update soc_passes set cancelled=true where id=%s and house_id=%s", (pid, u["house_id"]))
        else:
            c.execute("update soc_passes set cancelled=true where id=%s", (pid,))
    return {"ok": True}


# ---------------- blacklist ----------------
@router.get("/blacklist")
def get_blacklist(u: dict = Depends(STAFF)):
    with db() as c:
        rows = c.execute("select * from soc_blacklist order by created_at desc").fetchall()
        seen = c.execute("select plate, max(at) last, count(*) n from soc_events where category='blacklist' group by plate").fetchall()
    last = {r["plate"]: r for r in seen}
    return [dict(js(r), last_seen=iso(last[r["plate"]]["last"]) if r["plate"] in last else None,
                 times=last[r["plate"]]["n"] if r["plate"] in last else 0) for r in rows]


@router.post("/blacklist")
async def add_blacklist(request: Request, u: dict = Depends(ADMIN)):
    b = await request.json()
    plate = clean_plate(b.get("plate", ""))
    if len(plate.replace("-", "")) < 3:
        raise HTTPException(400, "Enter a plate number like LEB-1234")
    with db() as c:
        c.execute("insert into soc_blacklist (plate, reason) values (%s,%s) on conflict (plate) do update set reason=excluded.reason",
                  (plate, str(b.get("reason", ""))[:120]))
    return {"plate": plate}


@router.delete("/blacklist/{plate}")
def del_blacklist(plate: str, u: dict = Depends(ADMIN)):
    with db() as c:
        c.execute("delete from soc_blacklist where plate=%s", (plate,))
    return {"ok": True}


# ---------------- users ----------------
@router.get("/users")
def list_users(u: dict = Depends(ADMIN)):
    with db() as c:
        rows = c.execute("""select u.id, u.username, u.name, u.role, u.house_id, u.locked, u.created_at, h.block, h.number
                            from soc_users u left join soc_houses h on h.id=u.house_id order by u.role, u.username""").fetchall()
    return [dict(js(r), house=f"{r['block']}-{r['number']}" if r["block"] else None) for r in rows]


@router.post("/users")
async def save_user(request: Request, u: dict = Depends(ADMIN)):
    b = await request.json()
    username = str(b.get("username", "")).strip().lower()[:40]
    name = str(b.get("name", "")).strip()[:80]
    role = b.get("role") if b.get("role") in ("admin", "guard", "resident") else "guard"
    pw = str(b.get("password", ""))
    if not username.replace(".", "").replace("_", "").isalnum() or not name:
        raise HTTPException(400, "Username (letters and numbers) and name are required")
    if len(pw) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with db() as c:
        house_id = None
        if role == "resident":
            h = find_house(c, b.get("house", ""))
            if not h:
                raise HTTPException(400, "Choose the resident's house, e.g. A-12")
            house_id = h["id"]
        try:
            c.execute("insert into soc_users (username, name, password, role, house_id) values (%s,%s,%s,%s,%s)",
                      (username, name, hash_pw(pw), role, house_id))
        except psycopg.errors.UniqueViolation:
            raise HTTPException(400, "This username is taken") from None
    return {"ok": True}


@router.post("/users/{uid}/password")
async def reset_password(uid: int, request: Request, u: dict = Depends(ADMIN)):
    b = await request.json()
    pw = str(b.get("password", ""))
    if len(pw) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    with db() as c:
        if c.execute("select locked from soc_users where id=%s", (uid,)).fetchone()["locked"]:
            raise HTTPException(400, "Demo accounts cannot be changed")
        c.execute("update soc_users set password=%s where id=%s", (hash_pw(pw), uid))
        c.execute("delete from soc_sessions where user_id=%s", (uid,))
    return {"ok": True}


@router.delete("/users/{uid}")
def delete_user(uid: int, u: dict = Depends(ADMIN)):
    with db() as c:
        row = c.execute("select locked from soc_users where id=%s", (uid,)).fetchone()
        if row and row["locked"]:
            raise HTTPException(400, "Demo accounts cannot be deleted")
        if uid == u["id"]:
            raise HTTPException(400, "You cannot delete your own account")
        c.execute("delete from soc_users where id=%s", (uid,))
    return {"ok": True}


# ---------------- settings ----------------
@router.get("/settings")
def get_settings(u: dict = Depends(ADMIN)):
    return settings()


@router.post("/settings")
async def save_settings(request: Request, u: dict = Depends(ADMIN)):
    b = await request.json()
    with db() as c:
        for k in DEFAULT_SETTINGS:
            if k in b:
                v = str(b[k]).strip()[:80]
                if k != "society_name":
                    try:
                        v = str(max(0, min(1440, int(float(v)))))
                    except ValueError:
                        raise HTTPException(400, f"{k} must be a number") from None
                elif not v:
                    raise HTTPException(400, "Society name is required")
                c.execute("insert into soc_settings (key, value) values (%s,%s) on conflict (key) do update set value=excluded.value", (k, v))
    return settings()


# ---------------- live cameras ----------------
class CameraWorker:
    """Reads one camera (RTSP/HTTP stream, or a looping sample file in the demo), runs the pipeline on the latest
    frame, and turns vehicles into gate events."""

    IDLE_DEMO = 90  # a simulated camera only runs while someone is watching

    def __init__(self, cam: dict):
        self.cam = cam
        self.stop_flag = threading.Event()
        self.frame: np.ndarray | None = None
        self.frame_seq = 0
        self.jpeg: bytes | None = None
        self.state = "starting"
        self.error = ""
        self.fps = 0.0
        self.last_view = 0.0
        self.last_frame_at = 0.0
        self.tmp = SOC_DIR / "tmp" / f"cam{cam['id']}"
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def viewed(self):
        self.last_view = time.time()

    def active(self) -> bool:
        return not self.cam["demo"] or time.time() - self.last_view < self.IDLE_DEMO

    def _source(self):
        src = self.cam["source"]
        if src.startswith("demo:"):
            p = ROOT / "samples" / Path(src[5:]).name
            return str(p), True
        return src, False

    def reader(self, cap, is_file: bool):
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        started, n = time.time(), 0
        while not self.stop_flag.is_set() and self.active():
            ok, fr = cap.read()
            if not ok:
                if is_file:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    started, n = time.time(), 0
                    continue
                self.state, self.error = "offline", "stream ended"
                return
            n += 1
            self.frame, self.frame_seq, self.last_frame_at = fr, self.frame_seq + 1, time.time()
            if is_file:  # play the sample at real speed
                delay = started + n / fps - time.time()
                if delay > 0:
                    time.sleep(delay)

    def run(self):
        from .pipeline import FrameTracker, MODELS, VEHICLE_MODEL
        from ultralytics import YOLO

        model = None
        backoff = 2
        while not self.stop_flag.is_set():
            if not self.active():
                self.state = "paused"
                time.sleep(1)
                continue
            if engine is None:
                time.sleep(1)
                continue
            src, is_file = self._source()
            self.state, self.error = "connecting", ""
            os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")
            cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                self.state, self.error = "offline", "cannot connect to camera"
                cap.release()
                time.sleep(backoff)
                backoff = min(60, backoff * 2)
                continue
            backoff = 2
            if model is None:
                model = YOLO(str(MODELS / VEHICLE_MODEL))
            ft = FrameTracker(engine, model)
            emitted: dict[int, int] = {}
            rd = threading.Thread(target=self.reader, args=(cap, is_file), daemon=True)
            rd.start()
            self.state = "online"
            seen, t0, n = 0, time.time(), 0
            started_at = time.monotonic()
            w = h = 0
            while rd.is_alive() and not self.stop_flag.is_set():
                if self.frame_seq == seen or self.frame is None:
                    time.sleep(0.01)
                    continue
                seen, frame = self.frame_seq, self.frame
                t = time.monotonic() - started_at
                h, w = frame.shape[:2]
                sc = min(1.0, 960 / max(w, h))  # live view size: keeps the stream light enough for phones
                try:
                    small = ft.step(frame.copy(), t, (int(w * sc) // 2 * 2, int(h * sc) // 2 * 2))
                    self._events(ft, emitted, t, w * h)
                    if self.cam.get("barrier"):
                        draw_barrier(small, barrier_left(self.cam["id"]) > 0)
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)[:200]
                    time.sleep(0.5)
                    continue
                ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 62])
                if ok:
                    self.jpeg = buf.tobytes()
                n += 1
                if time.time() - t0 >= 2:
                    self.fps, t0, n = n / (time.time() - t0), time.time(), 0
            cap.release()
            for trk in ft.tracks.values():  # flush what is still on screen
                self._finish(ft, trk, emitted, w * h)
            if self.state != "offline":
                self.state = "paused" if not self.active() else "connecting"
            self.jpeg = None

    def _gate(self, trk) -> str:
        from .pipeline import _direction

        if self.cam["gate"] != "both":
            return self.cam["gate"]
        return "exit" if _direction(trk.first_c, trk.last_c) == "away from camera" else "entry"

    def _events(self, ft, emitted, t, frame_area):
        from .pipeline import FrameTracker

        for trk in list(ft.tracks.values()):
            if trk.id in emitted:
                continue
            text, _ = FrameTracker.plate(trk)
            if text and trk.voter.readings >= 4:
                rec = ft.record(trk, self.tmp, prefix=f"{int(time.time())}_")
                if rec:
                    ev = ingest(rec, self._gate(trk), datetime.now(PKT), "camera", self.tmp, self.cam["id"])
                    emitted[trk.id] = ev or 0
        for trk in ft.finished(t, idle=2.5):
            self._finish(ft, trk, emitted, frame_area)
        for f in self.tmp.glob("*.jpg"):
            if time.time() - f.stat().st_mtime > 120:
                f.unlink(missing_ok=True)

    def _finish(self, ft, trk, emitted, frame_area):
        rec = ft.record(trk, self.tmp, prefix=f"{int(time.time())}_")
        ev = emitted.pop(trk.id, None)
        if not rec:
            return
        if ev:
            refresh_event(ev, rec, self.tmp)
        elif ev is None and not rec["plate"]:
            big = trk.vehicle_crop_area >= 0.02 * frame_area
            if big and rec["direction"] != "stationary" and trk.last_t - trk.first_t >= 1.5:
                ingest(rec, self._gate(trk), datetime.now(PKT), "camera", self.tmp, self.cam["id"])

    def stop(self):
        self.stop_flag.set()


class Cameras:
    def __init__(self):
        self.workers: dict[int, CameraWorker] = {}
        self.lock = threading.Lock()

    def sync(self):
        with db() as c:
            cams = {r["id"]: r for r in c.execute("select * from soc_cameras where enabled").fetchall()}
        with self.lock:
            for cid in list(self.workers):
                w = self.workers[cid]
                if cid not in cams or cams[cid]["source"] != w.cam["source"] or cams[cid]["gate"] != w.cam["gate"]:
                    w.stop()
                    del self.workers[cid]
            for cid, cam in cams.items():
                if cid not in self.workers:
                    self.workers[cid] = CameraWorker(cam)
                else:
                    self.workers[cid].cam = cam

    def status(self, cid: int) -> dict:
        w = self.workers.get(cid)
        left = round(barrier_left(cid), 1)
        if not w:
            return {"state": "off", "fps": 0, "error": "", "barrier_open": left > 0, "barrier_left": left}
        return {"state": w.state, "fps": round(w.fps, 1), "error": w.error, "barrier_open": left > 0, "barrier_left": left}


cameras = Cameras()


def _cam_json(r) -> dict:
    d = js(r)
    d.update(cameras.status(r["id"]))
    d["barrier_kind"] = barrier_kind(r["barrier"])
    d["barrier"] = r["barrier"].split("://", 1)[-1].split("/", 1)[0].split("@")[-1] if d["barrier_kind"] == "relay" else ""
    if r["demo"]:
        d["source"] = "Simulated camera (sample video)"
    elif "@" in r["source"]:  # hide the camera password
        scheme, rest = r["source"].split("://", 1) if "://" in r["source"] else ("", r["source"])
        d["source"] = f"{scheme}://***@{rest.split('@', 1)[1]}"
    return d


@router.get("/cameras")
def list_cameras(u: dict = Depends(STAFF)):
    with db() as c:
        return [_cam_json(r) for r in c.execute("select * from soc_cameras order by id").fetchall()]


@router.post("/cameras")
async def save_camera(request: Request, u: dict = Depends(ADMIN)):
    b = await request.json()
    name = str(b.get("name", "")).strip()[:60]
    gate = b.get("gate") if b.get("gate") in ("entry", "exit", "both") else "entry"
    source = str(b.get("source", "")).strip()
    barrier = str(b.get("barrier", "")).strip() if "barrier" in b else None
    if barrier and barrier != "sim":
        if DEMO:
            raise HTTPException(400, "In the online demo the barrier is simulated. On your site we connect your barrier through a relay.")
        if barrier.split("://")[0] not in ("http", "https"):
            raise HTTPException(400, "The barrier relay address must start with http:// (for example http://192.168.1.50/relay/0?turn=on&timer=2)")
    with db() as c:
        if barrier is not None and b.get("id"):
            c.execute("update soc_cameras set barrier=%s where id=%s", (barrier[:300], b["id"]))
        if b.get("id"):
            cam = c.execute("select * from soc_cameras where id=%s", (b["id"],)).fetchone()
            if not cam:
                raise HTTPException(404)
            if cam["demo"] or not source:
                source = cam["source"]
            elif DEMO:
                raise HTTPException(400, "In the online demo cameras are simulated. On your site we connect your own CCTV cameras.")
            c.execute("update soc_cameras set name=%s, gate=%s, source=%s, enabled=%s where id=%s",
                      (name or cam["name"], gate, source, bool(b.get("enabled", True)), b["id"]))
        else:
            if DEMO:
                raise HTTPException(400, "In the online demo cameras are simulated. On your site we connect your own CCTV cameras (RTSP).")
            if not name or not source.split("://")[0] in ("rtsp", "rtsps", "http", "https"):
                raise HTTPException(400, "Enter a name and an RTSP or HTTP camera address")
            c.execute("insert into soc_cameras (name, gate, source, barrier) values (%s,%s,%s,%s)", (name, gate, source, (barrier or "")[:300]))
    cameras.sync()
    return {"ok": True}


@router.delete("/cameras/{cid}")
def delete_camera(cid: int, u: dict = Depends(ADMIN)):
    with db() as c:
        cam = c.execute("select demo from soc_cameras where id=%s", (cid,)).fetchone()
        if cam and cam["demo"] and DEMO:
            raise HTTPException(400, "The demo camera cannot be removed")
        c.execute("delete from soc_cameras where id=%s", (cid,))
    cameras.sync()
    return {"ok": True}


@router.get("/cameras/{cid}/live")
def camera_live(cid: int, u: dict = Depends(ANY)):
    w = cameras.workers.get(cid)
    if not w:
        raise HTTPException(404, "Camera is off")
    w.viewed()

    def gen():
        last, idle = None, 0
        while idle < 400:
            w.viewed()
            fr = w.jpeg
            if fr is not None and fr is not last:
                last, idle = fr, 0
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + fr + b"\r\n"
                time.sleep(0.12)  # at most ~8 frames per second per viewer
            else:
                idle += 1
                time.sleep(0.05)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/cameras/{cid}/wake")
def camera_wake(cid: int, u: dict = Depends(ANY)):
    w = cameras.workers.get(cid)
    if w:
        w.viewed()
    return cameras.status(cid)


# ---------------- barrier ----------------
BARRIER_SECONDS = 8  # how long the gate screen shows the barrier as open
barrier_until: dict[int, float] = {}


def barrier_kind(cfg: str) -> str:
    return "none" if not cfg else "sim" if cfg == "sim" else "relay"


def barrier_left(cid: int) -> float:
    return max(0.0, barrier_until.get(cid, 0) - time.time())


def draw_barrier(img: np.ndarray, is_open: bool):
    h, w = img.shape[:2]
    text = "BARRIER OPEN" if is_open else "BARRIER CLOSED"
    colour = (60, 170, 40) if is_open else (40, 40, 200)
    (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.6, 2)
    x, y = w - tw - 22, h - 14
    cv2.rectangle(img, (x - 10, y - th - 10), (w - 12, y + 8), colour, -1)
    cv2.putText(img, text, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)


def _barrier_log(cid: int, event_id, reason: str, user_id, note: str, ok: bool, error: str = ""):
    with db() as c:
        c.execute("""insert into soc_barrier_log (camera_id, event_id, reason, user_id, note, ok, error)
                     values (%s,%s,%s,%s,%s,%s,%s)""", (cid, event_id, reason, user_id, note[:200], ok, error[:200]))


def open_barrier(camera_id: int | None, reason: str, event_id: int | None = None, user_id: int | None = None,
                 note: str = "") -> dict | None:
    """Opens the barrier of a camera's lane. Returns None when that lane has no barrier.
    A relay is pulsed in the background so a slow relay never holds up the gate screen or the camera."""
    if not camera_id:
        return None
    with db() as c:
        cam = c.execute("select id, name, barrier from soc_cameras where id=%s", (camera_id,)).fetchone()
    if not cam or not cam["barrier"]:
        return None
    kind = barrier_kind(cam["barrier"])
    barrier_until[cam["id"]] = time.time() + BARRIER_SECONDS
    if kind == "sim":
        _barrier_log(cam["id"], event_id, reason, user_id, note, True)
    else:
        def pulse():
            try:
                with urllib.request.urlopen(cam["barrier"], timeout=4) as r:
                    ok = r.status < 400
                _barrier_log(cam["id"], event_id, reason, user_id, note, ok, "" if ok else f"relay answered {r.status}")
            except Exception as e:  # noqa: BLE001
                barrier_until.pop(cam["id"], None)
                _barrier_log(cam["id"], event_id, reason, user_id, note, False, str(e))
        threading.Thread(target=pulse, daemon=True).start()
    return {"camera_id": cam["id"], "camera": cam["name"], "kind": kind}


def open_for_gate(gate: str, reason: str, event_id: int | None, user_id: int | None, camera_id=None) -> dict | None:
    """For events without a camera (typed in by the guard): opens the given lane, or the first lane with a barrier for this gate."""
    with db() as c:
        if camera_id:
            cam = c.execute("select id from soc_cameras where id=%s and barrier<>''", (int(camera_id),)).fetchone()
        else:
            cam = c.execute("""select id from soc_cameras where enabled and barrier<>'' and gate in (%s, 'both')
                               order by (gate=%s) desc, id limit 1""", (gate, gate)).fetchone()
    return open_barrier(cam["id"], reason, event_id, user_id) if cam else None


@router.post("/cameras/{cid}/barrier")
async def barrier_button(cid: int, request: Request, u: dict = Depends(STAFF)):
    """The guard opens the barrier by hand (logged with a note)."""
    try:
        b = await request.json()
    except Exception:  # noqa: BLE001
        b = {}
    res = open_barrier(cid, "manual", user_id=u["id"], note=str(b.get("note", "")).strip())
    if not res:
        raise HTTPException(400, "No barrier is connected to this lane")
    return dict(res, **cameras.status(cid))


@router.get("/barrier/log")
def barrier_log(limit: int = 30, u: dict = Depends(ADMIN)):
    with db() as c:
        rows = c.execute("""select l.*, cam.name as camera, u.name as user_name, e.plate, e.category from soc_barrier_log l
                            left join soc_cameras cam on cam.id=l.camera_id left join soc_users u on u.id=l.user_id
                            left join soc_events e on e.id=l.event_id order by l.id desc limit %s""", (min(limit, 200),)).fetchall()
    return [js(r) for r in rows]


# ---------------- resident approval ----------------
def _drop_sub(endpoint: str):
    with db() as c:
        c.execute("delete from soc_push where endpoint=%s", (endpoint,))


@router.post("/events/{ev}/ask")
async def ask_resident(ev: int, request: Request, u: dict = Depends(STAFF)):
    """The guard asks the house a visitor names; the house's residents approve or reject on their phones.
    The guard still makes the final decision at the gate."""
    b = await request.json()
    with db() as c:
        if settings(c)["resident_approval"] != "1":
            raise HTTPException(400, "Asking residents is turned off in Settings")
        e = c.execute("select * from soc_events where id=%s", (ev,)).fetchone()
        if not e:
            raise HTTPException(404)
        if e["status"] != "pending":
            raise HTTPException(400, "This vehicle has already been decided")
        if e["category"] == "blacklist":
            raise HTTPException(400, "A blacklisted vehicle cannot be let in")
        h = find_house(c, b.get("house", ""))
        if not h:
            raise HTTPException(400, f"House {b.get('house', '')} not found")
        users = [r["id"] for r in c.execute("select id from soc_users where role='resident' and house_id=%s", (h["id"],)).fetchall()]
        if not users:
            raise HTTPException(400, f"House {house_label(h)} has no resident account. Call the house"
                                     + (f": {h['phone']}" if h["phone"] else "") + ".")
        visitor = str(b.get("visitor") or "").strip()[:80] or "Visitor"
        c.execute("update soc_requests set status='expired' where event_id=%s and status='waiting'", (ev,))
        rid = c.execute("insert into soc_requests (event_id, house_id, visitor, asked_by) values (%s,%s,%s,%s) returning id",
                        (ev, h["id"], visitor, u["id"])).fetchone()["id"]
        c.execute("update soc_events set house_id=%s, visitor=%s where id=%s", (h["id"], visitor, ev))
        subs = c.execute("select endpoint, keys from soc_push where user_id = any(%s)", (users,)).fetchall()
        row = with_requests(c, [_event_json(c.execute(f"{EVENT_SQL} where e.id=%s", (ev,)).fetchone())])[0]
    society_push.send([dict(x) for x in subs], {
        "title": f"{visitor} is at the gate",
        "body": f"{e['plate'] or 'Plate not read'} · {e['vehicle'] or 'Vehicle'} · Let them in?",
        "tag": f"req-{rid}", "request_id": rid, "url": "/society/#/home"}, on_gone=_drop_sub)
    return dict(row, notified=len(subs))


def _request_json(r) -> dict:
    d = js(r)
    d["house"] = f"{r['block']}-{r['number']}" if r.get("block") else None
    return d


@router.get("/requests")
def list_requests(u: dict = Depends(need("resident"))):
    expire_pending()
    with db() as c:
        rows = c.execute("""select r.*, e.plate, e.vehicle, e.plate_img, e.vehicle_img, e.at as event_at, e.gate, e.status as event_status,
                                   h.block, h.number, g.name as asked_by_name
                            from soc_requests r join soc_events e on e.id=r.event_id join soc_houses h on h.id=r.house_id
                            left join soc_users g on g.id=r.asked_by
                            where r.house_id=%s and r.asked_at > now() - interval '1 day' order by r.id desc limit 20""",
                         (u["house_id"],)).fetchall()
    return [_request_json(r) for r in rows]


@router.post("/requests/{rid}/answer")
async def answer_request(rid: int, request: Request, u: dict = Depends(need("resident"))):
    b = await request.json()
    ans = b.get("answer")
    if ans not in ("approved", "rejected"):
        raise HTTPException(400, "answer must be approved or rejected")
    expire_pending()
    with db() as c:
        r = c.execute("select * from soc_requests where id=%s", (rid,)).fetchone()
        if not r or r["house_id"] != u["house_id"]:
            raise HTTPException(404)
        if r["status"] != "waiting":
            raise HTTPException(400, "Already answered" if r["status"] in ("approved", "rejected") else "Too late: the guard has already decided")
        c.execute("update soc_requests set status=%s, answered_by=%s, answered_at=now() where id=%s", (ans, u["id"], rid))
    return {"ok": True, "status": ans}


# ---------------- phone notifications ----------------
@router.get("/push/key")
def push_key(u: dict = Depends(ANY)):
    return {"key": society_push.public_key()}


@router.post("/push/subscribe")
async def push_subscribe(request: Request, u: dict = Depends(ANY)):
    b = await request.json()
    endpoint, keys = str(b.get("endpoint", "")), b.get("keys") or {}
    if not endpoint.startswith("https://") or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(400, "Invalid subscription")
    import json

    with db() as c:
        c.execute("""insert into soc_push (user_id, endpoint, keys) values (%s,%s,%s)
                     on conflict (endpoint) do update set user_id=excluded.user_id, keys=excluded.keys""",
                  (u["id"], endpoint[:1000], json.dumps({"p256dh": keys["p256dh"], "auth": keys["auth"]})))
    return {"ok": True}


@router.post("/push/test")
def push_test(u: dict = Depends(ANY)):
    with db() as c:
        subs = c.execute("select endpoint, keys from soc_push where user_id=%s", (u["id"],)).fetchall()
    society_push.send([dict(x) for x in subs], {"title": "Notifications are on", "body": "You will be asked here when a visitor for your house is at the gate.",
                                                "tag": "test", "url": "/society/#/home"}, on_gone=_drop_sub)
    return {"sent": len(subs)}


# ---------------- startup ----------------
def first_run(c):
    """A real society's first start: an empty register with one admin account, both taken from the environment
    (SOC_NAME, SOC_ADMIN_USER, SOC_ADMIN_PASSWORD; set by deploy/install.sh). Houses, guards and cameras are added in the app."""
    name = os.environ.get("SOC_NAME", "").strip() or "Our Society"
    user = os.environ.get("SOC_ADMIN_USER", "admin").strip().lower() or "admin"
    pw = os.environ.get("SOC_ADMIN_PASSWORD", "")
    if len(pw) < 8:
        raise RuntimeError("SOC_ADMIN_PASSWORD must be set (at least 8 characters) for the first start of a real society")
    c.execute("insert into soc_settings (key, value) values ('society_name', %s) on conflict (key) do update set value=excluded.value",
              (name,))
    c.execute("insert into soc_users (username, name, password, role) values (%s, 'Administrator', %s, 'admin')", (user, hash_pw(pw)))


def startup():
    with db() as c:
        c.execute(SCHEMA)
        if not c.execute("select 1 from soc_users limit 1").fetchone():
            if DEMO:
                from .society_demo import seed

                seed(c)
            else:
                first_run(c)
        if DEMO:  # demo cameras created before the barrier feature
            c.execute("update soc_cameras set barrier='sim' where demo and barrier='' and name like 'Main gate%'")
    cameras.sync()

    def housekeeping():
        last_reset = today_pkt()
        while True:
            try:
                expire_pending()
                now = datetime.now(PKT)
                if DEMO and now.hour == 4 and now.date() != last_reset:  # fresh demo society every night
                    from .society_demo import reset

                    last_reset = now.date()
                    reset()
                    cameras.sync()
            except Exception:  # noqa: BLE001
                pass
            time.sleep(60)
    threading.Thread(target=housekeeping, daemon=True).start()
