import argparse
import asyncio
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from typing import Mapping
from typing import Sequence
from typing import TypedDict
from urllib.parse import urlsplit, urlunsplit

import playwright.async_api
from playwright.async_api import async_playwright, Page

from app.datamining.playwright_utils import connect_async_over_cdp
from app.datamining.playwright_utils import discover_ws_debugger_url
from app.datamining.playwright_utils import launch_async_persistent_context
from app.datamining.playwright_utils import wait_until_async

try:
    from .telegram_js import (
        JS_CHANNEL_RUNTIME_STATE,
        JS_CLICK_ACTIVE_CHAT_MENU,
        JS_CLICK_DISCUSSION_ANYWHERE,
        JS_CLICK_DISCUSSION_MENU_ITEM,
        JS_CLICK_VISIBLE_MEMBER_BY_PEER_ID,
        JS_CLICK_LEAVE_COMMENT_OR_COMMENTS,
        JS_CLICK_SUBSCRIBE_OR_JOIN,
        JS_DISCUSSION_DEBUG_SNAPSHOT,
        JS_EXTRACT_OPEN_USER_PROFILE_USERNAME,
        JS_EXTRACT_USERNAMES_BY_PEER_IDS,
        JS_EXTRACT_VISIBLE_MEMBERS,
        JS_HAS_MEMBERS_LIST_ROWS,
        JS_IS_ACTIVE_CHAT_OPEN,
        JS_IS_GROUP_CHAT_OPEN,
        JS_IS_GROUP_INFO_OPEN,
        JS_IS_LOGGED_IN_TELEGRAM_WEB,
        JS_OPEN_GROUP_INFO_BY_HEADER_CLICK,
        JS_SCROLL_MEMBERS_LIST,
        JS_SELECT_MEMBERS_TAB,
    )
except ImportError:
    from telegram_js import (
        JS_CHANNEL_RUNTIME_STATE,
        JS_CLICK_ACTIVE_CHAT_MENU,
        JS_CLICK_DISCUSSION_ANYWHERE,
        JS_CLICK_DISCUSSION_MENU_ITEM,
        JS_CLICK_VISIBLE_MEMBER_BY_PEER_ID,
        JS_CLICK_LEAVE_COMMENT_OR_COMMENTS,
        JS_CLICK_SUBSCRIBE_OR_JOIN,
        JS_DISCUSSION_DEBUG_SNAPSHOT,
        JS_EXTRACT_OPEN_USER_PROFILE_USERNAME,
        JS_EXTRACT_USERNAMES_BY_PEER_IDS,
        JS_EXTRACT_VISIBLE_MEMBERS,
        JS_HAS_MEMBERS_LIST_ROWS,
        JS_IS_ACTIVE_CHAT_OPEN,
        JS_IS_GROUP_CHAT_OPEN,
        JS_IS_GROUP_INFO_OPEN,
        JS_IS_LOGGED_IN_TELEGRAM_WEB,
        JS_OPEN_GROUP_INFO_BY_HEADER_CLICK,
        JS_SCROLL_MEMBERS_LIST,
        JS_SELECT_MEMBERS_TAB,
    )

URL_COLUMN_CANDIDATES = ("telegram_url", "url", "channel_url", "telegram", "tg")
TELEGRAM_MEMBER_FIELDNAMES = [
    "source_channel_url",
    "resolved_channel_url",
    "discussion_url",
    "member_name",
    "member_profile_url",
    "peer_id",
    "member_username",
    "member_public_url",
    "member_web_public_url",
    "member_status",
    "member_role",
    "member_is_admin",
    "status",
    "error",
]


class TelegramMemberRow(TypedDict):
    source_channel_url: str
    resolved_channel_url: str
    discussion_url: str
    member_name: str
    member_profile_url: str
    peer_id: str
    member_username: str
    member_public_url: str
    member_web_public_url: str
    member_status: str
    member_role: str
    member_is_admin: str
    status: str
    error: str


@dataclass(frozen=True, slots=True)
class TelegramMembersConfig:
    channel_url: str = ""
    input_csv: str | Path | None = None
    output_csv: str | Path = "output/telegram_discussion_members.csv"
    input_column: str = "telegram_url"
    cdp_endpoint: str = "http://127.0.0.1:9222"
    mode: str = "persistent"
    user_data_dir: str = ".playwright/telegram-profile"
    headless: bool = False
    browser_channel: str = ""
    login_wait_seconds: int = 120
    no_login_prompt: bool = False
    auto_subscribe: bool = False
    resolve_usernames: bool = False
    resolve_usernames_by_opening_profiles: bool = False
    profile_open_delay_ms: int = 500
    timeout_seconds: int = 35
    limit: int | None = None
    max_scrolls: int = 120
    stable_rounds: int = 8
    scroll_pause_ms: int = 300
    channel_timeout_seconds: int = 90


def normalize_telegram_input_url(raw_url: str) -> str | None:
    value = (raw_url or "").strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]

    if host == "web.telegram.org":
        fragment = parsed.fragment.strip()
        if fragment.startswith("/"):
            fragment = fragment[1:]
        if not fragment:
            return "https://web.telegram.org/k/"
        return urlunsplit(("https", "web.telegram.org", "/k/", "", fragment))

    if host not in {"t.me", "telegram.me"}:
        return None

    path = (parsed.path or "").strip("/")
    if not path:
        return None
    if path.startswith("s/"):
        path = path[2:]
    if path.startswith("+"):
        # invite-link format is not supported for this flow
        return None
    username = path.split("/", 1)[0].strip()
    if not username:
        return None
    return f"https://web.telegram.org/k/#@{username}"


