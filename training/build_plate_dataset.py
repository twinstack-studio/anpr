"""Merge the downloaded Pakistani plate datasets into one YOLO dataset with a single class: plate.

Output: datasets/pk_plates/{train,val}/{images,labels} + data.yaml
Duplicate images (same file hash) across sources are kept only once.
"""
import hashlib
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "datasets"
OUT = ROOT / "pk_plates"

# (images dir glob root, class ids that mean "plate" in that source)
SOURCES = [
    ("pakistani-cars-dataset/fyp.v1i.yolov8", {0}),
    ("pakistani-number-plate1/Pakistani License Plates.v2i.yolov8", {1}),
    ("pakistani-number-plate1/archive (4)", {0}),
    ("pakistani-number-plates-yolov11/PAKISTANI-NUMBER-PLATE-YOLO", {0}),
    ("pakistani-vehicle-number-plate-anpr-yolo", {0}),
]
IMG_EXT = {".jpg", ".jpeg", ".png"}


def label_for(img: Path) -> Path | None:
    # Label lives in a sibling "labels" dir somewhere up the tree with the same stem.
    for parent in img.parents:
        for cand in parent.glob("labels*"):
            if cand.is_dir():
                for p in [cand / (img.stem + ".txt"), cand / "labels" / (img.stem + ".txt")]:
                    if p.exists():
                        return p
        if parent == ROOT:
            break
    return None


def main():
    random.seed(0)
    if OUT.exists():
        shutil.rmtree(OUT)
    seen, items = set(), []
    for src, plate_ids in SOURCES:
        n = 0
        for img in sorted((ROOT / src).rglob("*")):
            if img.suffix.lower() not in IMG_EXT:
                continue
            h = hashlib.md5(img.read_bytes()).hexdigest()
            if h in seen:
                continue
            lab = label_for(img)
            if lab is None:
                continue
            lines = []
            for ln in lab.read_text().splitlines():
                parts = ln.split()
                if len(parts) == 5 and int(float(parts[0])) in plate_ids:
                    lines.append("0 " + " ".join(parts[1:]))
            if not lines:
                continue
            seen.add(h)
            items.append((img, lines, h))
            n += 1
        print(f"{src}: {n} images")

    random.shuffle(items)
    n_val = max(1, int(len(items) * 0.12))
    for i, (img, lines, h) in enumerate(items):
        split = "val" if i < n_val else "train"
        (OUT / split / "images").mkdir(parents=True, exist_ok=True)
        (OUT / split / "labels").mkdir(parents=True, exist_ok=True)
        shutil.copy(img, OUT / split / "images" / f"{h}{img.suffix.lower()}")
        (OUT / split / "labels" / f"{h}.txt").write_text("\n".join(lines) + "\n")

    (OUT / "data.yaml").write_text(
        f"path: {OUT}\ntrain: train/images\nval: val/images\nnames:\n  0: plate\n"
    )
    print(f"total {len(items)} images ({len(items) - n_val} train, {n_val} val) -> {OUT}")


if __name__ == "__main__":
    main()
