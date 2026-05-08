import argparse
import asyncio
import csv
from pathlib import Path
from urllib.parse import urlparse

from app.datamining.sites.contacts_scraper import extract_contacts
from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import domain_slug


def split_inns(raw: str) -> list[str]:
    inns: list[str] = []
    seen: set[str] = set()
    for value in raw.split(";"):
        inn = "".join(ch for ch in value if ch.isdigit())
        if len(inn) not in {10, 12}:
            continue
        if inn in seen:
            continue
        seen.add(inn)
        inns.append(inn)
    return inns


def collect_inns_from_contacts(site: str, contacts) -> list[dict[str, str]]:
    domain = domain_slug(site)
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for contact in contacts:
        for inn in split_inns(contact.inn):
            if inn in seen:
                continue
            seen.add(inn)
            rows.append(
                {
                    "site": site,
                    "domain": domain,
                    "source_url": contact.source_url,
                    "source_scope": contact.source_scope,
                    "inn": inn,
                }
            )
    return rows


async def collect_inns(site: str, max_pages: int = 8) -> list[dict[str, str]]:
    browser_config = build_browser_config()
    run_config = build_run_config()
    async with create_crawler(browser_config) as crawler:
        pages = await crawl_site_pages(
            crawler=crawler,
            start_url=site,
            max_pages=max(1, max_pages),
            run_config=run_config,
            verbose=False,
            page_concurrency=1,
        )
    contacts = extract_contacts(pages, domain_slug(site))
    return collect_inns_from_contacts(site, contacts)


def write_results_csv(path: str | Path, rows: list[dict[str, str]]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ("site", "domain", "source_url", "source_scope", "inn")
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def read_sites_from_csv(path: str | Path, site_column: str) -> list[str]:
    sites: list[str] = []
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            site = (row.get(site_column) or "").strip()
            if site:
                sites.append(site)
    return sites


async def collect_many(sites: list[str], max_pages: int) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for site in sites:
        try:
            rows.extend(await collect_inns(site, max_pages=max_pages))
        except Exception as exc:
            rows.append(
                {
                    "site": site,
                    "domain": urlparse(site).hostname or "",
                    "source_url": "",
                    "source_scope": "",
                    "inn": "",
                    "error": str(exc),
                }
            )
    return rows


def cli() -> None:
    parser = argparse.ArgumentParser(description="Extract INN values from site footer/pages.")
    parser.add_argument("--site")
    parser.add_argument("--csv")
    parser.add_argument("--site-column", default="site")
    parser.add_argument("--max-pages", type=int, default=8)
    parser.add_argument("--output-csv")
    args = parser.parse_args()

    sites: list[str] = []
    if args.site:
        sites.append(args.site)
    if args.csv:
        sites.extend(read_sites_from_csv(args.csv, args.site_column))
    if not sites:
        raise SystemExit("Укажите --site или --csv")

    rows = asyncio.run(collect_many(sites, max_pages=max(1, args.max_pages)))
    if args.output_csv:
        write_results_csv(args.output_csv, rows)
        print(f"Saved {len(rows)} rows to {args.output_csv}")
        return
    print(rows)


if __name__ == "__main__":
    cli()
