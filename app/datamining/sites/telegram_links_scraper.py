import asyncio
import re
from dataclasses import dataclass
from typing import Any
from typing import TypedDict
from urllib.parse import urljoin, urlsplit, urlunsplit, urldefrag

from app.datamining.contracts import SiteScrapeConfig
from app.datamining.contracts import WriteRepository
from app.datamining.playwright_utils import PAGE_FOOTER_SELECTORS
from app.datamining.playwright_utils import PAGE_HEADER_SELECTORS
from app.datamining.playwright_utils import normalize_browser_host
from app.datamining.playwright_utils import normalize_homepage_url
from playwright.sync_api import Page, sync_playwright

TELEGRAM_HOSTS = {"t.me", "telegram.me"}
CHAT_INPUT_SELECTOR = (
    "textarea[placeholder*='message' i],"
    "input[placeholder*='message' i],"
    "textarea[placeholder*='сообщ' i],"
    "input[placeholder*='сообщ' i],"
    "div[role='textbox'][contenteditable='true'],"
    "[contenteditable='true'][aria-label*='message' i],"
    "[contenteditable='true'][aria-label*='сообщ' i],"
    "[contenteditable='true'][data-placeholder*='message' i],"
    "[contenteditable='true'][data-placeholder*='сообщ' i]"
)
WEB_CHAT_INPUT_SELECTOR = (
    ".input-message-input[contenteditable='true'],"
    ".chat-input [contenteditable='true'][role='textbox'],"
    ".chat-input [contenteditable='true'],"
    ".new-message-box [contenteditable='true'],"
    ".chat-input textarea,"
    ".chat-input input[type='text']"
)
TELEGRAM_TECHNICAL_PATH_PREFIXES = {
    "share",
    "socks",
    "proxy",
    "login",
    "iv",
    "addstickers",
    "setlanguage",
    "invoice",
}


@dataclass(frozen=True, slots=True)
class TelegramLinkEntry:
    site: str
    source_scope: str
    telegram_url: str
    telegram_web_url: str
    telegram_type: str


class TelegramLinkRow(TypedDict):
    site: str
    source_scope: str
    telegram_url: str
    telegram_web_url: str
    telegram_type: str


@dataclass(frozen=True, slots=True)
class TelegramLinksScraperConfig(SiteScrapeConfig):
    page_timeout_ms: int = 30_000
    page_wait_ms: int = 1_000
    telegram_landing_timeout_ms: int = 25_000
    telegram_landing_wait_ms: int = 1_200
    telegram_web_timeout_ms: int = 30_000
    telegram_web_wait_ms: int = 1_800
    headless: bool = True


class TelegramLinksScraper:
    def __init__(
        self,
        config: TelegramLinksScraperConfig | None = None,
        repository: WriteRepository[TelegramLinkRow] | None = None,
    ) -> None:
        self.config = config or TelegramLinksScraperConfig()
        self.repository = repository

    async def scrape(self, site: str) -> list[TelegramLinkRow]:
        items = await asyncio.to_thread(collect_telegram_links, site, self.config)
        if self.repository:
            self.repository.add_many(items)
        return items


def normalize_telegram_url(url: str) -> str | None:
    resolved, _ = urldefrag(url.strip())
    if not resolved:
        return None
    if "://" not in resolved:
        resolved = f"https://{resolved}"
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_browser_host(parsed.hostname or "")
    if host not in TELEGRAM_HOSTS:
        return None
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[0].lower() in TELEGRAM_TECHNICAL_PATH_PREFIXES:
        return None
    return urlunsplit(("https", host, path, parsed.query, ""))


