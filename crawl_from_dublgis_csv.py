from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import SplitResult, parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import Request
from urllib.request import urlopen

from bs4 import BeautifulSoup

from crawlin import build_browser_config
from crawlin import build_run_config
from crawlin import crawl_site_pages
from crawlin import create_crawler
from crawlin import domain_slug
from crawlin import extract_contacts
from crawlin import extract_prices
from crawlin import extract_specialists
from crawlin import write_contacts_csv
from crawlin import write_pricing_csv
from crawlin import write_specialists_csv
from scrape2gis import get_firm_sites

logger = logging.getLogger(__name__)
HH_UTM_PARAMS_TO_REMOVE = {"utm_campaign", "utm_content", "utm_medium", "utm_source"}
MULTI_PART_TLDS = {"co", "com", "org", "net", "gov", "edu"}
RESOLVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


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


def split_http_like_url(url: str) -> SplitResult:
    value = url.strip()
    if not value:
        return urlsplit("")
    if "://" not in value:
        value = f"https://{value}"
    return urlsplit(value)


def is_hh_host(host: str) -> bool:
    value = host.lower()
    if value.startswith("www."):
        value = value[4:]
    return value == "hh.ru" or value.endswith(".hh.ru")


def is_hh_url(url: str) -> bool:
    return is_hh_host(split_http_like_url(url).hostname or "")


def is_telegram_host(host: str) -> bool:
    value = host.lower()
    if value.startswith("www."):
        value = value[4:]
    return value in {"t.me", "telegram.me"}


def is_jivo_host(host: str) -> bool:
    value = host.lower()
    if value.startswith("www."):
        value = value[4:]
    return value == "jivo.chat" or value.endswith(".jivo.chat")


def is_jivo_url(url: str) -> bool:
    return is_jivo_host(split_http_like_url(url).hostname or "")


def is_clck_host(host: str) -> bool:
    value = host.lower()
    if value.startswith("www."):
        value = value[4:]
    return value == "clck.ru" or value.endswith(".clck.ru")


def is_clck_url(url: str) -> bool:
    return is_clck_host(split_http_like_url(url).hostname or "")


def is_max_host(host: str) -> bool:
    value = host.lower()
    if value.startswith("www."):
        value = value[4:]
    return value == "max.ru" or value.endswith(".max.ru")


def is_max_url(url: str) -> bool:
    return is_max_host(split_http_like_url(url).hostname or "")


