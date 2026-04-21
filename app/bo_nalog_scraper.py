from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT_SECONDS = 45
DEFAULT_SECTION_TITLE = "Отчет о финансовых результатах - Упрощенная форма"
DEFAULT_FIELD_LABEL = "Выручка, млн ₽"
NUMERIC_RE = re.compile(r"[-−]?\d[\d\s\u00A0]*(?:[.,]\d+)?")
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


@dataclass(slots=True)
class RevenueResult:
    url: str
    organization_id: Optional[str]
    section_title: str
    field_label: str
    value_raw: Optional[str]
    value: Optional[float]
    year: Optional[int]
    source: str


class BoNalogScraperError(RuntimeError):
    """Raised when page fetching/parsing fails."""


def _normalize(text: str) -> str:
    return " ".join(text.replace("\u00A0", " ").split())


def _to_number(raw: Optional[str]) -> Optional[float]:
    if not raw:
        return None
    cleaned = raw.replace("\u00A0", " ").replace(" ", "").replace(",", ".").strip()
    if cleaned in {"-", "—", "–"}:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _extract_org_id(url: str) -> Optional[str]:
    match = re.search(r"/organizations-card/(\d+)", url)
    if match:
        return match.group(1)
    return None


def _fetch_html(url: str, timeout_seconds: int) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.7,en;q=0.6",
        },
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="ignore")
    except HTTPError as exc:
        raise BoNalogScraperError(f"HTTP error {exc.code} for {url}") from exc
    except URLError as exc:
        raise BoNalogScraperError(f"Network error for {url}: {exc}") from exc


def _fetch_rendered_html(url: str, timeout_seconds: int) -> str:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BoNalogScraperError(
            "Playwright is not installed. Install it with "
            "'uv run playwright install chromium' or run without --render."
        ) from exc

    timeout_ms = timeout_seconds * 1000
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=USER_AGENT,
                locale="ru-RU",
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except PlaywrightTimeoutError:
                # Some pages keep background requests open; DOM snapshot is still usable.
                pass
            page.wait_for_timeout(1000)
            html = page.content()
            context.close()
            browser.close()
            return html
    except Exception as exc:
        raise BoNalogScraperError(f"Failed to render page with Playwright: {exc}") from exc


def _open_playwright_page(url: str, timeout_seconds: int):
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BoNalogScraperError(
            "Playwright is not installed. Install it with "
            "'uv run playwright install chromium'."
        ) from exc

    timeout_ms = timeout_seconds * 1000
    try:
        playwright_ctx = sync_playwright().start()
        browser = playwright_ctx.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="ru-RU",
        )
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PlaywrightTimeoutError:
            pass
        page.wait_for_timeout(800)
    except Exception as exc:
        raise BoNalogScraperError(f"Failed to render page with Playwright: {exc}") from exc
    return playwright_ctx, browser, context, page


def _discover_year_tabs(page) -> list[int]:
    years = page.evaluate(
        """
        () => {
          const strictYear = /^(19|20)\\d{2}$/;
          const periodYear = /[?&]period=((19|20)\\d{2})(?:&|$)/i;
          const out = [];
          const seen = new Set();

          const pushYear = (value) => {
            const year = Number(value);
            if (!Number.isInteger(year)) return;
            if (year < 1990 || year > 2100) return;
            if (seen.has(year)) return;
            seen.add(year);
            out.push(year);
          };

          for (const node of document.querySelectorAll("*")) {
            const text = (node.textContent || "").replace(/\\s+/g, " ").trim();
            if (strictYear.test(text)) pushYear(text);

            if (node instanceof HTMLAnchorElement) {
              const href = node.getAttribute("href") || "";
              const m = href.match(periodYear);
              if (m) pushYear(m[1]);
            }

            for (const attr of ["data-year", "data-period", "aria-label", "title"]) {
              const value = node.getAttribute?.(attr);
              if (!value) continue;
              const cleaned = value.replace(/\\s+/g, " ").trim();
              if (strictYear.test(cleaned)) pushYear(cleaned);
              const m = cleaned.match(periodYear);
              if (m) pushYear(m[1]);
            }
          }
          return out;
        }
        """
    )
    return sorted({int(item) for item in years if isinstance(item, (int, float))}, reverse=True)


