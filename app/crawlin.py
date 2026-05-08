import argparse
import asyncio
from pathlib import Path

from app.datamining.sites.bookings_scraper import bookings_from_pages
from app.datamining.sites.bookings_scraper import write_results_csv as write_bookings_csv
from app.datamining.sites.contacts_scraper import extract_contacts
from app.datamining.sites.contacts_scraper import write_contacts_csv
from app.datamining.sites.pricing_scraper import extract_prices
from app.datamining.sites.pricing_scraper import write_pricing_csv
from app.datamining.sites.specialists_scraper import extract_specialists
from app.datamining.sites.specialists_scraper import write_specialists_csv
from app.datamining.sites.site_utils import AsyncWebCrawler
from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import display_url
from app.datamining.sites.site_utils import domain_slug
from app.datamining.sites.site_utils import load_urls


async def run(
    urls: list[str],
    max_pages: int,
    output_dir: Path,
    verbose: bool,
    site_concurrency: int,
    page_concurrency: int,
) -> None:
    if AsyncWebCrawler is None:
        raise SystemExit(
            "crawl4ai не установлен. Установите зависимости: "
            "pip install crawl4ai && python -m playwright install"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    browser_config = build_browser_config()
    run_config = build_run_config()

    async with create_crawler(browser_config) as crawler:
        site_semaphore = asyncio.Semaphore(max(1, site_concurrency))

        async def process_site(url: str) -> None:
            async with site_semaphore:
                domain = domain_slug(url)
                site_dir = output_dir / domain
                site_dir.mkdir(parents=True, exist_ok=True)

                pages = await crawl_site_pages(
                    crawler=crawler,
                    start_url=url,
                    max_pages=max_pages,
                    run_config=run_config,
                    verbose=verbose,
                    page_concurrency=max(1, page_concurrency),
                )
                prices = extract_prices(pages, domain)
                specialists = extract_specialists(pages, domain)
                contacts = extract_contacts(pages, domain)
                booking = bookings_from_pages(url, pages)

                write_pricing_csv(site_dir / "pricing.csv", prices)
                write_specialists_csv(site_dir / "specialists.csv", specialists)
                write_contacts_csv(site_dir / "contacts.csv", contacts)
                write_bookings_csv(site_dir / "bookings.csv", [booking])

                print(
                    f"[done] {display_url(url)} | pages={len(pages)} prices={len(prices)} "
                    f"specialists={len(specialists)} contacts={len(contacts)} "
                    f"booking={booking.get('booking_mode', 'none')} -> {site_dir}"
                )

        tasks = [asyncio.create_task(process_site(url)) for url in urls]
        for task in tasks:
            await task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl websites and export pricing/specialists/contacts/bookings to CSV."
    )
    parser.add_argument("--url", action="append", help="Website URL (can be repeated).")
    parser.add_argument("--url-file", help="Path to text file with URLs (one per line).")
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages to crawl per site.")
    parser.add_argument("--site-concurrency", type=int, default=1, help="How many sites to crawl in parallel.")
    parser.add_argument("--page-concurrency", type=int, default=1, help="How many pages to fetch in parallel.")
    parser.add_argument("--output-dir", default="output", help="Directory for CSV output.")
    parser.add_argument("--verbose", action="store_true", help="Print crawling progress.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    urls = load_urls(args)
    if not urls:
        raise SystemExit("Передайте хотя бы один URL через --url или --url-file")
    asyncio.run(
        run(
            urls=urls,
            max_pages=max(1, args.max_pages),
            output_dir=Path(args.output_dir),
            verbose=args.verbose,
            site_concurrency=max(1, args.site_concurrency),
            page_concurrency=max(1, args.page_concurrency),
        )
    )


if __name__ == "__main__":
    main()
