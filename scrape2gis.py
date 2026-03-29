from html import unescape
from typing import Optional
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup

# noinspection HttpUrlsUsage
HTTP_PROTOCOLS = ("http://", "https://")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20
LINK_PREFIX = "https://link.2gis.ru/"


def _fetch_html(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        return response.read().decode("utf-8", errors="ignore")


def _extract_url_from_2gis_link(url: str) -> Optional[str]:
    _, separator, target = url.partition("?")
    if not separator:
        return None

    target = target.strip()
    if target.startswith(HTTP_PROTOCOLS):
        return target

    return None


def _normalize_site_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/") or ""
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))


def _extract_sites_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all("a", href=True):
        href = unescape(tag["href"]).strip()
        if not href.startswith(LINK_PREFIX):
            continue

        target = _extract_url_from_2gis_link(href)
        if not target:
            continue

        normalized_target = _normalize_site_url(target)
        if normalized_target not in seen:
            seen.add(normalized_target)
            links.append(normalized_target)

    return links


def get_firm_sites(city_code: str, firm_id: str) -> list[str]:
    url = f"https://2gis.ru/{city_code}/firm/{firm_id}"
    html = _fetch_html(url)
    return _extract_sites_from_html(html)


if __name__ == "__main__":
    sites = get_firm_sites("spb", "5348552839303489")
    print(sites)
