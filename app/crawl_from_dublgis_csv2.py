from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from crawlin import build_browser_config
from crawlin import build_run_config
from crawlin import crawl_site_pages
from crawlin import create_crawler
from crawlin import domain_slug
from crawlin import extract_prices
from crawlin import extract_specialists
from crawlin import write_pricing_csv
from crawlin import write_specialists_csv
from scrape2gis import get_firm_sites

logger = logging.getLogger(__name__)


def is_hh_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host == "hh.ru" or host.endswith(".hh.ru")


def is_hh_employer_url(url: str) -> bool:
    return (urlsplit(url).path or "").lower().startswith("/employer")


def normalize_site_url_for_crawl(url: str) -> Optional[str]:
    parsed = urlsplit(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    port = parsed.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    # query/fragment intentionally dropped: dedupe tracking URLs and UTM labels
    return urlunsplit((scheme, netloc, path, "", ""))


def site_host_key(url: str) -> str:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower()


def prefer_site_url(current: str, candidate: str) -> str:
    current_parsed = urlsplit(current)
    candidate_parsed = urlsplit(candidate)

    def score(parsed) -> tuple[int, int, int, int]:
        # Prefer https, then root path, then no explicit port, then shorter path.
        return (
            1 if parsed.scheme == "https" else 0,
            1 if (parsed.path or "/") == "/" else 0,
            1 if parsed.port is None else 0,
            -(len(parsed.path or "/")),
        )

    return candidate if score(candidate_parsed) > score(current_parsed) else current


def write_hh_links_csv(records: list[dict[str, str]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "firm_id",
                "company_name",
                "address_name",
                "firm_card_url",
                "hh_url",
                "is_employer_link",
            ],
        )
        writer.writeheader()
        for record in records:
            writer.writerow(record)


def read_firms(csv_path: Path, limit: Optional[int]) -> list[dict[str, str]]:
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
            if limit is not None and len(firms) >= limit:
                break
    return firms


def collect_sites_for_firms(
    firms: list[dict[str, str]],
    city_code: str,
    max_sites_per_firm: int,
) -> tuple[list[dict[str, str]], list[str], list[dict[str, str]]]:
    site_tasks: list[dict[str, str]] = []
    failed_attempts: list[str] = []
    hh_records: list[dict[str, str]] = []

    seen_hh_records: set[tuple[str, str]] = set()
    seen_hosts_global: set[str] = set()

    for index, firm in enumerate(firms, start=1):
        firm_id = firm["id"]
        firm_card_url = f"https://2gis.ru/{city_code}/firm/{firm_id}"
        logger.info("Firm %s/%s: id=%s name=%s", index, len(firms), firm_id, firm["name"] or "-")
        try:
            sites = get_firm_sites(city_code=city_code, firm_id=firm_id)
        except Exception as exc:
            logger.exception("Failed to resolve site for firm_id=%s: %s", firm_id, exc)
            failed_attempts.append(firm_card_url)
            continue

        if not sites:
            logger.info("No sites found for firm_id=%s", firm_id)
            failed_attempts.append(firm_card_url)
            continue

        selected_per_host: dict[str, str] = {}
        for raw_site in sites:
            if is_hh_url(raw_site):
                hh_key = (firm_id, raw_site)
                if hh_key not in seen_hh_records:
                    seen_hh_records.add(hh_key)
                    hh_records.append(
                        {
                            "firm_id": firm_id,
                            "company_name": firm["name"],
                            "address_name": firm["address_name"],
                            "firm_card_url": firm_card_url,
                            "hh_url": raw_site,
                            "is_employer_link": "1" if is_hh_employer_url(raw_site) else "0",
                        }
                    )
                continue

            normalized = normalize_site_url_for_crawl(raw_site)
            if not normalized:
                continue
            host_key = site_host_key(normalized)
            if not host_key:
                continue

            current = selected_per_host.get(host_key)
            selected_per_host[host_key] = normalized if current is None else prefer_site_url(current, normalized)

        picked_sites = list(selected_per_host.values())[: max(1, max_sites_per_firm)]
        logger.info(
            "Found %s site(s): non_hh_hosts=%s hh=%s, using %s: %s",
            len(sites),
            len(selected_per_host),
            len(sites) - len(selected_per_host),
            len(picked_sites),
            picked_sites,
        )
        if not picked_sites:
            failed_attempts.append(firm_card_url)
            continue

        for site in picked_sites:
            host_key = site_host_key(site)
            if host_key in seen_hosts_global:
                logger.info("Skip duplicate host across firms: %s", host_key)
                continue
            seen_hosts_global.add(host_key)
            site_tasks.append(
                {
                    "firm_id": firm_id,
                    "company_name": firm["name"],
                    "address_name": firm["address_name"],
                    "firm_card_url": firm_card_url,
                    "site_url": site,
                }
            )

    return site_tasks, failed_attempts, hh_records


async def crawl_sites(
    site_tasks: list[dict[str, str]],
    max_pages: int,
    output_dir: Path,
    verbose: bool,
) -> list[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    browser_config = build_browser_config()
    run_config = build_run_config()
    failed_sites: list[str] = []

    async with create_crawler(browser_config) as crawler:
        for index, task in enumerate(site_tasks, start=1):
            site = task["site_url"]
            logger.info("Crawling site %s/%s: %s", index, len(site_tasks), site)
            try:
                pages = await crawl_site_pages(
                    crawler=crawler,
                    start_url=site,
                    max_pages=max_pages,
                    run_config=run_config,
                    verbose=verbose,
                )
            except Exception as exc:
                logger.exception("Crawler failed for site=%s: %s", site, exc)
                failed_sites.append(site)
                continue

            if not pages:
                logger.warning("No pages collected for site=%s", site)
                failed_sites.append(site)
                continue

            domain = domain_slug(site)
            prices = extract_prices(pages, domain)
            specialists = extract_specialists(pages, domain)
            site_dir = output_dir / domain
            site_dir.mkdir(parents=True, exist_ok=True)
            write_pricing_csv(site_dir / "pricing.csv", prices)
            write_specialists_csv(site_dir / "specialists.csv", specialists)
            logger.info(
                "Done %s: pages=%s prices=%s specialists=%s -> %s",
                site,
                len(pages),
                len(prices),
                len(specialists),
                site_dir,
            )

    return failed_sites


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read 2GIS branches CSV, resolve websites and run crawlin.py per site."
    )
    parser.add_argument("--csv", default="dublgis_items.csv", help="Path to 2GIS CSV file.")
    parser.add_argument("--limit", type=int, help="How many first rows to process (default: all).")
    parser.add_argument("--city-code", default="moscow", help="2GIS city code for firm card URL.")
    parser.add_argument(
        "--max-sites-per-firm",
        type=int,
        default=3,
        help="How many normalized non-hh websites to crawl for each firm.",
    )
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages per website.")
    parser.add_argument("--output-dir", default="output", help="Directory for per-site CSV output.")
    parser.add_argument(
        "--hh-links-csv",
        default="output/hh_links_review.csv",
        help="CSV path for collected hh.ru links (manual review).",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose crawling logs.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    limit = max(1, args.limit) if args.limit is not None else None
    firms = read_firms(csv_path=csv_path, limit=limit)
    if not firms:
        raise SystemExit("No firms with id found in CSV")

    logger.info("Loaded %s firm(s) from %s", len(firms), csv_path)
    site_tasks, failed_firm_cards, hh_records = collect_sites_for_firms(
        firms=firms,
        city_code=args.city_code,
        max_sites_per_firm=max(1, args.max_sites_per_firm),
    )

    hh_links_csv_path = Path(args.hh_links_csv)
    write_hh_links_csv(hh_records, hh_links_csv_path)
    logger.info("Saved hh.ru links for review: %s (%s rows)", hh_links_csv_path, len(hh_records))

    if not site_tasks:
        raise SystemExit("No websites resolved from selected firms")

    logger.info("Starting crawler for %s site(s)", len(site_tasks))
    failed_crawl_sites = asyncio.run(
        crawl_sites(
            site_tasks=site_tasks,
            max_pages=max(1, args.max_pages),
            output_dir=Path(args.output_dir),
            verbose=args.verbose,
        )
    )

    failed_total: list[str] = []
    seen_failed: set[str] = set()
    for url in failed_firm_cards + failed_crawl_sites:
        if url in seen_failed:
            continue
        seen_failed.add(url)
        failed_total.append(url)

    if failed_total:
        print("\nFailed attempts:")
        for url in failed_total:
            print(url)
    else:
        print("\nFailed attempts: none")


if __name__ == "__main__":
    main()
