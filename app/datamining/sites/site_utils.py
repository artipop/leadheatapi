import os
import re
from collections import deque
from collections.abc import AsyncIterable
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from app.datamining.contracts import PageData

os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(Path(__file__).resolve().parents[2]))

try:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig
except ImportError:
    try:
        from crawl4ai import AsyncWebCrawler, CacheMode
        from crawl4ai.async_configs import BrowserConfig, CrawlerRunConfig
    except ImportError:
        AsyncWebCrawler = None
        BrowserConfig = None
        CrawlerRunConfig = None
        CacheMode = None

try:
    from crawl4ai.async_dispatcher import SemaphoreDispatcher
except ImportError:
    try:
        from crawl4ai.dispatcher import SemaphoreDispatcher
    except ImportError:
        SemaphoreDispatcher = None

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_CRAWLER_PAGE_TIMEOUT_SECONDS = 15
SKIP_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".ico",
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".zip",
    ".rar",
    ".7z",
    ".mp4",
    ".avi",
    ".mov",
    ".mp3",
    ".wav",
    ".xml",
}
MULTI_PART_TLDS = {"co", "com", "org", "net", "gov", "edu"}
CRAWL_PRIORITY_HINTS = (
    "contact",
    "contacts",
    "kontakty",
    "kontakti",
    "kontakt",
    "requisite",
    "requisites",
    "rekvizity",
    "rekvizit",
    "legal",
    "about",
    "o-kompanii",
    "company",
    "контакт",
    "контакты",
    "реквизит",
    "реквизиты",
    "юрид",
    "о компании",
    "price",
    "pricing",
    "prices",
    "service",
    "services",
    "stoimost",
    "cost",
    "ceny",
    "tseny",
    "prays",
    "price-list",
    "цены",
    "цена",
    "прайс",
    "прайс-лист",
    "стоимость",
    "услуги",
    "doctor",
    "doctors",
    "team",
    "staff",
    "specialist",
    "specialists",
    "vrachi",
    "personnel",
    "врач",
    "врачи",
    "команда",
    "персонал",
    "специалист",
    "специалисты",
    "booking",
    "appointment",
    "record",
    "zapis",
    "онлайн запис",
    "запис",
)


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_markdown_noise(text: str) -> str:
    cleaned = re.sub(r"[*_`>#\[\]\(\)]", "", text)
    return compact_spaces(cleaned)


def normalize_host(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_reg_ru_host(host: str) -> bool:
    normalized = normalize_host(host)
    return normalized == "reg.ru" or normalized.endswith(".reg.ru")


def decode_idna_host(host: str) -> str:
    normalized = normalize_host(host)
    if not normalized:
        return ""
    try:
        return normalized.encode("ascii").decode("idna")
    except UnicodeError:
        return normalized


def display_url(url: str) -> str:
    parsed = urlparse(url)
    host = decode_idna_host(parsed.hostname or "")
    if not host:
        return url

    netloc = host
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    return f"{parsed.scheme}://{netloc}{path}{('?' + parsed.query) if parsed.query else ''}"


def extract_origin_domain(host: str) -> str:
    normalized = normalize_host(host)
    parts = [part for part in normalized.split(".") if part]
    if len(parts) <= 2:
        return normalized
    if len(parts[-1]) == 2 and parts[-2] in MULTI_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_start_url(url: str) -> str:
    trimmed = url.strip()
    if not trimmed:
        raise ValueError("Empty URL")
    if "://" not in trimmed:
        trimmed = f"https://{trimmed}"

    parsed = urlparse(trimmed)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"Invalid URL: {url}")
    if is_reg_ru_host(parsed.hostname or ""):
        raise ValueError(f"Blocked host for crawl: {parsed.hostname}")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def is_binary_path(path: str) -> bool:
    lowered = path.lower()
    return any(lowered.endswith(ext) for ext in SKIP_EXTENSIONS)


def normalize_internal_url(raw_url: str, source_url: str, origin_domain: str) -> str | None:
    candidate = raw_url.strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    if lowered.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return None

    joined = urljoin(source_url, candidate)
    joined, _ = urldefrag(joined)
    parsed = urlparse(joined)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None

    host = normalize_host(parsed.netloc)
    if is_reg_ru_host(host):
        return None
    if host != origin_domain and not host.endswith(f".{origin_domain}"):
        return None
    if is_binary_path(parsed.path):
        return None

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{parsed.netloc}{path}{query}"


