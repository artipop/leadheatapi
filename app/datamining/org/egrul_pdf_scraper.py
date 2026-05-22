import argparse
import json
import re

from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

EGRUL_SEARCH_URL = "https://egrul.nalog.ru/index.html"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
SECTION_EXECUTIVE = (
    "Сведения о лице, имеющем право без доверенности действовать "
    "от имени юридического лица"
)
SECTION_FOUNDERS = "Сведения об участниках / учредителях юридического лица"
BTN_NAME_RE = re.compile("найти", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
GRN_RE = re.compile(r"\bгрн\b.*?((?:\d[\s\u00A0]*){13})", re.IGNORECASE)
EMAIL_SECTION_RE = re.compile(r"^\s*(?:\d+\s+)?Адрес электронной почты\b(.*)$", re.IGNORECASE)


class EgrulScraperError(RuntimeError):
    """Raised when PDF parsing fails."""


def normalize_inn(raw: str) -> str:
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) not in {10, 12}:
        raise EgrulScraperError("INN must contain 10 or 12 digits.")
    return digits


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\u00A0", " ").split()).strip().lower()


def _single_line(text: str) -> str:
    return " ".join((text or "").replace("\u00A0", " ").split()).strip()


def _click_search_button(page, timeout_ms: int) -> None:
    button_candidates = [
        page.get_by_role("button", name=BTN_NAME_RE),
        page.locator("button#btnSearch"),
        page.locator("button.btn-search"),
        page.locator("button:has-text('Найти')"),
        page.locator("input[type='submit']"),
    ]
    for locator in button_candidates:
        try:
            if locator.count() == 0:
                continue
            locator.first.click(timeout=timeout_ms)
            return
        except Exception:
            continue
    raise EgrulScraperError("Search button was not found or could not be clicked.")


def _fill_search_input(page, inn: str, timeout_ms: int) -> None:
    input_candidates = [
        page.locator("input#query"),
        page.locator("input[name='query']"),
        page.get_by_placeholder(re.compile("инн|огрн", re.IGNORECASE)),
        page.locator("input[type='text']"),
    ]
    for locator in input_candidates:
        try:
            if locator.count() == 0:
                continue
            target = locator.first
            target.click(timeout=timeout_ms)
            target.fill(inn, timeout=timeout_ms)
            return
        except Exception:
            continue
    raise EgrulScraperError("Search input was not found on egrul.nalog.ru page.")


