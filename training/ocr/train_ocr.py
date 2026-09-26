"""Fine-tune the CCT-S global plate OCR model on hand-checked Pakistani plate crops.

Starts from fast-plate-ocr's cct_s_v2_global weights, trains the plate head + backbone with heavy
augmentation that mimics CCTV footage (blur, low resolution, tilt, glare, JPEG), keeps the checkpoint
with the best exact-match accuracy on the validation plates (vehicles never seen in training).

Run: KERAS_BACKEND=torch python train_ocr.py
Output: training/ocr/<TAG>.keras (PUBLISH=1 copies it to models/pk_plate_ocr.keras)
"""
import csv
import math
import os
import random
import shutil
from pathlib import Path

os.environ.setdefault("KERAS_BACKEND", "torch")

import cv2
import keras
import numpy as np

import fast_plate_ocr.train.model.layers  # noqa: F401  (registers the custom layers)

HERE = Path(__file__).resolve().parent
MODELS = HERE.parent.parent / "models"
ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
SLOTS, H, W = 10, 64, 128
EPOCHS = int(os.environ.get("EPOCHS", 12))  # 12 epochs at 1e-4 gave the best held-out accuracy
BATCH = 64
LR = float(os.environ.get("LR", 1e-4))
FREEZE = float(os.environ.get("FREEZE", 0))  # fraction of layers (from the input side) kept frozen
TAG = os.environ.get("TAG", "best")
SEED = 7


def load_split(name):
    rows = list(csv.DictReader(open(HERE / f"{name}.csv")))
    ims = [cv2.cvtColor(cv2.imread(str(HERE / r["image_path"])), cv2.COLOR_BGR2RGB) for r in rows]
    return ims, [r["plate_text"] for r in rows]


def encode(text):
    y = np.zeros((SLOTS, len(ALPHABET)), np.float32)
    for i, ch in enumerate(text.ljust(SLOTS, "_")[:SLOTS]):
        y[i, ALPHABET.index(ch)] = 1
    return y


def decode(probs):
    return "".join(ALPHABET[i] for i in probs.argmax(-1)).replace("_", "")


# ---------------- augmentation (CCTV-like) ----------------
def augment(img, rng):
    h, w = img.shape[:2]
    # geometric: small rotation, shear, perspective, loose/tight crop
    if rng.random() < 0.85:
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        j = 0.08
        dst = src + rng.uniform(-j, j, src.shape).astype(np.float32) * np.float32([w, h])
        ang = rng.uniform(-9, 9)
        m_rot = cv2.getRotationMatrix2D((w / 2, h / 2), ang, rng.uniform(0.9, 1.08))
        dst = cv2.transform(dst[None], m_rot)[0]
        m = cv2.getPerspectiveTransform(src, dst.astype(np.float32))
        img = cv2.warpPerspective(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE)
    if rng.random() < 0.4:  # crop jitter
        dx, dy = int(w * rng.uniform(0, 0.06)), int(h * rng.uniform(0, 0.08))
        x0, y0 = rng.integers(0, dx + 1), rng.integers(0, dy + 1)
        img = img[y0 : h - (dy - y0), x0 : w - (dx - x0)]
    # low resolution (far away plate in CCTV)
    if rng.random() < 0.45:
        tw = int(rng.uniform(38, 90))
        th = max(8, int(img.shape[0] * tw / img.shape[1]))
        img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
    # blur: motion or gaussian
    r = rng.random()
    if r < 0.25:
        k = int(rng.integers(3, 6))
        kern = np.zeros((k, k), np.float32)
        kern[k // 2, :] = 1 / k
        kern = cv2.warpAffine(kern, cv2.getRotationMatrix2D((k / 2, k / 2), rng.uniform(0, 180), 1), (k, k))
        img = cv2.filter2D(img, -1, kern / max(kern.sum(), 1e-6))
    elif r < 0.45:
        img = cv2.GaussianBlur(img, (0, 0), rng.uniform(0.5, 1.1))
    img = cv2.resize(img, (W, H), interpolation=cv2.INTER_LINEAR).astype(np.float32)
    # photometric
    if rng.random() < 0.8:
        img = img * rng.uniform(0.7, 1.3) + rng.uniform(-25, 25)
    if rng.random() < 0.3:
        img = img * rng.uniform(0.85, 1.15, (1, 1, 3))
    if rng.random() < 0.25:  # glare / shadow band
        x = np.linspace(0, 1, W)[None, :, None]
        c, s = rng.uniform(0, 1), rng.uniform(0.08, 0.3)
        img = img + rng.uniform(-50, 50) * np.exp(-((x - c) ** 2) / (2 * s**2))
    if rng.random() < 0.15:
        img = np.repeat(img.mean(-1, keepdims=True), 3, -1)
    if rng.random() < 0.35:
        img = img + rng.normal(0, rng.uniform(2, 10), img.shape)
    img = np.clip(img, 0, 255).astype(np.uint8)
    if rng.random() < 0.4:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(rng.integers(30, 75))])
        img = cv2.imdecode(buf, cv2.IMREAD_UNCHANGED)
    return img


