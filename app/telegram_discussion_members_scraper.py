from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
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
                pid = int(pid_match.group(1))
                should_cleanup = not _pid_is_running(pid)
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


def _is_active_chat_open(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
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
                    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                        || chats.find((chat) => isVisible(chat));
                    if (!active) return false;
                    const header = active.querySelector('.chat-info-container, .sidebar-header.topbar');
                    if (!header) return false;
                    const text = (header.textContent || '').trim();
                    return text.length > 0;
                }"""
            )
        )
    except Exception:
        return False


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


def _ensure_channel_open(page: Any, normalized_url: str, timeout_ms: int) -> bool:
    if _is_active_chat_open(page):
        return True

    target_key = _extract_hash_key(normalized_url).lower()
    target_username = target_key[1:] if target_key.startswith("@") else target_key
    target_username_norm = re.sub(r"[^a-z0-9а-яё]+", "", target_username.lower())
    rounds = max(12, timeout_ms // 250)
    for idx in range(rounds):
        if _is_active_chat_open(page):
            return True

        best_href = page.evaluate(
            """([targetKey, targetUserNorm]) => {
                const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
                const links = Array.from(document.querySelectorAll('a[href]'));
                let best = null;
                let bestScore = -1;
                for (const link of links) {
                    const href = normalize(link.getAttribute('href') || '');
                    const text = normalize(link.textContent || '');
                    const textCompact = compact(text);
                    if (!href && !text) continue;
                    let score = 0;
                    if (targetKey && href.includes(targetKey)) score += 100;
                    if (targetUserNorm && textCompact.includes(targetUserNorm)) score += 40;
                    if (link.className && String(link.className).includes('chatlist-chat')) score += 20;
                    const rect = link.getBoundingClientRect();
                    const style = window.getComputedStyle(link);
                    const visible =
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none';
                    if (!visible) score -= 100;
                    if (score > bestScore) {
                        best = link;
                        bestScore = score;
                    }
                }
                if (best && bestScore > 20) {
                    return best.getAttribute('href') || '';
                }
                return '';
            }""",
            [target_key, target_username_norm],
        )
        if best_href:
            if best_href.startswith("#-"):
                try:
                    page.goto(f"https://web.telegram.org/k/{best_href}", wait_until="domcontentloaded", timeout=3000)
                    page.wait_for_timeout(350)
                    if _is_active_chat_open(page):
                        return True
                except Exception:
                    pass
            try:
                page.locator(f'a[href="{best_href}"]').first.click(timeout=1200)
                page.wait_for_timeout(300)
            except Exception:
                pass

        clicked = bool(
            page.evaluate(
                """([targetKey, targetUserNorm]) => {
                    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
                    const links = Array.from(document.querySelectorAll('a[href]'));
                    let best = null;
                    let bestScore = -1;

                    for (const link of links) {
                        const href = normalize(link.getAttribute('href') || '');
                        const text = normalize(link.textContent || '');
                        const textCompact = compact(text);
                        if (!href && !text) continue;

                        let score = 0;
                        if (targetKey && href.includes(targetKey)) score += 100;
                        if (targetUserNorm && textCompact.includes(targetUserNorm)) score += 40;
                        if (link.className && String(link.className).includes('chatlist-chat')) score += 20;

                        const rect = link.getBoundingClientRect();
                        const style = window.getComputedStyle(link);
                        const visible =
                            rect.width > 0 &&
                            rect.height > 0 &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                        if (!visible) score -= 100;

                        if (score > bestScore) {
                            best = link;
                            bestScore = score;
                        }
                    }

                    if (best && bestScore > 20) {
                        best.click();
                        return true;
                    }
                    return false;
                }""",
                [target_key, target_username_norm],
            )
        )

        if clicked:
            page.wait_for_timeout(350)
            if _is_active_chat_open(page):
                return True

        # Periodic fallback via sidebar search (works when @username is unresolved).
        if target_username and idx % 4 == 0:
            _ = page.evaluate(
                """([rawUser, targetUserNorm]) => {
                    const inputs = Array.from(
                        document.querySelectorAll(
                            'input[type="text"], input, [contenteditable="true"][role="textbox"], [contenteditable="true"]'
                        )
                    );
                    const input = inputs.find((el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    });
                    if (!input) return false;

                    input.focus();
                    if ('value' in input) {
                        input.value = rawUser;
                    } else {
                        input.textContent = rawUser;
                    }
                    input.dispatchEvent(new Event('input', { bubbles: true }));

                    const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                    const compact = (s) => normalize(s).replace(/[^a-z0-9а-яё]+/g, '');
                    const links = Array.from(document.querySelectorAll('a[href], .chatlist-chat'));
                    let best = null;
                    let bestScore = -1;
                    for (const link of links) {
                        const text = normalize(link.textContent || '');
                        const textCompact = compact(text);
                        if (!text) continue;
                        let score = 0;
                        if (targetUserNorm && textCompact.includes(targetUserNorm)) score += 80;
                        const rect = link.getBoundingClientRect();
                        const style = window.getComputedStyle(link);
                        const visible =
                            rect.width > 0 &&
                            rect.height > 0 &&
                            style.visibility !== 'hidden' &&
                            style.display !== 'none';
                        if (!visible) score -= 100;
                        if (score > bestScore) {
                            best = link;
                            bestScore = score;
                        }
                    }
                    if (best && bestScore > 20) {
                        best.click();
                        return true;
                    }
                    return false;
                }""",
                [target_username, target_username_norm],
            )

        page.wait_for_timeout(250)
    return False


def _is_group_chat_open(page: Any) -> bool:
    try:
        return bool(
            page.evaluate(
                """() => {
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
                    const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                    const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                        || chats.find((chat) => isVisible(chat));
                    if (!active) return false;
                    const header = active.querySelector('.chat-info-container, .sidebar-header.topbar');
                    const headerText = (header?.textContent || '').toLowerCase();
                    if (/subscribers?|подписчик/.test(headerText)) return false;
                    if (/comments?|discussion|обсужд|коммент/.test(headerText)) return true;
                    if (/members?|участник/.test(headerText)) return true;
                    const sidebar = document.querySelector('.sidebar.sidebar-right');
                    const sidebarText = (sidebar?.textContent || '').toLowerCase();
                    if (/channel info|информация о канале/.test(sidebarText)) return false;
                    if (/group info|информация о группе/.test(sidebarText)) return true;
                    const hasMemberRows = !!sidebar?.querySelector('[data-peer-id]');
                    return /members|участник/.test(sidebarText) && hasMemberRows;
                }"""
            )
        )
    except Exception:
        return False


def _click_active_chat_menu(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
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
                const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                    || chats.find((chat) => isVisible(chat));
                if (!active) return false;

                // Close any previous overlays/popups that may steal focus.
                document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }));

                const buttons = Array.from(active.querySelectorAll('.chat-info-container button.btn-menu-toggle')).filter((button) => {
                    const rect = button.getBoundingClientRect();
                    const style = window.getComputedStyle(button);
                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        rect.top >= 0 &&
                        rect.top < 120 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none'
                    );
                });
                if (!buttons.length) return false;

                // Rightmost button is usually the chat header 3-dots menu.
                buttons.sort((a, b) => b.getBoundingClientRect().left - a.getBoundingClientRect().left);
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
                const items = Array.from(document.querySelectorAll('.btn-menu-item')).filter((item) => {
                    const rect = item.getBoundingClientRect();
                    const style = window.getComputedStyle(item);
                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none' &&
                        item.offsetParent !== null
                    );
                });
                const target = items.find((item) => pattern.test((item.textContent || '').replace(/\\s+/g, ' ').trim()));
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
    )


def _click_leave_comment(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const pattern = /(leave\\s+a\\s+comment|view\\s+comments|comments?|коммент|обсужд)/i;
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return (
                        rect.width > 0 &&
                        rect.height > 0 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none'
                    );
                };
                const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                    || chats.find((chat) => isVisible(chat));
                if (!active) return false;

                // Prefer explicit replies footer in channel posts.
                const replies = Array.from(
                    active.querySelectorAll('replies-element.replies-footer, .replies-footer, .replies-footer-text')
                ).filter((el) => {
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return text && pattern.test(text) && isVisible(el);
                });
                if (replies.length) {
                    replies.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
                    replies[0].click();
                    return true;
                }

                const candidates = Array.from(active.querySelectorAll('button, a, div, span')).filter((el) => {
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    return text && pattern.test(text) && isVisible(el);
                });
                if (!candidates.length) return false;
                candidates.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
                candidates[0].click();
                return true;
            }"""
        )
    )


