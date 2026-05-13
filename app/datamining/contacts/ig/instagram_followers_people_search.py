import argparse
import asyncio
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence

from playwright.async_api import Browser
from playwright.async_api import BrowserContext
from playwright.async_api import Page
from playwright.async_api import Playwright
from playwright.async_api import async_playwright

if __package__ in {None, ""}:
    sys.path.append(str(Path(__file__).resolve().parents[4]))

from app.datamining.contacts.ig.instagram_followers_name_search import (
    _cleanup_stale_profile_locks,
    _is_instagram_login_required,
    _open_followers_dialog,
    _search_name_in_followers,
    _wait_for_instagram_login,
    normalize_instagram_input_url,
)
from app.datamining.contacts.mail.email_generator import clean_name_token
from app.datamining.contacts.mail.email_generator import name_variants

DETAIL_FIELDNAMES = (
    "source_instagram_url",
    "resolved_instagram_url",
    "target_person",
    "searched_name",
    "found_username",
    "found_full_name",
    "found_profile_url",
    "matched_text",
    "match_score",
    "status",
    "confidence",
    "error",
)
SUMMARY_FIELDNAMES = (
    "source_instagram_url",
    "resolved_instagram_url",
    "target_person",
    "status",
    "matched_profiles_count",
    "matched_profiles",
    "queries_checked",
)


@dataclass(frozen=True, slots=True)
class PersonName:
    raw: str
    first_name: str
    last_name: str


@dataclass(frozen=True, slots=True)
class InstagramPeopleSearchConfig:
    profile_url: str
    people: tuple[str, ...]
    output_csv: Path
    summary_csv: Path
    user_data_dir: str = ".playwright/instagram-profile"
    cdp_endpoint: str = ""
    headless: bool = False
    browser_channel: str = ""
    login_wait_seconds: int = 300
    no_login_prompt: bool = False
    timeout_seconds: int = 60
    max_result_scrolls: int = 8
    scroll_pause_ms: int = 400
    max_name_variants: int = 8
    name_order: str = "first-last"
    include_not_found: bool = True


