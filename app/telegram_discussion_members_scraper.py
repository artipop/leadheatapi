from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

DISCUSSION_RE = re.compile(r"(view\s*discussion|discussion|обсужд)", re.IGNORECASE)
MEMBERS_COUNT_RE = re.compile(r"(members?|участник)", re.IGNORECASE)
MEMBERS_TAB_RE = re.compile(r"^(members|участники)$", re.IGNORECASE)
URL_COLUMN_CANDIDATES = ("telegram_url", "url", "channel_url", "telegram", "tg")


def _require_sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "Playwright не установлен. Установите: uv add playwright && uv run playwright install chromium"
        ) from exc
    return sync_playwright


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


def _is_group_chat_open(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
                    const active = document.querySelector('.chat.tabs-tab.active');
                    if (!active) return false;
                    const header = active.querySelector('.chat-info-container');
                    if (!header) return false;
                    const text = (header.textContent || '').toLowerCase();
                    return /members|участник/.test(text);
                }"""
            )
        )
    except Exception:
        return False


def _click_active_chat_menu(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const buttons = Array.from(
                    document.querySelectorAll('.chat.tabs-tab.active .chat-info-container button.btn-menu-toggle')
                ).filter((button) => {
                    const rect = button.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0 && rect.top >= 0 && rect.top < 120;
                });
                if (!buttons.length) return false;
                buttons[0].click();
                return true;
            }"""
        )
    )


def _click_discussion_item(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const pattern = /(view\\s*discussion|discussion|обсужд)/i;
                const items = Array.from(document.querySelectorAll('.btn-menu-item'));
                const target = items.find((item) => pattern.test((item.textContent || '').replace(/\\s+/g, ' ').trim()));
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
    )


def _open_group_info(page: Any) -> bool:
    clicked = bool(
        page.evaluate(
            """() => {
                const header = document.querySelector('.chat.tabs-tab.active .chat-info-container');
                if (!header) return false;
                header.click();
                return true;
            }"""
        )
    )
    if not clicked:
        return False

    for _ in range(20):
        ready = bool(
            page.evaluate(
                """() => {
                    const sidebar = document.querySelector('.sidebar.sidebar-right');
                    if (!sidebar) return false;
                    const text = (sidebar.textContent || '').toLowerCase();
                    return /group info|информация о группе|members|участники/.test(text);
                }"""
            )
        )
        if ready:
            return True
        page.wait_for_timeout(250)
    return False


def _ensure_members_tab(page: Any) -> None:
    page.evaluate(
        """() => {
            const sidebar = document.querySelector('.sidebar.sidebar-right') || document;
            const targets = Array.from(sidebar.querySelectorAll('nav *, [role="tab"], .tabs-with-icons *'));
            const normalized = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            for (const node of targets) {
                const text = normalized(node.textContent);
                if (text === 'members' || text === 'участники') {
                    node.click();
                    return;
                }
            }
        }"""
    )


def _extract_visible_members(page: Any) -> list[dict[str, str]]:
    data = page.evaluate(
        """() => {
            const rows = Array.from(
                document.querySelectorAll('.sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id]')
            );
            return rows.map((row) => {
                const peerId = (row.getAttribute('data-peer-id') || '').trim();
                const nameNode = row.querySelector('.fullName, .peer-title, .user-title, .title, .full-name');
                const statusNode = row.querySelector('.subtitle, .status, .user-status, .user-last-seen');
                const name = (nameNode?.textContent || '').replace(/\\s+/g, ' ').trim();
                const status = (statusNode?.textContent || '').replace(/\\s+/g, ' ').trim();
                const rawText = (row.textContent || '').replace(/\\s+/g, ' ').trim();
                return { peer_id: peerId, name, status, raw_text: rawText };
            });
        }"""
    )
    members: list[dict[str, str]] = []
    for item in data:
        peer_id = (item.get("peer_id") or "").strip()
        if not peer_id:
            continue
        name = (item.get("name") or "").strip()
        if not name:
            name = (item.get("raw_text") or "").strip()
        status = (item.get("status") or "").strip()
        members.append(
            {
                "peer_id": peer_id,
                "member_name": name,
                "member_status": status,
                "member_profile_url": f"https://web.telegram.org/k/#{peer_id}",
            }
        )
    return members