def _click_discussion_anywhere(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const patternStrong = /(view\\s*discussion|open\\s*comments|leave\\s+a\\s+comment|обсужд|коммент)/i;
                const patternWeak = /(comments?)/i;

                const isVisible = (el) => {
                    if (!(el instanceof Element)) return false;
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) return false;
                    const style = window.getComputedStyle(el);
                    if (style.visibility === 'hidden' || style.display === 'none') return false;
                    return true;
                };

                const isClickable = (el) => {
                    if (!(el instanceof Element)) return false;
                    if (el.matches('button, a, [role="button"], .btn-menu-item, .row-clickable, .rp')) return true;
                    const onclickAttr = el.getAttribute('onclick');
                    if (onclickAttr) return true;
                    const style = window.getComputedStyle(el);
                    return style.cursor === 'pointer';
                };

                const clickableAncestor = (el) => {
                    let node = el;
                    for (let i = 0; i < 6 && node; i += 1) {
                        if (isClickable(node) && isVisible(node)) return node;
                        node = node.parentElement;
                    }
                    return null;
                };

                const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                    || chats.find((chat) => {
                        const rect = chat.getBoundingClientRect();
                        const style = window.getComputedStyle(chat);
                        return (
                            rect.width > 260 &&
                            rect.height > 260 &&
                            style.display !== 'none' &&
                            style.visibility !== 'hidden'
                        );
                    });
                const roots = [
                    ...(active ? [active] : []),
                    ...Array.from(document.querySelectorAll('.btn-menu-item, .popup-container, .btn-menu')),
                ];
                if (!roots.length) roots.push(document.body);

                const seen = new Set();
                const candidates = [];
                for (const root of roots) {
                    const nodes = root.querySelectorAll('*');
                    for (const node of nodes) {
                        if (!(node instanceof Element) || !isVisible(node)) continue;
                        const text = normalize(node.textContent);
                        if (!text || text.length > 140) continue;
                        if (!patternStrong.test(text) && !patternWeak.test(text)) continue;
                        const clickable = clickableAncestor(node);
                        if (!clickable) continue;
                        if (seen.has(clickable)) continue;
                        seen.add(clickable);

                        let score = 0;
                        if (/view\\s*discussion|обсужд/.test(text)) score += 50;
                        if (/open\\s*comments|leave\\s+a\\s+comment|коммент/.test(text)) score += 30;
                        if (/comments?/.test(text)) score += 10;
                        if (clickable.matches('.btn-menu-item')) score += 20;
                        if (clickable.matches('button, a')) score += 10;
                        score -= Math.max(0, text.length - 40) / 10;
                        candidates.push({ clickable, score });
                    }
                }

                candidates.sort((a, b) => b.score - a.score);
                const target = candidates[0]?.clickable;
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
    )