def _click_year_tab(page, year: int, timeout_ms: int) -> bool:
    try:
        clicked = page.evaluate(
            """
            (year) => {
              const yr = String(year);
              const strictYear = new RegExp(`^\\\\s*${yr}\\\\s*$`);
              const periodYear = new RegExp(`[?&]period=${yr}(?:&|$)`, "i");

              const isVisible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                if (style.display === "none" || style.visibility === "hidden") return false;
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              };

              const score = (el) => {
                let s = 0;
                const text = (el.textContent || "").replace(/\\s+/g, " ").trim();
                if (strictYear.test(text)) s += 5;
                if (el.matches("button,[role='tab'],a")) s += 3;
                if (el.closest("[role='tablist'],.tabs,.tab,.nav")) s += 2;
                for (const attr of ["data-year", "data-period", "aria-label", "title", "href"]) {
                  const val = el.getAttribute?.(attr);
                  if (!val) continue;
                  const cleaned = val.replace(/\\s+/g, " ").trim();
                  if (strictYear.test(cleaned)) s += 4;
                  if (periodYear.test(cleaned)) s += 4;
                }
                return s;
              };

              const candidates = [];
              for (const el of document.querySelectorAll("*")) {
                const text = (el.textContent || "").replace(/\\s+/g, " ").trim();
                let matched = strictYear.test(text);
                if (!matched) {
                  for (const attr of ["data-year", "data-period", "aria-label", "title", "href"]) {
                    const val = el.getAttribute?.(attr);
                    if (!val) continue;
                    const cleaned = val.replace(/\\s+/g, " ").trim();
                    if (strictYear.test(cleaned) || periodYear.test(cleaned)) {
                      matched = true;
                      break;
                    }
                  }
                }
                if (matched) candidates.push(el);
              }
              if (!candidates.length) return false;

              candidates.sort((a, b) => score(b) - score(a));
              const target = candidates.find((el) => isVisible(el)) || candidates[0];
              if (!target) return false;

              const clickable = target.closest("button,[role='tab'],a,[onclick]") || target;
              clickable.scrollIntoView({block: "center", inline: "center"});
              clickable.dispatchEvent(new MouseEvent("click", {bubbles: true, cancelable: true, view: window}));
              if (typeof clickable.click === "function") clickable.click();
              return true;
            }
            """,
            year,
        )
    except Exception:
        return False

    if not clicked:
        return False
    page.wait_for_timeout(min(timeout_ms, 900))
    return True


def _has_section_ancestor(tag: Tag, section_title: str) -> bool:
    section_key = _normalize(section_title).casefold()
    current = tag
    steps = 0
    while current is not None and steps < 12:
        text = _normalize(current.get_text(" ", strip=True)).casefold()
        if section_key in text:
            return True
        current = current.parent if isinstance(current.parent, Tag) else None
        steps += 1
    return False


def _extract_from_row(tag: Tag, field_label: str) -> tuple[Optional[str], Optional[int]]:
    field_key = _normalize(field_label).casefold()
    row = tag.find_parent("tr")
    if row:
        row_cells = row.find_all(["th", "td"])
        label_index: Optional[int] = None
        for index, row_cell in enumerate(row_cells):
            cell_text = _normalize(row_cell.get_text(separator=" ", strip=True)).casefold()
            if field_key in cell_text:
                label_index = index
                break
        if label_index is None:
            label_index = -1

        value_candidates: list[tuple[Optional[int], str]] = []
        table = row.find_parent("table")

        for index, row_cell in enumerate(row_cells):
            if index <= label_index:
                continue
            cell_text = _normalize(row_cell.get_text(" ", strip=True))
            numeric_match = NUMERIC_RE.search(cell_text)
            if not numeric_match:
                continue

            column_year: Optional[int] = None
            if table:
                header_rows = row.find_previous_siblings("tr")
                for header_row in header_rows:
                    header_cells = header_row.find_all(["th", "td"])
                    if index >= len(header_cells):
                        continue
                    header_text = _normalize(header_cells[index].get_text(" ", strip=True))
                    year_match = YEAR_RE.search(header_text)
                    if year_match:
                        column_year = int(year_match.group(0))
                        break

            value_candidates.append((column_year, numeric_match.group(0)))

        if not value_candidates:
            return None, None
        with_year = [candidate for candidate in value_candidates if candidate[0] is not None]
        if with_year:
            selected_year, selected_value = max(with_year, key=lambda item: item[0])
            return selected_value, selected_year
        selected_year, selected_value = value_candidates[0]
        return selected_value, selected_year

    context_candidates = [tag]
    parent = tag.parent if isinstance(tag.parent, Tag) else None
    if parent is not None:
        context_candidates.append(parent)
    if parent is not None and isinstance(parent.parent, Tag):
        context_candidates.append(parent.parent)

    for candidate in context_candidates:
        chunks = [_normalize(chunk) for chunk in candidate.stripped_strings]
        if not chunks:
            continue
        for index, chunk in enumerate(chunks):
            if field_key not in chunk.casefold():
                continue
            for look_ahead in chunks[index + 1 : index + 6]:
                match = NUMERIC_RE.search(look_ahead)
                if not match:
                    continue
                year_match = YEAR_RE.search(look_ahead)
                year = int(year_match.group(0)) if year_match else None
                return match.group(0), year

    return None, None


