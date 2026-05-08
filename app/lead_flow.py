import argparse
import asyncio
import csv
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from app.datamining.sites.bookings_scraper import bookings_from_pages
from app.datamining.sites.contacts_scraper import extract_contacts
from app.datamining.sites.inn_scraper import collect_inns_from_contacts
from app.datamining.sites.instagram_links_scraper import main as collect_instagram_links
from app.datamining.sites.pricing_scraper import prices_from_pages
from app.datamining.sites.specialists_scraper import specialists_from_pages
from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import domain_slug
from app.datamining.sites.telegram_links_scraper import main as collect_telegram_links
from app.datamining.api.dublgis_api import fetch_all_items
from app.scrape2gis import get_firm_sites


SITE_COLUMNS = ("site", "site_url", "website", "domain", "url")
INN_COLUMNS = ("inn", "ИНН", "company_inn")


def normalize_site(site: str) -> str:
    value = site.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return urlunsplit(("https", host, "/", "", ""))


def site_key(site: str) -> str:
    normalized = normalize_site(site)
    return urlsplit(normalized).hostname or ""


def split_values(raw: str) -> list[str]:
    values: list[str] = []
    for part in raw.replace(",", ";").split(";"):
        value = part.strip()
        if value:
            values.append(value)
    return values


def normalize_inn(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) in {10, 12}:
        return digits
    return ""


def first_existing_column(fieldnames: list[str], candidates: tuple[str, ...]) -> str:
    lowered = {name.lower(): name for name in fieldnames}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return ""


