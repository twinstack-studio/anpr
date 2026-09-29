"""ANPR pipeline: vehicle detection + tracking, plate detection, plate OCR, per-vehicle voting."""
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from ultralytics import YOLO

from .plates import PlateVoter, normalize

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"

# Hardware settings. The server uses the GPU; a gate mini PC without a GPU sets ANPR_DEVICE=cpu, which also picks
# a smaller vehicle model and image size so one camera still runs at a few frames per second.
DEVICE = os.environ.get("ANPR_DEVICE", "0")
ON_GPU = DEVICE != "cpu"
VEHICLE_MODEL = os.environ.get("ANPR_VEHICLE_MODEL", "yolo11m.pt" if ON_GPU else "yolo11n.pt")
TRACK_SIZE = int(os.environ.get("ANPR_IMGSZ", 1280 if ON_GPU else 640))

VEHICLE_CLASSES = {2: "Car", 3: "Bike", 5: "Bus", 7: "Truck"}
COLORS = {"Car": (80, 200, 255), "Bike": (120, 255, 140), "Bus": (255, 170, 80), "Truck": (200, 140, 255), "Plate": (0, 230, 255)}


@dataclass
class Track:
    id: int
    vehicle: str
    first_t: float
    last_t: float
    first_c: tuple
    last_c: tuple
    voter: PlateVoter = field(default_factory=PlateVoter)
    plate_crop: np.ndarray | None = None
    plate_crop_score: float = 0.0
    vehicle_crop: np.ndarray | None = None
    vehicle_crop_area: float = 0.0
    types: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return max(self.types, key=lambda k: self.types[k]) if self.types else self.vehicle


