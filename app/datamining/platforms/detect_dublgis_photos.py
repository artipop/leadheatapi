#!/usr/bin/env python3
"""
Download photos from a 2GIS firm page and detect a YOLO class in each.

Usage:
    python detect_dublgis_photos.py <2gis_firm_url> <class_name> [--model yolo11m.pt] [--conf 0.25] [--max-photos N]

Example:
    python detect_dublgis_photos.py https://2gis.ru/novosibirsk/firm/70000001017590406 tv
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from app.datamining.platforms.dublgis_scraper import get_firm_photos_by_url
from app.datamining.detect_class import detect, COCO_CLASSES


def main():
    parser = argparse.ArgumentParser(description="Detect a YOLO class in 2GIS firm photos.")
    parser.add_argument("firm_url", help="2GIS firm page URL")
    parser.add_argument("class_name", help="YOLO class to detect (e.g. 'tv', 'person')")
    parser.add_argument("--model", default="yolo11m.pt", help="YOLO model weights")
    parser.add_argument("--conf", type=float, default=0.25, help="Confidence threshold")
    parser.add_argument("--max-photos", type=int, default=None, help="Limit photos to check (default: all)")
    parser.add_argument("--mode", choices=["api", "playwright"], default="api",
                        help="Photo fetch mode: api (default) or playwright (no API key)")
    parser.add_argument("--list-classes", action="store_true", help="Print supported classes and exit")
    args = parser.parse_args()

    if args.list_classes:
        print("\n".join(sorted(COCO_CLASSES)))
        return

    if args.class_name not in COCO_CLASSES:
        close = [c for c in COCO_CLASSES if args.class_name in c or c in args.class_name]
        print(f"Error: unknown class '{args.class_name}'.", file=sys.stderr)
        if close:
            print(f"Did you mean: {close}?", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching photos from: {args.firm_url}")
    photos = get_firm_photos_by_url(args.firm_url, max_photos=args.max_photos, mode=args.mode)

    if not photos:
        print("No photos found on that page.", file=sys.stderr)
        sys.exit(1)

    print(f"Found {len(photos)} photo(s). Scanning for '{args.class_name}'...\n")

    found_total = 0
    for i, url in enumerate(photos, 1):
        print(f"[{i}/{len(photos)}] {url}")
        try:
            result = detect(url, args.class_name, args.model, args.conf)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        if result["found"]:
            found_total += 1
            print(f"  ✓ DETECTED — {result['count']} instance(s), best conf={result['best_confidence']:.2%}")
            for d in result["detections"]:
                print(f"    conf={d['confidence']:.2%}  bbox={d['bbox_xyxy']}")
        else:
            print(f"  ✗ not found")

    print(f"\n--- Summary ---")
    print(f"Photos scanned : {len(photos)}")
    print(f"Photos with '{args.class_name}': {found_total}")


if __name__ == "__main__":
    main()