def _extract_telegram_links_from_scope(page: Page, scope_selectors: tuple[str, ...], scope_name: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for scope_selector in scope_selectors:
        for scope in page.query_selector_all(scope_selector):
            for link in scope.query_selector_all("a[href]"):
                href = (link.get_attribute("href") or "").strip()
                if not href:
                    continue
                resolved = urljoin(page.url, href)
                telegram_url = normalize_telegram_url(resolved)
                if not telegram_url:
                    continue
                key = f"{scope_name}::{telegram_url}"
                if key in seen:
                    continue
                seen.add(key)
                items.append({"source_scope": scope_name, "telegram_url": telegram_url})
    return items


def _extract_telegram_username(telegram_url: str) -> str | None:
    parsed = urlsplit(telegram_url)
    parts = [part for part in (parsed.path or "").strip("/").split("/") if part]
    if not parts:
        return None

    first = parts[0].lower()
    if first in TELEGRAM_TECHNICAL_PATH_PREFIXES:
        return None
    if first == "s":
        if len(parts) < 2:
            return None
        candidate = parts[1]
    elif first == "joinchat" or first.startswith("+"):
        return None
    else:
        candidate = parts[0]

    candidate = candidate.lstrip("@").strip()
    if not candidate:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", candidate):
        return None
    return candidate


def _build_web_telegram_url(telegram_url: str) -> str | None:
    username = _extract_telegram_username(telegram_url)
    if not username:
        return None
    return f"https://web.telegram.org/k/#@{username}"


def _classify_tme_landing_page(page: Page) -> str:
    try:
        data = page.evaluate(
            """() => {
                const title = (document.title || '').trim().toLowerCase();
                const actionText = (
                    document.querySelector('.tgme_action_button_new, .tgme_action_button')?.textContent || ''
                ).trim().toLowerCase();
                const body = (document.body?.innerText || '').toLowerCase();
                return { title, actionText, body };
            }"""
        )
    except Exception:
        return "unknown"

    title = str(data.get("title", ""))
    action_text = str(data.get("actionText", ""))
    body = str(data.get("body", ""))
    payload = f"{title} {action_text} {body}"

    if any(token in payload for token in ("start bot", "launch @", "send message", "join group")):
        return "chat_or_bot"
    if any(token in payload for token in ("view @", "preview channel", "view in telegram")):
        return "channel"
    return "unknown"


def _detect_chat_input_on_web_telegram(page: Page) -> str:
    state = page.evaluate(
        """(selector) => {
            const hasAuthScreen =
                !!document.querySelector('.auth-image, .auth-image-container, .tabs-tab.page-signQR') ||
                !!document.querySelector('.tabs-container.auth-pages__container');
            const isVisible = (el) => {
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return (
                    style.display !== 'none' &&
                    style.visibility !== 'hidden' &&
                    Number(style.opacity || '1') !== 0 &&
                    rect.width > 0 &&
                    rect.height > 0
                );
            };
            const candidates = Array.from(document.querySelectorAll(selector));
            const hasComposerInput = candidates.some((el) => {
                if (!isVisible(el)) return false;
                const disabled = ('disabled' in el && !!el.disabled) || (el.getAttribute('aria-disabled') || '').toLowerCase() === 'true';
                return !disabled;
            });
            return { hasAuthScreen, hasComposerInput };
        }""",
        WEB_CHAT_INPUT_SELECTOR,
    )
    if bool(state.get("hasComposerInput")):
        return "chat_or_bot"
    if bool(state.get("hasAuthScreen")):
        return "unknown_auth_required"
    return "channel"


def _detect_telegram_type(browser: Any, telegram_url: str, config: TelegramLinksScraperConfig) -> tuple[str, str]:
    tg_page = browser.new_page()
    fallback_type = "unknown"
    resolved_web_url = ""
    try:
        tg_page.goto(telegram_url, wait_until="domcontentloaded", timeout=config.telegram_landing_timeout_ms)
        tg_page.wait_for_timeout(config.telegram_landing_wait_ms)
        fallback_type = _classify_tme_landing_page(tg_page)

        web_url = _build_web_telegram_url(telegram_url)
        if not web_url:
            return fallback_type, resolved_web_url

        resolved_web_url = web_url
        tg_page.goto(web_url, wait_until="domcontentloaded", timeout=config.telegram_web_timeout_ms)
        tg_page.wait_for_timeout(config.telegram_web_wait_ms)
        web_type = _detect_chat_input_on_web_telegram(tg_page)
        if web_type == "unknown_auth_required":
            return fallback_type, resolved_web_url
        return web_type, resolved_web_url
    except Exception:
        return fallback_type, resolved_web_url
    finally:
        tg_page.close()


def _collect_telegram_links_for_site(browser: Any, site: str, config: TelegramLinksScraperConfig) -> list[TelegramLinkEntry]:
    homepage = normalize_homepage_url(site)
    if not homepage:
        return []

    page = browser.new_page()
    try:
        page.goto(homepage, wait_until="domcontentloaded", timeout=config.page_timeout_ms)
        page.wait_for_timeout(config.page_wait_ms)

        found: list[dict[str, str]] = []
        found.extend(_extract_telegram_links_from_scope(page, PAGE_HEADER_SELECTORS, "header"))
        found.extend(_extract_telegram_links_from_scope(page, PAGE_FOOTER_SELECTORS, "footer"))

        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in found:
            url = item["telegram_url"]
            if url in seen:
                continue
            seen.add(url)
            deduped.append(item)

        results: list[TelegramLinkEntry] = []
        for item in deduped:
            telegram_type, telegram_web_url = _detect_telegram_type(browser, item["telegram_url"], config)
            results.append(
                TelegramLinkEntry(
                    site=homepage,
                    source_scope=item["source_scope"],
                    telegram_url=item["telegram_url"],
                    telegram_web_url=telegram_web_url,
                    telegram_type=telegram_type,
                )
            )
        return results
    except Exception:
        return []
    finally:
        page.close()


def collect_telegram_link_entries(site: str, config: TelegramLinksScraperConfig | None = None) -> list[TelegramLinkEntry]:
    config = config or TelegramLinksScraperConfig()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=config.headless)
        try:
            return _collect_telegram_links_for_site(browser, site, config)
        finally:
            browser.close()


def telegram_link_entry_to_row(item: TelegramLinkEntry) -> TelegramLinkRow:
    return {
        "site": item.site,
        "source_scope": item.source_scope,
        "telegram_url": item.telegram_url,
        "telegram_web_url": item.telegram_web_url,
        "telegram_type": item.telegram_type,
    }


def telegram_link_entries_to_rows(items: list[TelegramLinkEntry]) -> list[TelegramLinkRow]:
    return [telegram_link_entry_to_row(item) for item in items]


def collect_telegram_links(site: str, config: TelegramLinksScraperConfig | None = None) -> list[TelegramLinkRow]:
    return telegram_link_entries_to_rows(collect_telegram_link_entries(site, config))