def _normalize_text(value: str) -> str:
    text = (value or "").replace("ё", "е").replace("Ё", "Е").lower()
    text = re.sub(r"[^0-9a-zа-я._@]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _read_people_from_csv(path: Path, column: str) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if not reader.fieldnames:
            return []
        if column not in reader.fieldnames:
            raise SystemExit(f"Column not found in people CSV: {column}")
        return [str(row.get(column) or "").strip() for row in reader if str(row.get(column) or "").strip()]


def read_people(args: argparse.Namespace) -> list[str]:
    people: list[str] = []
    if args.people:
        people.extend(item.strip() for item in args.people.split(",") if item.strip())
    if args.people_file:
        path = Path(args.people_file).expanduser().resolve()
        if path.suffix.lower() == ".csv":
            people.extend(_read_people_from_csv(path, args.person_column))
        else:
            with path.open("r", encoding="utf-8-sig") as file:
                people.extend(line.strip() for line in file if line.strip())

    result: list[str] = []
    seen: set[str] = set()
    for person in people:
        key = _normalize_text(person)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(person)
    return result


def split_person_name(person: str, name_order: str) -> PersonName:
    tokens = re.findall(r"[A-Za-zА-Яа-яЁё-]+", person)
    if len(tokens) < 2:
        raise SystemExit(f"Person must contain at least first and last name: {person}")
    if name_order == "last-first":
        last_name, first_name = tokens[0], tokens[1]
    else:
        first_name, last_name = tokens[0], tokens[1]
    return PersonName(raw=person.strip(), first_name=first_name, last_name=last_name)


def _append_unique(items: list[str], value: str) -> None:
    value = re.sub(r"\s+", " ", value.strip())
    if value and value not in items:
        items.append(value)


def build_search_queries(person: str, *, name_order: str = "first-last", max_name_variants: int = 8) -> list[str]:
    parsed = split_person_name(person, name_order)
    first_variants = name_variants(parsed.first_name, is_first_name=True, max_variants=max_name_variants)
    last_variants = name_variants(parsed.last_name, is_first_name=False, max_variants=max_name_variants)
    clean_first = clean_name_token(parsed.first_name)
    clean_last = clean_name_token(parsed.last_name)

    queries: list[str] = []
    _append_unique(queries, parsed.raw)
    _append_unique(queries, parsed.last_name)

    for last in last_variants:
        _append_unique(queries, last)

    for first in first_variants[:4]:
        for last in last_variants[:4]:
            _append_unique(queries, f"{first} {last}")

    for first in first_variants[:4]:
        for last in last_variants[:4]:
            _append_unique(queries, f"{first}{last}")

    if clean_first and clean_last:
        compact_raw = re.sub(r"[^0-9a-zа-я]+", "", f"{clean_first}{clean_last}".lower())
        _append_unique(queries, compact_raw)

    return queries


def _person_variant_payload(person: str, *, name_order: str, max_name_variants: int) -> tuple[list[str], list[str]]:
    parsed = split_person_name(person, name_order)
    first_variants = [parsed.first_name, *name_variants(parsed.first_name, is_first_name=True, max_variants=max_name_variants)]
    last_variants = [parsed.last_name, *name_variants(parsed.last_name, is_first_name=False, max_variants=max_name_variants)]
    return [_normalize_text(item) for item in first_variants if item], [_normalize_text(item) for item in last_variants if item]


def confidence_for_match(
    person: str,
    row: Mapping[str, str],
    *,
    name_order: str,
    max_name_variants: int,
) -> str:
    if row.get("status") != "ok":
        return ""

    first_variants, last_variants = _person_variant_payload(
        person,
        name_order=name_order,
        max_name_variants=max_name_variants,
    )
    payload = _normalize_text(
        " ".join(
            [
                row.get("found_username", ""),
                row.get("found_full_name", ""),
                row.get("matched_text", ""),
            ]
        )
    )
    username = _normalize_text(row.get("found_username", ""))

    has_first = any(first and first in payload for first in first_variants)
    has_last = any(last and last in payload for last in last_variants)
    has_compact = any(
        f"{first}{last}" and f"{first}{last}" in username
        for first in first_variants
        for last in last_variants
        if first and last
    )

    if has_first and has_last:
        return "high"
    if has_compact:
        return "high"
    if has_last:
        return "medium"
    return "low"


async def _create_persistent_context(config: InstagramPeopleSearchConfig) -> tuple[Playwright, BrowserContext, Page, None]:
    profile_dir = Path(config.user_data_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_profile_locks(profile_dir)

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": bool(config.headless),
        "locale": "ru-RU",
        "viewport": {"width": 1440, "height": 900},
    }
    if config.browser_channel:
        launch_kwargs["channel"] = config.browser_channel

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else await context.new_page()
    print(f"Instagram profile: {profile_dir}")
    return playwright, context, page, None


async def _connect_over_cdp(config: InstagramPeopleSearchConfig) -> tuple[Playwright, BrowserContext, Page, Browser]:
    playwright = await async_playwright().start()
    browser = await playwright.chromium.connect_over_cdp(config.cdp_endpoint)
    context = browser.contexts[0] if browser.contexts else await browser.new_context()
    normalized_profile_url = normalize_instagram_input_url(config.profile_url) or config.profile_url
    page = next((item for item in context.pages if item.url.rstrip("/") == normalized_profile_url.rstrip("/")), None)
    if page is None:
        page = next((item for item in context.pages if "instagram.com" in item.url), None)
    if page is None:
        page = await context.new_page()
    print(f"Connected to Chrome over CDP: {config.cdp_endpoint}")
    return playwright, context, page, browser


async def _create_page(config: InstagramPeopleSearchConfig) -> tuple[Playwright, BrowserContext, Page, Browser | None]:
    if config.cdp_endpoint:
        return await _connect_over_cdp(config)
    return await _create_persistent_context(config)


async def _ensure_login(page: Page, config: InstagramPeopleSearchConfig, timeout_ms: int) -> None:
    try:
        await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1_000)
    except Exception:
        pass

    if await _is_instagram_login_required(page):
        wait_seconds = max(10, int(config.login_wait_seconds))
        print(f"Ожидание ручного логина в Instagram: до {wait_seconds} сек.")
        if not await _wait_for_instagram_login(page, wait_seconds=wait_seconds):
            raise SystemExit("Логин в Instagram не завершён. Завершите вход и запустите скрипт снова.")

    if not config.no_login_prompt:
        print("Проверьте, что Instagram открыт в браузере и аккаунт залогинен.")
        input("После входа нажмите Enter, чтобы начать поиск: ")