def normalize_http_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url.strip())
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None

    host = (parsed.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    netloc = host
    if parsed.port and not ((parsed.scheme == "http" and parsed.port == 80) or (parsed.scheme == "https" and parsed.port == 443)):
        netloc = f"{host}:{parsed.port}"

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def resolve_short_url(url: str, timeout_seconds: int = 15) -> Optional[str]:
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    request = Request(value, headers={"User-Agent": RESOLVE_USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None

    normalized = normalize_http_url(final_url)
    if not normalized:
        return None
    if is_clck_url(normalized):
        return None
    return normalized


def extract_origin_domain(host: str) -> str:
    normalized = host.lower()
    if normalized.startswith("www."):
        normalized = normalized[4:]
    parts = [part for part in normalized.split(".") if part]
    if len(parts) <= 2:
        return normalized
    if len(parts[-1]) == 2 and parts[-2] in MULTI_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_internal_for_site(url: str, site_url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    if host.startswith("www."):
        host = host[4:]

    site_host = (urlsplit(site_url).hostname or "").lower()
    if site_host.startswith("www."):
        site_host = site_host[4:]
    if not site_host:
        return False

    origin_domain = extract_origin_domain(site_host)
    return host == origin_domain or host.endswith(f".{origin_domain}")


def origin_domain_from_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return extract_origin_domain(host) if host else ""


def normalize_hh_employer_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = (parsed.hostname or "").lower()
    if not is_hh_host(host):
        return None

    path = (parsed.path or "").rstrip("/")
    if not path.startswith("/employer"):
        return None

    if host.startswith("www."):
        host = host[4:]
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in HH_UTM_PARAMS_TO_REMOVE
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit(("https", host, path, query, ""))


def normalize_telegram_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = (parsed.hostname or "").lower()
    if not is_telegram_host(host):
        return None
    if host.startswith("www."):
        host = host[4:]
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    return urlunsplit(("https", host, path, parsed.query, ""))


def normalize_max_channel_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = (parsed.hostname or "").lower()
    if not is_max_host(host):
        return None
    if host.startswith("www."):
        host = host[4:]

    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    first = parts[0].lower()
    if first not in {"channel", "channels", "c"} and not first.startswith("@"):
        return None

    return urlunsplit(("https", host, path, parsed.query, ""))


def extract_page_links(pages) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    for page in pages:
        soup = BeautifulSoup(page.html or "", "html.parser")
        for tag in soup.find_all("a", href=True):
            href = str(tag.get("href", "")).strip()
            if not href:
                continue
            resolved, _ = urldefrag(urljoin(page.url, href))
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append(resolved)
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


def write_telegram_links_csv(rows: list[dict[str, str]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "firm_id",
                "company_name",
                "address_name",
                "site_url",
                "telegram_url",
                "source",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_max_channels_csv(rows: list[dict[str, str]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "firm_id",
                "company_name",
                "address_name",
                "site_url",
                "max_channel_url",
                "source",
            ],
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_other_links_csv(rows: list[dict[str, str]], output_csv: Path) -> None:
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "firm_id",
                "company_name",
                "address_name",
                "site_url",
                "link_url",
                "link_host",
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
    telegram_csv: Path,
    max_channels_csv: Path,
    other_links_csv: Path,
    verbose: bool,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    browser_config = build_browser_config()
    run_config = build_run_config()

    failed_attempts: list[str] = []
    hh_rows: list[dict[str, str]] = []
    telegram_rows: list[dict[str, str]] = []
    max_rows: list[dict[str, str]] = []
    other_links_rows: list[dict[str, str]] = []
    seen_hh: set[tuple[str, str]] = set()
    seen_telegram: set[tuple[str, str]] = set()
    seen_max: set[tuple[str, str]] = set()
    seen_other: set[tuple[str, str]] = set()
    seen_crawled_origins: set[str] = set()
    short_url_cache: dict[str, Optional[str]] = {}

    def resolve_short_url_cached(raw_url: str) -> Optional[str]:
        key = raw_url.strip()
        cached = short_url_cache.get(key)
        if cached is not None or key in short_url_cache:
            return cached
        resolved = resolve_short_url(key)
        short_url_cache[key] = resolved
        return resolved

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
                processed_site = raw_site
                if is_clck_url(raw_site):
                    resolved = resolve_short_url_cached(raw_site)
                    if not resolved:
                        logger.info("Skip unresolved clck.ru link from 2GIS card for firm_id=%s: %s", firm_id, raw_site)
                        continue
                    logger.info("Resolved clck.ru from 2GIS card: %s -> %s", raw_site, resolved)
                    processed_site = resolved

                if is_jivo_url(processed_site):
                    logger.info("Skip jivo.chat link from 2GIS card for firm_id=%s: %s", firm_id, processed_site)
                    continue

                if is_max_url(processed_site):
                    max_url = normalize_max_channel_url(processed_site)
                    if max_url:
                        key = (firm_id, max_url)
                        if key not in seen_max:
                            seen_max.add(key)
                            max_rows.append(
                                {
                                    "firm_id": firm_id,
                                    "company_name": company_name,
                                    "address_name": address_name,
                                    "site_url": "",
                                    "max_channel_url": max_url,
                                    "source": "2gis_card",
                                }
                            )
                    else:
                        logger.info("Skip max.ru link from 2GIS card for firm_id=%s: %s", firm_id, processed_site)
                    continue

                if is_hh_url(processed_site):
                    hh_url = normalize_hh_employer_url(processed_site)
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

                telegram_url = normalize_telegram_url(processed_site)
                if telegram_url:
                    key = (firm_id, telegram_url)
                    if key not in seen_telegram:
                        seen_telegram.add(key)
                        telegram_rows.append(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": "",
                                "telegram_url": telegram_url,
                                "source": "2gis_card",
                            }
                        )
                    continue

                normalized = normalize_site_url(processed_site)
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
                if is_max_url(site_url):
                    logger.info("Skip max.ru site crawl for firm_id=%s: %s", firm_id, site_url)
                    continue

                origin_domain = origin_domain_from_url(site_url)
                if origin_domain and origin_domain in seen_crawled_origins:
                    logger.info(
                        "Skip duplicate origin domain crawl for firm_id=%s: %s (origin=%s)",
                        firm_id,
                        site_url,
                        origin_domain,
                    )
                    continue
                if origin_domain:
                    seen_crawled_origins.add(origin_domain)

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
                contacts = extract_contacts(pages, domain)
                write_pricing_csv(site_dir / "pricing.csv", prices)
                write_specialists_csv(site_dir / "specialists.csv", specialists)
                write_contacts_csv(site_dir / "contacts.csv", contacts)

                extracted_hh_count = 0
                extracted_telegram_count = 0
                extracted_max_count = 0
                extracted_other_count = 0
                for link in extract_page_links(pages):
                    processed_link = link
                    if is_clck_url(link):
                        resolved = resolve_short_url_cached(link)
                        if not resolved:
                            logger.info("Skip unresolved clck.ru link from site crawl: %s", link)
                            continue
                        logger.info("Resolved clck.ru from site crawl: %s -> %s", link, resolved)
                        processed_link = resolved

                    if is_jivo_url(processed_link):
                        logger.info("Skip jivo.chat link from site crawl: %s", processed_link)
                        continue

                    hh_url = normalize_hh_employer_url(processed_link)
                    if hh_url:
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
                        extracted_hh_count += 1
                        continue

                    max_url = normalize_max_channel_url(processed_link)
                    if max_url:
                        key = (firm_id, max_url)
                        if key in seen_max:
                            continue
                        seen_max.add(key)
                        max_rows.append(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": site_url,
                                "max_channel_url": max_url,
                                "source": "site_crawl",
                            }
                        )
                        extracted_max_count += 1
                        continue

                    telegram_url = normalize_telegram_url(processed_link)
                    if telegram_url:
                        key = (firm_id, telegram_url)
                        if key in seen_telegram:
                            continue
                        seen_telegram.add(key)
                        telegram_rows.append(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": site_url,
                                "telegram_url": telegram_url,
                                "source": "site_crawl",
                            }
                        )
                        extracted_telegram_count += 1
                        continue

                    normalized_link = normalize_http_url(processed_link)
                    if not normalized_link:
                        continue
                    if is_internal_for_site(normalized_link, site_url):
                        continue

                    key = (firm_id, normalized_link)
                    if key in seen_other:
                        continue
                    seen_other.add(key)
                    other_links_rows.append(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": site_url,
                            "link_url": normalized_link,
                            "link_host": (urlsplit(normalized_link).hostname or "").lower(),
                            "source": "site_crawl",
                        }
                    )
                    extracted_other_count += 1

                write_hh_links_csv(hh_rows, hh_links_csv)
                write_telegram_links_csv(telegram_rows, telegram_csv)
                write_max_channels_csv(max_rows, max_channels_csv)
                write_other_links_csv(other_links_rows, other_links_csv)

                logger.info(
                    "SUCCESS: %s | pages=%s prices=%s specialists=%s contacts=%s hh_employer_links=%s telegram_links=%s max_channels=%s other_links=%s -> %s",
                    site_url,
                    len(pages),
                    len(prices),
                    len(specialists),
                    len(contacts),
                    extracted_hh_count,
                    extracted_telegram_count,
                    extracted_max_count,
                    extracted_other_count,
                    site_dir,
                )

    write_hh_links_csv(hh_rows, hh_links_csv)
    write_telegram_links_csv(telegram_rows, telegram_csv)
    write_max_channels_csv(max_rows, max_channels_csv)
    write_other_links_csv(other_links_rows, other_links_csv)
    logger.info("Saved hh employer links: %s rows -> %s", len(hh_rows), hh_links_csv)
    logger.info("Saved telegram links: %s rows -> %s", len(telegram_rows), telegram_csv)
    logger.info("Saved max channel links: %s rows -> %s", len(max_rows), max_channels_csv)
    logger.info("Saved other links: %s rows -> %s", len(other_links_rows), other_links_csv)

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
    parser.add_argument(
        "--telegram-csv",
        default="output/telegram.csv",
        help="CSV file for found t.me links.",
    )
    parser.add_argument(
        "--max-channels-csv",
        default="output/max_channels.csv",
        help="CSV file for found max channel links.",
    )
    parser.add_argument(
        "--other-links-csv",
        default="output/other_links.csv",
        help="CSV file for other external links found on crawled sites.",
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
            telegram_csv=Path(args.telegram_csv),
            max_channels_csv=Path(args.max_channels_csv),
            other_links_csv=Path(args.other_links_csv),
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
