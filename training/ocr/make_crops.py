"""Cut every labelled plate out of the merged dataset and pre-label it with the global OCR model.

Output: training/ocr/crops/<name>.png and training/ocr/prelabels.csv (image_path,plate_text,conf)
The pre-labels are then checked by eye (review grids) and fixed before fine-tuning.
"""
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
DS = HERE.parent.parent / "datasets" / "pk_plates"
OUT = HERE / "crops"
sys.path.insert(0, str(HERE.parent.parent))


def main():
    from fast_plate_ocr import LicensePlateRecognizer

    ocr = LicensePlateRecognizer("cct-s-v2-global-model", device="cpu")
    OUT.mkdir(exist_ok=True)
    rows = []
    for split in ("train", "val"):
        for img_path in sorted((DS / split / "images").iterdir()):
            lab = DS / split / "labels" / (img_path.stem + ".txt")
            img = cv2.imread(str(img_path))
            if img is None or not lab.exists():
                continue
            h, w = img.shape[:2]
            for k, line in enumerate(lab.read_text().split("\n")):
                parts = line.split()
                if len(parts) != 5:
                    continue
                cx, cy, bw, bh = (float(x) for x in parts[1:])
                x1, y1 = int((cx - bw / 2 * 1.08) * w), int((cy - bh / 2 * 1.1) * h)
                x2, y2 = int((cx + bw / 2 * 1.08) * w), int((cy + bh / 2 * 1.1) * h)
                crop = img[max(0, y1) : min(h, y2), max(0, x1) : min(w, x2)]
                if crop.size == 0 or crop.shape[1] < 24 or crop.shape[0] < 10:
                    continue
                if crop.shape[1] > 400:
                    crop = cv2.resize(crop, (400, int(crop.shape[0] * 400 / crop.shape[1])), interpolation=cv2.INTER_AREA)
                name = f"{split}_{img_path.stem[:10]}_{k}.png"
                cv2.imwrite(str(OUT / name), crop)
                pred = ocr.run(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB), return_confidence=True)[0]
                text = pred.plate.replace("_", "")
                conf = float(np.asarray(pred.char_probs)[: max(1, len(text))].mean())
                rows.append((f"crops/{name}", text, round(conf, 3), split))
    with open(HERE / "prelabels.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["image_path", "plate_text", "conf", "split"])
        wr.writerows(rows)
    print(len(rows), "crops")


if __name__ == "__main__":
    main()