def _extract_hash_key(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return ""
    fragment = (parsed.fragment or "").strip()
    if fragment.startswith("/"):
        fragment = fragment[1:]
    return fragment.strip()


def _username_from_telegram_web_url(url: str) -> str:
    key = _extract_hash_key(url)
    match = re.fullmatch(r"@([A-Za-z0-9_]{5,32})", key)
    return match.group(1) if match else ""


def _canonical_telegram_web_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
    except Exception:
        return url
    if (parsed.hostname or "").lower() != "web.telegram.org" or parsed.path.rstrip("/") != "/k":
        return url
    fragment = (parsed.fragment or "").strip()
    if not fragment:
        return "https://web.telegram.org/k/"
    return urlunsplit(("https", "web.telegram.org", "/k/", "", fragment))


async def _navigate_to_channel_url(page: Page, normalized_url: str, timeout_ms: int) -> None:
    target_key = _extract_hash_key(normalized_url)
    if target_key:
        # Telegram Web ignores in-page hash changes in some sessions; a cache-busted goto reloads the route.
        navigation_url = urlunsplit(
            ("https", "web.telegram.org", "/k/", f"agent_nav={time.time_ns()}", target_key)
        )
    else:
        navigation_url = normalized_url

    last_error: Exception | None = None
    for _ in range(3):
        try:
            await page.goto(navigation_url, wait_until="commit", timeout=min(timeout_ms, 12_000))
        except Exception as exc:
            last_error = exc
        await page.wait_for_timeout(1_200)

        current_key = _extract_hash_key(page.url).lower()
        if target_key and current_key == target_key.lower():
            return
        if not target_key and _canonical_telegram_web_url(page.url) == _canonical_telegram_web_url(normalized_url):
            return

        if target_key:
            navigation_url = urlunsplit(
                ("https", "web.telegram.org", "/k/", f"agent_nav={time.time_ns()}", target_key)
            )

    if last_error is not None:
        raise last_error


async def _is_active_chat_open(page: Page) -> bool:
    try:
        return bool(await page.evaluate(JS_IS_ACTIVE_CHAT_OPEN))
    except Exception:
        return False


async def _channel_runtime_state(page: Page) -> dict[Any, Any]:
    try:
        return dict(await page.evaluate(JS_CHANNEL_RUNTIME_STATE))
    except Exception:
        return {
            "has_active_chat": False,
            "header_text": "",
            "waiting_network": False,
            "bubbles_count": 0,
            "replies_count": 0,
        }


async def _stabilize_channel_view(
        page: Page,
        source_url: str,
        timeout_ms: int,
        max_reloads: int = 2,
) -> None:
    reloads_left = max(0, max_reloads)
    rounds = max(8, timeout_ms // 500)
    for _ in range(rounds):
        state = await _channel_runtime_state(page)
        has_content = bool(state.get("bubbles_count", 0) or state.get("replies_count", 0))
        if state.get("has_active_chat") and not state.get("waiting_network") and has_content:
            return
        await page.wait_for_timeout(500)

        # Telegram Web can get stuck in "Waiting for network"; reload helps.
        if state.get("waiting_network") and reloads_left > 0:
            reloads_left -= 1
            try:
                await page.reload(wait_until="commit", timeout=min(timeout_ms, 12_000))
                await page.wait_for_timeout(1_200)
                if source_url:
                    await _navigate_to_channel_url(page, normalized_url=source_url, timeout_ms=timeout_ms)
            except Exception:
                pass


def _looks_like_url(value: str) -> bool:
    text = (value or "").strip().lower()
    return text.startswith("http://") or text.startswith("https://") or "t.me/" in text or "telegram.me/" in text


def read_channel_urls_from_csv(csv_path: Path, input_column: str | None) -> list[str]:
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
    for row in data_rows:
        if column_index >= len(row):
            continue
        candidate = row[column_index].strip()
        if not candidate:
            continue
        if not _looks_like_url(candidate):
            continue
        urls.append(candidate)
    return urls


async def _ensure_channel_open(page: Page, normalized_url: str, timeout_ms: int) -> bool:
    target_key = _extract_hash_key(normalized_url).lower()
    target_username = target_key[1:] if target_key.startswith("@") else target_key
    target_username_norm = re.sub(r"[^a-z0-9а-яё]+", "", target_username.lower())

    async def is_target_chat_open() -> bool:
        if not await _is_active_chat_open(page):
            return False
        try:
            return bool(
                await page.evaluate(
                    """([targetKey, targetUserNorm]) => {
                        const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                        const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
                        const normalizeHash = (s) => {
                            let value = normalize(s || '');
                            if (value.startsWith('/')) value = value.slice(1);
                            if (value.startsWith('#')) value = value.slice(1);
                            return value;
                        };

                        const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                        const isVisible = (chat) => {
                            const rect = chat.getBoundingClientRect();
                            const style = window.getComputedStyle(chat);
                            return (
                                rect.width > 260 &&
                                rect.height > 260 &&
                                style.display !== 'none' &&
                                style.visibility !== 'hidden'
                            );
                        };
                        const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                            || chats.find((chat) => isVisible(chat));
                        if (!active) return false;

                        const headerText = compact(
                            active.querySelector('.chat-info-container, .sidebar-header.topbar')?.textContent || ''
                        );

                        const activeListRow = document.querySelector('a.chatlist-chat.active, .chatlist-chat.active');
                        const activeHref = normalize(activeListRow?.getAttribute?.('href') || '');
                        const activeText = compact(activeListRow?.textContent || '');
                        const currentHash = normalizeHash(location.hash || '');
                        const normalizedTarget = normalizeHash(targetKey || '');

                        // Numeric hash targets can be validated directly.
                        if (normalizedTarget && normalizedTarget.startsWith('-') && currentHash.endsWith(normalizedTarget)) return true;
                        if (normalizedTarget && normalizedTarget.startsWith('-') && activeHref.endsWith(normalizedTarget)) return true;

                        // Username targets should be opened by page.goto(hash).
                        if (normalizedTarget && normalizedTarget.startsWith('@')) {
                            if (targetUserNorm && activeText.includes(targetUserNorm)) return true;
                            if (targetUserNorm && headerText.includes(targetUserNorm)) return true;
                            if (currentHash === normalizedTarget && headerText.length > 2) {
                                return true;
                            }
                            return false;
                        }

                        if (targetUserNorm && headerText.includes(targetUserNorm)) return true;
                        return false;
                    }""",
                    [target_key, target_username_norm],
                )
            )
        except Exception:
            return False

    rounds = max(8, timeout_ms // 500)
    for _ in range(rounds):
        if await is_target_chat_open():
            return True
        await page.wait_for_timeout(350)
    return False


async def _is_group_chat_open(page: Page) -> bool:
    try:
        return bool(await page.evaluate(JS_IS_GROUP_CHAT_OPEN))
    except Exception:
        return False


async def _click_active_chat_menu(page: Page) -> bool:
    return bool(await page.evaluate(JS_CLICK_ACTIVE_CHAT_MENU))


async def _click_discussion_item(page: Page) -> bool:
    return bool(await page.evaluate(JS_CLICK_DISCUSSION_MENU_ITEM))


async def _click_leave_comment(page: Page) -> bool:
    return bool(await page.evaluate(JS_CLICK_LEAVE_COMMENT_OR_COMMENTS))


async def _click_discussion_anywhere(page: Page) -> bool:
    return bool(await page.evaluate(JS_CLICK_DISCUSSION_ANYWHERE))


async def _click_discussion_via_playwright(page: Page) -> bool:
    patterns = [
        re.compile(r"view\s*discussion|discussion|обсужд", re.IGNORECASE),
        re.compile(r"leave\s+a\s+comment|open\s+comments|comments?|коммент", re.IGNORECASE),
    ]
    for pattern in patterns:
        try:
            locator = page.get_by_text(pattern)
            count = min(await locator.count(), 10)
        except Exception:
            continue
        for index in range(count):
            try:
                await locator.nth(index).click(timeout=1200, force=True)
                await page.wait_for_timeout(450)
                return True
            except Exception:
                continue
    return False


async def _discussion_debug_snapshot(page: Page) -> str:
    try:
        payload = await page.evaluate(JS_DISCUSSION_DEBUG_SNAPSHOT)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"debug_collect_failed: {exc}"


async def _open_group_info(page: Page) -> bool:
    already_open = bool(await page.evaluate(JS_IS_GROUP_INFO_OPEN))
    if already_open:
        return True

    clicked = bool(await page.evaluate(JS_OPEN_GROUP_INFO_BY_HEADER_CLICK))
    if not clicked:
        return False

    for _ in range(20):
        ready = bool(await page.evaluate(JS_IS_GROUP_INFO_OPEN))
        if ready:
            return True
        await page.wait_for_timeout(250)
    return False


async def _maybe_subscribe(page: Page) -> bool:
    return bool(await page.evaluate(JS_CLICK_SUBSCRIBE_OR_JOIN))


async def _ensure_members_tab(page: Page) -> None:
    await page.evaluate(JS_SELECT_MEMBERS_TAB)


async def _extract_visible_members(page: Page) -> list[dict[str, str]]:
    data = await page.evaluate(JS_EXTRACT_VISIBLE_MEMBERS)
    members: list[dict[str, str]] = []
    for item in data:
        peer_id = (item.get("peer_id") or "").strip()
        if not peer_id:
            continue
        name = (item.get("name") or "").strip()
        if not name:
            name = (item.get("raw_text") or "").strip()
        status = (item.get("status") or "").strip()
        role = (item.get("role") or "").strip()
        is_admin = "yes" if item.get("is_admin") else ""
        username = (item.get("username") or "").strip()
        public_url = (item.get("public_url") or "").strip()
        web_public_url = f"https://web.telegram.org/k/#@{username}" if username else ""
        members.append(
            {
                "peer_id": peer_id,
                "member_name": name,
                "member_status": status,
                "member_role": role,
                "member_is_admin": is_admin,
                "member_profile_url": f"https://web.telegram.org/k/#{peer_id}",
                "member_username": username,
                "member_public_url": public_url,
                "member_web_public_url": web_public_url,
            }
        )
    return members


async def _resolve_member_usernames_from_runtime(
        page: Page,
        members: list[dict[str, str]],
) -> list[dict[str, str]]:
    unresolved_peer_ids = [
        member["peer_id"]
        for member in members
        if member.get("peer_id") and not member.get("member_username")
    ]
    if not unresolved_peer_ids:
        return members
    try:
        payload = await page.evaluate(JS_EXTRACT_USERNAMES_BY_PEER_IDS, unresolved_peer_ids)
    except Exception:
        return members
    if not isinstance(payload, dict):
        return members

    enriched: list[dict[str, str]] = []
    for member in members:
        update = payload.get(member.get("peer_id", ""))
        if isinstance(update, dict):
            merged = dict(member)
            merged["member_username"] = (update.get("username") or merged.get("member_username") or "").strip()
            merged["member_public_url"] = (update.get("public_url") or merged.get("member_public_url") or "").strip()
            if merged["member_username"] and not merged.get("member_web_public_url"):
                merged["member_web_public_url"] = f"https://web.telegram.org/k/#@{merged['member_username']}"
            enriched.append(merged)
        else:
            enriched.append(member)
    return enriched


async def _click_visible_member_and_read_username(
        page: Page,
        peer_id: str,
        profile_open_delay_ms: int,
) -> tuple[bool, dict[str, str]]:
    try:
        clicked = bool(await page.evaluate(JS_CLICK_VISIBLE_MEMBER_BY_PEER_ID, peer_id))
        if not clicked:
            return False, {"member_username": "", "member_public_url": "", "member_web_public_url": ""}

        deadline = time.monotonic() + max(3.0, max(250, profile_open_delay_ms) / 1000)
        while time.monotonic() < deadline:
            await page.wait_for_timeout(250)
            username_from_url = _username_from_telegram_web_url(page.url)
            if username_from_url:
                return True, {
                    "member_username": username_from_url,
                    "member_public_url": f"https://t.me/{username_from_url}",
                    "member_web_public_url": f"https://web.telegram.org/k/#@{username_from_url}",
                }

        payload = await page.evaluate(JS_EXTRACT_OPEN_USER_PROFILE_USERNAME)
        username = (payload.get("username") if isinstance(payload, dict) else "") or ""
        public_url = (payload.get("public_url") if isinstance(payload, dict) else "") or ""
        username = username.strip()
        return True, {
            "member_username": username,
            "member_public_url": public_url.strip(),
            "member_web_public_url": f"https://web.telegram.org/k/#@{username}" if username else "",
        }
    except Exception:
        return False, {"member_username": "", "member_public_url": "", "member_web_public_url": ""}


async def _restore_discussion_members_view(
        page: Page,
        discussion_url: str,
        timeout_ms: int,
) -> bool:
    try:
        if await _wait_for_members_list(page, timeout_ms=1_500):
            return True

        target_key = _extract_hash_key(discussion_url)
        current_key = _extract_hash_key(page.url)
        if current_key and current_key != target_key:
            try:
                await page.go_back(wait_until="commit", timeout=min(timeout_ms, 8_000))
                await page.wait_for_timeout(800)
                if await _wait_for_members_list(page, timeout_ms=2_500):
                    return True
                if await _is_group_chat_open(page):
                    if await _open_group_info(page):
                        await _ensure_members_tab(page)
                        if await _wait_for_members_list(page, timeout_ms=4_000):
                            return True
            except Exception:
                pass

        await _navigate_to_channel_url(page, normalized_url=discussion_url, timeout_ms=timeout_ms)
        group_open = False
        for _ in range(max(8, timeout_ms // 500)):
            if await _is_group_chat_open(page):
                group_open = True
                break
            await page.wait_for_timeout(500)
        if not group_open:
            return False

        if not await _open_group_info(page):
            return False
        await _ensure_members_tab(page)
        return await _wait_for_members_list(page, timeout_ms=max(4_000, timeout_ms // 2))
    except Exception:
        return False


async def _resolve_one_member_username_by_discussion_click(
        page: Page,
        peer_id: str,
        profile_open_delay_ms: int,
        max_scrolls: int,
        scroll_pause_ms: int,
) -> dict[str, str]:
    for _ in range(max(1, max_scrolls)):
        clicked, profile = await _click_visible_member_and_read_username(
            page=page,
            peer_id=peer_id,
            profile_open_delay_ms=profile_open_delay_ms,
        )
        if clicked:
            username = (profile.get("member_username") or "").strip()
            public_url = (profile.get("member_public_url") or "").strip()
            web_public_url = (profile.get("member_web_public_url") or "").strip()
            if username and not public_url:
                public_url = f"https://t.me/{username}"
            if username and not web_public_url:
                web_public_url = f"https://web.telegram.org/k/#@{username}"
            return {
                "member_username": username,
                "member_public_url": public_url,
                "member_web_public_url": web_public_url,
            }

        moved = await _scroll_members_list(page)
        if not moved:
            break
        await page.wait_for_timeout(scroll_pause_ms)

    return {"member_username": "", "member_public_url": "", "member_web_public_url": ""}


async def _resolve_member_usernames_by_discussion_click(
        page: Page,
        members: list[dict[str, str]],
        discussion_url: str,
        timeout_ms: int,
        profile_open_delay_ms: int,
        max_scrolls: int,
        scroll_pause_ms: int,
) -> list[dict[str, str]]:
    enriched: list[dict[str, str]] = []
    unresolved = [
        member for member in members
        if member.get("peer_id") and not member.get("member_username")
    ]
    if not unresolved:
        return members

    print(f"  resolving usernames by member clicks: {len(unresolved)}")
    for member in members:
        if member.get("member_username") or not member.get("peer_id"):
            enriched.append(member)
            continue

        merged = dict(member)
        if not await _restore_discussion_members_view(
                page=page,
                discussion_url=discussion_url,
                timeout_ms=timeout_ms,
        ):
            enriched.append(merged)
            continue

        profile = await _resolve_one_member_username_by_discussion_click(
            page=page,
            peer_id=member["peer_id"],
            profile_open_delay_ms=profile_open_delay_ms,
            max_scrolls=max_scrolls,
            scroll_pause_ms=scroll_pause_ms,
        )
        if profile.get("member_username"):
            merged.update(profile)
        enriched.append(merged)
    return enriched


async def _wait_for_members_list(page: Page, timeout_ms: int = 8_000) -> bool:
    rounds = max(4, timeout_ms // 250)
    for _ in range(rounds):
        ready = bool(await page.evaluate(JS_HAS_MEMBERS_LIST_ROWS))
        if ready:
            return True
        await _ensure_members_tab(page)
        await page.wait_for_timeout(250)
    return False


async def _scroll_members_list(page: Page) -> bool:
    return bool(await page.evaluate(JS_SCROLL_MEMBERS_LIST))


async def _scroll_active_chat_for_discussion(page: Page) -> bool:
    try:
        point = await page.evaluate(
            """() => {
                const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                const isVisible = (chat) => {
                    const rect = chat.getBoundingClientRect();
                    const style = window.getComputedStyle(chat);
                    return (
                        rect.width > 260 &&
                        rect.height > 260 &&
                        style.display !== 'none' &&
                        style.visibility !== 'hidden'
                    );
                };
                const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                    || chats.find((chat) => isVisible(chat));
                if (!active) return null;
                const rect = active.getBoundingClientRect();
                return {
                    x: Math.floor(rect.left + rect.width / 2),
                    y: Math.floor(rect.top + rect.height / 2),
                };
            }"""
        )
        if point:
            await page.mouse.move(point["x"], point["y"])
            await page.mouse.wheel(0, 900)
            await page.wait_for_timeout(150)

        moved = await page.evaluate(
            """() => {
                    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                    const isVisible = (chat) => {
                        const rect = chat.getBoundingClientRect();
                        const style = window.getComputedStyle(chat);
                        return (
                            rect.width > 260 &&
                            rect.height > 260 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        );
                    };
                    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                        || chats.find((chat) => isVisible(chat));
                    if (!active) return false;
                    const scrollers = [
                        active.querySelector('.bubbles'),
                        active.querySelector('.bubbles-inner'),
                        active.querySelector('.scrollable'),
                    ].filter(Boolean);
                    for (const s of scrollers) {
                        const prev = s.scrollTop;
                        s.scrollTop = prev + Math.max(280, Math.floor(s.clientHeight * 0.7));
                        if (s.scrollTop !== prev) return true;
                    }
                    return false;
                }"""
        )
        return bool(point or moved)
    except Exception:
        return False


async def _collect_members(
        page: Page,
        max_scrolls: int,
        stable_rounds: int,
        scroll_pause_ms: int,
        resolve_usernames: bool,
) -> list[dict[str, str]]:
    collected: dict[str, dict[str, str]] = {}
    stable = 0

    for _ in range(max_scrolls):
        visible = await _extract_visible_members(page)
        if resolve_usernames and visible:
            visible = await _resolve_member_usernames_from_runtime(page=page, members=visible)
        if not visible and not collected:
            await page.wait_for_timeout(max(200, scroll_pause_ms))
            continue
        before = len(collected)
        for item in visible:
            peer_id = item["peer_id"]
            if peer_id not in collected:
                collected[peer_id] = item
            else:
                if not collected[peer_id].get("member_name") and item.get("member_name"):
                    collected[peer_id]["member_name"] = item["member_name"]
                if not collected[peer_id].get("member_username") and item.get("member_username"):
                    collected[peer_id]["member_username"] = item["member_username"]
                if not collected[peer_id].get("member_public_url") and item.get("member_public_url"):
                    collected[peer_id]["member_public_url"] = item["member_public_url"]
                if not collected[peer_id].get("member_web_public_url") and item.get("member_web_public_url"):
                    collected[peer_id]["member_web_public_url"] = item["member_web_public_url"]
                if not collected[peer_id].get("member_status") and item.get("member_status"):
                    collected[peer_id]["member_status"] = item["member_status"]
                if not collected[peer_id].get("member_role") and item.get("member_role"):
                    collected[peer_id]["member_role"] = item["member_role"]
                if not collected[peer_id].get("member_is_admin") and item.get("member_is_admin"):
                    collected[peer_id]["member_is_admin"] = item["member_is_admin"]
        after = len(collected)

        if after == before:
            stable += 1
        else:
            stable = 0
        if stable >= stable_rounds:
            break

        moved = await _scroll_members_list(page)
        if not moved:
            break
        await page.wait_for_timeout(scroll_pause_ms)

    return list(collected.values())


async def _try_open_discussion(page: Page, timeout_ms: int) -> tuple[bool, str]:
    attempts = 3
    for _ in range(attempts):
        # Telegram Web exposes linked discussion via post comments more consistently than via top-right menu:
        # the menu may omit "View discussion" or open a post context menu instead.
        clicked = await _click_leave_comment(page)
        if not clicked:
            menu_opened = await _click_active_chat_menu(page)
            if menu_opened:
                clicked = await _click_discussion_item(page)
                if not clicked:
                    await page.keyboard.press("Escape")
        if not clicked:
            # Last-resort heuristic: click any visible discussion/comment action.
            clicked = await _click_discussion_anywhere(page)
        if not clicked:
            # Playwright locator fallback works better on some Telegram builds.
            clicked = await _click_discussion_via_playwright(page)
        if clicked:
            initial_url = page.url
            rounds = 12
            for _ in range(rounds):
                await page.wait_for_timeout(250)
                if await _is_group_chat_open(page):
                    return True, _canonical_telegram_web_url(page.url)
                if page.url != initial_url:
                    # Telegram often switches hash first and only then syncs header/sidebar.
                    return True, _canonical_telegram_web_url(page.url)

        # If nothing clickable was found yet, scroll the active channel feed and retry.
        moved = await _scroll_active_chat_for_discussion(page)
        await page.wait_for_timeout(350 if moved else 250)
    return False, ""


async def process_channel(
        page: Page,
        source_url: str,
        timeout_ms: int,
        max_scrolls: int,
        stable_rounds: int,
        scroll_pause_ms: int,
        auto_subscribe: bool,
        resolve_usernames: bool,
        resolve_usernames_by_opening_profiles: bool,
        profile_open_delay_ms: int,
) -> list[TelegramMemberRow]:
    normalized = normalize_telegram_input_url(source_url)
    if not normalized:
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": "",
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_username": "",
                "member_public_url": "",
                "member_web_public_url": "",
                "member_status": "",
                "member_role": "",
                "member_is_admin": "",
                "status": "invalid_url",
                "error": "Unsupported Telegram URL format",
            }
        ]

    try:
        await _navigate_to_channel_url(page, normalized_url=normalized, timeout_ms=timeout_ms)
    except Exception as exc:
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": normalized,
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_username": "",
                "member_public_url": "",
                "member_web_public_url": "",
                "member_status": "",
                "member_role": "",
                "member_is_admin": "",
                "status": "navigation_error",
                "error": str(exc),
            }
        ]

    if not await _ensure_channel_open(page, normalized_url=normalized, timeout_ms=timeout_ms):
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": _canonical_telegram_web_url(page.url),
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_username": "",
                "member_public_url": "",
                "member_web_public_url": "",
                "member_status": "",
                "member_role": "",
                "member_is_admin": "",
                "status": "channel_not_opened",
                "error": await _discussion_debug_snapshot(page),
            }
        ]

    resolved_channel_url = _canonical_telegram_web_url(page.url)
    await _stabilize_channel_view(page=page, source_url=normalized, timeout_ms=timeout_ms, max_reloads=2)
    if auto_subscribe and await _maybe_subscribe(page):
        await page.wait_for_timeout(800)

    opened, discussion_url = await _try_open_discussion(page, timeout_ms=timeout_ms)
    if not opened:
        # One more pass after stabilization/reload for flaky Telegram sessions.
        await _stabilize_channel_view(page=page, source_url=normalized, timeout_ms=max(8_000, timeout_ms // 2),
                                      max_reloads=1)
        opened, discussion_url = await _try_open_discussion(page, timeout_ms=timeout_ms)
    if not opened:
        debug_info = await _discussion_debug_snapshot(page)
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_username": "",
                "member_public_url": "",
                "member_web_public_url": "",
                "member_status": "",
                "member_role": "",
                "member_is_admin": "",
                "status": "discussion_not_found",
                "error": debug_info,
            }
        ]

    if not await _open_group_info(page):
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": discussion_url,
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_username": "",
                "member_public_url": "",
                "member_web_public_url": "",
                "member_status": "",
                "member_role": "",
                "member_is_admin": "",
                "status": "group_info_not_opened",
                "error": "",
            }
        ]

    await _ensure_members_tab(page)
    await _wait_for_members_list(page, timeout_ms=max(4_000, timeout_ms // 2))

    members = await _collect_members(
        page=page,
        max_scrolls=max_scrolls,
        stable_rounds=stable_rounds,
        scroll_pause_ms=scroll_pause_ms,
        resolve_usernames=resolve_usernames,
    )
    if not members:
        debug_info = await _discussion_debug_snapshot(page)
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": discussion_url,
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_username": "",
                "member_public_url": "",
                "member_web_public_url": "",
                "member_status": "",
                "member_role": "",
                "member_is_admin": "",
                "status": "members_not_found",
                "error": debug_info,
            }
        ]

    if resolve_usernames and resolve_usernames_by_opening_profiles:
        members = await _resolve_member_usernames_by_discussion_click(
            page=page,
            members=members,
            discussion_url=discussion_url,
            timeout_ms=timeout_ms,
            profile_open_delay_ms=profile_open_delay_ms,
            max_scrolls=max_scrolls,
            scroll_pause_ms=scroll_pause_ms,
        )

    rows: list[TelegramMemberRow] = []
    for member in members:
        rows.append(
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": discussion_url,
                "member_name": member.get("member_name", ""),
                "member_profile_url": member.get("member_profile_url", ""),
                "peer_id": member.get("peer_id", ""),
                "member_username": member.get("member_username", ""),
                "member_public_url": member.get("member_public_url", ""),
                "member_web_public_url": member.get("member_web_public_url", ""),
                "member_status": member.get("member_status", ""),
                "member_role": member.get("member_role", ""),
                "member_is_admin": member.get("member_is_admin", ""),
                "status": "ok",
                "error": "",
            }
        )
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сбор участников обсуждений Telegram-каналов из CSV через Telegram Web "
            "и Chrome DevTools Protocol (CDP)."
        )
    )
    parser.add_argument("--input-csv", required=True, help="CSV файл со списком URL каналов.")
    parser.add_argument(
        "--input-column",
        default="telegram_url",
        help="Имя колонки с URL. Если колонки нет, будет использован первый столбец.",
    )
    parser.add_argument(
        "--output-csv",
        default="output/telegram_discussion_members.csv",
        help="Куда сохранить результат.",
    )
    parser.add_argument(
        "--cdp-endpoint",
        default="http://127.0.0.1:9222",
        help="CDP endpoint Chrome, например http://127.0.0.1:9222.",
    )
    parser.add_argument(
        "--mode",
        choices=("cdp", "persistent"),
        default="persistent",
        help="Режим запуска: cdp (подключение к существующему Chrome) или persistent (собственный профиль Playwright).",
    )
    parser.add_argument(
        "--user-data-dir",
        default=".playwright/telegram-profile",
        help="Каталог профиля для режима --mode persistent.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Запускать браузер без UI (для Telegram обычно не рекомендуется).",
    )
    parser.add_argument(
        "--browser-channel",
        default="",
        help=(
            "Канал браузера для режима persistent. "
            "По умолчанию пусто => Playwright Chromium (Chrome for Testing). "
            "Пример: --browser-channel chrome"
        ),
    )
    parser.add_argument(
        "--login-wait-seconds",
        type=int,
        default=120,
        help="Сколько ждать ручной логин в Telegram в режиме persistent.",
    )
    parser.add_argument(
        "--no-login-prompt",
        action="store_true",
        help="Не ждать Enter от пользователя перед стартом (только авто-проверка логина).",
    )
    parser.add_argument(
        "--auto-subscribe",
        action="store_true",
        help="Разрешить скрипту нажимать SUBSCRIBE/JOIN перед поиском discussion.",
    )
    parser.add_argument(
        "--resolve-usernames",
        action="store_true",
        help=(
            "Пытаться собрать публичный @username из DOM/runtime Telegram Web без открытия карточек. "
            "Даёт стабильные t.me-ссылки там, где username виден Telegram Web."
        ),
    )
    parser.add_argument(
        "--resolve-usernames-by-opening-profiles",
        action="store_true",
        help=(
            "Дополнительно кликать участников из списка discussion members, если username не найден быстрым способом. "
            "После клика скрипт берёт username из редиректа URL вида #@username. "
            "Медленно; используйте вместе с увеличенным --channel-timeout-seconds."
        ),
    )
    parser.add_argument(
        "--profile-open-delay-ms",
        type=int,
        default=500,
        help="Пауза после открытия карточки участника при --resolve-usernames-by-opening-profiles.",
    )
    parser.add_argument("--timeout-seconds", type=int, default=35, help="Таймаут навигации, сек.")
    parser.add_argument("--limit", type=int, help="Ограничить количество ссылок из input-csv (с начала списка).")
    parser.add_argument("--max-scrolls", type=int, default=120, help="Максимум прокруток списка участников.")
    parser.add_argument(
        "--stable-rounds",
        type=int,
        default=8,
        help="Остановиться после N итераций без новых участников.",
    )
    parser.add_argument(
        "--scroll-pause-ms",
        type=int,
        default=300,
        help="Пауза после прокрутки списка участников.",
    )
    parser.add_argument(
        "--channel-timeout-seconds",
        type=int,
        default=90,
        help="Жёсткий таймаут на обработку одного канала.",
    )
    return parser.parse_args()


