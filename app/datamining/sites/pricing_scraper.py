import re
from dataclasses import dataclass
from typing import TypedDict

from bs4 import BeautifulSoup

from app.datamining.contracts import SiteScrapeConfig
from app.datamining.contracts import WriteRepository
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


class PriceRow(TypedDict):
    domain: str
    source_url: str
    service: str
    price_raw: str
    price_min: str
    price_max: str
    currency: str


@dataclass(frozen=True, slots=True)
class PricingScraperConfig(SiteScrapeConfig):
    pass


class PricingScraper:
    def __init__(
        self,
        config: PricingScraperConfig | None = None,
        repository: WriteRepository[PriceRow] | None = None,
    ) -> None:
        self.config = config or PricingScraperConfig()
        self.repository = repository

    async def scrape(self, site: str) -> list[PriceRow]:
        pages = await crawl_pages(site, self.config)
        items = price_rows_from_pages(site, pages)
        if self.repository:
            self.repository.add_many(items)
        return items


async def crawl_pages(site: str, config: SiteScrapeConfig):
    browser_config = build_browser_config()
    run_config = build_run_config()
    async with create_crawler(browser_config) as crawler:
        return await crawl_site_pages(
            crawler=crawler,
            start_url=site,
            max_pages=max(1, config.max_pages),
            run_config=run_config,
            verbose=config.verbose,
            page_concurrency=max(1, config.page_concurrency),
        )


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


def prices_from_pages(site: str, pages) -> list[PriceEntry]:
    return extract_prices(pages, domain_slug(site))


def price_entry_to_row(item: PriceEntry) -> PriceRow:
    return {
        "domain": item.domain,
        "source_url": item.source_url,
        "service": item.service,
        "price_raw": item.price_raw,
        "price_min": item.price_min,
        "price_max": item.price_max,
        "currency": item.currency,
    }


def price_rows_from_pages(site: str, pages) -> list[PriceRow]:
    return [price_entry_to_row(item) for item in prices_from_pages(site, pages)]


async def collect_prices(site: str, max_pages: int = 40) -> list[PriceRow]:
    scraper = PricingScraper(PricingScraperConfig(max_pages=max_pages))
    return await scraper.scrape(site)