def download_extract_pdf_by_inn(
    inn: str,
    out_dir: Path,
    timeout_seconds: int = 60,
    download_timeout_seconds: int = 120,
    headless: bool = True,
) -> tuple[Path, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EgrulScraperError(
            "Playwright is not installed. Install it with: uv add playwright && uv run playwright install chromium"
        ) from exc

    timeout_ms = timeout_seconds * 1000
    download_timeout_ms = download_timeout_seconds * 1000
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=headless)
        context = browser.new_context(
            accept_downloads=True,
            user_agent=USER_AGENT,
            locale="ru-RU",
        )
        try:
            page = context.new_page()
            try:
                page.goto(EGRUL_SEARCH_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                raise EgrulScraperError(
                    f"Could not open EGRUL page {EGRUL_SEARCH_URL}: {exc}"
                ) from exc

            _fill_search_input(page, inn=inn, timeout_ms=timeout_ms)
            _click_search_button(page, timeout_ms=timeout_ms)
            page.wait_for_timeout(1000)

            result_links = page.locator("a.op-excerpt[data-t]")
            try:
                result_links.first.wait_for(state="visible", timeout=timeout_ms)
            except Exception as exc:
                page_text = page.locator("body").inner_text(timeout=5000)
                if "капч" in _normalize_text(page_text):
                    raise EgrulScraperError(
                        "EGRUL page requested captcha; automated flow is blocked for this run."
                    ) from exc
                raise EgrulScraperError("No result links (a.op-excerpt[data-t]) found after search.") from exc

            links_count = result_links.count()
            if links_count == 0:
                raise EgrulScraperError("No result links found.")
            if links_count > 1:
                raise EgrulScraperError(f"Expected one result link, found {links_count}.")

            result_link = result_links.first
            result_title = result_link.get_attribute("title") or ""
            href_value = result_link.get_attribute("href") or ""
            absolute_href = urljoin(page.url, href_value)

            downloaded_path: Optional[Path] = None
            try:
                with page.expect_download(timeout=download_timeout_ms) as download_info:
                    result_link.click(timeout=timeout_ms)
                download = download_info.value
                suggested_name = download.suggested_filename or f"egrul_{inn}.pdf"
                if not suggested_name.lower().endswith(".pdf"):
                    suggested_name = f"{Path(suggested_name).stem or f'egrul_{inn}'}.pdf"
                downloaded_path = out_dir / suggested_name
                download.save_as(str(downloaded_path))
            except Exception:
                try:
                    with page.expect_download(timeout=download_timeout_ms) as download_info:
                        result_link.evaluate("el => el.click()")
                    download = download_info.value
                    suggested_name = download.suggested_filename or f"egrul_{inn}.pdf"
                    if not suggested_name.lower().endswith(".pdf"):
                        suggested_name = f"{Path(suggested_name).stem or f'egrul_{inn}'}.pdf"
                    downloaded_path = out_dir / suggested_name
                    download.save_as(str(downloaded_path))
                except Exception as exc:
                    raise EgrulScraperError(
                        "Failed to trigger PDF download from result link "
                        f"(href={absolute_href})."
                    ) from exc

            if downloaded_path is None:
                raise EgrulScraperError("Download finished with empty file path.")
            return downloaded_path, result_title
        finally:
            context.close()
            browser.close()


def _is_section_heading_text(normalized_line: str) -> bool:
    return normalized_line.startswith("сведения о ") or normalized_line.startswith("сведения об ")


def _match_labeled_value(line: str, label: str) -> Optional[str]:
    pattern = re.compile(rf"^\s*(?:\d+\s+)?{label}\s+(.+?)\s*$", re.IGNORECASE)
    match = pattern.match(line.strip())
    if not match:
        return None
    value = _single_line(match.group(1))
    return value or None


def _extract_lines_from_pdf(pdf_path: Path) -> list[str]:
    try:
        import pdfplumber
    except ImportError as exc:
        raise EgrulScraperError(
            "pdfplumber is not installed. Install it with: uv add pdfplumber"
        ) from exc

    lines: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text() or ""
            for raw_line in page_text.splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                normalized = _normalize_text(line)
                if normalized.startswith("выписка из егрюл"):
                    continue
                if re.match(r"^\d{2}\.\d{2}\.\d{4}\b.*\bстраница\s+\d+\s+из\s+\d+$", normalized):
                    continue
                lines.append(line)
    return lines


def _extract_legal_address(all_lines: list[str]) -> str:
    line_count = len(all_lines)
    for index, line in enumerate(all_lines):
        address_start = _match_labeled_value(line, "Адрес юридического лица")
        if address_start is None:
            continue

        parts: list[str] = [address_start] if address_start else []
        step = index + 1
        while step < line_count:
            probe_line = all_lines[step].strip()
            if not probe_line:
                step += 1
                continue
            probe_norm = _normalize_text(probe_line)
            if _is_section_heading_text(probe_norm):
                break
            if re.match(r"^\d+\s", probe_line):
                break
            parts.append(probe_line)
            step += 1

        value = _single_line(" ".join(parts))
        if value:
            return value
        return ""
    return ""


def _extract_grn_from_line(line: str) -> str:
    match = GRN_RE.search(line)
    if not match:
        return ""
    return "".join(ch for ch in match.group(1) if ch.isdigit())


def _extract_email_from_lines(lines: list[str]) -> str:
    if not lines:
        return ""

    compact_text = "".join(_single_line(line) for line in lines)
    spaced_text = _single_line(" ".join(lines))
    for text in (compact_text, spaced_text):
        match = EMAIL_RE.search(text)
        if match:
            return match.group(0).lower()
    return ""


def _extract_email_records(all_lines: list[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    line_count = len(all_lines)

    index = 0
    while index < line_count:
        line = all_lines[index]
        heading_match = EMAIL_SECTION_RE.match(line)
        if heading_match is None:
            index += 1
            continue

        heading_remainder = heading_match.group(1).strip()
        email_lines: list[str] = [heading_remainder] if heading_remainder else []
        grn = ""
        step = index + 1
        while step < line_count and step <= index + 12:
            probe_line = all_lines[step]
            probe_norm = _normalize_text(probe_line)
            if EMAIL_SECTION_RE.match(probe_line):
                break
            if _is_section_heading_text(probe_norm):
                break

            if "грн" in probe_norm:
                grn = _extract_grn_from_line(probe_line)
                break

            email_lines.append(probe_line)
            step += 1

        email = _extract_email_from_lines(email_lines)
        if email:
            key = (email, grn)
            if key not in seen:
                seen.add(key)
                records.append({"email": email, "grn": grn})

        index = max(index + 1, step)

    return records


def parse_only_filtered_from_pdf(pdf_path: Path) -> dict[str, list[str]]:
    if not pdf_path.exists():
        raise EgrulScraperError(f"PDF file does not exist: {pdf_path}")

    all_lines = _extract_lines_from_pdf(pdf_path)
    normalized_targets = {
        "executive_person": _normalize_text(SECTION_EXECUTIVE),
        "founders": _normalize_text(SECTION_FOUNDERS),
    }

    section_fio: dict[str, list[str]] = {
        SECTION_EXECUTIVE: [],
        SECTION_FOUNDERS: [],
    }
    current_section: Optional[str] = None

    line_count = len(all_lines)
    index = 0
    while index < line_count:
        line = all_lines[index]
        normalized_line = _normalize_text(line)

        if _is_section_heading_text(normalized_line):
            heading_combined = normalized_line
            look_ahead = 1
            while index + look_ahead < line_count and look_ahead <= 3:
                next_line = all_lines[index + look_ahead]
                next_normalized = _normalize_text(next_line)
                if not next_normalized:
                    look_ahead += 1
                    continue
                if _is_section_heading_text(next_normalized):
                    break
                if re.match(r"^\d+\s", next_normalized):
                    break
                heading_combined = f"{heading_combined} {next_normalized}"
                look_ahead += 1

            if normalized_targets["executive_person"] in heading_combined:
                current_section = SECTION_EXECUTIVE
            elif normalized_targets["founders"] in heading_combined:
                current_section = SECTION_FOUNDERS
            else:
                current_section = None

            index += 1
            continue

        if current_section is None:
            index += 1
            continue

        surname = _match_labeled_value(line, "Фамилия")
        if surname is None:
            index += 1
            continue

        name: Optional[str] = None
        patronymic: Optional[str] = None
        for step in range(1, 8):
            if index + step >= line_count:
                break
            probe_line = all_lines[index + step]
            probe_norm = _normalize_text(probe_line)
            if _is_section_heading_text(probe_norm):
                break
            if _match_labeled_value(probe_line, "Фамилия") is not None:
                break
            if name is None:
                name = _match_labeled_value(probe_line, "Имя")
            if patronymic is None:
                patronymic = _match_labeled_value(probe_line, "Отчество")
            if name is not None and patronymic is not None:
                break

        parts: list[str] = [part for part in (surname, name, patronymic) if part is not None]
        if len(parts) >= 2:
            fio = " ".join(parts)
            if fio not in section_fio[current_section]:
                section_fio[current_section].append(fio)

        index += 1

    return section_fio


def parse_egrul_pdf(pdf_path: Path, inn: str = "", result_title: str = "") -> dict[str, object]:
    all_lines = _extract_lines_from_pdf(pdf_path)
    sections = parse_only_filtered_from_pdf(pdf_path)
    legal_address = _extract_legal_address(all_lines)
    emails = _extract_email_records(all_lines)
    return {
        "inn": inn,
        "result_title": result_title or pdf_path.stem,
        "pdf_path": str(pdf_path.resolve()),
        "legal_address": legal_address,
        "emails": emails,
        "sections": sections,
    }


def extract_egrul_by_inn(
    inn: str,
    out_dir: Path,
    timeout_seconds: int = 60,
    download_timeout_seconds: int = 120,
    headless: bool = True,
) -> dict[str, object]:
    normalized_inn = normalize_inn(inn)
    pdf_path, result_title = download_extract_pdf_by_inn(
        inn=normalized_inn,
        out_dir=out_dir,
        timeout_seconds=timeout_seconds,
        download_timeout_seconds=download_timeout_seconds,
        headless=headless,
    )
    return parse_egrul_pdf(
        pdf_path=pdf_path,
        inn=normalized_inn,
        result_title=result_title,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download EGRUL PDF by INN (or read local PDF) and output only filtered FIO values "
            "for two sections."
        )
    )
    parser.add_argument("--inn", help="ИНН организации (если нужно скачать PDF с сайта).")
    parser.add_argument("--pdf-path", help="Path to local EGRUL PDF file.")
    parser.add_argument("--out-dir", default="output/egrul", help="Directory to save downloaded PDF.")
    parser.add_argument("--timeout", type=int, default=60, help="Navigation/search timeout in seconds.")
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=120,
        help="How long to wait for PDF download after clicking result link (seconds).",
    )
    parser.add_argument("--headful", action="store_true", help="Run browser in headed mode for debugging.")
    parser.add_argument(
        "--only-filtered",
        action="store_true",
        default=True,
        help="Enabled by default. Kept for backward compatibility.",
    )
    parser.add_argument("--json-out", help="Optional path to write JSON result.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.pdf_path and not args.inn:
        print("Error: specify either --inn (download PDF) or --pdf-path (local PDF).")
        return 1

    try:
        if args.pdf_path:
            pdf_path = Path(args.pdf_path)
            payload = parse_egrul_pdf(pdf_path=pdf_path, inn=args.inn or "")
        else:
            payload = extract_egrul_by_inn(
                inn=args.inn,
                out_dir=Path(args.out_dir),
                timeout_seconds=args.timeout,
                download_timeout_seconds=args.download_timeout,
                headless=not args.headful,
            )
    except EgrulScraperError as exc:
        print(f"Error: {exc}")
        return 1

    if args.json_out:
        output_path = Path(args.json_out)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
