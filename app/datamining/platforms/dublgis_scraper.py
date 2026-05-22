from html import unescape
import json
import re
from typing import Optional
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 60
# noinspection HttpUrlsUsage
LINK_PREFIXES = ("http://link.2gis.ru/", "https://link.2gis.ru/")
ASCII_FQDN_LABEL_RE = re.compile(r"^[a-z0-9-]{1,63}$")
MAX_HTML_BYTES = 2_000_000
READ_CHUNK_BYTES = 65_536


def _fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        chunks: list[bytes] = []
        total = 0
        while total < MAX_HTML_BYTES:
            to_read = min(READ_CHUNK_BYTES, MAX_HTML_BYTES - total)
            try:
                chunk = response.read(to_read)
            except TimeoutError:
                # Return already loaded content if the remote endpoint stalls.
                if chunks:
                    break
                raise
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks).decode("utf-8", errors="ignore")


def _is_valid_fqdn(value: str) -> bool:
    candidate = value.strip().lower().rstrip(".")
    if not candidate or "." not in candidate or any(ch.isspace() for ch in candidate):
        return False
    try:
        ascii_fqdn = candidate.encode("idna").decode("ascii")
    except UnicodeError:
        return False

    if len(ascii_fqdn) > 253:
        return False
    labels = ascii_fqdn.split(".")
    if len(labels) < 2:
        return False

    for label in labels:
        if not label:
            return False
        if label.startswith("-") or label.endswith("-"):
            return False
        if not ASCII_FQDN_LABEL_RE.fullmatch(label):
            return False
    return len(labels[-1]) >= 2


def _extract_fqdn_from_anchor_text(text: str) -> Optional[str]:
    candidate = unescape(text).strip().lower()
    if not candidate:
        return None

    if "://" in candidate:
        parsed = urlsplit(candidate)
        candidate = (parsed.hostname or "").lower()
    else:
        candidate = candidate.split("/", 1)[0].strip().lower()

    candidate = candidate.rstrip(".")
    if not candidate or not _is_valid_fqdn(candidate):
        return None
    return candidate


def _extract_sites_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = unescape(tag["href"]).strip()
        if not href.startswith(LINK_PREFIXES):
            continue

        # Keep only explicit plain-text website anchors; ignore nested UI links from similar-org blocks.
        if tag.find(True) is not None:
            continue

        fqdn = _extract_fqdn_from_anchor_text(tag.get_text(" ", strip=True))
        if not fqdn:
            continue
        if fqdn in seen:
            continue
        seen.add(fqdn)
        links.append(fqdn)

    return links


def get_firm_sites_by_url(firm_url: str) -> list[str]:
    html = _fetch_html(firm_url)
    return _extract_sites_from_html(html)


def get_firm_sites(city_code: str, firm_id: str) -> list[str]:
    url = f"https://2gis.ru/{city_code}/firm/{firm_id}"
    return get_firm_sites_by_url(url)


PHOTO_API_URL = "https://api.photo.2gis.com/2.0/photo/get"
# Matches full-size photo-gallery and legacy images/branch URLs served by 2GIS CDN.
_PHOTO_URL_RE = re.compile(
    r"https?://[a-z0-9]+\.photo\.2gis\.com/(?:photo-gallery|images/(?:branch|profile))/[^\s\"'<>]+"
)
# Fallback key embedded in 2GIS frontend — extracted dynamically when possible.
_PHOTO_API_KEY_FALLBACK = "gYu1s9N1wP"
_PHOTO_API_KEY_RE = re.compile(r'"photoApiKey"\s*:\s*"([^"]+)"')
_FIRM_ID_RE = re.compile(r"/firm/(\d+)")


def _extract_photo_api_key(html: str) -> Optional[str]:
    m = _PHOTO_API_KEY_RE.search(html)
    return m.group(1) if m else None


