from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path

from crawlin import run as crawl_run
from scrape2gis import get_firm_sites

logger = logging.getLogger(__name__)


def read_first_firms(csv_path: Path, limit: int) -> list[dict[str, str]]:
    firms: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            firm_id = (row.get("id") or "").strip()
            if not firm_id:
                continue
            firms.append(
                {
                    "id": firm_id,
                    "name": (row.get("name") or "").strip(),
                    "address_name": (row.get("address_name") or "").strip(),
                }
            )
            if len(firms) >= limit:
                break
    return firms


def collect_sites_for_firms(
    firms: list[dict[str, str]],
    city_code: str,
    max_sites_per_firm: int,
) -> list[str]:
    all_sites: list[str] = []
    seen_sites: set[str] = set()

    for index, firm in enumerate(firms, start=1):
        firm_id = firm["id"]
        logger.info(
            "Firm %s/%s: id=%s name=%s",
            index,
            len(firms),
            firm_id,
            firm["name"] or "-",
        )
        try:
            sites = get_firm_sites(city_code=city_code, firm_id=firm_id)
        except Exception as exc:
            logger.exception("Failed to resolve site for firm_id=%s: %s", firm_id, exc)
            continue

        if not sites:
            logger.info("No sites found for firm_id=%s", firm_id)
            continue

        picked = sites[: max(1, max_sites_per_firm)]
        logger.info("Found %s site(s), using %s: %s", len(sites), len(picked), picked)
        for site in picked:
            if site in seen_sites:
                continue
            seen_sites.add(site)
            all_sites.append(site)

    return all_sites


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Take first N firms from dublgis CSV and run crawlin.py for contacts/prices."
    )
    parser.add_argument("--csv", default="dublgis_items.csv", help="Path to 2GIS CSV file.")
    parser.add_argument("--limit", type=int, default=5, help="How many first rows to process.")
    parser.add_argument(
        "--city-code",
        default="moscow",
        help="2GIS city code used in firm cards URL, e.g. moscow, spb.",
    )
    parser.add_argument(
        "--max-sites-per-firm",
        type=int,
        default=1,
        help="How many websites to crawl for each firm.",
    )
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages per website.")
    parser.add_argument("--output-dir", default="output", help="Directory for crawler CSV output.")
    parser.add_argument("--verbose", action="store_true", help="Verbose crawling logs.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    firms = read_first_firms(csv_path=csv_path, limit=max(1, args.limit))
    if not firms:
        raise SystemExit("No firms with id found in CSV")
    logger.info("Loaded %s firm(s) from %s", len(firms), csv_path)

    sites = collect_sites_for_firms(
        firms=firms,
        city_code=args.city_code,
        max_sites_per_firm=max(1, args.max_sites_per_firm),
    )
    if not sites:
        raise SystemExit("No websites resolved from selected firms")

    logger.info("Starting crawler for %s unique site(s)", len(sites))
    asyncio.run(
        crawl_run(
            urls=sites,
            max_pages=max(1, args.max_pages),
            output_dir=Path(args.output_dir),
            verbose=args.verbose,
        )
    )
    logger.info("Finished")


if __name__ == "__main__":
    main()
