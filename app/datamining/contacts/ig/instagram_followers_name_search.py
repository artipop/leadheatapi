import argparse
import asyncio
import csv
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence
from typing import TypedDict
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright

URL_COLUMN_CANDIDATES = ("instagram_url", "url", "profile_url", "instagram", "ig")
NAME_COLUMN_CANDIDATES = ("name", "full_name", "person_name", "fio", "person", "query")
INSTAGRAM_FOLLOWER_FIELDNAMES = (
    "source_instagram_url",
    "resolved_instagram_url",
    "searched_name",
    "found_username",
    "found_full_name",
    "found_profile_url",
    "matched_text",
    "match_score",
    "status",
    "error",
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


class InstagramFollowerRow(TypedDict):
    source_instagram_url: str
    resolved_instagram_url: str
    searched_name: str
    found_username: str
    found_full_name: str
    found_profile_url: str
    matched_text: str
    match_score: str
    status: str
    error: str


@dataclass(frozen=True, slots=True)
class InstagramFollowersSearchConfig:
    profile_url: str = ""
    input_csv: str | Path | None = None
    input_column: str = "instagram_url"
    output_csv: str | Path = "output/instagram_followers_name_matches.csv"
    target_names: tuple[str, ...] = ()
    user_data_dir: str = ".playwright/instagram-profile"
    headless: bool = False
    browser_channel: str = ""
    login_wait_seconds: int = 180
    no_login_prompt: bool = False
    timeout_seconds: int = 45
    limit: int | None = None
    max_result_scrolls: int = 6
    scroll_pause_ms: int = 400
    include_not_found: bool = False


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _cleanup_stale_profile_locks(profile_dir: Path) -> None:
    lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    lock_path = profile_dir / "SingletonLock"
    should_cleanup = True

    if lock_path.exists() or lock_path.is_symlink():
        try:
            target = os.readlink(lock_path)
            pid_match = re.search(r"-(\d+)$", target)
            if pid_match:
                should_cleanup = not _pid_is_running(int(pid_match.group(1)))
        except OSError:
            should_cleanup = True

    if not should_cleanup:
        return

    for name in lock_names:
        path = profile_dir / name
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
        except OSError:
            pass


def normalize_instagram_input_url(raw_url: str) -> str | None:
    value = (raw_url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"instagram.com", "instagr.am"}:
        return None

    parts = [part for part in (parsed.path or "").strip("/").split("/") if part]
    if not parts:
        return None
    username = parts[0].strip().lstrip("@")
    if username.lower() in INSTAGRAM_TECHNICAL_PATH_PREFIXES:
        return None
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return None
    return f"https://www.instagram.com/{username}/"


def _username_from_profile_url(profile_url: str) -> str:
    try:
        parsed = urlsplit(profile_url)
    except Exception:
        return ""
    parts = [part for part in (parsed.path or "").strip("/").split("/") if part]
    if not parts:
        return ""
    username = parts[0].strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
        return ""
    return username


def _canonical_instagram_profile_url(url: str) -> str:
    normalized = normalize_instagram_input_url(url)
    return normalized or url


def _looks_like_instagram_url(value: str) -> bool:
    text = (value or "").strip().lower()
    return "instagram.com/" in text or "instagr.am/" in text


def read_instagram_urls_from_csv(csv_path: Path, input_column: str | None) -> list[str]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    if not rows:
        return []

    first_row = rows[0]
    normalized_first_row = [cell.strip().lower() for cell in first_row]
    requested_column = (input_column or "").strip().lower()

    has_header = any(cell in URL_COLUMN_CANDIDATES for cell in normalized_first_row)
    if has_header:
        if requested_column and requested_column in normalized_first_row:
            column_index = normalized_first_row.index(requested_column)
        else:
            column_index = next(
                (idx for idx, name in enumerate(normalized_first_row) if name in URL_COLUMN_CANDIDATES),
                0,
            )
        data_rows = rows[1:]
    else:
        column_index = 0
        data_rows = rows

    urls: list[str] = []
    seen: set[str] = set()
    for row in data_rows:
        if column_index >= len(row):
            continue
        normalized = normalize_instagram_input_url(row[column_index])
        if not normalized:
            if not _looks_like_instagram_url(row[column_index]):
                continue
            urls.append(row[column_index].strip())
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


def _read_names_from_csv(path: Path, name_column: str | None) -> list[str]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.reader(file))
    if not rows:
        return []

    first_row = rows[0]
    normalized_first_row = [cell.strip().lower() for cell in first_row]
    requested_column = (name_column or "").strip().lower()
    has_header = any(cell in NAME_COLUMN_CANDIDATES for cell in normalized_first_row)
    if has_header:
        if requested_column and requested_column in normalized_first_row:
            column_index = normalized_first_row.index(requested_column)
        else:
            column_index = next(
                (idx for idx, name in enumerate(normalized_first_row) if name in NAME_COLUMN_CANDIDATES),
                0,
            )
        data_rows = rows[1:]
    else:
        column_index = 0
        data_rows = rows

    return [row[column_index].strip() for row in data_rows if column_index < len(row) and row[column_index].strip()]


