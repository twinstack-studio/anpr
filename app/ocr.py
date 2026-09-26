"""Plate OCR with our fine-tuned CCT model (Keras 3 on the PyTorch backend, runs on the GPU).

The model was fine-tuned from fast-plate-ocr's cct_s_v2_global on hand-checked Pakistani plates
(training/ocr/train_ocr.py). Its ONNX export fails on the torch backend, so it runs in Keras directly.
"""
import os

os.environ.setdefault("KERAS_BACKEND", "torch")

from pathlib import Path  # noqa: E402

import warnings  # noqa: E402

import cv2  # noqa: E402
import keras  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

import fast_plate_ocr.train.model.layers  # noqa: E402,F401  (registers the custom layers)

warnings.filterwarnings("ignore", message=".*structure of `inputs`.*")

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_"
H, W = 64, 128


class PlateOCR:
    def __init__(self, model_path: Path):
        full = keras.models.load_model(model_path, compile=False)
        self.model = keras.Model(full.inputs, full.outputs[0])

    def run(self, crops_bgr: list[np.ndarray]) -> list[tuple[str, float]]:
        """Returns (text, mean character confidence) for each BGR plate crop."""
        if not crops_bgr:
            return []
        x = np.stack([cv2.resize(cv2.cvtColor(c, cv2.COLOR_BGR2RGB), (W, H), interpolation=cv2.INTER_LINEAR) for c in crops_bgr])
        with torch.no_grad():
            probs = keras.ops.convert_to_numpy(self.model(x.astype(np.float32), training=False))
        out = []
        for p in probs:
            idx = p.argmax(-1)
            text = "".join(ALPHABET[i] for i in idx)
            n = len(text.rstrip("_")) or 1
            out.append((text.replace("_", ""), float(p.max(-1)[:n].mean())))
        return out
