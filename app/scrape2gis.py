from html import unescape
import re
from typing import Optional
from urllib.parse import urlsplit
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


if __name__ == "__main__":
    sites = get_firm_sites("spb", "5348552839303489")
    print(sites)