def _extract_from_html(
    html: str,
    url: str,
    section_title: str,
    field_label: str,
    source: str,
) -> RevenueResult:
    soup = BeautifulSoup(html, "html.parser")
    field_key = _normalize(field_label).casefold()
    found_tag: Optional[Tag] = None
    best_text_len: Optional[int] = None

    for tag in soup.find_all(True):
        text = _normalize(tag.get_text(" ", strip=True))
        if not text:
            continue
        if field_key not in text.casefold():
            continue
        if not _has_section_ancestor(tag, section_title):
            continue
        text_len = len(text)
        if best_text_len is None or text_len < best_text_len:
            found_tag = tag
            best_text_len = text_len

    value_raw: Optional[str] = None
    year: Optional[int] = None

    if found_tag is not None:
        value_raw, year = _extract_from_row(found_tag, field_label)

    if value_raw is None:
        whole_text = _normalize(soup.get_text(" ", strip=True))
        section_key = _normalize(section_title)
        section_pos = whole_text.casefold().find(section_key.casefold())
        if section_pos != -1:
            window = whole_text[section_pos : section_pos + 5000]
        else:
            window = whole_text
        pattern = re.compile(
            rf"{re.escape(_normalize(field_label))}\s*[:\-]?\s*([-−]?\d[\d\s\u00A0]*(?:[.,]\d+)?)",
            re.IGNORECASE,
        )
        fallback_match = pattern.search(window)
        if fallback_match:
            value_raw = fallback_match.group(1)
        if year is None:
            years = [int(match.group(0)) for match in YEAR_RE.finditer(window)]
            if years:
                year = max(years)

    return RevenueResult(
        url=url,
        organization_id=_extract_org_id(url),
        section_title=section_title,
        field_label=field_label,
        value_raw=value_raw,
        value=_to_number(value_raw),
        year=year,
        source=source,
    )


def scrape_revenue(
    url: str,
    section_title: str = DEFAULT_SECTION_TITLE,
    field_label: str = DEFAULT_FIELD_LABEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    render: bool = False,
    fallback_render: bool = True,
) -> RevenueResult:
    if render:
        html = _fetch_rendered_html(url, timeout_seconds)
        return _extract_from_html(
            html=html,
            url=url,
            section_title=section_title,
            field_label=field_label,
            source="playwright",
        )

    html = _fetch_html(url, timeout_seconds)
    result = _extract_from_html(
        html=html,
        url=url,
        section_title=section_title,
        field_label=field_label,
        source="http",
    )
    if result.value_raw is not None or not fallback_render:
        return result

    try:
        rendered_html = _fetch_rendered_html(url, timeout_seconds)
    except BoNalogScraperError:
        return result
    rendered_result = _extract_from_html(
        html=rendered_html,
        url=url,
        section_title=section_title,
        field_label=field_label,
        source="playwright",
    )
    return rendered_result if rendered_result.value_raw is not None else result


