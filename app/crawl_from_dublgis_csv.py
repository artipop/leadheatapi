from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from pathlib import Path
from typing import Iterable
from typing import Optional
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import SplitResult, parse_qsl, urlencode, urldefrag, urljoin, urlsplit, urlunsplit
from urllib.request import Request
from urllib.request import urlopen

from bs4 import BeautifulSoup

from crawlin import build_browser_config
from crawlin import build_run_config
from crawlin import crawl_site_pages
from crawlin import create_crawler
from crawlin import domain_slug
from crawlin import extract_contacts
from crawlin import extract_prices
from crawlin import extract_specialists
from crawlin import write_contacts_csv
from crawlin import write_pricing_csv
from crawlin import write_specialists_csv
from scrape2gis import get_firm_sites

logger = logging.getLogger(__name__)
HH_UTM_PARAMS_TO_REMOVE = {"utm_campaign", "utm_content", "utm_medium", "utm_source"}
MULTI_PART_TLDS = {"co", "com", "org", "net", "gov", "edu"}
RESOLVE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
HH_LINKS_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "hh_employer_url", "source")
TELEGRAM_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "telegram_url", "source")
MAX_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "max_channel_url", "source")
DZEN_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "dzen_url", "source")
RUTUBE_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "link_url", "link_host", "source")
VK_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "link_url", "link_host", "source")
DRIVE2_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "link_url", "link_host", "source")
OTHER_LINKS_FIELDNAMES = ("firm_id", "company_name", "address_name", "site_url", "link_url", "link_host", "source")
BOOKING_FIELDNAMES = (
    "firm_id",
    "company_name",
    "address_name",
    "site_url",
    "booking_mode",
    "has_booking_form",
    "has_booking_widget",
    "booking_widget_provider",
    "booking_widget_host",
    "booking_evidence",
)
VIDEO_HOST_DOMAINS = {
    "rutube.ru",
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv",
    "video.mail.ru",
    "smotrim.ru",
}
SOCIAL_HOST_DOMAINS = {
    "vk.com",
    "vkontakte.ru",
    "ok.ru",
    "instagram.com",
    "facebook.com",
    "fb.com",
    "x.com",
    "twitter.com",
    "linkedin.com",
    "pinterest.com",
    "tiktok.com",
    "threads.net",
    "t.me",
    "telegram.me",
    "max.ru",
    "dzen.ru",
}
BOOKING_TEXT_HINTS = (
    "запис",
    "онлайн запис",
    "запись на прием",
    "запись к врачу",
    "appointment",
    "book appointment",
    "online booking",
    "schedule appointment",
)
BOOKING_URL_HINTS = (
    "booking",
    "appointment",
    "book",
    "schedule",
    "record",
    "zapis",
    "online-record",
    "online-zapis",
)
BOOKING_FORM_FIELD_HINTS = (
    "name",
    "phone",
    "email",
    "date",
    "time",
    "doctor",
    "service",
    "patient",
    "имя",
    "тел",
    "почт",
    "дата",
    "время",
    "врач",
    "услуг",
    "пациент",
)
BOOKING_PROVIDER_DOMAINS: dict[str, set[str]] = {
    "yclients": {"yclients.com", "alteg.io", "altegio.com", "altegio.ru"},
    "dikidi": {"dikidi.net", "dikidi.ru"},
    "medflex": {"medflex.ru"},
    "docdoc": {"docdoc.ru", "sberhealth.ru"},
    "napopravku": {"napopravku.ru"},
    "prodoctorov": {"prodoctorov.ru"},
    "bitrix24_forms": {"bitrix24.ru", "bitrix.info"},
    "amocrm_forms": {"amocrm.ru", "amoforms.com"},
    "infoclinica": {"infoclinica.ru"},
}
BOOKING_SOURCE_ATTRS = (
    "href",
    "src",
    "action",
    "data-url",
    "data-href",
    "data-src",
    "data-widget-url",
    "data-form-url",
    "data-booking-url",
)


def normalize_site_url(url: str) -> Optional[str]:
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_host_for_url(parsed.hostname or "")
    if not host:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    scheme = "https" if parsed.scheme in {"http", "https"} and port in {None, 80, 443} else parsed.scheme
    return urlunsplit((scheme, netloc, path, "", ""))


def normalize_site_origin_url(url: str) -> Optional[str]:
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return None
    host = normalize_host_for_url(parsed.hostname or "")
    if not host:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    scheme = "https" if parsed.scheme in {"http", "https"} and port in {None, 80, 443} else parsed.scheme
    return urlunsplit((scheme, netloc, "/", "", ""))


def split_http_like_url(url: str) -> SplitResult:
    value = url.strip()
    if not value:
        return urlsplit("")
    if "://" not in value:
        value = f"https://{value}"
    return urlsplit(value)


def normalize_host_for_url(host: str) -> str:
    value = host.strip().lower()
    if value.startswith("www."):
        value = value[4:]
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def decode_host_from_idna(host: str) -> str:
    value = host.strip().lower()
    if not value:
        return ""
    try:
        decoded = value.encode("ascii").decode("idna")
    except UnicodeError:
        decoded = value
    return decoded


def to_display_url(url: str) -> str:
    parsed = split_http_like_url(url)
    host = decode_host_from_idna(parsed.hostname or "")
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
    scheme = parsed.scheme or "https"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def is_hh_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value == "hh.ru" or value.endswith(".hh.ru")


