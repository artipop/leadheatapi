import argparse
import asyncio
import csv
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import compact_spaces
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import domain_slug
from app.datamining.sites.site_utils import strip_markdown_noise
from app.datamining.sites.site_utils import unique_lines

AMOUNT_PATTERN = r"(?:\d{1,3}(?:[ \u00A0.,]\d{3})+|\d{2,7})"
PRICE_RANGE_RE = re.compile(
    rf"(?P<min>{AMOUNT_PATTERN})\s*[-–—]\s*(?P<max>{AMOUNT_PATTERN})\s*"
    r"(?:₽|руб(?:\.|лей|ля)?|р\b)",
    re.IGNORECASE,
)
PRICE_SINGLE_RE = re.compile(
    rf"(?P<prefix>\bот\b|\bдо\b)?\s*(?P<price>{AMOUNT_PATTERN})\s*(?:₽|руб(?:\.|лей|ля)?|р\b)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PriceEntry:
    domain: str
    source_url: str
    service: str
    price_raw: str
    price_min: str
    price_max: str
    currency: str


def to_price_value(raw_number: str) -> str:
    digits = re.sub(r"[^\d]", "", raw_number)
    return digits if digits else ""


def strip_price_tokens(text: str) -> str:
    cleaned = PRICE_RANGE_RE.sub("", text)
    cleaned = PRICE_SINGLE_RE.sub("", cleaned)
    cleaned = re.sub(r"^[\-\–—:|/•*]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[\-\–—:|/•*]+\s*$", "", cleaned)
    return strip_markdown_noise(cleaned)


def parse_price_line(text: str, source_url: str, domain: str) -> PriceEntry | None:
    line = strip_markdown_noise(text)
    if len(line) < 4:
        return None

    range_match = PRICE_RANGE_RE.search(line)
    if range_match:
        min_value = to_price_value(range_match.group("min"))
        max_value = to_price_value(range_match.group("max"))
        service = strip_price_tokens(line) or line
        return PriceEntry(
            domain=domain,
            source_url=source_url,
            service=service,
            price_raw=range_match.group(0),
            price_min=min_value,
            price_max=max_value,
            currency="RUB",
        )

    single_match = PRICE_SINGLE_RE.search(line)
    if not single_match:
        return None

    value = to_price_value(single_match.group("price"))
    if not value:
        return None
    prefix = (single_match.group("prefix") or "").strip().lower()
    min_value = value if prefix != "до" else ""
    max_value = value if prefix == "до" else value
    service = strip_price_tokens(line) or line
    return PriceEntry(
        domain=domain,
        source_url=source_url,
        service=service,
        price_raw=single_match.group(0),
        price_min=min_value,
        price_max=max_value,
        currency="RUB",
    )


def extract_price_lines(page) -> list[str]:
    lines: list[str] = []
    if page.markdown:
        for raw_line in page.markdown.splitlines():
            line = compact_spaces(raw_line)
            if 6 <= len(line) <= 220 and (PRICE_RANGE_RE.search(line) or PRICE_SINGLE_RE.search(line)):
                lines.append(line)

    if page.html:
        soup = BeautifulSoup(page.html, "html.parser")
        for row in soup.find_all("tr"):
            cells = [
                compact_spaces(cell.get_text(" ", strip=True))
                for cell in row.find_all(["th", "td"])
                if compact_spaces(cell.get_text(" ", strip=True))
            ]
            if len(cells) < 2:
                continue
            row_text = " | ".join(cells)
            if PRICE_RANGE_RE.search(row_text) or PRICE_SINGLE_RE.search(row_text):
                lines.append(row_text)

        for item in soup.find_all(["li", "p"]):
            text = compact_spaces(item.get_text(" ", strip=True))
            if 8 <= len(text) <= 220 and (PRICE_RANGE_RE.search(text) or PRICE_SINGLE_RE.search(text)):
                lines.append(text)

    return unique_lines(lines)


def extract_prices(pages, domain: str) -> list[PriceEntry]:
    items: list[PriceEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for page in pages:
        for line in extract_price_lines(page):
            parsed = parse_price_line(line, page.url, domain)
            if not parsed:
                continue
            key = (parsed.source_url, parsed.service.lower(), parsed.price_raw.lower())
            if key in seen:
                continue
            seen.add(key)
            items.append(parsed)
    return items


def write_pricing_csv(path: Path, prices) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["domain", "source_url", "service", "price_raw", "price_min", "price_max", "currency"],
        )
        writer.writeheader()
        for item in prices:
            writer.writerow(
                {
                    "domain": item.domain,
                    "source_url": item.source_url,
                    "service": item.service,
                    "price_raw": item.price_raw,
                    "price_min": item.price_min,
                    "price_max": item.price_max,
                    "currency": item.currency,
                }
            )


def prices_from_pages(site: str, pages) -> list:
    return extract_prices(pages, domain_slug(site))


async def collect_prices(site: str, max_pages: int = 40) -> list:
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
    return prices_from_pages(site, pages)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Extract pricing rows from a site.")
    parser.add_argument("--site", required=True)
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--output-csv", required=True)
    args = parser.parse_args()

    prices = asyncio.run(collect_prices(args.site, max_pages=max(1, args.max_pages)))
    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_pricing_csv(output_path, prices)
    print(f"Saved {len(prices)} rows to {output_path}")


if __name__ == "__main__":
    cli()