def scrape_revenue_all_years(
    url: str,
    section_title: str = DEFAULT_SECTION_TITLE,
    field_label: str = DEFAULT_FIELD_LABEL,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> list[RevenueResult]:
    timeout_ms = timeout_seconds * 1000
    playwright_ctx, browser, context, page = _open_playwright_page(url, timeout_seconds)
    try:
        results_by_year: dict[int, RevenueResult] = {}

        html = page.content()
        first = _extract_from_html(
            html=html,
            url=url,
            section_title=section_title,
            field_label=field_label,
            source="playwright",
        )
        if first.year is not None:
            results_by_year[first.year] = first

        years = _discover_year_tabs(page)
        for year in years:
            if not _click_year_tab(page, year, timeout_ms):
                continue
            parsed = _extract_from_html(
                html=page.content(),
                url=url,
                section_title=section_title,
                field_label=field_label,
                source="playwright",
            )
            if parsed.year is None:
                parsed = RevenueResult(
                    url=parsed.url,
                    organization_id=parsed.organization_id,
                    section_title=parsed.section_title,
                    field_label=parsed.field_label,
                    value_raw=parsed.value_raw,
                    value=parsed.value,
                    year=year,
                    source=parsed.source,
                )
            existing = results_by_year.get(parsed.year) if parsed.year is not None else None
            if parsed.year is not None and (existing is None or (existing.value_raw is None and parsed.value_raw is not None)):
                results_by_year[parsed.year] = parsed

        if results_by_year:
            return [results_by_year[year] for year in sorted(results_by_year.keys(), reverse=True)]
        return [first]
    finally:
        context.close()
        browser.close()
        playwright_ctx.stop()


def _build_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url
    if args.org_id:
        return f"https://bo.nalog.gov.ru/organizations-card/{args.org_id}"
    raise BoNalogScraperError("Specify either --url or --org-id.")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scrape field value from bo.nalog.gov.ru organization card. "
            "Default target: 'Выручка, млн ₽' from "
            "'Отчет о финансовых результатах - Упрощенная форма'."
        )
    )
    parser.add_argument("--url")
    parser.add_argument("--org-id")
    parser.add_argument("--section-title", default=DEFAULT_SECTION_TITLE)
    parser.add_argument("--field-label", default=DEFAULT_FIELD_LABEL)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--render",
        action="store_true",
        help="Use Playwright rendering (useful when data is loaded by JavaScript).",
    )
    parser.add_argument(
        "--no-fallback-render",
        action="store_true",
        help="Disable automatic Playwright fallback when plain HTTP parsing finds no value.",
    )
    parser.add_argument(
        "--all-years",
        action="store_true",
        help="Collect values across all year tabs (uses Playwright).",
    )
    parser.add_argument("--json", action="store_true", help="Print output as JSON.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        url = _build_url(args)
        if args.all_years:
            all_results = scrape_revenue_all_years(
                url=url,
                section_title=args.section_title,
                field_label=args.field_label,
                timeout_seconds=args.timeout,
            )
        else:
            result = scrape_revenue(
                url=url,
                section_title=args.section_title,
                field_label=args.field_label,
                timeout_seconds=args.timeout,
                render=args.render,
                fallback_render=not args.no_fallback_render,
            )
    except BoNalogScraperError as exc:
        print(f"Error: {exc}")
        return 1

    if args.all_years:
        if args.json:
            print(json.dumps([asdict(item) for item in all_results], ensure_ascii=False, indent=2))
            return 0
        if not all_results:
            print("No values found.")
            return 2
        print(f"URL: {url}")
        print(f"Найдено годов: {len(all_results)}")
        for item in all_results:
            year_text = str(item.year) if item.year is not None else "n/a"
            raw = item.value_raw if item.value_raw is not None else "not found"
            number = item.value if item.value is not None else "not found"
            print(f"{year_text}: raw={raw}, float={number}")
        return 0 if any(item.value_raw is not None for item in all_results) else 2

    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    print(f"URL: {result.url}")
    print(f"Организация ID: {result.organization_id or 'n/a'}")
    print(f"Раздел: {result.section_title}")
    print(f"Поле: {result.field_label}")
    print(f"Источник: {result.source}")
    print(f"Год: {result.year if result.year is not None else 'n/a'}")
    print(f"Значение (raw): {result.value_raw if result.value_raw is not None else 'not found'}")
    print(f"Значение (float): {result.value if result.value is not None else 'not found'}")
    return 0 if result.value_raw is not None else 2


if __name__ == "__main__":
    raise SystemExit(main())