def _click_discussion_via_playwright(page: Any) -> bool:
    patterns = [
        re.compile(r"view\s*discussion|discussion|обсужд", re.IGNORECASE),
        re.compile(r"leave\s+a\s+comment|open\s+comments|comments?|коммент", re.IGNORECASE),
    ]
    for pattern in patterns:
        try:
            locator = page.get_by_text(pattern)
            count = min(locator.count(), 10)
        except Exception:
            continue
        for index in range(count):
            try:
                locator.nth(index).click(timeout=1200, force=True)
                page.wait_for_timeout(450)
                return True
            except Exception:
                continue
    return False


def _discussion_debug_snapshot(page: Any) -> str:
    try:
        payload = page.evaluate(
            """() => {
                const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
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
                const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                    || chats.find((chat) => isVisible(chat));
                const activeHeaderText = normalize(
                    active?.querySelector('.chat-info-container, .sidebar-header.topbar')?.textContent || ''
                );

                const menuItems = Array.from(document.querySelectorAll('.btn-menu-item'))
                    .map((el) => normalize(el.textContent))
                    .filter(Boolean)
                    .slice(0, 20);

                const anchors = Array.from(document.querySelectorAll('a[href]'))
                    .map((el) => ({
                        href: normalize(el.getAttribute('href') || ''),
                        text: normalize(el.textContent || '').slice(0, 120),
                        cls: normalize(String(el.className || '')).slice(0, 120),
                    }))
                    .slice(0, 60);

                const chatlistLike = anchors.filter((item) => /chatlist-chat|row-clickable|chatlist/.test(item.cls)).slice(0, 30);

                const commentLike = Array.from(
                    (active || document).querySelectorAll('button, a, div, span')
                )
                    .map((el) => normalize(el.textContent))
                    .filter((t) => /(discussion|comment|обсужд|коммент)/i.test(t))
                    .slice(0, 30);

                const activeUrl = location.href;
                return {
                    active_url: activeUrl,
                    active_header_text: activeHeaderText.slice(0, 220),
                    menu_items: menuItems,
                    discussion_like_texts: commentLike,
                    anchors_count: anchors.length,
                    anchors_sample: anchors.slice(0, 20),
                    chatlist_links_sample: chatlistLike,
                };
            }"""
        )
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"debug_collect_failed: {exc}"


