#!/usr/bin/env python3
"""
Detect a specific YOLO class in an image downloaded from a URL.

Usage:
    python detect_class.py <image_url> <class_name> [--model yolo11m] [--conf 0.25]

Example:
    python detect_class.py https://example.com/room.jpg tv
"""

import argparse
import sys
import tempfile
import urllib.request
from pathlib import Path

try:
    from ultralytics import YOLO
except ImportError:
    print("Install ultralytics: pip install ultralytics", file=sys.stderr)
    sys.exit(1)


# COCO 80-class names — the default dataset YOLO11 is pretrained on
COCO_CLASSES = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
    "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana",
    "apple", "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza",
    "donut", "cake", "chair", "couch", "potted plant", "bed", "dining table",
    "toilet", "tv", "laptop", "mouse", "remote", "keyboard", "cell phone",
    "microwave", "oven", "toaster", "sink", "refrigerator", "book", "clock",
    "vase", "scissors", "teddy bear", "hair drier", "toothbrush",
]


def download_image(url: str) -> Path:
    suffix = Path(url.split("?")[0]).suffix or ".jpg"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    urllib.request.urlretrieve(url, tmp.name)
    return Path(tmp.name)


def detect(image_url: str, class_name: str, model_name: str, conf_threshold: float) -> dict:
    # Validate class name against known COCO classes
    if class_name not in COCO_CLASSES:
        close = [c for c in COCO_CLASSES if class_name in c or c in class_name]
        msg = f"Unknown class '{class_name}'."
        if close:
            msg += f" Did you mean: {close}?"
        else:
            msg += f" Supported classes: {COCO_CLASSES}"
        raise ValueError(msg)

    image_path = download_image(image_url)

    try:
        model = YOLO(model_name)

        # model.names maps class index -> name; build reverse lookup
        name_to_id = {v: k for k, v in model.names.items()}
        if class_name not in name_to_id:
            raise ValueError(
                f"Model '{model_name}' does not include class '{class_name}'. "
                f"Available: {sorted(model.names.values())}"
            )
        target_id = name_to_id[class_name]

        results = model(str(image_path), conf=conf_threshold, verbose=False)[0]

        detections = []
        for box in results.boxes:
            if int(box.cls.item()) == target_id:
                detections.append({
                    "confidence": round(float(box.conf.item()), 4),
                    "bbox_xyxy": [round(v, 1) for v in box.xyxy[0].tolist()],
                })

        detections.sort(key=lambda d: d["confidence"], reverse=True)

        return {
            "class": class_name,
            "found": len(detections) > 0,
            "count": len(detections),
            "best_confidence": detections[0]["confidence"] if detections else None,
            "detections": detections,
            "model": model_name,
            "image_path": str(image_path),
        }
    finally:
        image_path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="Detect a class in an image via YOLO11.")
    parser.add_argument("url", help="Image URL to download and analyse")
    parser.add_argument("class_name", help="YOLO class to look for (e.g. 'tv', 'person')")
    parser.add_argument(
        "--model",  # todo: try yolo26m
        default="yolo11m.pt",
        help="Model weights (default: yolo11m.pt). Options: yolo11n/s/m/l/x.pt",
    )
    parser.add_argument(
        "--conf",
        type=float,
        default=0.25,
        help="Confidence threshold 0..1 (default: 0.25)",
    )
    parser.add_argument("--list-classes", action="store_true", help="Print supported classes and exit")
    args = parser.parse_args()

    if args.list_classes:
        print("\n".join(sorted(COCO_CLASSES)))
        return

    try:
        result = detect(args.url, args.class_name, args.model, args.conf)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if result["found"]:
        print(f"✓ '{result['class']}' detected — {result['count']} instance(s)")
        print(f"  best confidence : {result['best_confidence']:.2%}")
        for i, d in enumerate(result["detections"], 1):
            print(f"  [{i}] conf={d['confidence']:.2%}  bbox={d['bbox_xyxy']}")
    else:
        print(f"✗ '{result['class']}' NOT found (conf threshold: {args.conf})")

    # Also return the dict so this module is importable
    return result


if __name__ == "__main__":
    main()