def read_target_names(args: argparse.Namespace) -> list[str]:
    names: list[str] = []
    if args.names:
        for chunk in args.names.split(","):
            value = chunk.strip()
            if value:
                names.append(value)
    if args.names_file:
        path = Path(args.names_file).expanduser().resolve()
        if path.suffix.lower() == ".csv":
            names.extend(_read_names_from_csv(path, args.name_column))
        else:
            with path.open("r", encoding="utf-8-sig") as file:
                names.extend(line.strip() for line in file if line.strip())

    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = _normalize_name(name)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(name)
    return deduped


def _normalize_name(value: str) -> str:
    text = (value or "").replace("ё", "е").replace("Ё", "Е").lower()
    text = re.sub(r"[^0-9a-zа-я._@]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _name_tokens(value: str) -> list[str]:
    normalized = _normalize_name(value)
    return [token for token in normalized.split(" ") if token and token not in {"и", "the"}]


def _match_score(target_name: str, candidate_text: str, username: str, full_name: str) -> int:
    target_norm = _normalize_name(target_name)
    if not target_norm:
        return 0

    username_norm = _normalize_name(username)
    full_name_norm = _normalize_name(full_name)
    text_norm = _normalize_name(candidate_text)
    payload = " ".join(part for part in (username_norm, full_name_norm, text_norm) if part)

    if target_norm in {username_norm, full_name_norm}:
        return 100
    if target_norm and target_norm in payload:
        return 90

    tokens = _name_tokens(target_name)
    if len(tokens) >= 2 and all(token in payload for token in tokens):
        return 80
    if len(tokens) == 1 and tokens[0] in {username_norm, full_name_norm}:
        return 70
    if len(tokens) == 1 and tokens[0] in payload:
        return 55
    return 0


async def _is_instagram_login_required(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const url = location.href.toLowerCase();
                    if (url.includes('/accounts/login')) return true;
                    return !!document.querySelector(
                        'input[name="username"], input[name="password"], form[action*="/accounts/login"]'
                    );
                }"""
            )
        )
    except Exception:
        return False


async def _wait_for_instagram_login(page: Page, wait_seconds: int) -> bool:
    deadline = time.monotonic() + max(10, wait_seconds)
    while time.monotonic() < deadline:
        if not await _is_instagram_login_required(page):
            return True
        await page.wait_for_timeout(2_000)
    return not await _is_instagram_login_required(page)


async def _open_followers_dialog(page: Page, profile_url: str, timeout_ms: int) -> tuple[bool, str]:
    if await _wait_for_dialog(page, timeout_ms=1_000):
        return True, ""

    username = _username_from_profile_url(profile_url)
    followers_url = f"https://www.instagram.com/{username}/followers/" if username else ""
    selectors = [
        f'a[href="/{username}/followers/"]' if username else "",
        f'a[href$="/{username}/followers/"]' if username else "",
        'a[href$="/followers/"]',
        'a[href*="/followers/"]',
    ]
    text_selectors = [
        'a:has-text("followers")',
        'a:has-text("Followers")',
        'a:has-text("подпис")',
        'a:has-text("Подпис")',
    ]

    for selector in [item for item in [*selectors, *text_selectors] if item]:
        try:
            locator = page.locator(selector).first
            if await locator.count():
                await locator.click(timeout=min(timeout_ms, 6_000), force=True)
                if await _wait_for_dialog(page, timeout_ms=min(timeout_ms, 10_000)):
                    return True, ""
        except Exception:
            continue

    try:
        clicked = await page.evaluate(
            """() => {
                const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const candidates = Array.from(document.querySelectorAll('a, button, [role="link"], [role="button"]'));
                const followersNode = candidates.find((node) => {
                    const text = normalize(node.innerText || node.textContent || node.getAttribute('aria-label') || '');
                    return text.includes('followers') || text.includes('подпис');
                });
                if (!followersNode) return false;
                followersNode.click();
                return true;
            }"""
        )
        if clicked and await _wait_for_dialog(page, timeout_ms=min(timeout_ms, 10_000)):
            return True, ""
    except Exception:
        pass

    if followers_url:
        try:
            await page.goto(followers_url, wait_until="domcontentloaded", timeout=timeout_ms)
            if await _wait_for_dialog(page, timeout_ms=min(timeout_ms, 10_000)):
                return True, ""
        except Exception as exc:
            return False, str(exc)

    return False, "followers link/dialog not found"


async def _wait_for_dialog(page: Page, timeout_ms: int) -> bool:
    rounds = max(4, timeout_ms // 250)
    for _ in range(rounds):
        try:
            if await page.locator('div[role="dialog"]').count():
                return True
        except Exception:
            pass
        await page.wait_for_timeout(250)
    return False


async def _find_followers_search_input(page: Page):
    selectors = [
        'div[role="dialog"] input[placeholder*="Search" i]',
        'div[role="dialog"] input[placeholder*="Поиск" i]',
        'div[role="dialog"] input[type="text"]',
        'input[placeholder*="Search" i]',
        'input[placeholder*="Поиск" i]',
        'input[type="text"]',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible(timeout=1_000):
                return locator
        except Exception:
            continue
    return None


async def _clear_and_type_search(search_input: Any, query: str) -> None:
    try:
        await search_input.fill("")
    except Exception:
        await search_input.click(force=True)
        await search_input.press("Meta+A")
        await search_input.press("Backspace")
    await search_input.fill(query)


async def _extract_dialog_profiles(page: Page) -> list[dict[str, str]]:
    payload = await page.evaluate(
        """() => {
            const technical = new Set([
                'about', 'accounts', 'api', 'ar', 'blog', 'business', 'challenge', 'developer',
                'direct', 'directory', 'explore', 'graphql', 'help', 'invites', 'legal', 'oauth', 'p',
                'privacy', 'reel', 'reels', 'stories', 'terms', 'tv', 'web'
            ]);
            const dialog = document.querySelector('div[role="dialog"]') || document.body;
            const normalize = (value) => (value || '').replace(/\\s+/g, ' ').trim();
            const result = [];
            const seen = new Set();

            for (const link of Array.from(dialog.querySelectorAll('a[href]'))) {
                let url;
                try {
                    url = new URL(link.href, location.href);
                } catch {
                    continue;
                }
                let host = url.hostname.toLowerCase();
                if (host.startsWith('www.')) host = host.slice(4);
                if (host !== 'instagram.com') continue;

                const parts = url.pathname.split('/').filter(Boolean);
                if (parts.length !== 1) continue;
                const username = (parts[0] || '').trim();
                if (!/^[A-Za-z0-9._]{1,30}$/.test(username)) continue;
                if (technical.has(username.toLowerCase())) continue;

                let container = link;
                for (let i = 0; i < 7; i += 1) {
                    if (!container.parentElement) break;
                    container = container.parentElement;
                    const text = normalize(container.innerText || '');
                    if (text && text.includes(username) && text.length > username.length) break;
                }

                const rowText = normalize(container.innerText || link.innerText || '');
                const lines = rowText.split('\\n').map(normalize).filter(Boolean);
                const fullName = lines.find((line) => line !== username && !/^follow|following|подпис/i.test(line)) || '';
                const profileUrl = `https://www.instagram.com/${username}/`;
                if (seen.has(profileUrl)) continue;
                seen.add(profileUrl);
                result.push({
                    username,
                    full_name: fullName,
                    profile_url: profileUrl,
                    row_text: rowText,
                });
            }
            return result;
        }"""
    )
    if not isinstance(payload, list):
        return []
    rows: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "username": str(item.get("username") or "").strip(),
                "full_name": str(item.get("full_name") or "").strip(),
                "profile_url": _canonical_instagram_profile_url(str(item.get("profile_url") or "")),
                "row_text": str(item.get("row_text") or "").strip(),
            }
        )
    return rows


async def _scroll_followers_dialog(page: Page) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const dialog = document.querySelector('div[role="dialog"]');
                    if (!dialog) return false;
                    const scrollers = Array.from(dialog.querySelectorAll('*'))
                        .filter((el) => el.scrollHeight > el.clientHeight + 20)
                        .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
                    const scroller = scrollers[0] || dialog;
                    const previous = scroller.scrollTop;
                    scroller.scrollTop = previous + Math.max(240, Math.floor(scroller.clientHeight * 0.85));
                    return scroller.scrollTop !== previous;
                }"""
            )
        )
    except Exception:
        return False