class Plates(keras.utils.PyDataset):
    def __init__(self, ims, labels, train, **kw):
        super().__init__(**kw)
        self.ims, self.y = ims, np.stack([encode(t) for t in labels])
        self.train = train
        self.idx = np.arange(len(ims))
        self.rng = np.random.default_rng(SEED)
        self.on_epoch_end()

    def __len__(self):
        return math.ceil(len(self.ims) / BATCH)

    def on_epoch_end(self):
        if self.train:
            self.rng.shuffle(self.idx)

    def __getitem__(self, b):
        ids = self.idx[b * BATCH : (b + 1) * BATCH]
        rng = np.random.default_rng(self.rng.integers(1 << 31))
        x = np.stack([augment(self.ims[i], rng) if self.train else cv2.resize(self.ims[i], (W, H)) for i in ids])
        return x.astype(np.float32), self.y[ids]


class ExactMatch(keras.callbacks.Callback):
    def __init__(self, ims, labels, full_model):
        super().__init__()
        self.x = np.stack([cv2.resize(i, (W, H)) for i in ims]).astype(np.float32)
        self.labels, self.full, self.best = labels, full_model, -1.0

    def on_epoch_end(self, epoch, logs=None):
        pred = self.model.predict(self.x, batch_size=128, verbose=0)
        acc = float(np.mean([decode(p) == t for p, t in zip(pred, self.labels)]))
        logs = logs if logs is not None else {}
        logs["val_exact"] = acc
        mark = ""
        if acc > self.best:
            self.best = acc
            self.full.save(HERE / f"{TAG}.keras")
            mark = "  <- best"
        print(f"epoch {epoch + 1}: loss {logs.get('loss', 0):.4f}  val exact-match {acc * 100:.1f}%{mark}", flush=True)


def main():
    random.seed(SEED)
    keras.utils.set_random_seed(SEED)
    tr_x, tr_y = load_split("train")
    va_x, va_y = load_split("val")
    full = keras.models.load_model(HERE / "cct_s_v2_global.keras", compile=False)
    plate_model = keras.Model(full.inputs, full.outputs[0])  # shares weights with `full`
    n_freeze = int(len(full.layers) * FREEZE)
    for layer in full.layers[:n_freeze]:
        layer.trainable = False
    print(f"epochs {EPOCHS} lr {LR} frozen layers {n_freeze}/{len(full.layers)}", flush=True)

    steps = EPOCHS * math.ceil(len(tr_x) / BATCH)
    sched = keras.optimizers.schedules.CosineDecay(LR * 0.1, steps, alpha=0.02, warmup_target=LR, warmup_steps=int(steps * 0.05))
    plate_model.compile(
        optimizer=keras.optimizers.AdamW(sched, weight_decay=0.02, clipnorm=1.0),
        loss=keras.losses.CategoricalCrossentropy(label_smoothing=0.05),
    )
    cb = ExactMatch(va_x, va_y, full)
    cb.set_model(plate_model)
    cb.on_epoch_end(-1)  # baseline before fine-tuning
    plate_model.fit(Plates(tr_x, tr_y, True, workers=6, use_multiprocessing=False), epochs=EPOCHS, callbacks=[cb], verbose=0)
    print(f"best val exact-match {cb.best * 100:.1f}%")

    if os.environ.get("PUBLISH"):
        MODELS.mkdir(exist_ok=True)
        shutil.copy(HERE / f"{TAG}.keras", MODELS / "pk_plate_ocr.keras")
        print("copied to", MODELS / "pk_plate_ocr.keras")

if __name__ == "__main__":
    main()