def is_hh_url(url: str) -> bool:
    return is_hh_host(split_http_like_url(url).hostname or "")


def is_telegram_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value in {"t.me", "telegram.me"}


def is_jivo_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value == "jivo.chat" or value.endswith(".jivo.chat")


def is_jivo_url(url: str) -> bool:
    return is_jivo_host(split_http_like_url(url).hostname or "")


def is_clck_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value == "clck.ru" or value.endswith(".clck.ru")


def is_clck_url(url: str) -> bool:
    return is_clck_host(split_http_like_url(url).hostname or "")


def is_max_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value == "max.ru" or value.endswith(".max.ru")


def is_max_url(url: str) -> bool:
    return is_max_host(split_http_like_url(url).hostname or "")


def is_dzen_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return (
        value == "dzen.ru"
        or value.endswith(".dzen.ru")
    )


def is_dzen_url(url: str) -> bool:
    return is_dzen_host(split_http_like_url(url).hostname or "")


def is_drive2_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value == "drive2.ru" or value.endswith(".drive2.ru")


def is_drive2_url(url: str) -> bool:
    return is_drive2_host(split_http_like_url(url).hostname or "")


def host_matches_domain_list(host: str, domains: set[str]) -> bool:
    normalized = normalize_host_for_url(host)
    if not normalized:
        return False
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


def classify_platform_url(url: str) -> Optional[str]:
    host = split_http_like_url(url).hostname or ""
    if host_matches_domain_list(host, VIDEO_HOST_DOMAINS):
        return "video_hosting"
    if is_drive2_host(host):
        return "drive2"
    if host_matches_domain_list(host, SOCIAL_HOST_DOMAINS):
        return "social_network"
    return None


