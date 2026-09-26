"""Render review sheets: numbered plate crops with the current label under each one.
usage: review_grid.py <labels.csv> <out_dir> [split]
"""
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
COLS, ROWS, TW, TH = 5, 8, 300, 100


def main():
    rows = list(csv.DictReader(open(sys.argv[1])))
    start = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    for i, r in enumerate(rows):
        r["idx"] = i
    rows = rows[start:]
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)
    per = COLS * ROWS
    for s in range(0, len(rows), per):
        sheet = np.full((ROWS * (TH + 34), COLS * TW, 3), 30, np.uint8)
        for i, r in enumerate(rows[s : s + per]):
            im = cv2.imread(str(HERE / r["image_path"]))
            h, w = im.shape[:2]
            sc = min(TW / w, TH / h)
            im = cv2.resize(im, (max(1, int(w * sc)), max(1, int(h * sc))))
            y, x = (i // COLS) * (TH + 34), (i % COLS) * TW
            sheet[y : y + im.shape[0], x : x + im.shape[1]] = im
            cv2.putText(sheet, f"{r['idx']}: {r['plate_text']}", (x + 4, y + TH + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (0, 255, 255), 2)
        cv2.imwrite(str(out / f"t_{rows[s]['idx']:04d}.jpg"), sheet, [cv2.IMWRITE_JPEG_QUALITY, 88])


if __name__ == "__main__":
    main()