def load_company_inns(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    result: dict[str, str] = {}
    with Path(path).open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        site_column = first_existing_column(fieldnames, SITE_COLUMNS)
        inn_column = first_existing_column(fieldnames, INN_COLUMNS)
        if not site_column or not inn_column:
            return result
        for row in reader:
            inn = normalize_inn(row.get(inn_column) or "")
            if not inn:
                continue
            for raw_site in split_values(row.get(site_column) or ""):
                key = site_key(raw_site)
                if key and key not in result:
                    result[key] = inn
    return result


def write_csv(path: Path, fieldnames: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def price_rows(firm: dict[str, str], site: str, prices) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in prices:
        rows.append(
            {
                "firm_id": firm["id"],
                "company_name": firm["name"],
                "site": site,
                "domain": item.domain,
                "source_url": item.source_url,
                "service": item.service,
                "price_raw": item.price_raw,
                "price_min": item.price_min,
                "price_max": item.price_max,
                "currency": item.currency,
            }
        )
    return rows


def specialist_rows(firm: dict[str, str], site: str, specialists) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in specialists:
        rows.append(
            {
                "firm_id": firm["id"],
                "company_name": firm["name"],
                "site": site,
                "domain": item.domain,
                "source_url": item.source_url,
                "full_name": item.full_name,
                "role": item.role,
                "phone": item.phone,
                "email": item.email,
            }
        )
    return rows


def booking_row(firm: dict[str, str], site: str, booking: dict[str, str]) -> dict[str, str]:
    return {
        "firm_id": firm["id"],
        "company_name": firm["name"],
        "site": site,
        "booking_mode": booking.get("booking_mode", "none"),
        "has_booking_form": booking.get("has_booking_form", "0"),
        "has_booking_widget": booking.get("has_booking_widget", "0"),
        "booking_widget_provider": booking.get("booking_widget_provider", ""),
        "booking_widget_host": booking.get("booking_widget_host", ""),
        "booking_evidence": booking.get("booking_evidence", ""),
    }


def social_rows(firm: dict[str, str], site: str, links: list[dict[str, str]], kind: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for item in links:
        row = {
            "firm_id": firm["id"],
            "company_name": firm["name"],
            "source_site": site,
            "site": item.get("site", ""),
            "source_scope": item.get("source_scope", ""),
        }
        if kind == "instagram":
            row["instagram_url"] = item.get("instagram_url", "")
            row["instagram_username"] = item.get("instagram_username", "")
        if kind == "telegram":
            row["telegram_url"] = item.get("telegram_url", "")
            row["telegram_web_url"] = item.get("telegram_web_url", "")
            row["telegram_type"] = item.get("telegram_type", "")
        rows.append(row)
    return rows


def firm_from_item(item: dict) -> dict[str, str]:
    return {
        "id": str(item.get("id") or "").strip(),
        "name": str(item.get("name") or "").strip(),
        "address_name": str(item.get("address_name") or "").strip(),
    }


async def crawl_pages_for_site(crawler, run_config, site: str, max_pages: int):
    return await crawl_site_pages(
        crawler=crawler,
        start_url=site,
        max_pages=max(1, max_pages),
        run_config=run_config,
        verbose=False,
        page_concurrency=1,
    )


def run_telegram_members(input_csv: Path, output_csv: Path, args: argparse.Namespace) -> None:
    command = [
        sys.executable,
        "app/datamining/contacts/tg/telegram_channel_members.py",
        "--input-csv",
        str(input_csv),
        "--input-column",
        "telegram_url",
        "--output-csv",
        str(output_csv),
        "--mode",
        args.telegram_mode,
        "--user-data-dir",
        args.telegram_user_data_dir,
        "--timeout-seconds",
        str(args.telegram_timeout_seconds),
        "--channel-timeout-seconds",
        str(args.telegram_channel_timeout_seconds),
    ]
    if args.no_login_prompt:
        command.append("--no-login-prompt")
    if args.auto_subscribe:
        command.append("--auto-subscribe")
    if args.resolve_usernames:
        command.append("--resolve-usernames")
    subprocess.run(command, check=False)


async def run_flow(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    company_inns = load_company_inns(args.company_csv)
    items = fetch_all_items(
        rubric_ids=args.rubric_id,
        region_id=args.region_id,
        api_key=args.api_key,
        page_size=args.page_size,
        search_type=args.search_type,
    )
    if args.limit:
        items = items[: args.limit]

    company_rows: list[dict[str, str]] = []
    pricing_rows: list[dict[str, str]] = []
    specialists_rows: list[dict[str, str]] = []
    bookings_rows: list[dict[str, str]] = []
    instagram_rows: list[dict[str, str]] = []
    telegram_rows: list[dict[str, str]] = []

    browser_config = build_browser_config()
    run_config = build_run_config()
    async with create_crawler(browser_config) as crawler:
        for item in items:
            firm = firm_from_item(item)
            if not firm["id"]:
                continue

            try:
                raw_sites = await asyncio.to_thread(get_firm_sites, args.city_code, firm["id"])
            except Exception:
                raw_sites = []

            sites: list[str] = []
            seen_sites: set[str] = set()
            for raw_site in raw_sites:
                site = normalize_site(raw_site)
                if not site or site in seen_sites:
                    continue
                seen_sites.add(site)
                sites.append(site)

            for site in sites[: max(1, args.max_sites_per_firm)]:
                inn = company_inns.get(site_key(site), "")
                inn_source = "company_csv" if inn else ""
                pages = []
                try:
                    pages = await crawl_pages_for_site(crawler, run_config, site, args.max_pages)
                except Exception:
                    pages = []

                if not inn and pages:
                    contacts = extract_contacts(pages, domain_slug(site))
                    site_inns = collect_inns_from_contacts(site, contacts)
                    if site_inns:
                        inn = site_inns[0]["inn"]
                        inn_source = "site"

                company_rows.append(
                    {
                        "firm_id": firm["id"],
                        "company_name": firm["name"],
                        "address_name": firm["address_name"],
                        "site": site,
                        "inn": inn,
                        "inn_source": inn_source,
                    }
                )

                if pages:
                    pricing_rows.extend(price_rows(firm, site, prices_from_pages(site, pages)))
                    specialists_rows.extend(specialist_rows(firm, site, specialists_from_pages(site, pages)))
                    bookings_rows.append(booking_row(firm, site, bookings_from_pages(site, pages)))

                instagram_links = await asyncio.to_thread(collect_instagram_links, site)
                instagram_rows.extend(social_rows(firm, site, instagram_links, "instagram"))

                telegram_links = await asyncio.to_thread(collect_telegram_links, site)
                telegram_rows.extend(social_rows(firm, site, telegram_links, "telegram"))

    write_csv(
        output_dir / "companies.csv",
        ("firm_id", "company_name", "address_name", "site", "inn", "inn_source"),
        company_rows,
    )
    write_csv(
        output_dir / "pricing.csv",
        ("firm_id", "company_name", "site", "domain", "source_url", "service", "price_raw", "price_min", "price_max", "currency"),
        pricing_rows,
    )
    write_csv(
        output_dir / "specialists.csv",
        ("firm_id", "company_name", "site", "domain", "source_url", "full_name", "role", "phone", "email"),
        specialists_rows,
    )
    write_csv(
        output_dir / "bookings.csv",
        (
            "firm_id",
            "company_name",
            "site",
            "booking_mode",
            "has_booking_form",
            "has_booking_widget",
            "booking_widget_provider",
            "booking_widget_host",
            "booking_evidence",
        ),
        bookings_rows,
    )
    write_csv(
        output_dir / "instagram_links.csv",
        ("firm_id", "company_name", "source_site", "site", "source_scope", "instagram_url", "instagram_username"),
        instagram_rows,
    )
    write_csv(
        output_dir / "telegram_links.csv",
        ("firm_id", "company_name", "source_site", "site", "source_scope", "telegram_url", "telegram_web_url", "telegram_type"),
        telegram_rows,
    )

    telegram_input_rows = [{"telegram_url": row["telegram_url"]} for row in telegram_rows if row.get("telegram_url")]
    telegram_input_csv = output_dir / "telegram_channels_for_members.csv"
    write_csv(telegram_input_csv, ("telegram_url",), telegram_input_rows)

    if telegram_input_rows and not args.skip_telegram_members:
        run_telegram_members(telegram_input_csv, output_dir / "telegram_members.csv", args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Simple lead flow: 2GIS -> sites -> INN -> site enrichment -> socials -> TG members.")
    parser.add_argument("--rubric-id", type=int, action="append", required=True)
    parser.add_argument("--region-id", type=int, required=True)
    parser.add_argument("--city-code", required=True)
    parser.add_argument("--api-key", default=os.getenv("DUBLGIS_API_KEY", ""))
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--search-type", default="one_branch")
    parser.add_argument("--company-csv")
    parser.add_argument("--output-dir", default="output/lead_flow")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--max-sites-per-firm", type=int, default=1)
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--skip-telegram-members", action="store_true")
    parser.add_argument("--telegram-mode", choices=("persistent", "cdp"), default="persistent")
    parser.add_argument("--telegram-user-data-dir", default=".playwright/telegram-profile")
    parser.add_argument("--telegram-timeout-seconds", type=int, default=35)
    parser.add_argument("--telegram-channel-timeout-seconds", type=int, default=90)
    parser.add_argument("--no-login-prompt", action="store_true")
    parser.add_argument("--auto-subscribe", action="store_true")
    parser.add_argument("--resolve-usernames", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if not args.api_key:
        raise SystemExit("Передайте --api-key или задайте DUBLGIS_API_KEY")
    asyncio.run(run_flow(args))


if __name__ == "__main__":
    main()
