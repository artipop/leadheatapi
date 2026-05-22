import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.error import URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import urlopen

# JS that collects every URL-like string visible in img[src], img[data-src],
# source[srcset] and any element whose background-image points to an image host.
_IMG_URLS_JS = """
() => {
    const urls = new Set();
    document.querySelectorAll('img[src],img[data-src]').forEach(el => {
        if (el.src) urls.add(el.src);
        if (el.dataset && el.dataset.src) urls.add(el.dataset.src);
    });
    document.querySelectorAll('[style*="background"]').forEach(el => {
        const m = el.style.backgroundImage.match(/url\\(['"']?(https?:[^'"')]+)/);
        if (m) urls.add(m[1]);
    });
    return Array.from(urls);
}
"""

PAGE_HEADER_SELECTORS = (
    "header",
    "[role='banner']",
    "[id*='header' i]",
    "[class*='header' i]",
)
PAGE_FOOTER_SELECTORS = (
    "footer",
    "[role='contentinfo']",
    "[id*='footer' i]",
    "[class*='footer' i]",
)


def pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cleanup_stale_profile_locks(profile_dir: Path) -> None:
    lock_names = ("SingletonLock", "SingletonSocket", "SingletonCookie")
    lock_path = profile_dir / "SingletonLock"
    should_cleanup = True

    if lock_path.exists() or lock_path.is_symlink():
        try:
            target = os.readlink(lock_path)
            pid_match = re.search(r"-(\d+)$", target)
            if pid_match:
                should_cleanup = not pid_is_running(int(pid_match.group(1)))
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


def normalize_browser_host(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("www."):
        value = value[4:]
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def normalize_homepage_url(site: str) -> str | None:
    value = site.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_browser_host(parsed.hostname or "")
    if not host:
        return None
    return urlunsplit(("https", host, "/", "", ""))


async def wait_until_async(
    check: Callable[[], Awaitable[bool]],
    timeout_seconds: int,
    interval_ms: int = 2_000,
) -> bool:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        if await check():
            return True
        await asyncio.sleep(max(100, interval_ms) / 1000)
    return await check()


async def wait_for_manual_login(
    page: Any,
    is_login_required: Callable[[Any], Awaitable[bool]],
    wait_seconds: int,
    interval_ms: int = 2_000,
) -> bool:
    async def logged_in() -> bool:
        return not await is_login_required(page)

    deadline = time.monotonic() + max(10, wait_seconds)
    while time.monotonic() < deadline:
        if await logged_in():
            return True
        await page.wait_for_timeout(max(100, interval_ms))
    return await logged_in()


def wait_until_sync(check: Callable[[], bool], timeout_seconds: int, interval_ms: int = 1_000) -> bool:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(max(100, interval_ms) / 1000)
    return check()


async def launch_async_persistent_context(
    playwright: Any,
    user_data_dir: str | Path,
    *,
    headless: bool,
    locale: str = "ru-RU",
    viewport: dict[str, int] | None = None,
    browser_channel: str = "",
    user_agent: str = "",
    retry_system_chrome: bool = False,
    fallback_suffix: str = "cft",
) -> tuple[Any, Any, Path, str]:
    profile_dir = Path(user_data_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_profile_locks(profile_dir)

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": bool(headless),
        "locale": locale,
        "viewport": viewport or {"width": 1440, "height": 900},
    }
    if browser_channel:
        launch_kwargs["channel"] = browser_channel
    if user_agent:
        launch_kwargs["user_agent"] = user_agent

    try:
        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        return context, context.pages[0] if context.pages else await context.new_page(), profile_dir, browser_channel
    except Exception:
        if browser_channel or not retry_system_chrome:
            raise

    system_kwargs = dict(launch_kwargs)
    system_kwargs["channel"] = "chrome"
    try:
        context = await playwright.chromium.launch_persistent_context(**system_kwargs)
        return context, context.pages[0] if context.pages else await context.new_page(), profile_dir, "chrome"
    except Exception:
        fallback_dir = profile_dir.with_name(f"{profile_dir.name}-{fallback_suffix}")
        fallback_dir.mkdir(parents=True, exist_ok=True)
        cleanup_stale_profile_locks(fallback_dir)
        launch_kwargs["user_data_dir"] = str(fallback_dir)
        context = await playwright.chromium.launch_persistent_context(**launch_kwargs)
        return context, context.pages[0] if context.pages else await context.new_page(), fallback_dir, browser_channel


def discover_ws_debugger_url(cdp_endpoint: str, timeout_seconds: int = 5) -> str | None:
    endpoint = (cdp_endpoint or "").strip().rstrip("/")
    if not endpoint or not endpoint.startswith(("http://", "https://")):
        return None

    for url in (f"{endpoint}/json/version", f"{endpoint}/json"):
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
        if isinstance(payload, list):
            for item in payload:
                if not isinstance(item, dict):
                    continue
                ws = item.get("webSocketDebuggerUrl")
                if isinstance(ws, str) and ws.startswith("ws://"):
                    return ws
    return None


def scroll_and_collect_urls(
    page: Any,
    url_pattern: re.Pattern,
    *,
    scroll_container_js: str = "window.scrollTo(0, document.body.scrollHeight)",
    extra_js: str | None = None,
    max_scrolls: int = 60,
    scroll_pause_ms: int = 800,
    stable_rounds: int = 4,
    max_items: int | None = None,
) -> list[str]:
    """Scroll *page* and collect URLs matching *url_pattern* until results stabilise.

    Works with both sync and async Playwright page objects (sync version here).
    Returns deduplicated URLs in discovery order.
    """
    seen: dict[str, None] = {}  # insertion-ordered set
    stable = 0
    prev_count = -1

    for _ in range(max(1, max_scrolls)):
        raw: list[str] = page.evaluate(_IMG_URLS_JS)
        if extra_js:
            raw += page.evaluate(extra_js)
        for url in raw:
            if url_pattern.search(url) and url not in seen:
                seen[url] = None

        if max_items and len(seen) >= max_items:
            break

        if len(seen) == prev_count:
            stable += 1
        else:
            stable = 0
        prev_count = len(seen)

        if stable >= max(1, stable_rounds):
            break

        page.evaluate(scroll_container_js)
        page.wait_for_timeout(max(100, scroll_pause_ms))
        try:
            page.wait_for_load_state("networkidle", timeout=2000)
        except Exception:
            pass

    result = list(seen.keys())
    return result[:max_items] if max_items else result


async def connect_async_over_cdp(playwright: Any, cdp_endpoint: str, timeout_ms: int) -> tuple[Any | None, Exception | None]:
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint, timeout=timeout_ms)
        return browser, None
    except TypeError:
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
            return browser, None
        except Exception as exc:
            return None, exc
    except Exception as exc:
        return None, exc