async def _search_name_in_followers(
    page: Page,
    source_url: str,
    resolved_url: str,
    target_name: str,
    max_result_scrolls: int,
    scroll_pause_ms: int,
) -> list[InstagramFollowerRow]:
    search_input = await _find_followers_search_input(page)
    if search_input is None:
        return [
            {
                "source_instagram_url": source_url,
                "resolved_instagram_url": resolved_url,
                "searched_name": target_name,
                "found_username": "",
                "found_full_name": "",
                "found_profile_url": "",
                "matched_text": "",
                "match_score": "",
                "status": "followers_search_not_found",
                "error": "Search input was not found in followers dialog",
            }
        ]

    await _clear_and_type_search(search_input, target_name)
    await page.wait_for_timeout(1_200)

    found: dict[str, InstagramFollowerRow] = {}
    stable = 0
    for _ in range(max(1, max_result_scrolls)):
        profiles = await _extract_dialog_profiles(page)
        before = len(found)
        for profile in profiles:
            score = _match_score(
                target_name=target_name,
                candidate_text=profile.get("row_text", ""),
                username=profile.get("username", ""),
                full_name=profile.get("full_name", ""),
            )
            if score <= 0:
                continue
            profile_url = profile.get("profile_url", "")
            found[profile_url] = {
                "source_instagram_url": source_url,
                "resolved_instagram_url": resolved_url,
                "searched_name": target_name,
                "found_username": profile.get("username", ""),
                "found_full_name": profile.get("full_name", ""),
                "found_profile_url": profile_url,
                "matched_text": profile.get("row_text", ""),
                "match_score": str(score),
                "status": "ok",
                "error": "",
            }
        if len(found) == before:
            stable += 1
        else:
            stable = 0
        if stable >= 2:
            break

        moved = await _scroll_followers_dialog(page)
        if not moved:
            break
        await page.wait_for_timeout(scroll_pause_ms)

    try:
        await search_input.fill("")
    except Exception:
        pass
    return list(found.values())


