from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

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


def normalize_site_url(url: str) -> Optional[str]:
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    port = parsed.port
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    scheme = "https" if parsed.scheme in {"http", "https"} and port in {None, 80, 443} else parsed.scheme
    return urlunsplit((scheme, netloc, path, "", ""))


def is_hh_host(host: str) -> bool:
    value = host.lower()
    if value.startswith("www."):
        value = value[4:]
    return value == "hh.ru" or value.endswith(".hh.ru")


def is_hh_url(url: str) -> bool:
    return is_hh_host(urlsplit(url).hostname or "")


def normalize_hh_employer_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = urlsplit(resolved)
    host = (parsed.hostname or "").lower()
    if not is_hh_host(host):
        return None

    path = (parsed.path or "").rstrip("/")
    if not path.startswith("/employer"):
        return None

    if host.startswith("www."):
        host = host[4:]
    query = parsed.query
    return urlunsplit(("https", host, path, query, ""))


def extract_hh_employer_links(pages) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    for page in pages:
        soup = BeautifulSoup(page.html or "", "html.parser")
        for tag in soup.find_all("a", href=True):
            href = str(tag.get("href", "")).strip()
            if not href:
                continue
            resolved, _ = urldefrag(urljoin(page.url, href))
            normalized = normalize_hh_employer_url(resolved)
            if not normalized:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            found.append(normalized)
    return found


def write_hh_links_csv(rows: list[dict[str, str]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "firm_id",
                "company_name",
                "address_name",
                "site_url",
                "hh_employer_url",
                "source",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def read_firms(
    csv_path: Path,
    limit: Optional[int],
    start_row: int,
    end_row: Optional[int],
) -> list[dict[str, str]]:
    firms: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row_index, row in enumerate(reader, start=1):
            if row_index < start_row:
                continue
            if end_row is not None and row_index > end_row:
                break

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


async def run_streaming(
    firms: list[dict[str, str]],
    city_code: str,
    max_sites_per_firm: int,
    max_pages: int,
    output_dir: Path,
    hh_links_csv: Path,
    verbose: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    browser_config = build_browser_config()
    run_config = build_run_config()

    failed_attempts: list[str] = []
    hh_rows: list[dict[str, str]] = []
    seen_hh: set[tuple[str, str]] = set()

    async with create_crawler(browser_config) as crawler:
        for index, firm in enumerate(firms, start=1):
            firm_id = firm["id"]
            company_name = firm["name"]
            address_name = firm["address_name"]

            logger.info("Firm %s/%s: id=%s name=%s", index, len(firms), firm_id, company_name or "-")

            try:
                sites = get_firm_sites(city_code=city_code, firm_id=firm_id)
            except Exception as exc:
                logger.exception("Failed to resolve sites for firm_id=%s: %s", firm_id, exc)
                failed_attempts.append(f"https://2gis.ru/{city_code}/firm/{firm_id}")
                continue

            if not sites:
                logger.info("No sites found for firm_id=%s", firm_id)
                failed_attempts.append(f"https://2gis.ru/{city_code}/firm/{firm_id}")
                continue

            normalized_sites: list[str] = []
            seen_in_firm: set[str] = set()
            for raw_site in sites:
                if is_hh_url(raw_site):
                    hh_url = normalize_hh_employer_url(raw_site)
                    if hh_url:
                        key = (firm_id, hh_url)
                        if key not in seen_hh:
                            seen_hh.add(key)
                            hh_rows.append(
                                {
                                    "firm_id": firm_id,
                                    "company_name": company_name,
                                    "address_name": address_name,
                                    "site_url": "",
                                    "hh_employer_url": hh_url,
                                    "source": "2gis_card",
                                }
                            )
                    continue

                normalized = normalize_site_url(raw_site)
                if not normalized:
                    continue
                if normalized in seen_in_firm:
                    continue
                seen_in_firm.add(normalized)
                normalized_sites.append(normalized)

            selected_sites = normalized_sites[: max(1, max_sites_per_firm)]
            logger.info("Resolved %s raw site(s), selected for crawl: %s", len(sites), selected_sites)

            for site_url in selected_sites:
                if is_hh_url(site_url):
                    continue

                try:
                    pages = await crawl_site_pages(
                        crawler=crawler,
                        start_url=site_url,
                        max_pages=max_pages,
                        run_config=run_config,
                        verbose=verbose,
                    )
                except Exception as exc:
                    logger.exception("Crawler failed for site=%s: %s", site_url, exc)
                    failed_attempts.append(site_url)
                    continue

                if not pages:
                    logger.warning("No pages collected for site=%s", site_url)
                    failed_attempts.append(site_url)
                    continue

                domain = domain_slug(site_url)
                site_dir = output_dir / domain
                site_dir.mkdir(parents=True, exist_ok=True)

                prices = extract_prices(pages, domain)
                specialists = extract_specialists(pages, domain)
                write_pricing_csv(site_dir / "pricing.csv", prices)
                write_specialists_csv(site_dir / "specialists.csv", specialists)

                hh_links = extract_hh_employer_links(pages)
                for hh_url in hh_links:
                    key = (firm_id, hh_url)
                    if key in seen_hh:
                        continue
                    seen_hh.add(key)
                    hh_rows.append(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": site_url,
                            "hh_employer_url": hh_url,
                            "source": "site_crawl",
                        }
                    )
                write_hh_links_csv(hh_rows, hh_links_csv)

                logger.info(
                    "SUCCESS: %s | pages=%s prices=%s specialists=%s hh_employer_links=%s -> %s",
                    site_url,
                    len(pages),
                    len(prices),
                    len(specialists),
                    len(hh_links),
                    site_dir,
                )

    write_hh_links_csv(hh_rows, hh_links_csv)
    logger.info("Saved hh employer links: %s rows -> %s", len(hh_rows), hh_links_csv)

    if failed_attempts:
        print("\nFailed attempts:")
        for value in failed_attempts:
            print(value)
    else:
        print("\nFailed attempts: none")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stream crawl sites from dublgis_items.csv in order.")
    parser.add_argument("--csv", default="dublgis_items.csv", help="Path to 2GIS CSV file.")
    parser.add_argument("--limit", type=int, help="How many first rows to process (default: all).")
    parser.add_argument("--start-row", type=int, default=1, help="Start row number (1-based, excluding header).")
    parser.add_argument("--end-row", type=int, help="End row number (1-based, inclusive, excluding header).")
    parser.add_argument("--city-code", default="moscow", help="2GIS city code for firm card URL.")
    parser.add_argument("--max-sites-per-firm", type=int, default=3, help="Max websites to crawl per firm.")
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages per website.")
    parser.add_argument("--output-dir", default="output", help="Directory for per-site CSV output.")
    parser.add_argument(
        "--hh-links-csv",
        default="output/hh_links_review.csv",
        help="CSV file for found hh.ru/employer links.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose crawling progress.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    start_row = max(1, args.start_row)
    end_row = args.end_row
    if end_row is not None and end_row < start_row:
        raise SystemExit("--end-row must be greater than or equal to --start-row")

    limit = max(1, args.limit) if args.limit is not None else None
    firms = read_firms(
        csv_path=csv_path,
        limit=limit,
        start_row=start_row,
        end_row=end_row,
    )
    if not firms:
        raise SystemExit("No firms with id found in CSV")

    asyncio.run(
        run_streaming(
            firms=firms,
            city_code=args.city_code,
            max_sites_per_firm=max(1, args.max_sites_per_firm),
            max_pages=max(1, args.max_pages),
            output_dir=Path(args.output_dir),
            hh_links_csv=Path(args.hh_links_csv),
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
