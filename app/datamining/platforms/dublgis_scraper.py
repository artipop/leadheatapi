from html import unescape
import json
import re
from typing import Optional
from urllib.parse import urlencode, urlsplit
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
) -> list[str]:
    """Return all company photo URLs from the 2GIS photo API for a given firm page URL."""
    m = _FIRM_ID_RE.search(firm_url)
    if not m:
        raise ValueError(f"Cannot extract firm ID from URL: {firm_url}")
    firm_id = m.group(1)

    # Fetch HTML to extract the embedded API key (fall back to known key on timeout).
    api_key = _PHOTO_API_KEY_FALLBACK
    for _ in range(max_retries):
        try:
            html = _fetch_html(firm_url)
            api_key = _extract_photo_api_key(html) or _PHOTO_API_KEY_FALLBACK
            break
        except TimeoutError:
            pass

    return _fetch_photos_from_api(firm_id, api_key, max_photos=max_photos)


def get_firm_photos(city_code: str, firm_id: str, **kwargs) -> list[str]:
    url = f"https://2gis.ru/{city_code}/firm/{firm_id}"
    return get_firm_photos_by_url(url, **kwargs)


if __name__ == "__main__":
    sites = get_firm_sites("spb", "5348552839303489")
    print(sites)
