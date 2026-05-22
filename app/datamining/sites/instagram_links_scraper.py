import asyncio
import re
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urljoin, urlsplit, urlunsplit, urldefrag

from app.datamining.contracts import SiteScrapeConfig
from app.datamining.contracts import WriteRepository
from app.datamining.playwright_utils import PAGE_FOOTER_SELECTORS
from app.datamining.playwright_utils import PAGE_HEADER_SELECTORS
from app.datamining.playwright_utils import normalize_browser_host
from app.datamining.playwright_utils import normalize_homepage_url
from playwright.sync_api import Page, sync_playwright

INSTAGRAM_TECHNICAL_PATH_PREFIXES = {
    "about",
    "accounts",
    "api",
    "ar",
    "blog",
    "business",
    "challenge",
    "developer",
    "direct",
    "directory",
    "explore",
    "graphql",
    "help",
    "invites",
    "legal",
    "oauth",
    "p",
    "privacy",
    "reel",
    "reels",
    "stories",
    "terms",
    "tv",
    "web",
}


@dataclass(frozen=True, slots=True)
class InstagramLinkEntry:
    site: str
    source_scope: str
    instagram_url: str
    instagram_username: str


class InstagramLinkRow(TypedDict):
    site: str
    source_scope: str
    instagram_url: str
    instagram_username: str


@dataclass(frozen=True, slots=True)
class InstagramLinksScraperConfig(SiteScrapeConfig):
    page_timeout_ms: int = 30_000
    page_wait_ms: int = 1_000
    headless: bool = True


class InstagramLinksScraper:
    def __init__(
        self,
        config: InstagramLinksScraperConfig | None = None,
        repository: WriteRepository[InstagramLinkRow] | None = None,
    ) -> None:
        self.config = config or InstagramLinksScraperConfig()
        self.repository = repository

    async def scrape(self, site: str) -> list[InstagramLinkRow]:
        items = await asyncio.to_thread(collect_instagram_links, site, self.config)
        if self.repository:
            self.repository.add_many(items)
        return items


def _extract_instagram_username(instagram_url: str) -> str | None:
    parsed = urlsplit(instagram_url)
    parts = [part for part in (parsed.path or "").strip("/").split("/") if part]
    if not parts:
        return None

    candidate = parts[0].strip().lstrip("@")
    if not candidate or candidate.lower() in INSTAGRAM_TECHNICAL_PATH_PREFIXES:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", candidate):
        return None
    return candidate


def normalize_instagram_url(url: str) -> str | None:
    resolved, _ = urldefrag(url.strip())
    if not resolved:
        return None
    if "://" not in resolved:
        resolved = f"https://{resolved}"
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_browser_host(parsed.hostname or "")
    if host not in {"instagram.com", "instagr.am"}:
        return None

    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    username = _extract_instagram_username(urlunsplit(("https", "instagram.com", path, "", "")))
    if not username:
        return None
    return f"https://www.instagram.com/{username}/"


def _extract_instagram_links_from_scope(page: Page, scope_selectors: tuple[str, ...], scope_name: str) -> list[
    dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for scope_selector in scope_selectors:
        for scope in page.query_selector_all(scope_selector):
            for link in scope.query_selector_all("a[href]"):
                href = (link.get_attribute("href") or "").strip()
                if not href:
                    continue
                resolved = urljoin(page.url, href)
                instagram_url = normalize_instagram_url(resolved)
                if not instagram_url or instagram_url in seen:
                    continue
                seen.add(instagram_url)
                items.append(
                    {
                        "source_scope": scope_name,
                        "instagram_url": instagram_url,
                        "instagram_username": _extract_instagram_username(instagram_url) or "",
                    }
                )
    return items


def _collect_instagram_links_for_site(browser, site: str, config: InstagramLinksScraperConfig) -> list[InstagramLinkEntry]:
    homepage = normalize_homepage_url(site)
    if not homepage:
        return []

    page = browser.new_page()
    try:
        page.goto(homepage, wait_until="domcontentloaded", timeout=config.page_timeout_ms)
        page.wait_for_timeout(config.page_wait_ms)

        found: list[dict[str, str]] = []
        found.extend(_extract_instagram_links_from_scope(page, PAGE_HEADER_SELECTORS, "header"))
        found.extend(_extract_instagram_links_from_scope(page, PAGE_FOOTER_SELECTORS, "footer"))

        results: list[InstagramLinkEntry] = []
        seen: set[str] = set()
        for item in found:
            url = item["instagram_url"]
            if url in seen:
                continue
            seen.add(url)
            results.append(
                InstagramLinkEntry(
                    site=homepage,
                    source_scope=item["source_scope"],
                    instagram_url=url,
                    instagram_username=item.get("instagram_username", ""),
                )
            )
        return results
    except Exception:
        return []
    finally:
        page.close()


def collect_instagram_link_entries(site: str, config: InstagramLinksScraperConfig | None = None) -> list[InstagramLinkEntry]:
    config = config or InstagramLinksScraperConfig()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        try:
            return _collect_instagram_links_for_site(browser, site, config)
        finally:
            browser.close()


def instagram_link_entry_to_row(item: InstagramLinkEntry) -> InstagramLinkRow:
    return {
        "site": item.site,
        "source_scope": item.source_scope,
        "instagram_url": item.instagram_url,
        "instagram_username": item.instagram_username,
    }


def instagram_link_entries_to_rows(items: list[InstagramLinkEntry]) -> list[InstagramLinkRow]:
    return [instagram_link_entry_to_row(item) for item in items]


def collect_instagram_links(site: str, config: InstagramLinksScraperConfig | None = None) -> list[InstagramLinkRow]:
    return instagram_link_entries_to_rows(collect_instagram_link_entries(site, config))
