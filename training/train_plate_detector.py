"""Fine-tune YOLO11 on the merged Pakistani plate dataset.

Result: training/runs/plate-yolo11s/weights/best.pt, copied to models/plate_detector.pt
"""
import shutil
from pathlib import Path

from ultralytics import YOLO

HERE = Path(__file__).resolve().parent
DATA = HERE.parent / "datasets" / "pk_plates" / "data.yaml"
MODELS = HERE.parent / "models"


def main():
    model = YOLO("yolo11s.pt")
    model.train(
        data=str(DATA),
        epochs=120,
        patience=30,
        imgsz=960,
        batch=16,
        device=0,
        workers=6,
        project=str(HERE / "runs"),
        name="plate-yolo11s",
        exist_ok=True,
        degrees=5,
        mixup=0.1,
        close_mosaic=15,
        plots=True,
    )
    MODELS.mkdir(exist_ok=True)
    shutil.copy(HERE / "runs" / "plate-yolo11s" / "weights" / "best.pt", MODELS / "plate_detector.pt")


if __name__ == "__main__":
    main()