def _scroll_members_list(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const row = document.querySelector('.sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id]');
                if (!row) return false;
                let container = row.parentElement;
                while (container && container !== document.body) {
                    if (container.scrollHeight > container.clientHeight + 4) {
                        const previousTop = container.scrollTop;
                        container.scrollTop = previousTop + Math.max(220, Math.floor(container.clientHeight * 0.85));
                        return container.scrollTop > previousTop;
                    }
                    container = container.parentElement;
                }
                return false;
            }"""
        )
    )


def _collect_members(
    page: Any,
    max_scrolls: int,
    stable_rounds: int,
    scroll_pause_ms: int,
) -> list[dict[str, str]]:
    collected: dict[str, dict[str, str]] = {}
    stable = 0

    for _ in range(max_scrolls):
        visible = _extract_visible_members(page)
        before = len(collected)
        for item in visible:
            peer_id = item["peer_id"]
            if peer_id not in collected:
                collected[peer_id] = item
            elif not collected[peer_id].get("member_name") and item.get("member_name"):
                collected[peer_id] = item
        after = len(collected)

        if after == before:
            stable += 1
        else:
            stable = 0
        if stable >= stable_rounds:
            break

        moved = _scroll_members_list(page)
        if not moved:
            break
        page.wait_for_timeout(scroll_pause_ms)

    return list(collected.values())


def _try_open_discussion(page: Any, timeout_ms: int) -> tuple[bool, str]:
    if _is_group_chat_open(page):
        return True, page.url

    menu_opened = _click_active_chat_menu(page)
    if not menu_opened:
        return False, ""

    clicked = _click_discussion_item(page)
    if not clicked:
        page.keyboard.press("Escape")
        return False, ""

    initial_url = page.url
    for _ in range(40):
        page.wait_for_timeout(250)
        if _is_group_chat_open(page):
            return True, page.url
        if page.url != initial_url:
            # some chats update URL first, then header text
            if _is_group_chat_open(page):
                return True, page.url
    return False, ""


def process_channel(
    page: Any,
    source_url: str,
    timeout_ms: int,
    max_scrolls: int,
    stable_rounds: int,
    scroll_pause_ms: int,
) -> list[dict[str, str]]:
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
                "member_status": "",
                "status": "invalid_url",
                "error": "Unsupported Telegram URL format",
            }
        ]

    try:
        page.goto(normalized, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(1_200)
    except Exception as exc:
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": normalized,
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_status": "",
                "status": "navigation_error",
                "error": str(exc),
            }
        ]

    resolved_channel_url = page.url
    opened, discussion_url = _try_open_discussion(page, timeout_ms=timeout_ms)
    if not opened:
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_status": "",
                "status": "discussion_not_found",
                "error": "",
            }
        ]

    if not _open_group_info(page):
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": discussion_url,
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_status": "",
                "status": "group_info_not_opened",
                "error": "",
            }
        ]

    _ensure_members_tab(page)
    page.wait_for_timeout(500)

    members = _collect_members(
        page=page,
        max_scrolls=max_scrolls,
        stable_rounds=stable_rounds,
        scroll_pause_ms=scroll_pause_ms,
    )
    if not members:
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": discussion_url,
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_status": "",
                "status": "members_not_found",
                "error": "",
            }
        ]

    rows: list[dict[str, str]] = []
    for member in members:
        rows.append(
            {
                "source_channel_url": source_url,
                "resolved_channel_url": resolved_channel_url,
                "discussion_url": discussion_url,
                "member_name": member.get("member_name", ""),
                "member_profile_url": member.get("member_profile_url", ""),
                "peer_id": member.get("peer_id", ""),
                "member_status": member.get("member_status", ""),
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
    parser.add_argument("--timeout-seconds", type=int, default=35, help="Таймаут навигации, сек.")
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
    return parser.parse_args()


def write_output(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "source_channel_url",
        "resolved_channel_url",
        "discussion_url",
        "member_name",
        "member_profile_url",
        "peer_id",
        "member_status",
        "status",
        "error",
    ]
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def run() -> int:
    args = parse_args()
    input_path = Path(args.input_csv).expanduser().resolve()
    output_path = Path(args.output_csv).expanduser().resolve()
    if not input_path.exists():
        print(f"Input CSV not found: {input_path}")
        return 1

    source_urls = read_channel_urls_from_csv(input_path, args.input_column)
    if not source_urls:
        print("No Telegram URLs found in CSV.")
        return 1

    timeout_ms = max(5, args.timeout_seconds) * 1000
    sync_playwright = _require_sync_playwright()
    all_rows: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint, timeout=timeout_ms)
        except TypeError:
            browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint)
        except Exception as exc:
            print(f"Could not connect to Chrome CDP at {args.cdp_endpoint}: {exc}")
            print("Запустите Chrome с --remote-debugging-port=9222 и войдите в Telegram Web.")
            return 1

        if not browser.contexts:
            print("Connected to CDP, but no browser contexts are available.")
            return 1
        context = browser.contexts[0]
        page = context.pages[0] if context.pages else context.new_page()

        try:
            page.goto("https://web.telegram.org/k/", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1_000)
        except Exception:
            pass

        for index, source_url in enumerate(source_urls, start=1):
            print(f"[{index}/{len(source_urls)}] {source_url}")
            try:
                rows = process_channel(
                    page=page,
                    source_url=source_url,
                    timeout_ms=timeout_ms,
                    max_scrolls=max(1, args.max_scrolls),
                    stable_rounds=max(1, args.stable_rounds),
                    scroll_pause_ms=max(100, args.scroll_pause_ms),
                )
            except Exception as exc:
                rows = [
                    {
                        "source_channel_url": source_url,
                        "resolved_channel_url": "",
                        "discussion_url": "",
                        "member_name": "",
                        "member_profile_url": "",
                        "peer_id": "",
                        "member_status": "",
                        "status": "runtime_error",
                        "error": str(exc),
                    }
                ]
            all_rows.extend(rows)

    write_output(output_path, all_rows)
    ok_count = sum(1 for row in all_rows if row.get("status") == "ok")
    print(f"Done. Saved: {output_path}")
    print(f"Rows: {len(all_rows)} | Members: {ok_count}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
