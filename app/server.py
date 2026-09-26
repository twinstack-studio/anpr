"""TwinStack ANPR web app: upload videos/photos, watch them processed live, browse the vehicle log."""
import os
import queue
import subprocess
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import timedelta, timezone
from pathlib import Path

import cv2
import numpy as np
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .pipeline import ANPR

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "runtime"
UPLOADS, RESULTS = DATA / "uploads", DATA / "results"
for d in (UPLOADS, RESULTS):
    d.mkdir(parents=True, exist_ok=True)
DSN = os.environ.get("ANPR_DSN", "postgresql:///anpr?host=/var/run/postgresql")
MAX_UPLOAD = 95 * 1024 * 1024  # Cloudflare rejects request bodies over 100 MB
MAX_SECONDS = 45.0
UPLOADS_PER_HOUR = 8
PKT = timezone(timedelta(hours=5))

app = FastAPI(title="TwinStack ANPR", docs_url=None, redoc_url=None)
engine: ANPR | None = None
jobs_q: "queue.Queue[str]" = queue.Queue()
live_frames: dict[str, bytes] = {}
upload_log: dict[str, deque] = defaultdict(deque)


def db():
    return psycopg.connect(DSN, row_factory=dict_row, autocommit=True)


SCHEMA = """
create table if not exists jobs (
  id text primary key,
  kind text not null,               -- video | image
  filename text not null,
  gate text not null default 'entry',  -- entry | exit | none
  status text not null default 'queued',  -- queued | processing | done | failed
  progress real not null default 0,
  error text,
  sample boolean not null default false,
  duration_s real,
  process_s real,
  created_at timestamptz not null default now()
);
create table if not exists detections (
  id bigserial primary key,
  job_id text not null references jobs(id) on delete cascade,
  track int,
  vehicle text,
  plate text,
  confidence real,
  readings int,
  t_first real,
  t_last real,
  direction text,
  plate_img text,
  vehicle_img text,
  seen_at timestamptz not null,
  gate text not null
);
create index if not exists detections_plate on detections(plate);
create table if not exists watchlist (
  plate text primary key,
  kind text not null,  -- blacklist | vip | staff
  note text not null default '',
  created_at timestamptz not null default now()
);
"""


@app.on_event("startup")
def startup():
    global engine
    with db() as c:
        c.execute(SCHEMA)
        c.execute("update jobs set status='failed', error='server restarted' where status in ('queued','processing')")
    engine = ANPR()
    threading.Thread(target=worker, daemon=True).start()


# ---------------- worker ----------------
def worker():
    while True:
        job_id = jobs_q.get()
        try:
            run_job(job_id)
        except Exception as e:  # noqa: BLE001
            with db() as c:
                c.execute("update jobs set status='failed', error=%s where id=%s", (str(e)[:500], job_id))
        finally:
            live_frames.pop(job_id, None)