def _fetch_photos_from_api(
    firm_id: str,
    api_key: str,
    page_size: int = 50,
    max_photos: Optional[int] = None,
) -> list[str]:
    """Paginate through the 2GIS photo API and return direct photo URLs.

    Stops when the API returns an empty page or fewer items than requested
    (reliable last-page signal that doesn't rely on the 'total' field).
    """
    photos: list[str] = []
    page = 1

    while True:
        fetch_size = page_size
        if max_photos is not None:
            remaining = max_photos - len(photos)
            if remaining <= 0:
                break
            fetch_size = min(page_size, remaining)

        params = urlencode({
            "key": api_key,
            "object_id": firm_id,
            "object_type": "branch",
            "locale": "ru",
            "status": "active",
            "sort_by": "position",
            "album_code": "common",
            "size": fetch_size,
            "page": page,
        })
        req = Request(f"{PHOTO_API_URL}?{params}", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))

        albums = data.get("result", [])
        if not albums:
            break
        items = albums[0].get("items", [])
        if not items:
            break

        for item in items:
            url = item.get("url")
            if url:
                photos.append(url)

        if len(items) < fetch_size:
            break  # fewer items than requested → last page
        page += 1

    return photos


def get_firm_photos_by_url(
    firm_url: str,
    max_photos: Optional[int] = None,
    max_retries: int = 3,
    mode: str = "api",
) -> list[str]:
    """Return company photo URLs.

    mode="api"        — 2GIS internal photo API (fast, no browser).
    mode="playwright" — headless browser scroll on the /tab/photos page (no API key).
    """
    if mode == "playwright":
        return _get_firm_photos_playwright(firm_url, max_photos=max_photos)

    m = _FIRM_ID_RE.search(firm_url)
    if not m:
        raise ValueError(f"Cannot extract firm ID from URL: {firm_url}")
    firm_id = m.group(1)

    api_key = _PHOTO_API_KEY_FALLBACK
    for _ in range(max_retries):
        try:
            html = _fetch_html(firm_url)
            api_key = _extract_photo_api_key(html) or _PHOTO_API_KEY_FALLBACK
            break
        except TimeoutError:
            pass

    return _fetch_photos_from_api(firm_id, api_key, max_photos=max_photos)


def _get_firm_photos_playwright(
    firm_url: str,
    max_photos: Optional[int] = None,
    headless: bool = False,
    timeout_ms: int = 90_000,
    **_kwargs,
) -> list[str]:
    """Load the 2GIS photo tab in a real browser, capture the API key that the
    page's own JavaScript sends to api.photo.2gis.com, then fetch all photo
    pages via our own HTTP requests with that key.

    Defaults to headless=False so the browser fingerprint is indistinguishable
    from a normal user session.  Pass headless=True for unattended use (may be
    blocked by 2GIS anti-bot).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: uv add playwright && uv run playwright install chromium"
        ) from exc

    from app.datamining.playwright_utils import cleanup_stale_profile_locks

    m = _FIRM_ID_RE.search(firm_url)
    if not m:
        raise ValueError(f"Cannot extract firm ID from URL: {firm_url}")
    firm_id = m.group(1)

    base = firm_url.split("/tab/")[0]
    photo_tab_url = f"{base}/tab/photos"

    profile_dir = Path.home() / ".cache" / "leadheat" / "playwright-2gis"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_profile_locks(profile_dir)

    # Capture the API key from the first request the browser itself makes.
    captured_key: list[str] = []

    def _on_request(request) -> None:
        if captured_key:
            return
        if "api.photo.2gis.com" not in request.url:
            return
        key = parse_qs(urlsplit(request.url).query).get("key", [None])[0]
        if key:
            captured_key.append(key)

    with sync_playwright() as pw:
        launch_kwargs: dict = dict(
            user_data_dir=str(profile_dir),
            headless=headless,
            user_agent=USER_AGENT,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            context = pw.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception:
            context = pw.chromium.launch_persistent_context(**launch_kwargs)

        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.on("request", _on_request)
        try:
            try:
                page.goto(photo_tab_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception:
                pass  # timeout on slow pages — still try to collect
            try:
                page.wait_for_request(
                    lambda r: "api.photo.2gis.com" in r.url,
                    timeout=20_000,
                )
            except Exception:
                pass
            page.wait_for_timeout(1_000)
        finally:
            context.close()

    if not captured_key:
        return []
    return _fetch_photos_from_api(firm_id, captured_key[0], max_photos=max_photos)


def get_firm_photos(city_code: str, firm_id: str, **kwargs) -> list[str]:
    url = f"https://2gis.ru/{city_code}/firm/{firm_id}"
    return get_firm_photos_by_url(url, **kwargs)


if __name__ == "__main__":
    sites = get_firm_sites("spb", "5348552839303489")
    print(sites)