def _open_group_info(page: Any) -> bool:
    already_open = bool(
        page.evaluate(
            """() => {
                const sidebar = document.querySelector('.sidebar.sidebar-right');
                if (!sidebar) return false;
                const text = (sidebar.textContent || '').toLowerCase();
                if (/channel info|информация о канале/.test(text)) return false;
                if (/group info|информация о группе/.test(text)) return true;
                return /members|участники/.test(text) && !!sidebar.querySelector('[data-peer-id]');
            }"""
        )
    )
    if already_open:
        return True

    clicked = bool(
        page.evaluate(
            """() => {
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
                const chats = Array.from(document.querySelectorAll('.chat, .chat.tabs-tab'));
                const active = chats.find((chat) => chat.classList.contains('active') && isVisible(chat))
                    || chats.find((chat) => isVisible(chat));
                const header = active?.querySelector('.chat-info-container, .sidebar-header.topbar');
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
                    if (/channel info|информация о канале/.test(text)) return false;
                    if (/group info|информация о группе/.test(text)) return true;
                    return /members|участники/.test(text) && !!sidebar.querySelector('[data-peer-id]');
                }"""
            )
        )
        if ready:
            return True
        page.wait_for_timeout(250)
    return False


def _maybe_subscribe(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const pattern = /(subscribe|join|подпис|вступить)/i;
                const buttons = Array.from(document.querySelectorAll('button, a'));
                const target = buttons.find((el) => {
                    const text = (el.textContent || '').replace(/\\s+/g, ' ').trim();
                    if (!text || !pattern.test(text)) return false;
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                });
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
    )