class ANPR:
    _lock = threading.Lock()  # one GPU call at a time
    _video_lock = threading.Lock()  # one uploaded video at a time

    def __init__(self, device: int | str = DEVICE):
        self.device = int(device) if str(device).isdigit() else device
        self.half = ON_GPU
        self.vehicles = YOLO(str(MODELS / VEHICLE_MODEL))
        self.plates = YOLO(str(MODELS / "plate_detector.pt"))
        custom = MODELS / "pk_plate_ocr.keras"
        if custom.exists():  # our OCR fine-tuned on Pakistani plates (training/ocr/train_ocr.py)
            from .ocr import PlateOCR

            self._ocr = PlateOCR(custom)
            self._global = None
        else:
            from fast_plate_ocr import LicensePlateRecognizer

            self._ocr = None
            self._global = LicensePlateRecognizer("cct-s-v2-global-model", device="cpu")

    # ---------- building blocks ----------
    def read_plates(self, crops: list[np.ndarray]) -> list[tuple[str, float]]:
        if not crops:
            return []
        if self._ocr is not None:
            return self._ocr.run(crops)
        out = []
        preds = self._global.run([cv2.cvtColor(c, cv2.COLOR_BGR2RGB) for c in crops], return_confidence=True)
        for p in preds:
            text = p.plate.replace("_", "")
            probs = np.asarray(p.char_probs)[: max(1, len(text))] if p.char_probs is not None else np.array([0.5])
            out.append((text, float(probs.mean())))
        return out

    def detect_plates(self, frame: np.ndarray, conf=0.4, imgsz=1280):
        r = self.plates.predict(frame, imgsz=imgsz, conf=conf, device=self.device, half=self.half, verbose=False)[0]
        return [(tuple(map(int, b)), float(c)) for b, c in zip(r.boxes.xyxy.tolist(), r.boxes.conf.tolist())]

    def detect_plates_on(self, frame: np.ndarray, boxes: list[tuple], conf=0.4, max_n=24):
        """Look for plates inside each vehicle crop (small plates survive, and plates must belong to a vehicle).
        Returns [(plate_box_in_frame, conf, index_of_vehicle)]."""
        h, w = frame.shape[:2]
        order = sorted(range(len(boxes)), key=lambda i: -(boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1]))[:max_n]
        crops, offs = [], []
        for i in order:
            x1, y1, x2, y2 = boxes[i]
            if min(x2 - x1, y2 - y1) < 40:
                continue
            px, py = int((x2 - x1) * 0.05), int((y2 - y1) * 0.05)
            cx1, cy1, cx2, cy2 = max(0, x1 - px), max(0, y1 - py), min(w, x2 + px), min(h, y2 + py)
            crops.append(frame[cy1:cy2, cx1:cx2])
            offs.append((cx1, cy1, i))
        if not crops:
            return []
        out = []
        for r, (ox, oy, i) in zip(self.plates.predict(crops, imgsz=640, conf=conf, device=self.device, half=self.half, verbose=False), offs):
            if not len(r.boxes):
                continue
            k = int(r.boxes.conf.argmax())  # one plate per vehicle
            b = r.boxes.xyxy[k].tolist()
            out.append(((int(b[0]) + ox, int(b[1]) + oy, int(b[2]) + ox, int(b[3]) + oy), float(r.boxes.conf[k]), i))
        # overlapping vehicle boxes can find the same plate twice: keep the more confident one
        kept = []
        for p in sorted(out, key=lambda p: -p[1]):
            if all(_iou(p[0], q[0]) < 0.5 for q in kept):
                kept.append(p)
        return kept

    @staticmethod
    def _crop(frame, box, pad=0.06):
        x1, y1, x2, y2 = box
        h, w = frame.shape[:2]
        px, py = int((x2 - x1) * pad), int((y2 - y1) * pad)
        return frame[max(0, y1 - py) : min(h, y2 + py), max(0, x1 - px) : min(w, x2 + px)]

    # ---------- single image ----------
    def process_image(self, img: np.ndarray) -> tuple[list[dict], np.ndarray]:
        sz = min(1280, -(-max(img.shape[:2]) // 32) * 32)  # never upscale: big close-up cars get missed
        with self._lock:
            vr = self.vehicles.predict(img, imgsz=sz, conf=0.35, classes=list(VEHICLE_CLASSES), device=self.device, half=self.half, verbose=False)[0]
            vehicles = [(tuple(map(int, b)), VEHICLE_CLASSES[int(c)]) for b, c in zip(vr.boxes.xyxy.tolist(), vr.boxes.cls.tolist())]
            plates = [(b, c) for b, c, _ in self.detect_plates_on(img, [b for b, _ in vehicles])]
            for b, c in self.detect_plates(img, imgsz=sz):
                if not any(_iou(b, p) > 0.3 for p, _ in plates):
                    plates.append((b, c))
            texts = self.read_plates([self._crop(img, b) for b, _ in plates])
        results, canvas = [], img.copy()
        for (box, det_conf), (raw, ocr_conf) in zip(plates, texts):
            text, valid = normalize(raw)
            veh = _owner(box, vehicles)
            results.append({"plate": text, "valid": valid, "raw": raw, "confidence": round(ocr_conf * det_conf ** 0.5, 3),
                            "vehicle": veh[1] if veh else None, "box": box})
            if veh:
                _draw_box(canvas, veh[0], veh[1], COLORS[veh[1]])
            _draw_box(canvas, box, text, COLORS["Plate"], big=True)
        for vbox, vtype in vehicles:
            if not any(_owner(r["box"], [(vbox, vtype)]) for r in results):
                _draw_box(canvas, vbox, vtype, COLORS[vtype])
        return results, canvas

    # ---------- video ----------
    def process_video(self, src: Path, out_video: Path, crops_dir: Path, on_progress=None, on_frame=None,
                      max_seconds: float = 60.0, out_width: int = 1280) -> list[dict]:
        """Runs the full pipeline on a video. Writes an annotated H.264 MP4 and returns one record per vehicle."""
        cap = cv2.VideoCapture(str(src))
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        total = int(min(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 1e9, fps * max_seconds))
        w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        scale = min(1.0, out_width / max(w, h))
        ow, oh = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
        step = 2 if fps > 28 else 1  # process ~15-25 fps
        out_fps = fps / step
        ff = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{ow}x{oh}", "-r", f"{out_fps:.3f}",
             "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out_video)],
            stdin=subprocess.PIPE,
        )
        ft = FrameTracker(self)
        crops_dir.mkdir(parents=True, exist_ok=True)
        frame_i, done = 0, 0
        started = time.time()
        with self._video_lock:  # one uploaded video at a time; the GPU itself is shared per frame with live cameras
            while frame_i < total:
                ok, frame = cap.read()
                if not ok:
                    break
                frame_i += 1
                if (frame_i - 1) % step:
                    continue
                t = (frame_i - 1) / fps
                small = ft.step(frame, t, (ow, oh))
                ff.stdin.write(small.tobytes())
                done += 1
                if on_frame and done % 2 == 0:
                    on_frame(small)
                if on_progress and done % 5 == 0:
                    on_progress(min(0.99, frame_i / total), time.time() - started)
        cap.release()
        ff.stdin.close()
        ff.wait()

        records = [r for r in (ft.record(trk, crops_dir) for trk in ft.tracks.values()) if r]
        records.sort(key=lambda r: r["t_first"])
        return merge_records(records, crops_dir)


class FrameTracker:
    """Vehicle tracking + plate reading one frame at a time. Used for uploaded videos and for live cameras.
    Each tracker needs its own vehicle model, because ByteTrack keeps its state inside the model."""

    def __init__(self, anpr: ANPR, vehicle_model: YOLO | None = None):
        self.a = anpr
        self.model = vehicle_model or anpr.vehicles
        self.model.predictor = None  # fresh tracker state
        self.tracks: dict[int, Track] = {}

    def step(self, frame: np.ndarray, t: float, out_size: tuple[int, int]) -> np.ndarray:
        """Processes one frame taken at time t (seconds). Returns the annotated frame resized to out_size."""
        a = self.a
        with a._lock:
            tr = self.model.track(frame, imgsz=TRACK_SIZE, conf=0.3, classes=list(VEHICLE_CLASSES), persist=True, agnostic_nms=True,
                                  tracker="bytetrack.yaml", device=a.device, half=a.half, verbose=False)[0]
            live = []
            if tr.boxes.id is not None:
                for b, c, i in zip(tr.boxes.xyxy.tolist(), tr.boxes.cls.tolist(), tr.boxes.id.tolist()):
                    box, vtype, tid = tuple(map(int, b)), VEHICLE_CLASSES[int(c)], int(i)
                    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
                    if tid not in self.tracks:
                        self.tracks[tid] = Track(tid, vtype, t, t, (cx, cy), (cx, cy))
                    trk = self.tracks[tid]
                    trk.last_t, trk.last_c = t, (cx, cy)
                    trk.types[vtype] = trk.types.get(vtype, 0) + 1
                    area = (box[2] - box[0]) * (box[3] - box[1])
                    if area > trk.vehicle_crop_area:
                        trk.vehicle_crop, trk.vehicle_crop_area = a._crop(frame, box, 0.02).copy(), area
                    live.append((box, trk))
            owned = []
            for pbox, pconf, i in a.detect_plates_on(frame, [b for b, _ in live]):
                own = _owner(pbox, live)  # a big box (bike + riders) can cover another vehicle's plate
                owned.append((pbox, pconf, own[1] if own else live[i][1]))
            texts = a.read_plates([a._crop(frame, p[0]) for p in owned])
        for (pbox, pconf, trk), (raw, oconf) in zip(owned, texts):
            parea = (pbox[2] - pbox[0]) * (pbox[3] - pbox[1])
            trk.voter.add(raw, oconf, parea, pconf)
            score = oconf * parea ** 0.5
            if score > trk.plate_crop_score:
                trk.plate_crop, trk.plate_crop_score = a._crop(frame, pbox, 0.15).copy(), score
        # annotate
        scale = out_size[0] / frame.shape[1]
        canvas = frame
        for box, trk in live:
            text = self.plate(trk)[0]
            label = f"{trk.kind} #{trk.id}" + (f"  {text}" if text else "")
            _draw_box(canvas, box, label, COLORS[trk.kind])
        for pbox, _, _ in owned:
            cv2.rectangle(canvas, pbox[:2], pbox[2:], COLORS["Plate"], max(2, int(3 / scale)))
        small = cv2.resize(canvas, out_size, interpolation=cv2.INTER_AREA)
        _hud(small, t, sum(1 for k in self.tracks.values() if self.plate(k)[0]))
        return small

    @staticmethod
    def plate(trk: Track) -> tuple[str | None, float]:
        """The track's plate if it is trusted enough to be logged, else (None, conf)."""
        text, conf = trk.voter.result()
        if text and not _trusted(text, conf, trk.voter.readings, trk.voter.det_conf):
            text = None
        return text, conf

    def finished(self, t: float, idle: float = 2.0) -> list[Track]:
        """Removes and returns tracks not seen for `idle` seconds (live cameras)."""
        gone = [k for k, trk in self.tracks.items() if t - trk.last_t > idle]
        return [self.tracks.pop(k) for k in gone]

    def record(self, trk: Track, crops_dir: Path, prefix: str = "") -> dict | None:
        text, conf = self.plate(trk)
        if trk.last_t - trk.first_t < 0.5 and not text:
            return None  # flicker
        rec = {"track": trk.id, "vehicle": trk.kind, "plate": text, "confidence": conf, "det_conf": round(trk.voter.det_conf, 3),
               "readings": trk.voter.readings, "t_first": round(trk.first_t, 2), "t_last": round(trk.last_t, 2),
               "direction": _direction(trk.first_c, trk.last_c), "plate_img": None, "vehicle_img": None}
        if trk.plate_crop is not None and text:
            p = crops_dir / f"{prefix}plate_{trk.id}.jpg"
            cv2.imwrite(str(p), _fit(trk.plate_crop, 360))
            rec["plate_img"] = p.name
        if trk.vehicle_crop is not None:
            p = crops_dir / f"{prefix}vehicle_{trk.id}.jpg"
            cv2.imwrite(str(p), _fit(trk.vehicle_crop, 360))
            rec["vehicle_img"] = p.name
        return rec


def _owner(pbox, vehicles):
    """The vehicle a plate belongs to. Boxes overlap in traffic (a bike with riders can cover the car behind),
    so among the boxes that contain the plate centre pick the one whose centre line the plate sits on."""
    cx, cy = (pbox[0] + pbox[2]) / 2, (pbox[1] + pbox[3]) / 2
    best = None
    for vbox, v in vehicles:
        if vbox[0] <= cx <= vbox[2] and vbox[1] <= cy <= vbox[3]:
            w, h = vbox[2] - vbox[0], vbox[3] - vbox[1]
            off_x = abs(cx - (vbox[0] + vbox[2]) / 2) / w
            rel_y = (cy - vbox[1]) / h  # plates sit in the lower part of a vehicle
            score = off_x + 0.5 * max(0.0, 0.4 - rel_y)
            if best is None or score < best[0]:
                best = (score, vbox, v)
    return (best[1], best[2]) if best else None


def _trusted(text: str, conf: float, readings: int, det_conf: float) -> bool:
    if det_conf < 0.5:
        return False
    _, valid = normalize(text)
    if not valid or len(text.replace("-", "")) < 5:
        return conf >= 0.8 and readings >= 3
    if readings < 2:
        return conf >= 0.85 and det_conf >= 0.75
    return conf >= 0.7 or (conf >= 0.45 and readings >= 3)


def _close(a: str, b: str) -> bool:
    """Same plate, allowing one wrong character and ignoring the small year digits that OCR often drops."""
    def core(p):
        parts = p.split("-")
        return parts[0] + parts[-1] if len(parts) == 3 else p.replace("-", "")
    a, b = core(a), core(b)
    if a == b:
        return True
    if len(a) != len(b) or len(a) < 5:
        return False
    return sum(x != y for x, y in zip(a, b)) <= 1


def merge_records(records: list[dict], crops_dir: Path, gap: float = 8.0) -> list[dict]:
    """One vehicle can be split into several tracks (camera shake, occlusion). Merge tracks that read the
    same plate within a few seconds; the reading with more support wins."""
    merged: list[dict] = []
    for r in records:
        target = None
        if r["plate"]:
            for m in reversed(merged):
                if m["plate"] and r["t_first"] - m["t_last"] <= gap and _close(r["plate"], m["plate"]):
                    target = m
                    break
        if target is None:
            merged.append(dict(r, _support={r["vehicle"]: r["readings"]}))
            continue
        target["_support"][r["vehicle"]] = target["_support"].get(r["vehicle"], 0) + r["readings"]
        if r["readings"] * r["confidence"] > target["readings"] * target["confidence"]:
            for k in ("plate", "plate_img"):
                target[k] = r[k]
        if r["vehicle_img"] and not target["vehicle_img"]:
            target["vehicle_img"] = r["vehicle_img"]
        target["confidence"] = max(target["confidence"], r["confidence"])
        target["readings"] += r["readings"]
        target["t_last"] = max(target["t_last"], r["t_last"])
    for m in merged:
        sup = m.pop("_support")
        m["vehicle"] = max(sup, key=lambda k: sup[k])
    return merged


def _iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union else 0.0


def _direction(c0, c1):
    dx, dy = c1[0] - c0[0], c1[1] - c0[1]
    if max(abs(dx), abs(dy)) < 40:
        return "stationary"
    if abs(dy) >= abs(dx):
        return "towards camera" if dy > 0 else "away from camera"
    return "left to right" if dx > 0 else "right to left"


def _fit(img, width):
    h, w = img.shape[:2]
    if w <= width:
        return img
    return cv2.resize(img, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)


def _draw_box(img, box, label, color, big=False):
    s = max(1.0, img.shape[1] / 1280)
    th = max(2, int(2 * s))
    cv2.rectangle(img, box[:2], box[2:], color, th)
    if not label:
        return
    fs = (0.9 if big else 0.6) * s
    (tw, tht), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, th)
    y = max(tht + 8, box[1])
    cv2.rectangle(img, (box[0], y - tht - int(10 * s)), (box[0] + tw + int(10 * s), y), color, -1)
    cv2.putText(img, label, (box[0] + int(5 * s), y - int(5 * s)), cv2.FONT_HERSHEY_SIMPLEX, fs, (15, 15, 15), th, cv2.LINE_AA)


def _hud(img, t, plates_read):
    txt = f"TwinStack ANPR   t={t:5.1f}s   plates read: {plates_read}"
    cv2.rectangle(img, (0, 0), (img.shape[1], 30), (20, 20, 20), -1)
    cv2.putText(img, txt, (10, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (230, 230, 230), 1, cv2.LINE_AA)
