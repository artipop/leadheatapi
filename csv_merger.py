from __future__ import annotations

import argparse
import csv
from pathlib import Path


def collect_input_files(input_dir: Path, kind: str, pattern: str | None) -> list[Path]:
    if pattern:
        return sorted(path for path in input_dir.glob(pattern) if path.is_file())
    return sorted(path for path in input_dir.glob(f"*/{kind}.csv") if path.is_file())


def collect_fieldnames(files: list[Path]) -> list[str]:
    fieldnames: list[str] = []
    seen: set[str] = set()
    for file_path in files:
        with file_path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            for name in reader.fieldnames or []:
                if name in seen:
                    continue
                seen.add(name)
                fieldnames.append(name)
    return fieldnames


def merge_csv_files(files: list[Path], output_file: Path) -> int:
    fieldnames = collect_fieldnames(files)
    if not fieldnames:
        raise SystemExit("No CSV columns found in selected files.")

    output_file.parent.mkdir(parents=True, exist_ok=True)
    rows_written = 0
    with output_file.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fieldnames)
        writer.writeheader()
        for file_path in files:
            with file_path.open("r", encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                for row in reader:
                    writer.writerow({name: row.get(name, "") for name in fieldnames})
                    rows_written += 1
    return rows_written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Merge per-site CSV files into one combined CSV (pricing or specialists)."
    )
    parser.add_argument(
        "--kind",
        choices=["pricing", "specialists"],
        default="pricing",
        help="CSV type to merge when --pattern is not specified.",
    )
    parser.add_argument(
        "--input-dir",
        default="output",
        help="Root directory with site subfolders.",
    )
    parser.add_argument(
        "--pattern",
        help="Optional glob pattern relative to --input-dir, e.g. '*/specialists.csv'.",
    )
    parser.add_argument(
        "--output",
        help="Output file path. Defaults to '<input-dir>/merged_<kind>.csv'.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()

    input_dir = Path(args.input_dir)
    if not input_dir.exists():
        raise SystemExit(f"Input directory not found: {input_dir}")

    output_path = Path(args.output) if args.output else input_dir / f"merged_{args.kind}.csv"
    files = collect_input_files(input_dir=input_dir, kind=args.kind, pattern=args.pattern)
    if not files:
        raise SystemExit("No input CSV files found for merge.")

    rows = merge_csv_files(files=files, output_file=output_path)
    print(f"Merged {len(files)} file(s), {rows} row(s) -> {output_path}")


if __name__ == "__main__":
    main()