def _ensure_members_tab(page: Any) -> None:
    page.evaluate(
        """() => {
            const sidebar = document.querySelector('.sidebar.sidebar-right') || document;
            const targets = Array.from(sidebar.querySelectorAll('nav *, [role="tab"], .tabs-with-icons *'));
            const normalized = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            for (const node of targets) {
                const text = normalized(node.textContent);
                if (!text) continue;
                if (text === 'members' || text === 'участники' || text.includes('members') || text.includes('участник')) {
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
                document.querySelectorAll(
                    '.sidebar.sidebar-right a.chatlist-chat-abitbigger[data-peer-id], .sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id], .sidebar.sidebar-right .chatlist-chat[data-peer-id]'
                )
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


def _wait_for_members_list(page: Any, timeout_ms: int = 8_000) -> bool:
    rounds = max(4, timeout_ms // 250)
    for _ in range(rounds):
        ready = bool(
            page.evaluate(
                """() => {
                    const sidebar = document.querySelector('.sidebar.sidebar-right');
                    if (!sidebar) return false;
                    return !!sidebar.querySelector(
                        'a.chatlist-chat-abitbigger[data-peer-id], .chatlist-chat-abitbigger[data-peer-id], .chatlist-chat[data-peer-id]'
                    );
                }"""
            )
        )
        if ready:
            return True
        _ensure_members_tab(page)
        page.wait_for_timeout(250)
    return False


def _scroll_members_list(page: Any) -> bool:
    return bool(
        page.evaluate(
            """() => {
                const row = document.querySelector(
                    '.sidebar.sidebar-right a.chatlist-chat-abitbigger[data-peer-id], .sidebar.sidebar-right .chatlist-chat-abitbigger[data-peer-id], .sidebar.sidebar-right .chatlist-chat[data-peer-id]'
                );
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
        if not visible and not collected:
            page.wait_for_timeout(max(200, scroll_pause_ms))
            continue
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
    clicked = False
    # Most reliable path in Telegram Web: click comments/discussion footer in a channel post.
    clicked = _click_leave_comment(page)
    if not clicked:
        menu_opened = _click_active_chat_menu(page)
        if menu_opened:
            clicked = _click_discussion_item(page)
            if not clicked:
                page.keyboard.press("Escape")
    if not clicked:
        # Last-resort heuristic: click any visible discussion/comment action.
        clicked = _click_discussion_anywhere(page)
    if not clicked:
        # Playwright locator fallback works better on some Telegram builds.
        clicked = _click_discussion_via_playwright(page)
    if not clicked:
        return False, ""

    initial_url = page.url
    rounds = max(8, timeout_ms // 250)
    for _ in range(rounds):
        page.wait_for_timeout(250)
        if _is_group_chat_open(page):
            return True, page.url
        if page.url != initial_url:
            # Telegram often switches hash first and only then syncs header/sidebar.
            return True, page.url
    return False, ""


def _discover_ws_debugger_url(cdp_endpoint: str, timeout_seconds: int = 5) -> str | None:
    endpoint = (cdp_endpoint or "").strip().rstrip("/")
    if not endpoint or not endpoint.startswith(("http://", "https://")):
        return None

    candidates = [
        f"{endpoint}/json/version",
        f"{endpoint}/json",
    ]
    for url in candidates:
        try:
            with urlopen(url, timeout=timeout_seconds) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except (URLError, OSError):
            continue

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if isinstance(payload, dict):
            ws = payload.get("webSocketDebuggerUrl")
            if isinstance(ws, str) and ws.startswith("ws://"):
                return ws
            continue

        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                ws = item.get("webSocketDebuggerUrl")
                if isinstance(ws, str) and ws.startswith("ws://"):
                    return ws
    return None


def process_channel(
    page: Any,
    source_url: str,
    timeout_ms: int,
    max_scrolls: int,
    stable_rounds: int,
    scroll_pause_ms: int,
    auto_subscribe: bool,
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

    if not _ensure_channel_open(page, normalized_url=normalized, timeout_ms=timeout_ms):
        return [
            {
                "source_channel_url": source_url,
                "resolved_channel_url": page.url,
                "discussion_url": "",
                "member_name": "",
                "member_profile_url": "",
                "peer_id": "",
                "member_status": "",
                "status": "channel_not_opened",
                "error": _discussion_debug_snapshot(page),
            }
        ]

    resolved_channel_url = page.url
    if auto_subscribe and _maybe_subscribe(page):
        page.wait_for_timeout(800)

    opened, discussion_url = _try_open_discussion(page, timeout_ms=timeout_ms)
    if not opened:
        debug_info = _discussion_debug_snapshot(page)
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
                "error": debug_info,
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
    _wait_for_members_list(page, timeout_ms=max(4_000, timeout_ms // 2))

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
        context = None
        own_context = False
        if args.mode == "persistent":
            profile_dir = Path(args.user_data_dir).expanduser().resolve()
            profile_dir.mkdir(parents=True, exist_ok=True)
            _cleanup_stale_profile_locks(profile_dir)
            launch_kwargs: dict[str, Any] = dict(
                user_data_dir=str(profile_dir),
                headless=bool(args.headless),
                locale="ru-RU",
                viewport={"width": 1440, "height": 900},
            )
            channel = (args.browser_channel or "").strip()
            effective_channel = channel
            if channel:
                launch_kwargs["channel"] = channel
            try:
                context = playwright.chromium.launch_persistent_context(
                    **launch_kwargs,
                )
            except Exception as exc:
                # CFT may crash on a previously used/corrupted profile; retry with a clean sibling profile.
                if not channel:
                    system_kwargs = dict(launch_kwargs)
                    system_kwargs["channel"] = "chrome"
                    try:
                        print(f"Primary profile failed for CFT: {profile_dir}")
                        print("Retrying with system Chrome channel...")
                        context = playwright.chromium.launch_persistent_context(**system_kwargs)
                        effective_channel = "chrome"
                    except Exception:
                        fallback_dir = profile_dir.with_name(f"{profile_dir.name}-cft")
                        fallback_dir.mkdir(parents=True, exist_ok=True)
                        _cleanup_stale_profile_locks(fallback_dir)
                        launch_kwargs["user_data_dir"] = str(fallback_dir)
                        print(f"Retrying with fallback profile: {fallback_dir}")
                        context = playwright.chromium.launch_persistent_context(**launch_kwargs)
                        profile_dir = fallback_dir
                else:
                    raise
            own_context = True
            page = context.pages[0] if context.pages else context.new_page()
            print(f"Persistent profile: {profile_dir}")
            print(f"Persistent browser: {'chromium(cft)' if not effective_channel else effective_channel}")
        else:
            browser = None
            primary_error: Exception | None = None
            try:
                browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint, timeout=timeout_ms)
            except TypeError:
                try:
                    browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint)
                except Exception as exc:
                    primary_error = exc
            except Exception as exc:
                primary_error = exc

            if browser is None:
                ws_url = _discover_ws_debugger_url(args.cdp_endpoint)
                if ws_url:
                    try:
                        browser = playwright.chromium.connect_over_cdp(ws_url, timeout=timeout_ms)
                        print(f"CDP fallback: connected via {ws_url}")
                    except Exception:
                        browser = None

            if browser is None:
                print(f"Could not connect to Chrome CDP at {args.cdp_endpoint}: {primary_error}")
                print("Запустите отдельный Chrome с --remote-debugging-port=<port> и войдите в Telegram Web.")
                print(
                    "Пример: open -na \"Google Chrome\" --args "
                    "--remote-debugging-port=9223 --user-data-dir=/tmp/chrome-cdp-9223"
                )
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

        if args.mode == "persistent":
            is_logged_in = bool(
                page.evaluate(
                    """() => {
                        const loginMarkers = [
                            ...document.querySelectorAll('input, button, div, span')
                        ].some((el) => {
                            const text = (el.textContent || '').toLowerCase();
                            return (
                                text.includes('log in') ||
                                text.includes('sign in') ||
                                text.includes('войти') ||
                                text.includes('phone number') ||
                                text.includes('номер телефона')
                            );
                        });
                        const chatListExists = !!document.querySelector('.chatlist-container, .tabs-container');
                        return chatListExists && !loginMarkers;
                    }"""
                )
            )
            if not is_logged_in:
                wait_seconds = max(10, int(args.login_wait_seconds))
                print(f"Ожидание ручного логина в Telegram Web: до {wait_seconds} сек.")
                deadline_ms = wait_seconds * 1000
                elapsed = 0
                while elapsed < deadline_ms:
                    page.wait_for_timeout(2000)
                    elapsed += 2000
                    is_logged_in = bool(
                        page.evaluate(
                            """() => {
                                const loginMarkers = [
                                    ...document.querySelectorAll('input, button, div, span')
                                ].some((el) => {
                                    const text = (el.textContent || '').toLowerCase();
                                    return (
                                        text.includes('log in') ||
                                        text.includes('sign in') ||
                                        text.includes('войти') ||
                                        text.includes('phone number') ||
                                        text.includes('номер телефона')
                                    );
                                });
                                const chatListExists = !!document.querySelector('.chatlist-container, .tabs-container');
                                return chatListExists && !loginMarkers;
                            }"""
                        )
                    )
                    if is_logged_in:
                        break
                if not is_logged_in:
                    print("Логин в Telegram не завершён. Завершите вход и запустите скрипт снова.")
                    if own_context and context is not None:
                        context.close()
                    return 1

            if not bool(args.no_login_prompt):
                print("Войдите в Telegram Web в открытом окне браузера.")
                try:
                    input("После входа нажмите Enter, чтобы начать сбор: ")
                except EOFError:
                    # Non-interactive shell: continue with current state.
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
                    auto_subscribe=bool(args.auto_subscribe),
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

        if own_context and context is not None:
            context.close()

    write_output(output_path, all_rows)
    ok_count = sum(1 for row in all_rows if row.get("status") == "ok")
    print(f"Done. Saved: {output_path}")
    print(f"Rows: {len(all_rows)} | Members: {ok_count}")
    return 0


if __name__ == "__main__":
    sys.exit(run())
