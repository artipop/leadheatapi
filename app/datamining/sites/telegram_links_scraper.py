import argparse
import csv
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit, urldefrag

from playwright.sync_api import Page, sync_playwright

TELEGRAM_HOSTS = {"t.me", "telegram.me"}
COMMON_SITE_COLUMNS = ("site_url", "site", "url", "website", "domain")
HEADER_SELECTORS = (
    "header",
    "[role='banner']",
    "[id*='header' i]",
    "[class*='header' i]",
)
FOOTER_SELECTORS = (
    "footer",
    "[role='contentinfo']",
    "[id*='footer' i]",
    "[class*='footer' i]",
)
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


def normalize_host(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("www."):
        value = value[4:]
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def normalize_site_url(site: str) -> str | None:
    value = site.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_host(parsed.hostname or "")
    if not host:
        return None
    return urlunsplit(("https", host, "/", "", ""))


def normalize_telegram_url(url: str) -> str | None:
    resolved, _ = urldefrag(url.strip())
    if not resolved:
        return None
    if "://" not in resolved:
        resolved = f"https://{resolved}"
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_host(parsed.hostname or "")
    if host not in TELEGRAM_HOSTS:
        return None
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    parts = [part for part in path.strip("/").split("/") if part]
    if parts and parts[0].lower() in TELEGRAM_TECHNICAL_PATH_PREFIXES:
        return None
    return urlunsplit(("https", host, path, parsed.query, ""))


def _extract_site_from_row(
    row: dict[str, str],
    fieldnames: list[str],
    site_column: str | None,
) -> str | None:
    if site_column:
        value = (row.get(site_column) or "").strip()
        return value or None

    for name in COMMON_SITE_COLUMNS:
        value = (row.get(name) or "").strip()
        if value:
            return value

    if fieldnames:
        value = (row.get(fieldnames[0]) or "").strip()
        if value:
            return value
    return None


def get_site_from_csv_row(csv_path: str | Path, row_number: int, site_column: str | None = None) -> str | None:
    path = Path(csv_path)
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        for index, row in enumerate(reader, start=1):
            if index != row_number:
                continue
            return _extract_site_from_row(row, fieldnames, site_column)
    return None


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


def _detect_telegram_type(browser: Any, telegram_url: str) -> tuple[str, str]:
    tg_page = browser.new_page()
    fallback_type = "unknown"
    resolved_web_url = ""
    try:
        tg_page.goto(telegram_url, wait_until="domcontentloaded", timeout=25_000)
        tg_page.wait_for_timeout(1_200)
        # Быстрый fallback по публичной t.me странице.
        fallback_type = _classify_tme_landing_page(tg_page)

        web_url = _build_web_telegram_url(telegram_url)
        if not web_url:
            return fallback_type, resolved_web_url

        # Основной способ: проверка поля ввода в web.telegram.org.
        resolved_web_url = web_url
        tg_page.goto(web_url, wait_until="domcontentloaded", timeout=30_000)
        tg_page.wait_for_timeout(1_800)
        web_type = _detect_chat_input_on_web_telegram(tg_page)
        if web_type == "unknown_auth_required":
            return fallback_type, resolved_web_url
        return web_type, resolved_web_url
    except Exception:
        return fallback_type, resolved_web_url
    finally:
        tg_page.close()


def _collect_telegram_links_for_site(browser: Any, site: str) -> list[dict[str, str]]:
    homepage = normalize_site_url(site)
    if not homepage:
        return []

    page = browser.new_page()
    try:
        page.goto(homepage, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_000)

        found: list[dict[str, str]] = []
        found.extend(_extract_telegram_links_from_scope(page, HEADER_SELECTORS, "header"))
        found.extend(_extract_telegram_links_from_scope(page, FOOTER_SELECTORS, "footer"))

        deduped: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in found:
            url = item["telegram_url"]
            if url in seen:
                continue
            seen.add(url)
            deduped.append(item)

        results: list[dict[str, str]] = []
        for item in deduped:
            telegram_type, telegram_web_url = _detect_telegram_type(browser, item["telegram_url"])
            results.append(
                {
                    "site": homepage,
                    "source_scope": item["source_scope"],
                    "telegram_url": item["telegram_url"],
                    "telegram_web_url": telegram_web_url,
                    "telegram_type": telegram_type,
                }
            )
        return results
    except Exception:
        return []
    finally:
        page.close()


def main(site: str) -> list[dict[str, str]]:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            return _collect_telegram_links_for_site(browser, site)
        finally:
            browser.close()


def process_csv_row_by_row(
    csv_path: str | Path,
    site_column: str | None = None,
    limit: int | None = None,
    start_row: int = 1,
    end_row: int | None = None,
) -> list[dict[str, str]]:
    path = Path(csv_path)
    rows_output: list[dict[str, str]] = []
    start = max(1, start_row)
    end = end_row if end_row is None or end_row >= 1 else None

    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for index, row in enumerate(reader, start=1):
                    if index < start:
                        continue
                    if end is not None and index > end:
                        break
                    if limit is not None and len(rows_output) >= limit:
                        break
                    site = _extract_site_from_row(row, fieldnames, site_column)
                    if not site:
                        continue
                    rows_output.append(
                        {
                            "row_number": str(index),
                            "site": site,
                            "telegram_links": _collect_telegram_links_for_site(browser, site),
                        }
                    )
            finally:
                browser.close()
    return rows_output


def _flatten_rows_for_csv(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    flat_rows: list[dict[str, str]] = []
    for row in rows:
        row_number = row.get("row_number", "")
        source_site = row.get("site", "")
        links = row.get("telegram_links", [])
        if not links:
            flat_rows.append(
                {
                    "row_number": str(row_number),
                    "source_site": str(source_site),
                    "site": "",
                    "source_scope": "",
                    "telegram_url": "",
                    "telegram_web_url": "",
                    "telegram_type": "",
                }
            )
            continue
        for item in links:
            flat_rows.append(
                {
                    "row_number": str(row_number),
                    "source_site": str(source_site),
                    "site": item.get("site", ""),
                    "source_scope": item.get("source_scope", ""),
                    "telegram_url": item.get("telegram_url", ""),
                    "telegram_web_url": item.get("telegram_web_url", ""),
                    "telegram_type": item.get("telegram_type", ""),
                }
            )
    return flat_rows


def write_results_csv(output_csv: str | Path, rows: list[dict[str, str]]) -> None:
    output_path = Path(output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        "row_number",
        "source_site",
        "site",
        "source_scope",
        "telegram_url",
        "telegram_web_url",
        "telegram_type",
    )
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def cli() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Проверка Telegram-ссылок на главной странице сайта (header/footer) "
            "и чтение сайтов из CSV по одной строке."
        )
    )
    parser.add_argument("--site", help="Один сайт для проверки (например example.com).")
    parser.add_argument("--csv", help="CSV с сайтами.")
    parser.add_argument("--site-column", help="Название колонки с сайтом в CSV.")
    parser.add_argument("--row", type=int, help="Номер строки CSV (1-based, без заголовка).")
    parser.add_argument("--start-row", type=int, default=1, help="Начальная строка CSV (1-based, без заголовка).")
    parser.add_argument("--end-row", type=int, help="Конечная строка CSV (1-based, без заголовка).")
    parser.add_argument("--limit", type=int, help="Лимит строк при проходе CSV построчно.")
    parser.add_argument("--output-csv", help="Куда сохранить результат в отдельный CSV.")
    args = parser.parse_args()

    if args.end_row is not None and args.end_row < args.start_row:
        raise SystemExit("--end-row должен быть больше или равен --start-row")

    if args.site:
        result = main(args.site)
        if args.output_csv:
            rows = _flatten_rows_for_csv([{"row_number": "", "site": args.site, "telegram_links": result}])
            write_results_csv(args.output_csv, rows)
            print(f"Saved {len(rows)} rows to {args.output_csv}")
            return
        print(result)
        return

    if args.csv and args.row:
        site = get_site_from_csv_row(args.csv, args.row, args.site_column)
        if not site:
            print([])
            return
        result = main(site)
        if args.output_csv:
            rows = _flatten_rows_for_csv([{"row_number": str(args.row), "site": site, "telegram_links": result}])
            write_results_csv(args.output_csv, rows)
            print(f"Saved {len(rows)} rows to {args.output_csv}")
            return
        print(result)
        return

    if args.csv:
        result = process_csv_row_by_row(
            args.csv,
            args.site_column,
            args.limit,
            start_row=args.start_row,
            end_row=args.end_row,
        )
        if args.output_csv:
            rows = _flatten_rows_for_csv(result)
            write_results_csv(args.output_csv, rows)
            print(f"Saved {len(rows)} rows to {args.output_csv}")
            return
        print(result)
        return

    raise SystemExit("Укажите --site или --csv")


if __name__ == "__main__":
    cli()
