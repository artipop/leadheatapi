from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urlparse

from app.datamining.contracts import SiteScrapeConfig
from app.datamining.contracts import WriteRepository
from app.datamining.sites.contacts_scraper import extract_contacts
from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import domain_slug


@dataclass(frozen=True, slots=True)
class InnEntry:
    site: str
    domain: str
    source_url: str
    source_scope: str
    inn: str


class InnRow(TypedDict):
    site: str
    domain: str
    source_url: str
    source_scope: str
    inn: str


@dataclass(frozen=True, slots=True)
class InnScraperConfig(SiteScrapeConfig):
    max_pages: int = 8


class InnScraper:
    def __init__(
        self,
        config: InnScraperConfig | None = None,
        repository: WriteRepository[InnRow] | None = None,
    ) -> None:
        self.config = config or InnScraperConfig()
        self.repository = repository

    async def scrape(self, site: str) -> list[InnRow]:
        pages = await crawl_pages(site, self.config)
        contacts = extract_contacts(pages, domain_slug(site))
        items = inn_rows_from_contacts(site, contacts)
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


def collect_inns_from_contacts(site: str, contacts) -> list[InnEntry]:
    domain = domain_slug(site)
    rows: list[InnEntry] = []
    seen: set[str] = set()
    for contact in contacts:
        for inn in split_inns(contact.inn):
            if inn in seen:
                continue
            seen.add(inn)
            rows.append(
                InnEntry(
                    site=site,
                    domain=domain,
                    source_url=contact.source_url,
                    source_scope=contact.source_scope,
                    inn=inn,
                )
            )
    return rows


def inn_entry_to_row(item: InnEntry) -> InnRow:
    return {
        "site": item.site,
        "domain": item.domain,
        "source_url": item.source_url,
        "source_scope": item.source_scope,
        "inn": item.inn,
    }


def inn_rows_from_contacts(site: str, contacts) -> list[InnRow]:
    return [inn_entry_to_row(item) for item in collect_inns_from_contacts(site, contacts)]


async def collect_inns(site: str, max_pages: int = 8) -> list[InnRow]:
    scraper = InnScraper(InnScraperConfig(max_pages=max_pages))
    return await scraper.scrape(site)


async def collect_many(sites: list[str], max_pages: int) -> list[InnRow]:
    rows: list[InnRow] = []
    for site in sites:
        try:
            rows.extend(await collect_inns(site, max_pages=max_pages))
        except Exception as exc:
            rows.append(
                inn_entry_to_row(
                    InnEntry(
                        site=site,
                        domain=urlparse(site).hostname or "",
                        source_url="",
                        source_scope=f"error:{type(exc).__name__}: {exc}",
                        inn="",
                    )
                )
            )
    return rows