async def search_people_in_profile(
    page: Page,
    config: InstagramPeopleSearchConfig,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    resolved_url = normalize_instagram_input_url(config.profile_url)
    if not resolved_url:
        raise SystemExit(f"Unsupported Instagram profile URL: {config.profile_url}")

    timeout_ms = max(5, config.timeout_seconds) * 1000
    await page.goto(resolved_url, wait_until="domcontentloaded", timeout=timeout_ms)
    await page.wait_for_timeout(1_500)

    if await _is_instagram_login_required(page):
        raise SystemExit("Instagram login is required.")

    opened, error = await _open_followers_dialog(page, resolved_url, timeout_ms=timeout_ms)
    if not opened:
        raise SystemExit(f"Followers dialog was not opened: {error}")

    detail_rows: list[dict[str, str]] = []
    queries_by_person = {
        person: build_search_queries(
            person,
            name_order=config.name_order,
            max_name_variants=config.max_name_variants,
        )
        for person in config.people
    }

    for person, queries in queries_by_person.items():
        print(f"{person}: {len(queries)} queries")
        for query in queries:
            rows = await _search_name_in_followers(
                page=page,
                source_url=config.profile_url,
                resolved_url=resolved_url,
                target_name=query,
                max_result_scrolls=config.max_result_scrolls,
                scroll_pause_ms=config.scroll_pause_ms,
            )
            ok_rows = [row for row in rows if row.get("status") == "ok"]
            if ok_rows:
                for row in ok_rows:
                    detail_row = dict(row)
                    detail_row["target_person"] = person
                    detail_row["confidence"] = confidence_for_match(
                        person,
                        detail_row,
                        name_order=config.name_order,
                        max_name_variants=config.max_name_variants,
                    )
                    detail_rows.append(detail_row)
                    print(f"  ok {query}: {detail_row.get('found_username')} ({detail_row.get('confidence')})")
            elif any(row.get("status") != "ok" and row.get("status") != "not_found" for row in rows):
                for row in rows:
                    detail_row = dict(row)
                    detail_row["target_person"] = person
                    detail_row["confidence"] = ""
                    detail_rows.append(detail_row)
                    print(f"  error {query}: {detail_row.get('status')}")
            elif config.include_not_found:
                detail_rows.append(
                    {
                        "source_instagram_url": config.profile_url,
                        "resolved_instagram_url": resolved_url,
                        "target_person": person,
                        "searched_name": query,
                        "found_username": "",
                        "found_full_name": "",
                        "found_profile_url": "",
                        "matched_text": "",
                        "match_score": "",
                        "status": "not_found",
                        "confidence": "",
                        "error": "",
                    }
                )

    detail_rows = dedupe_detail_rows(detail_rows, config.people)
    summary_rows = build_summary_rows(config.profile_url, resolved_url, config.people, queries_by_person, detail_rows)
    return detail_rows, summary_rows


def _match_rank(row: Mapping[str, str]) -> tuple[int, int]:
    confidence_rank = {"high": 3, "medium": 2, "low": 1}.get(row.get("confidence", ""), 0)
    try:
        score = int(row.get("match_score") or 0)
    except ValueError:
        score = 0
    return confidence_rank, score


def dedupe_detail_rows(
    detail_rows: Sequence[Mapping[str, str]],
    people: Sequence[str],
) -> list[dict[str, str]]:
    best_by_person_profile: dict[tuple[str, str], dict[str, str]] = {}
    errors: list[dict[str, str]] = []
    not_found_by_person: dict[str, dict[str, str]] = {}

    for row in detail_rows:
        copied = dict(row)
        person = copied.get("target_person", "")
        status = copied.get("status", "")
        profile_url = copied.get("found_profile_url", "")

        if status == "ok" and profile_url:
            key = (person, profile_url)
            current = best_by_person_profile.get(key)
            if current is None or _match_rank(copied) > _match_rank(current):
                best_by_person_profile[key] = copied
            continue

        if status == "not_found":
            not_found_by_person.setdefault(person, copied)
            continue

        errors.append(copied)

    found_people = {person for person, _ in best_by_person_profile}
    deduped: list[dict[str, str]] = sorted(
        best_by_person_profile.values(),
        key=lambda row: (row.get("target_person", ""), *_match_rank(row), row.get("found_profile_url", "")),
        reverse=True,
    )
    deduped.extend(errors)

    for person in people:
        if person not in found_people and person in not_found_by_person:
            row = not_found_by_person[person]
            row["searched_name"] = ""
            deduped.append(row)

    return deduped


def build_summary_rows(
    source_url: str,
    resolved_url: str,
    people: Sequence[str],
    queries_by_person: Mapping[str, Sequence[str]],
    detail_rows: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    summaries: list[dict[str, str]] = []
    for person in people:
        best_by_profile: dict[str, Mapping[str, str]] = {}
        for row in detail_rows:
            if row.get("target_person") != person or row.get("status") != "ok":
                continue
            profile_url = row.get("found_profile_url", "")
            if not profile_url:
                continue
            current = best_by_profile.get(profile_url)
            if current is None or _match_rank(row) > _match_rank(current):
                best_by_profile[profile_url] = row

        matches = sorted(best_by_profile.values(), key=_match_rank, reverse=True)
        summaries.append(
            {
                "source_instagram_url": source_url,
                "resolved_instagram_url": resolved_url,
                "target_person": person,
                "status": "found" if matches else "not_found",
                "matched_profiles_count": str(len(matches)),
                "matched_profiles": "; ".join(
                    (
                        f"{row.get('found_username', '')} | {row.get('found_full_name', '')} | "
                        f"{row.get('found_profile_url', '')} | confidence={row.get('confidence', '')} | "
                        f"query={row.get('searched_name', '')}"
                    )
                    for row in matches
                ),
                "queries_checked": "; ".join(queries_by_person.get(person, ())),
            }
        )
    return summaries


def write_csv(path: Path, fieldnames: Sequence[str], rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def default_summary_path(output_csv: Path) -> Path:
    if output_csv.suffix.lower() == ".csv":
        return output_csv.with_name(f"{output_csv.stem}_summary.csv")
    return output_csv.with_name(f"{output_csv.name}_summary.csv")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search people in an Instagram profile followers dialog using generated Russian/translit queries."
    )
    parser.add_argument("--profile-url", required=True, help="Instagram profile whose followers should be searched.")
    parser.add_argument("--people", help="People to search, comma-separated. Example: 'Дамир Мухамадиев,Анастасия Вышлова'.")
    parser.add_argument("--people-file", help="TXT/CSV with people to search.")
    parser.add_argument("--person-column", default="person", help="CSV column for --people-file.")
    parser.add_argument(
        "--output-csv",
        default="output/instagram_followers_people_search.csv",
        help="Detailed output CSV.",
    )
    parser.add_argument("--summary-csv", help="Summary output CSV. Defaults to <output>_summary.csv.")
    parser.add_argument(
        "--name-order",
        choices=("first-last", "last-first"),
        default="first-last",
        help="How to parse two-part names. Default matches 'Дамир Мухамадиев'.",
    )
    parser.add_argument("--max-name-variants", type=int, default=8, help="Max transliteration variants per name part.")
    parser.add_argument(
        "--cdp-endpoint",
        default="",
        help="Connect to an existing Chrome launched with --remote-debugging-port, e.g. http://127.0.0.1:9222.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=".playwright/instagram-profile",
        help="Playwright profile dir for non-CDP mode.",
    )
    parser.add_argument("--headless", action="store_true", help="Run browser headless in non-CDP mode.")
    parser.add_argument("--browser-channel", default="", help="Browser channel for non-CDP mode, e.g. chrome.")
    parser.add_argument("--login-wait-seconds", type=int, default=300, help="Manual login wait time.")
    parser.add_argument("--no-login-prompt", action="store_true", help="Do not wait for Enter before searching.")
    parser.add_argument("--timeout-seconds", type=int, default=60, help="Navigation timeout.")
    parser.add_argument("--max-result-scrolls", type=int, default=8, help="Scroll attempts per query.")
    parser.add_argument("--scroll-pause-ms", type=int, default=400, help="Pause after scrolling search results.")
    parser.add_argument(
        "--skip-not-found",
        action="store_true",
        help="Do not write not_found rows to the detailed CSV.",
    )
    return parser.parse_args()


def config_from_args(args: argparse.Namespace) -> InstagramPeopleSearchConfig:
    people = tuple(read_people(args))
    if not people:
        raise SystemExit("Set --people or --people-file.")
    output_csv = Path(args.output_csv).expanduser().resolve()
    summary_csv = Path(args.summary_csv).expanduser().resolve() if args.summary_csv else default_summary_path(output_csv)
    return InstagramPeopleSearchConfig(
        profile_url=args.profile_url,
        people=people,
        output_csv=output_csv,
        summary_csv=summary_csv,
        user_data_dir=args.user_data_dir,
        cdp_endpoint=args.cdp_endpoint,
        headless=args.headless,
        browser_channel=args.browser_channel,
        login_wait_seconds=args.login_wait_seconds,
        no_login_prompt=args.no_login_prompt,
        timeout_seconds=args.timeout_seconds,
        max_result_scrolls=args.max_result_scrolls,
        scroll_pause_ms=args.scroll_pause_ms,
        max_name_variants=max(1, args.max_name_variants),
        name_order=args.name_order,
        include_not_found=not args.skip_not_found,
    )


async def run(config: InstagramPeopleSearchConfig | None = None) -> int:
    config = config or config_from_args(parse_args())
    playwright: Playwright | None = None
    context: BrowserContext | None = None
    cdp_browser: Browser | None = None
    try:
        playwright, context, page, cdp_browser = await _create_page(config)
        timeout_ms = max(5, config.timeout_seconds) * 1000
        await _ensure_login(page, config, timeout_ms)
        detail_rows, summary_rows = await search_people_in_profile(page, config)
        write_csv(config.output_csv, DETAIL_FIELDNAMES, detail_rows)
        write_csv(config.summary_csv, SUMMARY_FIELDNAMES, summary_rows)
        print(f"Saved {len(detail_rows)} detail rows to {config.output_csv}")
        print(f"Saved {len(summary_rows)} summary rows to {config.summary_csv}")
        return 0
    finally:
        if cdp_browser is None and context is not None:
            await context.close()
        if playwright is not None:
            await playwright.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