def run_job(job_id: str):
    with db() as c:
        job = c.execute("select * from jobs where id=%s", (job_id,)).fetchone()
        c.execute("update jobs set status='processing' where id=%s", (job_id,))
    src = next(UPLOADS.glob(f"{job_id}.*"))
    out = RESULTS / job_id
    out.mkdir(exist_ok=True)
    started = time.time()

    def progress(p, _elapsed):
        with db() as c:
            c.execute("update jobs set progress=%s where id=%s", (p, job_id))

    def frame(img):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            live_frames[job_id] = buf.tobytes()

    assert engine is not None
    if job["kind"] == "image":
        img = cv2.imread(str(src))
        if img is None:
            raise ValueError("could not read image")
        res, canvas = engine.process_image(img)
        cv2.imwrite(str(out / "annotated.jpg"), canvas)
        recs = []
        for i, r in enumerate(res):
            x1, y1, x2, y2 = r["box"]
            crop = img[max(0, y1 - 8) : y2 + 8, max(0, x1 - 12) : x2 + 12]
            cv2.imwrite(str(out / f"plate_{i}.jpg"), crop)
            recs.append({"track": i, "vehicle": r["vehicle"], "plate": r["plate"], "confidence": r["confidence"], "readings": 1,
                         "t_first": 0, "t_last": 0, "direction": None, "plate_img": f"plate_{i}.jpg", "vehicle_img": None})
        duration = 0.0
    else:
        recs = engine.process_video(src, out / "annotated.mp4", out, on_progress=progress, on_frame=frame, max_seconds=MAX_SECONDS)
        cap = cv2.VideoCapture(str(src))
        duration = min(MAX_SECONDS, (cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0) / (cap.get(cv2.CAP_PROP_FPS) or 25))
        cap.release()

    thumb_src = out / ("annotated.jpg" if job["kind"] == "image" else "annotated.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *([] if job["kind"] == "image" else ["-ss", str(min(2.0, duration / 3))]),
                    "-i", str(thumb_src), "-frames:v", "1", "-vf", "scale=480:-2", str(out / "thumb.jpg")], check=False)

    base = job["created_at"]
    with db() as c:
        for r in recs:
            c.execute(
                """insert into detections (job_id, track, vehicle, plate, confidence, readings, t_first, t_last, direction,
                   plate_img, vehicle_img, seen_at, gate) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (job_id, r["track"], r["vehicle"], r["plate"], r["confidence"], r["readings"], r["t_first"], r["t_last"],
                 r["direction"], r["plate_img"], r["vehicle_img"], base + timedelta(seconds=r["t_first"]), job["gate"]),
            )
        c.execute("update jobs set status='done', progress=1, duration_s=%s, process_s=%s where id=%s",
                  (duration, time.time() - started, job_id))


# ---------------- API ----------------
def client_ip(req: Request) -> str:
    return req.headers.get("cf-connecting-ip") or (req.client.host if req.client else "?")


@app.post("/api/jobs")
async def create_job(request: Request, file: UploadFile = File(...), gate: str = Form("entry")):
    ip = client_ip(request)
    now = time.time()
    log = upload_log[ip]
    while log and now - log[0] > 3600:
        log.popleft()
    if len(log) >= UPLOADS_PER_HOUR:
        raise HTTPException(429, "Upload limit reached for this hour. Please try again later.")
    ext = Path(file.filename or "").suffix.lower()
    kind = "image" if ext in {".jpg", ".jpeg", ".png", ".webp"} else "video" if ext in {".mp4", ".mov", ".avi", ".mkv", ".webm"} else None
    if not kind:
        raise HTTPException(400, "Please upload a photo (JPG/PNG) or a video (MP4/MOV).")
    if gate not in ("entry", "exit", "none"):
        gate = "entry"
    job_id = uuid.uuid4().hex[:12]
    dst = UPLOADS / f"{job_id}{ext}"
    size = 0
    with dst.open("wb") as f:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                dst.unlink(missing_ok=True)
                raise HTTPException(413, "File is too large (max 95 MB). Trim the clip to under a minute.")
            f.write(chunk)
    log.append(now)
    with db() as c:
        c.execute("insert into jobs (id, kind, filename, gate) values (%s,%s,%s,%s)", (job_id, kind, (file.filename or "upload")[:120], gate))
    jobs_q.put(job_id)
    return {"id": job_id, "kind": kind}


def _job_json(j):
    j = dict(j)
    j["created_at"] = j["created_at"].isoformat()
    j["queue_position"] = None
    return j


@app.get("/api/jobs")
def list_jobs():
    with db() as c:
        rows = c.execute("select * from jobs order by sample desc, created_at desc limit 40").fetchall()
    return [_job_json(r) for r in rows]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with db() as c:
        j = c.execute("select * from jobs where id=%s", (job_id,)).fetchone()
        if not j:
            raise HTTPException(404)
        dets = c.execute(
            """select d.*, w.kind as watch_kind, w.note as watch_note from detections d
               left join watchlist w on w.plate = d.plate where job_id=%s order by t_first""", (job_id,)).fetchall()
    job = _job_json(j)
    if j["status"] == "queued":
        waiting = list(jobs_q.queue)
        job["queue_position"] = waiting.index(job_id) + 1 if job_id in waiting else None
    return {"job": job, "detections": [_det_json(d) for d in dets]}


def _det_json(d):
    d = dict(d)
    d["seen_at"] = d["seen_at"].astimezone(PKT).isoformat()
    return d


@app.get("/api/jobs/{job_id}/live")
def live(job_id: str):
    def gen():
        last = None
        idle = 0
        while idle < 600:
            fr = live_frames.get(job_id)
            if fr is not None and fr is not last:
                last, idle = fr, 0
                yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + fr + b"\r\n"
            else:
                idle += 1
                if fr is None and idle > 20:
                    break
            time.sleep(0.05)
    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/api/jobs/{job_id}/file/{name}")
def job_file(job_id: str, name: str):
    if not job_id.isalnum() or "/" in name or ".." in name:
        raise HTTPException(404)
    p = RESULTS / job_id / name
    if name == "source":
        p = next(UPLOADS.glob(f"{job_id}.*"), None)
    if not p or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/detections")
def detections(q: str = "", limit: int = 200):
    q = "".join(ch for ch in q.upper() if ch.isalnum())
    with db() as c:
        rows = c.execute(
            """select d.*, j.filename, w.kind as watch_kind, w.note as watch_note from detections d
               join jobs j on j.id=d.job_id left join watchlist w on w.plate=d.plate
               where d.plate is not null and (%s='' or replace(d.plate,'-','') like %s)
               order by d.seen_at desc limit %s""", (q, f"%{q}%", min(limit, 500))).fetchall()
    return [_det_json(r) for r in rows]


@app.get("/api/stats")
def stats():
    with db() as c:
        s = c.execute("""select count(*) as vehicles, count(plate) as plates, count(distinct plate) as unique_plates,
                         coalesce(avg(confidence) filter (where plate is not null),0) as avg_conf from detections""").fetchone()
        by_type = c.execute("select vehicle, count(*) n from detections group by vehicle order by n desc").fetchall()
        s["alerts"] = c.execute("select count(*) n from detections d join watchlist w on w.plate=d.plate").fetchone()["n"]
        s["videos"] = c.execute("select count(*) n, coalesce(sum(duration_s),0) secs from jobs where status='done'").fetchone()
    s["by_type"] = by_type
    return s


@app.get("/api/parking")
def parking(rate_first: int = 50, rate_next: int = 30):
    """Match each entry with the next exit of the same plate. Fee: first hour + each extra started hour."""
    with db() as c:
        rows = c.execute("""select plate, vehicle, gate, seen_at, plate_img, job_id from detections
                            where plate is not null and gate in ('entry','exit') order by seen_at""").fetchall()
    open_, sessions = {}, []
    for r in rows:
        if r["gate"] == "entry":
            open_[r["plate"]] = r
        elif r["plate"] in open_:
            e = open_.pop(r["plate"])
            mins = max(1, int((r["seen_at"] - e["seen_at"]).total_seconds() // 60))
            hours = -(-mins // 60)
            sessions.append({"plate": r["plate"], "vehicle": e["vehicle"], "entry": e["seen_at"].astimezone(PKT).isoformat(),
                             "exit": r["seen_at"].astimezone(PKT).isoformat(), "minutes": mins,
                             "fee": rate_first + max(0, hours - 1) * rate_next, "plate_img": e["plate_img"], "job_id": e["job_id"]})
    inside = [{"plate": p, "vehicle": e["vehicle"], "entry": e["seen_at"].astimezone(PKT).isoformat(), "plate_img": e["plate_img"],
               "job_id": e["job_id"]} for p, e in open_.items()]
    return {"sessions": sessions[::-1], "inside": inside[::-1]}


@app.get("/api/watchlist")
def get_watchlist():
    with db() as c:
        return [dict(r, created_at=r["created_at"].isoformat()) for r in c.execute("select * from watchlist order by created_at desc")]


@app.post("/api/watchlist")
async def add_watch(request: Request):
    body = await request.json()
    from .plates import normalize

    plate, _ = normalize(str(body.get("plate", "")))
    kind = body.get("kind") if body.get("kind") in ("blacklist", "vip", "staff") else "blacklist"
    if len(plate) < 3:
        raise HTTPException(400, "Enter a plate number like LEB-1234")
    with db() as c:
        c.execute("""insert into watchlist (plate, kind, note) values (%s,%s,%s)
                     on conflict (plate) do update set kind=excluded.kind, note=excluded.note""",
                  (plate, kind, str(body.get("note", ""))[:120]))
    return {"plate": plate}


@app.delete("/api/watchlist/{plate}")
def del_watch(plate: str):
    with db() as c:
        c.execute("delete from watchlist where plate=%s", (plate,))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": engine is not None, "queue": jobs_q.qsize()}


app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