def config_from_args(args: argparse.Namespace) -> TelegramMembersConfig:
    return TelegramMembersConfig(
        input_csv=args.input_csv,
        input_column=args.input_column,
        output_csv=args.output_csv,
        cdp_endpoint=args.cdp_endpoint,
        mode=args.mode,
        user_data_dir=args.user_data_dir,
        headless=args.headless,
        browser_channel=args.browser_channel,
        login_wait_seconds=args.login_wait_seconds,
        no_login_prompt=args.no_login_prompt,
        auto_subscribe=args.auto_subscribe,
        resolve_usernames=args.resolve_usernames,
        resolve_usernames_by_opening_profiles=args.resolve_usernames_by_opening_profiles,
        profile_open_delay_ms=args.profile_open_delay_ms,
        timeout_seconds=args.timeout_seconds,
        limit=args.limit,
        max_scrolls=args.max_scrolls,
        stable_rounds=args.stable_rounds,
        scroll_pause_ms=args.scroll_pause_ms,
        channel_timeout_seconds=args.channel_timeout_seconds,
    )


def write_output(path: Path, rows: Sequence[Mapping[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized_rows = normalize_telegram_member_rows(rows)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=TELEGRAM_MEMBER_FIELDNAMES)
        writer.writeheader()
        writer.writerows(normalized_rows)


def telegram_member_row(row: Mapping[str, str]) -> TelegramMemberRow:
    return {
        "source_channel_url": row.get("source_channel_url", ""),
        "resolved_channel_url": row.get("resolved_channel_url", ""),
        "discussion_url": row.get("discussion_url", ""),
        "member_name": row.get("member_name", ""),
        "member_profile_url": row.get("member_profile_url", ""),
        "peer_id": row.get("peer_id", ""),
        "member_username": row.get("member_username", ""),
        "member_public_url": row.get("member_public_url", ""),
        "member_web_public_url": row.get("member_web_public_url", ""),
        "member_status": row.get("member_status", ""),
        "member_role": row.get("member_role", ""),
        "member_is_admin": row.get("member_is_admin", ""),
        "status": row.get("status", ""),
        "error": row.get("error", ""),
    }


def normalize_telegram_member_rows(rows: Sequence[Mapping[str, str]]) -> list[TelegramMemberRow]:
    normalized_rows: list[TelegramMemberRow] = []
    for row in rows:
        normalized = dict(row)
        username = (normalized.get("member_username") or "").strip()
        if username:
            normalized.setdefault("member_public_url", "")
            normalized.setdefault("member_web_public_url", "")
            if not normalized["member_public_url"]:
                normalized["member_public_url"] = f"https://t.me/{username}"
            if not normalized["member_web_public_url"]:
                normalized["member_web_public_url"] = f"https://web.telegram.org/k/#@{username}"
        normalized_rows.append(telegram_member_row(normalized))
    return normalized_rows


async def collect_telegram_member_rows(config: TelegramMembersConfig | None = None) -> list[TelegramMemberRow]:
    config = config or config_from_args(parse_args())
    if config.channel_url:
        source_urls = [config.channel_url]
    else:
        if config.input_csv is None:
            print("Set channel_url or input_csv.")
            return []
        input_path = Path(config.input_csv).expanduser().resolve()
        if not input_path.exists():
            print(f"Input CSV not found: {input_path}")
            return []
        source_urls = read_channel_urls_from_csv(input_path, config.input_column)
    if not source_urls:
        print("No Telegram URLs found in CSV.")
        return []
    if config.limit is not None:
        source_urls = source_urls[: max(0, int(config.limit))]
        if not source_urls:
            print("No Telegram URLs left after applying --limit.")
            return []

    timeout_ms = max(5, config.timeout_seconds) * 1000
    all_rows: list[TelegramMemberRow] = []

    async with async_playwright() as pw:
        own_context = False
        if config.mode == "persistent":
            context, page, profile_dir, effective_channel = await launch_async_persistent_context(
                pw,
                config.user_data_dir,
                headless=config.headless,
                browser_channel=(config.browser_channel or "").strip(),
                retry_system_chrome=True,
            )
            own_context = True
            print(f"Persistent profile: {profile_dir}")
            print(f"Persistent browser: {'chromium(cft)' if not effective_channel else effective_channel}")
        else:
            browser: playwright.async_api.Browser
            # for context in browser.contexts:
            #     ...
            # or
            # browser.new_context()

            browser, primary_error = await connect_async_over_cdp(pw, config.cdp_endpoint, timeout_ms)

            if browser is None:
                ws_url = discover_ws_debugger_url(config.cdp_endpoint)
                if ws_url:
                    browser, _ = await connect_async_over_cdp(pw, ws_url, timeout_ms)
                    if browser is not None:
                        print(f"CDP fallback: connected via {ws_url}")

            if browser is None:
                print(f"Could not connect to Chrome CDP at {config.cdp_endpoint}: {primary_error}")
                print("Запустите отдельный Chrome с --remote-debugging-port=<port> и войдите в Telegram Web.")
                print(
                    "Пример: open -na \"Google Chrome\" --args "
                    "--remote-debugging-port=9223 --user-data-dir=/tmp/chrome-cdp-9223"
                )
                return []

            if not browser.contexts:
                print("Connected to CDP, but no browser contexts are available.")
                return []
            context = browser.contexts[0]
            page: Page = context.pages[0] if context.pages else await context.new_page()

        try:
            await page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(1_000)
        except Exception:
            pass

        if config.mode == "persistent":
            async def is_logged_in() -> bool:
                return bool(await page.evaluate(JS_IS_LOGGED_IN_TELEGRAM_WEB))

            is_logged_in_now = await is_logged_in()
            if not is_logged_in_now:
                wait_seconds = max(10, int(config.login_wait_seconds))
                print(f"Ожидание ручного логина в Telegram Web: до {wait_seconds} сек.")
                if not await wait_until_async(is_logged_in, wait_seconds, interval_ms=2_000):
                    print("Логин в Telegram не завершён. Завершите вход и запустите скрипт снова.")
                    if own_context and context is not None:
                        await context.close()
                    return []

            if not bool(config.no_login_prompt):
                print("Войдите в Telegram Web в открытом окне браузера.")
                try:
                    input("После входа нажмите Enter, чтобы начать сбор: ")
                except EOFError:
                    # Non-interactive shell: continue with current state.
                    pass

        for index, source_url in enumerate(source_urls, start=1):
            print(f"[{index}/{len(source_urls)}] {source_url}")
            rows: list[TelegramMemberRow]
            try:
                rows = await asyncio.wait_for(
                    process_channel(
                        page=page,
                        source_url=source_url,
                        timeout_ms=timeout_ms,
                        max_scrolls=max(1, config.max_scrolls),
                        stable_rounds=max(1, config.stable_rounds),
                        scroll_pause_ms=max(100, config.scroll_pause_ms),
                        auto_subscribe=bool(config.auto_subscribe),
                        resolve_usernames=bool(config.resolve_usernames),
                        resolve_usernames_by_opening_profiles=bool(config.resolve_usernames_by_opening_profiles),
                        profile_open_delay_ms=max(250, int(config.profile_open_delay_ms)),
                    ),
                    timeout=max(15, int(config.channel_timeout_seconds)),
                )
            except asyncio.TimeoutError:
                rows = [
                    {
                        "source_channel_url": source_url,
                        "resolved_channel_url": _canonical_telegram_web_url(page.url),
                        "discussion_url": "",
                        "member_name": "",
                        "member_profile_url": "",
                        "peer_id": "",
                        "member_username": "",
                        "member_public_url": "",
                        "member_web_public_url": "",
                        "member_status": "",
                        "member_role": "",
                        "member_is_admin": "",
                        "status": "channel_timeout",
                        "error": await _discussion_debug_snapshot(page),
                    }
                ]
            except Exception as exc:
                rows = [
                    {
                        "source_channel_url": source_url,
                        "resolved_channel_url": "",
                        "discussion_url": "",
                        "member_name": "",
                        "member_profile_url": "",
                        "peer_id": "",
                        "member_username": "",
                        "member_public_url": "",
                        "member_web_public_url": "",
                        "member_status": "",
                        "member_role": "",
                        "member_is_admin": "",
                        "status": "runtime_error",
                        "error": str(exc),
                    }
                ]
            all_rows.extend(rows)

        if own_context and context is not None:
            await context.close()

    return normalize_telegram_member_rows(all_rows)


async def run(config: TelegramMembersConfig | None = None) -> int:
    config = config or config_from_args(parse_args())
    output_path = Path(config.output_csv).expanduser().resolve()
    all_rows = await collect_telegram_member_rows(config)
    write_output(output_path, all_rows)
    print(f"Done. Saved: {output_path}")
    print(f"Rows: {len(all_rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