def normalize_http_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url.strip())
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None

    host = normalize_host_for_url(parsed.hostname or "")
    if not host:
        return None

    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = host
    if port and not ((parsed.scheme == "http" and port == 80) or (parsed.scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"

    path = parsed.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunsplit((parsed.scheme.lower(), netloc, path, parsed.query, ""))


def resolve_short_url(url: str, timeout_seconds: int = 15) -> Optional[str]:
    value = url.strip()
    if not value:
        return None
    if "://" not in value:
        value = f"https://{value}"

    request = Request(value, headers={"User-Agent": RESOLVE_USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):  # OSErr -> ConnResetErr
        return None

    normalized = normalize_http_url(final_url)
    if not normalized:
        return None
    if is_clck_url(normalized):
        return None
    return normalized


def resolve_site_redirect(url: str, timeout_seconds: int = 15) -> Optional[str]:
    normalized_input = normalize_site_origin_url(url)
    if not normalized_input:
        return None

    request = Request(normalized_input, headers={"User-Agent": RESOLVE_USER_AGENT})
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            final_url = response.geturl()
    except (HTTPError, URLError, TimeoutError, ValueError, OSError):  # OSErr -> ConnResetErr
        return normalized_input

    normalized_final = normalize_site_origin_url(final_url)
    return normalized_final or normalized_input


def extract_origin_domain(host: str) -> str:
    normalized = normalize_host_for_url(host)
    parts = [part for part in normalized.split(".") if part]
    if len(parts) <= 2:
        return normalized
    if len(parts[-1]) == 2 and parts[-2] in MULTI_PART_TLDS and len(parts) >= 3:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def is_internal_for_site(url: str, site_url: str) -> bool:
    parsed = urlsplit(url)
    host = normalize_host_for_url(parsed.hostname or "")
    if not host:
        return False

    site_host = normalize_host_for_url(urlsplit(site_url).hostname or "")
    if not site_host:
        return False

    origin_domain = extract_origin_domain(site_host)
    return host == origin_domain or host.endswith(f".{origin_domain}")


def origin_domain_from_url(url: str) -> str:
    host = normalize_host_for_url(urlsplit(url).hostname or "")
    return extract_origin_domain(host) if host else ""


def normalize_hh_employer_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = normalize_host_for_url(parsed.hostname or "")
    if not is_hh_host(host):
        return None

    path = (parsed.path or "").rstrip("/")
    if not path.startswith("/employer"):
        return None

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in HH_UTM_PARAMS_TO_REMOVE
    ]
    query = urlencode(query_pairs, doseq=True)
    return urlunsplit(("https", host, path, query, ""))


def normalize_telegram_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = normalize_host_for_url(parsed.hostname or "")
    if not is_telegram_host(host):
        return None
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    return urlunsplit(("https", host, path, parsed.query, ""))


def normalize_max_channel_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = normalize_host_for_url(parsed.hostname or "")
    if not is_max_host(host):
        return None

    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    parts = [part for part in path.split("/") if part]
    if not parts:
        return None
    first = parts[0].lower()
    if first not in {"channel", "channels", "c"} and not first.startswith("@"):
        return None

    return urlunsplit(("https", host, path, parsed.query, ""))


def normalize_dzen_url(url: str) -> Optional[str]:
    resolved, _ = urldefrag(url)
    parsed = split_http_like_url(resolved)
    host = normalize_host_for_url(parsed.hostname or "")
    if not is_dzen_host(host):
        return None
    path = (parsed.path or "").rstrip("/")
    if not path:
        return None
    return urlunsplit(("https", host, path, parsed.query, ""))


def has_invalid_http_port(url: str) -> bool:
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    try:
        _ = parsed.port
    except ValueError:
        return True
    return False


def compact_match_text(text: str) -> str:
    return " ".join((text or "").lower().replace("ё", "е").split())


def contains_any_fragment(text: str, fragments: tuple[str, ...]) -> bool:
    compact = compact_match_text(text)
    return any(fragment in compact for fragment in fragments)


def url_has_booking_hint(url: str) -> bool:
    normalized = normalize_http_url(url)
    if not normalized:
        return False
    parsed = urlsplit(normalized)
    normalized_parts = compact_match_text(f"{parsed.netloc}{parsed.path}?{parsed.query}")
    return any(hint in normalized_parts for hint in BOOKING_URL_HINTS)


def booking_provider_by_host(host: str) -> Optional[str]:
    normalized = normalize_host_for_url(host)
    if not normalized:
        return None
    for provider, domains in BOOKING_PROVIDER_DOMAINS.items():
        if host_matches_domain_list(normalized, domains):
            return provider
    return None


def extract_http_urls_from_tag(tag, page_url: str) -> set[str]:
    urls: set[str] = set()
    for attr_name in BOOKING_SOURCE_ATTRS:
        raw_value = str(tag.get(attr_name, "")).strip()
        if not raw_value:
            continue
        resolved, _ = urldefrag(urljoin(page_url, raw_value))
        normalized = normalize_http_url(resolved)
        if normalized:
            urls.add(normalized)
    return urls


def detect_booking_features(pages, site_url: str) -> dict[str, str]:
    has_booking_form = False
    has_booking_widget = False
    provider_names: set[str] = set()
    provider_hosts: set[str] = set()
    evidence: list[str] = []

    def add_evidence(kind: str, page_url: str, details: str) -> None:
        page = to_display_url(page_url)
        compact_details = compact_match_text(details)[:160]
        line = f"{kind}:{page} ({compact_details})" if compact_details else f"{kind}:{page}"
        if line not in evidence:
            evidence.append(line)

    for page in pages:
        if not page.html:
            continue
        soup = BeautifulSoup(page.html, "html.parser")

        for form in soup.find_all("form"):
            class_attr = form.get("class", [])
            class_value = " ".join(class_attr) if isinstance(class_attr, list) else str(class_attr)
            form_text = " ".join(
                [
                    str(form.get("id", "")),
                    class_value,
                    str(form.get("name", "")),
                    str(form.get("action", "")),
                    form.get_text(" ", strip=True)[:1000],
                ]
            )
            controls_text = " ".join(
                " ".join(
                    [
                        str(control.get("name", "")),
                        str(control.get("id", "")),
                        str(control.get("placeholder", "")),
                        str(control.get("aria-label", "")),
                    ]
                )
                for control in form.find_all(["input", "textarea", "select", "button"])
            )
            searchable = compact_match_text(f"{form_text} {controls_text}")
            has_form_hints = contains_any_fragment(searchable, BOOKING_TEXT_HINTS)
            has_form_fields = contains_any_fragment(searchable, BOOKING_FORM_FIELD_HINTS)

            action_raw = str(form.get("action", "")).strip()
            action_url = normalize_http_url(urljoin(page.url, action_raw)) if action_raw else None
            action_has_booking_hint = bool(action_url and url_has_booking_hint(action_url))
            if not ((has_form_hints and has_form_fields) or action_has_booking_hint):
                continue

            has_booking_form = True
            if action_url and not is_internal_for_site(action_url, site_url):
                action_host = normalize_host_for_url(urlsplit(action_url).hostname or "")
                if action_host:
                    has_booking_widget = True
                    provider_hosts.add(action_host)
                    provider = booking_provider_by_host(action_host)
                    provider_names.add(provider or f"external:{decode_host_from_idna(action_host)}")
                    add_evidence("form_action", page.url, decode_host_from_idna(action_host))
                    continue
            add_evidence("form", page.url, "booking form detected")

        for tag in soup.find_all(["a", "iframe", "script"]):
            class_attr = tag.get("class", [])
            class_value = " ".join(class_attr) if isinstance(class_attr, list) else str(class_attr)
            tag_text = compact_match_text(
                " ".join(
                    [
                        str(tag.get("id", "")),
                        class_value,
                        str(tag.get("title", "")),
                        str(tag.get("aria-label", "")),
                        tag.get_text(" ", strip=True)[:400],
                    ]
                )
            )

            for link_url in extract_http_urls_from_tag(tag, page.url):
                host = normalize_host_for_url(urlsplit(link_url).hostname or "")
                if not host:
                    continue
                is_external = not is_internal_for_site(link_url, site_url)
                has_booking_hint = url_has_booking_hint(link_url) or contains_any_fragment(
                    tag_text, BOOKING_TEXT_HINTS
                )
                provider = booking_provider_by_host(host)
                if provider and is_external:
                    has_booking_widget = True
                    provider_names.add(provider)
                    provider_hosts.add(host)
                    add_evidence("widget", page.url, decode_host_from_idna(host))
                    continue
                if is_external and has_booking_hint:
                    has_booking_widget = True
                    provider_hosts.add(host)
                    provider_names.add(f"external:{decode_host_from_idna(host)}")
                    add_evidence("widget_link", page.url, decode_host_from_idna(host))
                    continue
                if tag.name == "a" and not is_external and has_booking_hint:
                    has_booking_form = True
                    add_evidence("booking_link", page.url, to_display_url(link_url))

    has_booking = has_booking_form or has_booking_widget
    if has_booking_form and has_booking_widget:
        booking_mode = "form+widget"
    elif has_booking_form:
        booking_mode = "form"
    elif has_booking_widget:
        booking_mode = "widget"
    else:
        booking_mode = "none"

    return {
        "booking_mode": booking_mode,
        "has_booking_form": "1" if has_booking_form else "0",
        "has_booking_widget": "1" if has_booking_widget else "0",
        "booking_widget_provider": "; ".join(sorted(provider_names)),
        "booking_widget_host": "; ".join(sorted(decode_host_from_idna(host) for host in provider_hosts)),
        "booking_evidence": " | ".join(evidence[:6]),
    }


def extract_page_links(pages) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    seen: set[str] = set()

    for page in pages:
        soup = BeautifulSoup(page.html or "", "html.parser")
        for tag in soup.find_all("a", href=True):
            href = str(tag.get("href", "")).strip()
            if not href:
                continue
            resolved, _ = urldefrag(urljoin(page.url, href))
            if resolved in seen:
                continue
            seen.add(resolved)
            found.append((page.url, resolved))
    return found


def append_csv_rows(output_csv: Path, fieldnames: tuple[str, ...], rows: Iterable[dict[str, str]]) -> int:
    prepared_rows = list(rows)
    if not prepared_rows:
        return 0

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    write_header = not output_csv.exists() or output_csv.stat().st_size == 0

    with output_csv.open("a", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(fieldnames))
        if write_header:
            writer.writeheader()
        for row in prepared_rows:
            writer.writerow({name: row.get(name, "") for name in fieldnames})
    return len(prepared_rows)


def load_seen_pairs(csv_path: Path, left_field: str, right_field: str) -> set[tuple[str, str]]:
    seen: set[tuple[str, str]] = set()
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return seen

    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row in reader:
            left = (row.get(left_field) or "").strip()
            right = (row.get(right_field) or "").strip()
            if not left or not right:
                continue
            seen.add((left, right))
    return seen


def read_firms(
    csv_path: Path,
    limit: Optional[int],
    start_row: int,
    end_row: Optional[int],
) -> list[dict[str, str]]:
    firms: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        for row_index, row in enumerate(reader, start=1):
            if row_index < start_row:
                continue
            if end_row is not None and row_index > end_row:
                break

            firm_id = (row.get("id") or "").strip()
            if not firm_id:
                continue
            firms.append(
                {
                    "id": firm_id,
                    "name": (row.get("name") or "").strip(),
                    "address_name": (row.get("address_name") or "").strip(),
                }
            )
            if limit is not None and len(firms) >= limit:
                break
    return firms


async def run_streaming(
    firms: list[dict[str, str]],
    city_code: str,
    max_sites_per_firm: int,
    max_pages: int,
    output_dir: Path,
    hh_links_csv: Path,
    telegram_csv: Path,
    max_channels_csv: Path,
    dzen_csv: Path,
    rutube_csv: Path,
    vk_csv: Path,
    drive2_csv: Path,
    other_links_csv: Path,
    booking_csv: Path,
    verbose: bool,
    site_concurrency: int,
    page_concurrency: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    browser_config = build_browser_config()
    run_config = build_run_config()

    failed_attempts: list[str] = []
    seen_hh = load_seen_pairs(hh_links_csv, "firm_id", "hh_employer_url")
    seen_telegram = load_seen_pairs(telegram_csv, "firm_id", "telegram_url")
    seen_max = load_seen_pairs(max_channels_csv, "firm_id", "max_channel_url")
    seen_dzen = load_seen_pairs(dzen_csv, "firm_id", "dzen_url")
    seen_rutube = load_seen_pairs(rutube_csv, "firm_id", "link_url")
    seen_vk = load_seen_pairs(vk_csv, "firm_id", "link_url")
    seen_drive2 = load_seen_pairs(drive2_csv, "firm_id", "link_url")
    seen_other = load_seen_pairs(other_links_csv, "firm_id", "link_url")
    seen_booking = load_seen_pairs(booking_csv, "firm_id", "site_url")
    seen_crawled_origins: set[str] = set()
    short_url_cache: dict[str, Optional[str]] = {}
    site_redirect_cache: dict[str, Optional[str]] = {}
    appended_hh_rows = 0
    appended_telegram_rows = 0
    appended_max_rows = 0
    appended_dzen_rows = 0
    appended_rutube_rows = 0
    appended_vk_rows = 0
    appended_drive2_rows = 0
    appended_other_rows = 0
    appended_booking_rows = 0

    async def resolve_short_url_cached(raw_url: str) -> Optional[str]:
        key = raw_url.strip()
        cached = short_url_cache.get(key)
        if cached is not None or key in short_url_cache:
            return cached
        resolved = await asyncio.to_thread(resolve_short_url, key)
        short_url_cache[key] = resolved
        return resolved

    async def resolve_site_redirect_cached(raw_url: str) -> Optional[str]:
        normalized = normalize_site_origin_url(raw_url)
        if not normalized:
            return None
        cached = site_redirect_cache.get(normalized)
        if cached is not None or normalized in site_redirect_cache:
            return cached
        resolved = await asyncio.to_thread(resolve_site_redirect, normalized)
        site_redirect_cache[normalized] = resolved
        return resolved

    def append_hh_row(row: dict[str, str]) -> bool:
        nonlocal appended_hh_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("hh_employer_url") or "").strip())
        if not key[0] or not key[1] or key in seen_hh:
            return False
        seen_hh.add(key)
        appended_hh_rows += append_csv_rows(hh_links_csv, HH_LINKS_FIELDNAMES, [row])
        return True

    def append_telegram_row(row: dict[str, str]) -> bool:
        nonlocal appended_telegram_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("telegram_url") or "").strip())
        if not key[0] or not key[1] or key in seen_telegram:
            return False
        seen_telegram.add(key)
        appended_telegram_rows += append_csv_rows(telegram_csv, TELEGRAM_FIELDNAMES, [row])
        return True

    def append_max_row(row: dict[str, str]) -> bool:
        nonlocal appended_max_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("max_channel_url") or "").strip())
        if not key[0] or not key[1] or key in seen_max:
            return False
        seen_max.add(key)
        appended_max_rows += append_csv_rows(max_channels_csv, MAX_FIELDNAMES, [row])
        return True

    def append_dzen_row(row: dict[str, str]) -> bool:
        nonlocal appended_dzen_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("dzen_url") or "").strip())
        if not key[0] or not key[1] or key in seen_dzen:
            return False
        seen_dzen.add(key)
        appended_dzen_rows += append_csv_rows(dzen_csv, DZEN_FIELDNAMES, [row])
        return True

    def append_other_row(row: dict[str, str]) -> bool:
        nonlocal appended_other_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("link_url") or "").strip())
        if not key[0] or not key[1] or key in seen_other:
            return False
        seen_other.add(key)
        appended_other_rows += append_csv_rows(other_links_csv, OTHER_LINKS_FIELDNAMES, [row])
        return True

    def append_rutube_row(row: dict[str, str]) -> bool:
        nonlocal appended_rutube_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("link_url") or "").strip())
        if not key[0] or not key[1] or key in seen_rutube:
            return False
        seen_rutube.add(key)
        appended_rutube_rows += append_csv_rows(rutube_csv, RUTUBE_FIELDNAMES, [row])
        return True

    def append_vk_row(row: dict[str, str]) -> bool:
        nonlocal appended_vk_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("link_url") or "").strip())
        if not key[0] or not key[1] or key in seen_vk:
            return False
        seen_vk.add(key)
        appended_vk_rows += append_csv_rows(vk_csv, VK_FIELDNAMES, [row])
        return True

    def append_drive2_row(row: dict[str, str]) -> bool:
        nonlocal appended_drive2_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("link_url") or "").strip())
        if not key[0] or not key[1] or key in seen_drive2:
            return False
        seen_drive2.add(key)
        appended_drive2_rows += append_csv_rows(drive2_csv, DRIVE2_FIELDNAMES, [row])
        return True

    def append_booking_row(row: dict[str, str]) -> bool:
        nonlocal appended_booking_rows
        key = ((row.get("firm_id") or "").strip(), (row.get("site_url") or "").strip())
        if not key[0] or not key[1] or key in seen_booking:
            return False
        seen_booking.add(key)
        appended_booking_rows += append_csv_rows(booking_csv, BOOKING_FIELDNAMES, [row])
        return True

    def append_platform_link(
        *,
        firm_id_value: str,
        company_name_value: str,
        address_name_value: str,
        site_url_value: str,
        link_url_value: str,
        source_value: str,
        platform_kind: str,
    ) -> bool:
        normalized_link = normalize_http_url(link_url_value)
        if not normalized_link:
            return False
        host = normalize_host_for_url(urlsplit(normalized_link).hostname or "")
        base_row = {
            "firm_id": firm_id_value,
            "company_name": company_name_value,
            "address_name": address_name_value,
            "site_url": site_url_value,
            "link_url": normalized_link,
            "link_host": host,
            "source": f"{source_value}_{platform_kind}",
        }
        if platform_kind == "video_hosting":
            inserted = append_rutube_row(base_row)
        elif platform_kind == "drive2":
            inserted = append_drive2_row(base_row)
        else:
            inserted = append_vk_row(base_row)
        if platform_kind != "drive2":
            append_other_row(base_row)
        return inserted

    async with create_crawler(browser_config) as crawler:
        for index, firm in enumerate(firms, start=1):
            firm_id = firm["id"]
            company_name = firm["name"]
            address_name = firm["address_name"]

            logger.info("Firm %s/%s: id=%s name=%s", index, len(firms), firm_id, company_name or "-")

            try:
                sites = await asyncio.to_thread(get_firm_sites, city_code, firm_id)
            except Exception as exc:
                logger.exception("Failed to resolve sites for firm_id=%s: %s", firm_id, exc)
                failed_attempts.append(f"https://2gis.ru/{city_code}/firm/{firm_id}")
                continue

            if not sites:
                logger.info("No sites found for firm_id=%s", firm_id)
                failed_attempts.append(f"https://2gis.ru/{city_code}/firm/{firm_id}")
                continue

            normalized_sites: list[str] = []
            seen_in_firm: set[str] = set()
            for raw_site in sites:
                processed_site = raw_site
                if is_clck_url(raw_site):
                    resolved = await resolve_short_url_cached(raw_site)
                    if not resolved:
                        logger.info(
                            "Skip unresolved clck.ru link from 2GIS card for firm_id=%s: %s",
                            firm_id,
                            to_display_url(raw_site),
                        )
                        continue
                    logger.info(
                        "Resolved clck.ru from 2GIS card: %s -> %s",
                        to_display_url(raw_site),
                        to_display_url(resolved),
                    )
                    processed_site = resolved

                if is_jivo_url(processed_site):
                    logger.info(
                        "Skip jivo.chat link from 2GIS card for firm_id=%s: %s",
                        firm_id,
                        to_display_url(processed_site),
                    )
                    continue

                platform_kind = classify_platform_url(processed_site)
                if platform_kind is not None:
                    append_platform_link(
                        firm_id_value=firm_id,
                        company_name_value=company_name,
                        address_name_value=address_name,
                        site_url_value="",
                        link_url_value=processed_site,
                        source_value="2gis_card",
                        platform_kind=platform_kind,
                    )
                    continue

                if is_max_url(processed_site):
                    max_url = normalize_max_channel_url(processed_site)
                    if max_url:
                        append_max_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": "",
                                "max_channel_url": max_url,
                                "source": "2gis_card",
                            }
                        )
                    else:
                        logger.info(
                            "Skip max.ru link from 2GIS card for firm_id=%s: %s",
                            firm_id,
                            to_display_url(processed_site),
                        )
                    continue

                if is_hh_url(processed_site):
                    hh_url = normalize_hh_employer_url(processed_site)
                    if hh_url:
                        append_hh_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": "",
                                "hh_employer_url": hh_url,
                                "source": "2gis_card",
                            }
                        )
                    continue

                telegram_url = normalize_telegram_url(processed_site)
                if telegram_url:
                    append_telegram_row(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": "",
                            "telegram_url": telegram_url,
                            "source": "2gis_card",
                        }
                    )
                    continue

                dzen_url = normalize_dzen_url(processed_site)
                if dzen_url:
                    append_dzen_row(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": "",
                            "dzen_url": dzen_url,
                            "source": "2gis_card",
                        }
                    )
                    continue

                normalized = normalize_site_origin_url(processed_site)
                if not normalized:
                    continue
                resolved_site = await resolve_site_redirect_cached(normalized) or normalized
                if resolved_site != normalized:
                    logger.info(
                        "Merged site by redirect for firm_id=%s: %s -> %s",
                        firm_id,
                        to_display_url(normalized),
                        to_display_url(resolved_site),
                    )

                platform_kind = classify_platform_url(resolved_site)
                if platform_kind is not None:
                    append_platform_link(
                        firm_id_value=firm_id,
                        company_name_value=company_name,
                        address_name_value=address_name,
                        site_url_value="",
                        link_url_value=resolved_site,
                        source_value="2gis_card_redirect",
                        platform_kind=platform_kind,
                    )
                    continue

                if is_max_url(resolved_site):
                    max_url = normalize_max_channel_url(resolved_site)
                    if max_url:
                        append_max_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": "",
                                "max_channel_url": max_url,
                                "source": "2gis_card_redirect",
                            }
                        )
                    continue
                if is_hh_url(resolved_site):
                    hh_url = normalize_hh_employer_url(resolved_site)
                    if hh_url:
                        append_hh_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": "",
                                "hh_employer_url": hh_url,
                                "source": "2gis_card_redirect",
                            }
                        )
                    continue
                telegram_url = normalize_telegram_url(resolved_site)
                if telegram_url:
                    append_telegram_row(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": "",
                            "telegram_url": telegram_url,
                            "source": "2gis_card_redirect",
                        }
                    )
                    continue
                dzen_url = normalize_dzen_url(resolved_site)
                if dzen_url:
                    append_dzen_row(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": "",
                            "dzen_url": dzen_url,
                            "source": "2gis_card_redirect",
                        }
                    )
                    continue

                if resolved_site in seen_in_firm:
                    continue
                seen_in_firm.add(resolved_site)
                normalized_sites.append(resolved_site)

            selected_sites = normalized_sites[: max(1, max_sites_per_firm)]
            logger.info(
                "Resolved %s raw site(s), selected for crawl: %s",
                len(sites),
                [to_display_url(site) for site in selected_sites],
            )

            site_semaphore = asyncio.Semaphore(max(1, site_concurrency))

            async def process_site_url(site_url: str) -> None:
                if is_hh_url(site_url):
                    return
                platform_kind = classify_platform_url(site_url)
                if platform_kind is not None:
                    logger.info(
                        "Skip %s site crawl for firm_id=%s: %s",
                        platform_kind,
                        firm_id,
                        to_display_url(site_url),
                    )
                    return
                if is_max_url(site_url):
                    logger.info("Skip max.ru site crawl for firm_id=%s: %s", firm_id, to_display_url(site_url))
                    return
                if is_dzen_url(site_url):
                    logger.info("Skip dzen site crawl for firm_id=%s: %s", firm_id, to_display_url(site_url))
                    return

                origin_domain = origin_domain_from_url(site_url)
                if origin_domain and origin_domain in seen_crawled_origins:
                    logger.info(
                        "Skip duplicate origin domain crawl for firm_id=%s: %s (origin=%s)",
                        firm_id,
                        to_display_url(site_url),
                        decode_host_from_idna(origin_domain),
                    )
                    return
                if origin_domain:
                    seen_crawled_origins.add(origin_domain)

                async with site_semaphore:
                    try:
                        pages = await crawl_site_pages(
                            crawler=crawler,
                            start_url=site_url,
                            max_pages=max_pages,
                            run_config=run_config,
                            verbose=verbose,
                            page_concurrency=max(1, page_concurrency),
                        )
                    except Exception as exc:
                        logger.exception("Crawler failed for site=%s: %s", to_display_url(site_url), exc)
                        failed_attempts.append(to_display_url(site_url))
                        return

                if not pages:
                    logger.warning("No pages collected for site=%s", to_display_url(site_url))
                    failed_attempts.append(to_display_url(site_url))
                    return

                domain = domain_slug(site_url)
                site_dir = output_dir / domain
                site_dir.mkdir(parents=True, exist_ok=True)

                prices = extract_prices(pages, domain)
                specialists = extract_specialists(pages, domain)
                contacts = extract_contacts(pages, domain)
                write_pricing_csv(site_dir / "pricing.csv", prices)
                write_specialists_csv(site_dir / "specialists.csv", specialists)
                write_contacts_csv(site_dir / "contacts.csv", contacts)
                booking_features = detect_booking_features(pages, site_url)
                if booking_features["booking_mode"] != "none":
                    append_booking_row(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": site_url,
                            **booking_features,
                        }
                    )

                extracted_hh_count = 0
                extracted_telegram_count = 0
                extracted_max_count = 0
                extracted_dzen_count = 0
                extracted_other_count = 0
                for source_page_url, link in extract_page_links(pages):
                    processed_link = link
                    if is_clck_url(link):
                        resolved = await resolve_short_url_cached(link)
                        if not resolved:
                            logger.info("Skip unresolved clck.ru link from site crawl: %s", to_display_url(link))
                            continue
                        logger.info(
                            "Resolved clck.ru from site crawl: %s -> %s",
                            to_display_url(link),
                            to_display_url(resolved),
                        )
                        processed_link = resolved

                    if is_jivo_url(processed_link):
                        logger.info("Skip jivo.chat link from site crawl: %s", to_display_url(processed_link))
                        continue

                    if has_invalid_http_port(processed_link):
                        logger.warning(
                            "Skip malformed link with invalid port: firm_id=%s site=%s source_page=%s link=%s raw_link=%s",
                            firm_id,
                            to_display_url(site_url),
                            to_display_url(source_page_url),
                            to_display_url(processed_link),
                            processed_link,
                        )
                        continue

                    platform_kind = classify_platform_url(processed_link)
                    if platform_kind is not None:
                        append_platform_link(
                            firm_id_value=firm_id,
                            company_name_value=company_name,
                            address_name_value=address_name,
                            site_url_value=site_url,
                            link_url_value=processed_link,
                            source_value="site_crawl",
                            platform_kind=platform_kind,
                        )
                        continue

                    hh_url = normalize_hh_employer_url(processed_link)
                    if hh_url:
                        if append_hh_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": site_url,
                                "hh_employer_url": hh_url,
                                "source": "site_crawl",
                            }
                        ):
                            extracted_hh_count += 1
                        continue

                    max_url = normalize_max_channel_url(processed_link)
                    if max_url:
                        if append_max_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": site_url,
                                "max_channel_url": max_url,
                                "source": "site_crawl",
                            }
                        ):
                            extracted_max_count += 1
                        continue

                    telegram_url = normalize_telegram_url(processed_link)
                    if telegram_url:
                        if append_telegram_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": site_url,
                                "telegram_url": telegram_url,
                                "source": "site_crawl",
                            }
                        ):
                            extracted_telegram_count += 1
                        continue

                    dzen_url = normalize_dzen_url(processed_link)
                    if dzen_url:
                        if append_dzen_row(
                            {
                                "firm_id": firm_id,
                                "company_name": company_name,
                                "address_name": address_name,
                                "site_url": site_url,
                                "dzen_url": dzen_url,
                                "source": "site_crawl",
                            }
                        ):
                            extracted_dzen_count += 1
                        continue

                    normalized_link = normalize_http_url(processed_link)
                    if not normalized_link:
                        continue
                    if is_internal_for_site(normalized_link, site_url):
                        continue

                    if append_other_row(
                        {
                            "firm_id": firm_id,
                            "company_name": company_name,
                            "address_name": address_name,
                            "site_url": site_url,
                            "link_url": normalized_link,
                            "link_host": normalize_host_for_url(urlsplit(normalized_link).hostname or ""),
                            "source": "site_crawl",
                        }
                    ):
                        extracted_other_count += 1

                logger.info(
                    "SUCCESS: %s | pages=%s prices=%s specialists=%s contacts=%s booking_mode=%s has_form=%s has_widget=%s providers=%s hh_employer_links=%s telegram_links=%s max_channels=%s dzen_links=%s other_links=%s -> %s",
                    to_display_url(site_url),
                    len(pages),
                    len(prices),
                    len(specialists),
                    len(contacts),
                    booking_features["booking_mode"],
                    booking_features["has_booking_form"],
                    booking_features["has_booking_widget"],
                    booking_features["booking_widget_provider"] or "-",
                    extracted_hh_count,
                    extracted_telegram_count,
                    extracted_max_count,
                    extracted_dzen_count,
                    extracted_other_count,
                    site_dir,
                )

            tasks = [asyncio.create_task(process_site_url(site_url)) for site_url in selected_sites]
            for task in tasks:
                await task
    logger.info(
        "Appended rows this run -> hh:%s telegram:%s max:%s dzen:%s vk:%s rutube:%s drive2:%s other:%s booking:%s",
        appended_hh_rows,
        appended_telegram_rows,
        appended_max_rows,
        appended_dzen_rows,
        appended_vk_rows,
        appended_rutube_rows,
        appended_drive2_rows,
        appended_other_rows,
        appended_booking_rows,
    )
    logger.info("HH links CSV: %s", hh_links_csv)
    logger.info("Telegram links CSV: %s", telegram_csv)
    logger.info("Max channel links CSV: %s", max_channels_csv)
    logger.info("Dzen links CSV: %s", dzen_csv)
    logger.info("VK/social links CSV: %s", vk_csv)
    logger.info("Rutube/video links CSV: %s", rutube_csv)
    logger.info("Drive2 links CSV: %s", drive2_csv)
    logger.info("Other links CSV: %s", other_links_csv)
    logger.info("Booking features CSV: %s", booking_csv)

    if failed_attempts:
        print("\nFailed attempts:")
        for value in failed_attempts:
            print(value)
    else:
        print("\nFailed attempts: none")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stream crawl sites from dublgis_items.csv in order.")
    parser.add_argument("--csv", default="dublgis_items.csv", help="Path to 2GIS CSV file.")
    parser.add_argument("--limit", type=int, help="How many first rows to process (default: all).")
    parser.add_argument("--start-row", type=int, default=1, help="Start row number (1-based, excluding header).")
    parser.add_argument("--end-row", type=int, help="End row number (1-based, inclusive, excluding header).")
    parser.add_argument("--city-code", default="moscow", help="2GIS city code for firm card URL.")
    parser.add_argument("--max-sites-per-firm", type=int, default=3, help="Max websites to crawl per firm.")
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages per website.")
    parser.add_argument("--site-concurrency", type=int, default=1, help="How many sites to crawl in parallel.")
    parser.add_argument("--page-concurrency", type=int, default=1, help="How many pages to fetch in parallel.")
    parser.add_argument("--output-dir", default="output", help="Directory for per-site CSV output.")
    parser.add_argument(
        "--hh-links-csv",
        default="output/hh_links_review.csv",
        help="CSV file for found hh.ru/employer links.",
    )
    parser.add_argument(
        "--telegram-csv",
        default="output/telegram.csv",
        help="CSV file for found t.me links.",
    )
    parser.add_argument(
        "--max-channels-csv",
        default="output/max_channels.csv",
        help="CSV file for found max channel links.",
    )
    parser.add_argument(
        "--dzen-csv",
        default="output/dzen.csv",
        help="CSV file for found dzen links.",
    )
    parser.add_argument(
        "--rutube-csv",
        default="output/rutube.csv",
        help="CSV file for video hosting links (rutube and similar).",
    )
    parser.add_argument(
        "--vk-csv",
        default="output/vk.csv",
        help="CSV file for social network links (vk and similar).",
    )
    parser.add_argument(
        "--drive2-csv",
        default="output/drive2.csv",
        help="CSV file for found drive2.ru links (not crawled).",
    )
    parser.add_argument(
        "--other-links-csv",
        default="output/other_links.csv",
        help="CSV file for other external links found on crawled sites.",
    )
    parser.add_argument(
        "--booking-csv",
        default="output/booking_features.csv",
        help="CSV file with booking form/widget detection per crawled site.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose crawling progress.")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_parser().parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    start_row = max(1, args.start_row)
    end_row = args.end_row
    if end_row is not None and end_row < start_row:
        raise SystemExit("--end-row must be greater than or equal to --start-row")

    limit = max(1, args.limit) if args.limit is not None else None
    firms = read_firms(
        csv_path=csv_path,
        limit=limit,
        start_row=start_row,
        end_row=end_row,
    )
    if not firms:
        raise SystemExit("No firms with id found in CSV")

    asyncio.run(
        run_streaming(
            firms=firms,
            city_code=args.city_code,
            max_sites_per_firm=max(1, args.max_sites_per_firm),
            max_pages=max(1, args.max_pages),
            output_dir=Path(args.output_dir),
            hh_links_csv=Path(args.hh_links_csv),
            telegram_csv=Path(args.telegram_csv),
            max_channels_csv=Path(args.max_channels_csv),
            dzen_csv=Path(args.dzen_csv),
            rutube_csv=Path(args.rutube_csv),
            vk_csv=Path(args.vk_csv),
            drive2_csv=Path(args.drive2_csv),
            other_links_csv=Path(args.other_links_csv),
            booking_csv=Path(args.booking_csv),
            verbose=args.verbose,
            site_concurrency=max(1, args.site_concurrency),
            page_concurrency=max(1, args.page_concurrency),
        )
    )


if __name__ == "__main__":
    main()