async def process_profile(
    page: Page,
    source_url: str,
    target_names: list[str],
    timeout_ms: int,
    max_result_scrolls: int,
    scroll_pause_ms: int,
    include_not_found: bool,
) -> list[InstagramFollowerRow]:
    resolved_url = normalize_instagram_input_url(source_url)
    if not resolved_url:
        return [
            {
                "source_instagram_url": source_url,
                "resolved_instagram_url": "",
                "searched_name": "",
                "found_username": "",
                "found_full_name": "",
                "found_profile_url": "",
                "matched_text": "",
                "match_score": "",
                "status": "invalid_url",
                "error": "Unsupported Instagram URL format",
            }
        ]

    try:
        await page.goto(resolved_url, wait_until="domcontentloaded", timeout=timeout_ms)
        await page.wait_for_timeout(1_500)
    except Exception as exc:
        return [
            {
                "source_instagram_url": source_url,
                "resolved_instagram_url": resolved_url,
                "searched_name": "",
                "found_username": "",
                "found_full_name": "",
                "found_profile_url": "",
                "matched_text": "",
                "match_score": "",
                "status": "navigation_error",
                "error": str(exc),
            }
        ]

    if await _is_instagram_login_required(page):
        return [
            {
                "source_instagram_url": source_url,
                "resolved_instagram_url": resolved_url,
                "searched_name": "",
                "found_username": "",
                "found_full_name": "",
                "found_profile_url": "",
                "matched_text": "",
                "match_score": "",
                "status": "login_required",
                "error": "Instagram login is required",
            }
        ]

    opened, error = await _open_followers_dialog(page, resolved_url, timeout_ms=timeout_ms)
    if not opened:
        return [
            {
                "source_instagram_url": source_url,
                "resolved_instagram_url": resolved_url,
                "searched_name": "",
                "found_username": "",
                "found_full_name": "",
                "found_profile_url": "",
                "matched_text": "",
                "match_score": "",
                "status": "followers_not_opened",
                "error": error,
            }
        ]

    rows: list[InstagramFollowerRow] = []
    for target_name in target_names:
        matches = await _search_name_in_followers(
            page=page,
            source_url=source_url,
            resolved_url=resolved_url,
            target_name=target_name,
            max_result_scrolls=max_result_scrolls,
            scroll_pause_ms=scroll_pause_ms,
        )
        ok_matches = [row for row in matches if row.get("status") == "ok"]
        if ok_matches or any(row.get("status") != "ok" for row in matches):
            rows.extend(matches)
        elif include_not_found:
            rows.append(
                {
                    "source_instagram_url": source_url,
                    "resolved_instagram_url": resolved_url,
                    "searched_name": target_name,
                    "found_username": "",
                    "found_full_name": "",
                    "found_profile_url": "",
                    "matched_text": "",
                    "match_score": "",
                    "status": "not_found",
                    "error": "",
                }
            )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Поиск переданных имён среди followers Instagram-аккаунтов из CSV через Instagram Web."
    )
    parser.add_argument("--input-csv", required=True, help="CSV файл со списком Instagram URL.")
    parser.add_argument(
        "--input-column",
        default="instagram_url",
        help="Имя колонки с Instagram URL. Если колонки нет, будет использован первый столбец.",
    )
    parser.add_argument(
        "--output-csv",
        default="output/instagram_followers_name_matches.csv",
        help="Куда сохранить найденные профили.",
    )
    parser.add_argument("--names", help="Имена для поиска, через запятую.")
    parser.add_argument("--names-file", help="TXT/CSV файл с именами для поиска.")
    parser.add_argument(
        "--name-column",
        default="name",
        help="Колонка с именем в --names-file, если это CSV.",
    )
    parser.add_argument(
        "--user-data-dir",
        default=".playwright/instagram-profile",
        help="Каталог профиля Playwright для сохранения логина Instagram.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Запускать браузер без UI. Для первого входа в Instagram не подходит.",
    )
    parser.add_argument(
        "--browser-channel",
        default="",
        help="Канал браузера. По умолчанию Playwright Chromium; пример: --browser-channel chrome.",
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=180,
        help="Сколько ждать ручной логин в Instagram.",
    )
    parser.add_argument(
        "--no-login-prompt",
        action="store_true",
        help="Не ждать Enter от пользователя после проверки логина.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=45, help="Таймаут навигации, сек.")
    parser.add_argument("--limit", type=int, help="Ограничить количество Instagram-ссылок из input-csv.")
    parser.add_argument(
        "--max-result-scrolls",
        type=int,
        default=6,
        help="Сколько раз прокручивать результаты поиска по одному имени.",
    )
    parser.add_argument(
        "--scroll-pause-ms",
        type=int,
        default=400,
        help="Пауза после прокрутки результатов.",
    )
    parser.add_argument(
        "--include-not-found",
        action="store_true",
        help="Писать строки not_found для имён без совпадений.",
    )
    return parser.parse_args()


def config_from_args(args: argparse.Namespace) -> InstagramFollowersSearchConfig:
    return InstagramFollowersSearchConfig(
        input_csv=args.input_csv,
        input_column=args.input_column,
        output_csv=args.output_csv,
        target_names=tuple(read_target_names(args)),
        user_data_dir=args.user_data_dir,
        headless=args.headless,
        browser_channel=args.browser_channel,
        login_wait_seconds=args.login_wait_seconds,
        no_login_prompt=args.no_login_prompt,
        timeout_seconds=args.timeout_seconds,
        limit=args.limit,
        max_result_scrolls=args.max_result_scrolls,
        scroll_pause_ms=args.scroll_pause_ms,
        include_not_found=args.include_not_found,
    )


def instagram_follower_row(row: Mapping[str, str]) -> InstagramFollowerRow:
    return {
        "source_instagram_url": row.get("source_instagram_url", ""),
        "resolved_instagram_url": row.get("resolved_instagram_url", ""),
        "searched_name": row.get("searched_name", ""),
        "found_username": row.get("found_username", ""),
        "found_full_name": row.get("found_full_name", ""),
        "found_profile_url": row.get("found_profile_url", ""),
        "matched_text": row.get("matched_text", ""),
        "match_score": row.get("match_score", ""),
        "status": row.get("status", ""),
        "error": row.get("error", ""),
    }


def write_output(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=INSTAGRAM_FOLLOWER_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            writer.writerow(instagram_follower_row(row))


async def _create_context(config: InstagramFollowersSearchConfig) -> tuple[Playwright, BrowserContext, Page, Path]:
    profile_dir = Path(config.user_data_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    _cleanup_stale_profile_locks(profile_dir)

    launch_kwargs: dict[str, Any] = dict(
        user_data_dir=str(profile_dir),
        headless=bool(config.headless),
        locale="ru-RU",
        viewport={"width": 1440, "height": 900},
    )
    if config.browser_channel:
        launch_kwargs["channel"] = config.browser_channel

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else await context.new_page()
    return playwright, context, page, profile_dir


async def collect_instagram_follower_rows(config: InstagramFollowersSearchConfig | None = None) -> list[InstagramFollowerRow]:
    config = config or config_from_args(parse_args())

    target_names = list(config.target_names)
    if not target_names:
        print("No target names were provided. Use --names or --names-file.")
        return []

    if config.profile_url:
        source_urls = [config.profile_url]
    else:
        if config.input_csv is None:
            print("Set profile_url or input_csv.")
            return []
        input_path = Path(config.input_csv).expanduser().resolve()
        if not input_path.exists():
            print(f"Input CSV not found: {input_path}")
            return []
        source_urls = read_instagram_urls_from_csv(input_path, config.input_column)
    if not source_urls:
        print("No Instagram URLs found in CSV.")
        return []
    if config.limit is not None:
        source_urls = source_urls[: max(0, int(config.limit))]
        if not source_urls:
            print("No Instagram URLs left after applying --limit.")
            return []

    timeout_ms = max(5, config.timeout_seconds) * 1000
    all_rows: list[InstagramFollowerRow] = []
    context: BrowserContext | None = None
    playwright = None

    try:
        playwright, context, page, profile_dir = await _create_context(config)
        print(f"Instagram profile: {profile_dir}")

        try:
            await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1_000)
        except Exception:
            pass

        if await _is_instagram_login_required(page):
            wait_seconds = max(10, int(config.login_wait_seconds))
            print(f"Ожидание ручного логина в Instagram: до {wait_seconds} сек.")
            if not await _wait_for_instagram_login(page, wait_seconds=wait_seconds):
                print("Логин в Instagram не завершён. Завершите вход и запустите скрипт снова.")
                return []

        if not bool(config.no_login_prompt):
            print("Проверьте, что Instagram открыт в браузере и аккаунт залогинен.")
            input("После входа нажмите Enter, чтобы начать поиск: ")

        total = len(source_urls)
        for index, source_url in enumerate(source_urls, start=1):
            print(f"[{index}/{total}] {source_url}")
            rows = await process_profile(
                page=page,
                source_url=source_url,
                target_names=target_names,
                timeout_ms=timeout_ms,
                max_result_scrolls=config.max_result_scrolls,
                scroll_pause_ms=config.scroll_pause_ms,
                include_not_found=bool(config.include_not_found),
            )
            ok_count = sum(1 for row in rows if row.get("status") == "ok")
            print(f"  found: {ok_count}, rows: {len(rows)}")
            all_rows.extend(rows)

        return all_rows
    finally:
        if context is not None:
            await context.close()
        if playwright is not None:
            await playwright.stop()


async def run(config: InstagramFollowersSearchConfig | None = None) -> int:
    config = config or config_from_args(parse_args())
    output_path = Path(config.output_csv).expanduser().resolve()
    all_rows = await collect_instagram_follower_rows(config)
    write_output(output_path, all_rows)
    print(f"Saved {len(all_rows)} rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