def link_priority(url: str, text: str) -> int:
    joined = f"{unquote(url)} {text}".lower().replace("ё", "е")
    score = 0
    for hint in CRAWL_PRIORITY_HINTS:
        if hint in joined:
            score += 1
    return score


def extract_markdown_text(markdown: Any) -> str:
    if markdown is None:
        return ""
    if isinstance(markdown, str):
        return markdown

    chunks: list[str] = []
    for attr in ("raw_markdown", "markdown_with_citations", "fit_markdown"):
        value = getattr(markdown, attr, None)
        if isinstance(value, str) and value.strip():
            chunks.append(value)
    return "\n".join(chunks)


def extract_title(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    title = soup.find("title")
    if title:
        return compact_spaces(title.get_text(" ", strip=True))
    return ""


def unique_lines(lines) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        normalized = compact_spaces(line)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def unique_values(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = compact_spaces(value)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def domain_slug(url: str) -> str:
    host = decode_idna_host(urlparse(url).hostname or "")
    return re.sub(r"[^\w.-]", "_", host, flags=re.UNICODE)


def build_browser_config() -> Any:
    if BrowserConfig is None:
        return None

    candidates = (
        {"headless": True, "verbose": False, "user_agent": USER_AGENT},
        {"headless": True, "verbose": False},
        {"headless": True},
        {},
    )
    for kwargs in candidates:
        try:
            return BrowserConfig(**kwargs)
        except TypeError:
            continue
    return BrowserConfig()


def build_run_config() -> Any:
    if CrawlerRunConfig is None:
        return None

    raw_timeout = os.getenv("CRAWLER_PAGE_TIMEOUT_SECONDS", str(DEFAULT_CRAWLER_PAGE_TIMEOUT_SECONDS))
    try:
        timeout_seconds = float(raw_timeout)
    except ValueError:
        timeout_seconds = float(DEFAULT_CRAWLER_PAGE_TIMEOUT_SECONDS)
    timeout_ms = max(1000, int(timeout_seconds * 1000))
    remove_overlays = os.getenv("CRAWLER_REMOVE_OVERLAY_ELEMENTS", "0").strip().lower() in {"1", "true", "yes"}

    candidates = []
    timed_kwargs_variants = (
        {"page_timeout": timeout_ms},
        {"timeout": timeout_ms},
        {},
    )
    common_kwargs = {"remove_overlay_elements": remove_overlays, "word_count_threshold": 1, "exclude_domains": ["reg.ru"]}

    if CacheMode is not None and hasattr(CacheMode, "BYPASS"):
        for timed_kwargs in timed_kwargs_variants:
            candidates.append({"cache_mode": CacheMode.BYPASS, **common_kwargs, **timed_kwargs})

    for timed_kwargs in timed_kwargs_variants:
        candidates.append({"bypass_cache": True, **common_kwargs, **timed_kwargs})

    candidates.extend(
        [
            {"bypass_cache": True},
            {"exclude_domains": ["reg.ru"]},
            {},
        ]
    )

    for kwargs in candidates:
        try:
            return CrawlerRunConfig(**kwargs)
        except TypeError:
            continue
    return CrawlerRunConfig()


def create_crawler(browser_config: Any) -> Any:
    if browser_config is None:
        return AsyncWebCrawler()

    candidates = (
        {"config": browser_config},
        {"browser_config": browser_config},
        {},
    )
    for kwargs in candidates:
        try:
            return AsyncWebCrawler(**kwargs)
        except TypeError:
            continue
    return AsyncWebCrawler()


async def crawl_page(crawler: Any, url: str, run_config: Any) -> Any:
    if run_config is not None:
        try:
            return await crawler.arun(url=url, config=run_config)
        except TypeError:
            pass
    return await crawler.arun(url=url)


def build_dispatcher(page_concurrency: int) -> Any:
    if page_concurrency <= 1 or SemaphoreDispatcher is None:
        return None

    candidates = (
        {"semaphore_count": page_concurrency, "max_session_permit": max(20, page_concurrency)},
        {"semaphore_count": page_concurrency},
        {},
    )
    for kwargs in candidates:
        try:
            return SemaphoreDispatcher(**kwargs)
        except TypeError:
            continue
    return None


async def collect_arun_many_results(results: Any) -> list[Any]:
    if results is None:
        return []
    if isinstance(results, list):
        return results
    if isinstance(results, AsyncIterable):
        collected: list[Any] = []
        async for item in results:
            collected.append(item)
        return collected
    try:
        return list(results)
    except TypeError:
        return [results]


async def crawl_pages_batch(crawler: Any, urls: list[str], run_config: Any, page_concurrency: int) -> list[tuple[str, Any]]:
    if not urls:
        return []

    run_many = getattr(crawler, "arun_many", None)
    dispatcher = build_dispatcher(page_concurrency)
    if run_many is None or len(urls) <= 1 or page_concurrency <= 1:
        return [(url, await crawl_page(crawler=crawler, url=url, run_config=run_config)) for url in urls]

    try:
        if run_config is not None and dispatcher is not None:
            raw_results = await run_many(urls=urls, config=run_config, dispatcher=dispatcher)
        elif run_config is not None:
            raw_results = await run_many(urls=urls, config=run_config)
        elif dispatcher is not None:
            raw_results = await run_many(urls=urls, dispatcher=dispatcher)
        else:
            raw_results = await run_many(urls=urls)
        results = await collect_arun_many_results(raw_results)
    except TypeError:
        results = [(url, await crawl_page(crawler=crawler, url=url, run_config=run_config)) for url in urls]
        return results

    paired: list[tuple[str, Any]] = []
    for index, result in enumerate(results):
        source_url = urls[index] if index < len(urls) else str(getattr(result, "url", "") or "")
        paired.append((source_url, result))
    if len(results) < len(urls):
        for url in urls[len(results):]:
            paired.append((url, await crawl_page(crawler=crawler, url=url, run_config=run_config)))
    return paired


async def crawl_site_pages(
    crawler: Any,
    start_url: str,
    max_pages: int,
    run_config: Any,
    verbose: bool,
    page_concurrency: int = 1,
) -> list[PageData]:
    normalized_start = normalize_start_url(start_url)
    root_host = normalize_host(urlparse(normalized_start).netloc)
    origin_domain = extract_origin_domain(root_host)
    queue: deque[str] = deque([normalized_start])
    enqueued: set[str] = {normalized_start}
    visited: set[str] = set()
    pages: list[PageData] = []

    while queue and len(visited) < max_pages:
        current_batch: list[str] = []
        while queue and len(current_batch) < max(1, page_concurrency) and len(visited) < max_pages:
            current_url = queue.popleft()
            enqueued.discard(current_url)
            if current_url in visited:
                continue
            visited.add(current_url)
            current_batch.append(current_url)

        if not current_batch:
            continue
        if verbose:
            for batch_url in current_batch:
                print(f"[crawl] {display_url(batch_url)}")

        crawl_results = await crawl_pages_batch(
            crawler=crawler,
            urls=current_batch,
            run_config=run_config,
            page_concurrency=page_concurrency,
        )
        for requested_url, result in crawl_results:
            if isinstance(result, Exception):
                if verbose:
                    print(f"[skip] {display_url(requested_url)} -> {result}")
                continue
            if not result.success:
                if verbose:
                    print(f"[skip] {display_url(requested_url)} -> {result.error_message}")
                continue

            effective_url = result.url or requested_url
            html = result.cleaned_html or result.html or ""
            markdown = extract_markdown_text(result.markdown)
            pages.append(PageData(url=effective_url, title=extract_title(html), html=html, markdown=markdown))

            internal_links = (result.links or {}).get("internal", [])
            prioritized: list[tuple[int, str]] = []
            regular: list[str] = []

            for link in internal_links:
                if isinstance(link, dict):
                    raw_href = str(link.get("href", "")).strip()
                    link_text = str(link.get("text", "")).strip()
                else:
                    raw_href = str(link).strip()
                    link_text = ""

                normalized = normalize_internal_url(raw_href, effective_url, origin_domain)
                if not normalized:
                    continue
                if normalized in visited or normalized in enqueued:
                    continue

                priority = link_priority(normalized, link_text)
                if priority > 0:
                    prioritized.append((priority, normalized))
                else:
                    regular.append(normalized)

            for _, url in sorted(prioritized, key=lambda item: item[0]):
                queue.appendleft(url)
                enqueued.add(url)
            for url in regular:
                queue.append(url)
                enqueued.add(url)

    return pages


def load_urls(raw_urls: list[str] | None = None, url_file: str | None = None) -> list[str]:
    urls = [value for value in (raw_urls or []) if value.strip()]
    if url_file:
        for line in Path(url_file).read_text(encoding="utf-8").splitlines():
            value = line.strip()
            if not value or value.startswith("#"):
                continue
            urls.append(value)

    unique: list[str] = []
    seen: set[str] = set()
    for url in urls:
        normalized = normalize_start_url(url)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique
