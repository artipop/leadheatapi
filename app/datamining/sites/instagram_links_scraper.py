import argparse
import csv
import re
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, urldefrag

from playwright.sync_api import Page, sync_playwright

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
    host = normalize_host(parsed.hostname or "")
    if host not in {"instagram.com", "instagr.am"}:
        return None

    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    username = _extract_instagram_username(urlunsplit(("https", "instagram.com", path, "", "")))
    if not username:
        return None
    return f"https://www.instagram.com/{username}/"


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
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        fieldnames = list(reader.fieldnames or [])
        for index, row in enumerate(reader, start=1):
            if index != row_number:
                continue
            return _extract_site_from_row(row, fieldnames, site_column)
    return None


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


def _collect_instagram_links_for_site(browser, site: str) -> list[dict[str, str]]:
    homepage = normalize_site_url(site)
    if not homepage:
        return []

    page = browser.new_page()
    try:
        page.goto(homepage, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_000)

        found: list[dict[str, str]] = []
        found.extend(_extract_instagram_links_from_scope(page, HEADER_SELECTORS, "header"))
        found.extend(_extract_instagram_links_from_scope(page, FOOTER_SELECTORS, "footer"))

        results: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in found:
            url = item["instagram_url"]
            if url in seen:
                continue
            seen.add(url)
            results.append(
                {
                    "site": homepage,
                    "source_scope": item["source_scope"],
                    "instagram_url": url,
                    "instagram_username": item.get("instagram_username", ""),
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
            return _collect_instagram_links_for_site(browser, site)
        finally:
            browser.close()


def process_csv_row_by_row(
        csv_path: str | Path,
        site_column: str | None = None,
        limit: int | None = None,
        start_row: int = 1,
        end_row: int | None = None,
) -> list[dict[str, object]]:
    path = Path(csv_path)
    rows_output: list[dict[str, object]] = []
    start = max(1, start_row)
    end = end_row if end_row is None or end_row >= 1 else None

    with path.open("r", encoding="utf-8-sig", newline="") as file:
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
                            "instagram_links": _collect_instagram_links_for_site(browser, site),
                        }
                    )
            finally:
                browser.close()
    return rows_output


def _flatten_rows_for_csv(rows: list[dict[str, object]]) -> list[dict[str, str]]:
    flat_rows: list[dict[str, str]] = []
    for row in rows:
        row_number = str(row.get("row_number", ""))
        source_site = str(row.get("site", ""))
        links = row.get("instagram_links", [])
        if not isinstance(links, list) or not links:
            flat_rows.append(
                {
                    "row_number": row_number,
                    "source_site": source_site,
                    "site": "",
                    "source_scope": "",
                    "instagram_url": "",
                    "instagram_username": "",
                }
            )
            continue
        for item in links:
            flat_rows.append(
                {
                    "row_number": row_number,
                    "source_site": source_site,
                    "site": item.get("site", ""),
                    "source_scope": item.get("source_scope", ""),
                    "instagram_url": item.get("instagram_url", ""),
                    "instagram_username": item.get("instagram_username", ""),
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
        "instagram_url",
        "instagram_username",
    )
    with output_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})


def cli() -> None:
    parser = argparse.ArgumentParser(
        description="Проверка Instagram-ссылок на главной странице сайта (header/footer) и чтение сайтов из CSV."
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
            rows = _flatten_rows_for_csv([{"row_number": "", "site": args.site, "instagram_links": result}])
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
            rows = _flatten_rows_for_csv([{"row_number": str(args.row), "site": site, "instagram_links": result}])
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
