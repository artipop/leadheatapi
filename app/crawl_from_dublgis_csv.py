from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol
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
CRAWL_STATUS_FIELDNAMES = (
    "firm_id",
    "company_name",
    "address_name",
    "firm_card_url",
    "site_url",
    "stage",
    "status",
    "details",
    "updated_at",
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
    if is_reg_ru_host(host):
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
    if is_reg_ru_host(host):
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


def is_reg_ru_host(host: str) -> bool:
    value = normalize_host_for_url(host)
    return value == "reg.ru" or value.endswith(".reg.ru")


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
    if is_reg_ru_host(host):
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

    final_host = normalize_host_for_url(urlsplit(final_url).hostname or "")
    if is_reg_ru_host(final_host):
        return None

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


@dataclass(frozen=True, slots=True)
class FirmRecord:
    firm_id: str
    company_name: str
    address_name: str
    firm_card_url: str


@dataclass(frozen=True, slots=True)
class CrawlTarget:
    firm: FirmRecord
    site_url: str


@dataclass(frozen=True, slots=True)
class ExternalLinkRecord:
    firm: FirmRecord
    site_url: str
    link_url: str
    link_host: str
    kind: str
    source: str


@dataclass(frozen=True, slots=True)
class CrawlPipelineConfig:
    city_code: str
    max_sites_per_firm: int
    max_pages: int
    site_concurrency: int
    page_concurrency: int
    verbose: bool


class CrawlDataSource(Protocol):
    async def list_firms(self) -> list[FirmRecord]:
        raise NotImplementedError


class CrawlRepository(Protocol):
    async def record_status(
        self,
        *,
        firm: FirmRecord,
        stage: str,
        status: str,
        site_url: str = "",
        details: str = "",
    ) -> None:
        raise NotImplementedError

    async def save_external_link(self, link: ExternalLinkRecord) -> bool:
        raise NotImplementedError

    async def save_site_payload(
        self,
        *,
        target: CrawlTarget,
        pages: list[Any],
        prices: list[Any],
        specialists: list[Any],
        contacts: list[Any],
        booking_features: dict[str, str],
    ) -> None:
        raise NotImplementedError

    def emit_summary(self) -> None:
        raise NotImplementedError


class ListFirmDataSource:
    def __init__(self, firms: list[dict[str, str]], city_code: str) -> None:
        self._city_code = city_code
        self._firms = firms

    async def list_firms(self) -> list[FirmRecord]:
        return [
            FirmRecord(
                firm_id=firm["id"],
                company_name=firm["name"],
                address_name=firm["address_name"],
                firm_card_url=f"https://2gis.ru/{self._city_code}/firm/{firm['id']}",
            )
            for firm in self._firms
        ]


class CsvCrawlRepository:
    def __init__(
        self,
        *,
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
        crawl_status_csv: Path,
    ) -> None:
        self.output_dir = output_dir
        self.hh_links_csv = hh_links_csv
        self.telegram_csv = telegram_csv
        self.max_channels_csv = max_channels_csv
        self.dzen_csv = dzen_csv
        self.rutube_csv = rutube_csv
        self.vk_csv = vk_csv
        self.drive2_csv = drive2_csv
        self.other_links_csv = other_links_csv
        self.booking_csv = booking_csv
        self.crawl_status_csv = crawl_status_csv
        self._lock = asyncio.Lock()

        self._seen_hh = load_seen_pairs(self.hh_links_csv, "firm_id", "hh_employer_url")
        self._seen_telegram = load_seen_pairs(self.telegram_csv, "firm_id", "telegram_url")
        self._seen_max = load_seen_pairs(self.max_channels_csv, "firm_id", "max_channel_url")
        self._seen_dzen = load_seen_pairs(self.dzen_csv, "firm_id", "dzen_url")
        self._seen_rutube = load_seen_pairs(self.rutube_csv, "firm_id", "link_url")
        self._seen_vk = load_seen_pairs(self.vk_csv, "firm_id", "link_url")
        self._seen_drive2 = load_seen_pairs(self.drive2_csv, "firm_id", "link_url")
        self._seen_other = load_seen_pairs(self.other_links_csv, "firm_id", "link_url")
        self._seen_booking = load_seen_pairs(self.booking_csv, "firm_id", "site_url")
        self._inserted_counts: defaultdict[str, int] = defaultdict(int)

    def _append_unique_row(
        self,
        *,
        csv_path: Path,
        fieldnames: tuple[str, ...],
        row: dict[str, str],
        seen: set[tuple[str, str]],
        key_field: str,
        counter_key: str,
    ) -> bool:
        key = ((row.get("firm_id") or "").strip(), (row.get(key_field) or "").strip())
        if not key[0] or not key[1] or key in seen:
            return False
        seen.add(key)
        self._inserted_counts[counter_key] += append_csv_rows(csv_path, fieldnames, [row])
        return True

    async def record_status(
        self,
        *,
        firm: FirmRecord,
        stage: str,
        status: str,
        site_url: str = "",
        details: str = "",
    ) -> None:
        row = {
            "firm_id": firm.firm_id,
            "company_name": firm.company_name,
            "address_name": firm.address_name,
            "firm_card_url": firm.firm_card_url,
            "site_url": site_url,
            "stage": stage,
            "status": status,
            "details": details[:1200],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        async with self._lock:
            append_csv_rows(self.crawl_status_csv, CRAWL_STATUS_FIELDNAMES, [row])

    async def save_external_link(self, link: ExternalLinkRecord) -> bool:
        source_with_kind = f"{link.source}_{link.kind}"
        base_row = {
            "firm_id": link.firm.firm_id,
            "company_name": link.firm.company_name,
            "address_name": link.firm.address_name,
            "site_url": link.site_url,
            "link_url": link.link_url,
            "link_host": link.link_host,
            "source": source_with_kind,
        }

        async with self._lock:
            if link.kind == "hh":
                row = {
                    "firm_id": link.firm.firm_id,
                    "company_name": link.firm.company_name,
                    "address_name": link.firm.address_name,
                    "site_url": link.site_url,
                    "hh_employer_url": link.link_url,
                    "source": source_with_kind,
                }
                return self._append_unique_row(
                    csv_path=self.hh_links_csv,
                    fieldnames=HH_LINKS_FIELDNAMES,
                    row=row,
                    seen=self._seen_hh,
                    key_field="hh_employer_url",
                    counter_key="hh",
                )
            if link.kind == "telegram":
                row = {
                    "firm_id": link.firm.firm_id,
                    "company_name": link.firm.company_name,
                    "address_name": link.firm.address_name,
                    "site_url": link.site_url,
                    "telegram_url": link.link_url,
                    "source": source_with_kind,
                }
                return self._append_unique_row(
                    csv_path=self.telegram_csv,
                    fieldnames=TELEGRAM_FIELDNAMES,
                    row=row,
                    seen=self._seen_telegram,
                    key_field="telegram_url",
                    counter_key="telegram",
                )
            if link.kind == "max":
                row = {
                    "firm_id": link.firm.firm_id,
                    "company_name": link.firm.company_name,
                    "address_name": link.firm.address_name,
                    "site_url": link.site_url,
                    "max_channel_url": link.link_url,
                    "source": source_with_kind,
                }
                return self._append_unique_row(
                    csv_path=self.max_channels_csv,
                    fieldnames=MAX_FIELDNAMES,
                    row=row,
                    seen=self._seen_max,
                    key_field="max_channel_url",
                    counter_key="max",
                )
            if link.kind == "dzen":
                row = {
                    "firm_id": link.firm.firm_id,
                    "company_name": link.firm.company_name,
                    "address_name": link.firm.address_name,
                    "site_url": link.site_url,
                    "dzen_url": link.link_url,
                    "source": source_with_kind,
                }
                return self._append_unique_row(
                    csv_path=self.dzen_csv,
                    fieldnames=DZEN_FIELDNAMES,
                    row=row,
                    seen=self._seen_dzen,
                    key_field="dzen_url",
                    counter_key="dzen",
                )

            if link.kind == "video":
                inserted = self._append_unique_row(
                    csv_path=self.rutube_csv,
                    fieldnames=RUTUBE_FIELDNAMES,
                    row=base_row,
                    seen=self._seen_rutube,
                    key_field="link_url",
                    counter_key="video",
                )
                self._append_unique_row(
                    csv_path=self.other_links_csv,
                    fieldnames=OTHER_LINKS_FIELDNAMES,
                    row=base_row,
                    seen=self._seen_other,
                    key_field="link_url",
                    counter_key="other",
                )
                return inserted
            if link.kind == "social":
                inserted = self._append_unique_row(
                    csv_path=self.vk_csv,
                    fieldnames=VK_FIELDNAMES,
                    row=base_row,
                    seen=self._seen_vk,
                    key_field="link_url",
                    counter_key="social",
                )
                self._append_unique_row(
                    csv_path=self.other_links_csv,
                    fieldnames=OTHER_LINKS_FIELDNAMES,
                    row=base_row,
                    seen=self._seen_other,
                    key_field="link_url",
                    counter_key="other",
                )
                return inserted
            if link.kind == "drive2":
                return self._append_unique_row(
                    csv_path=self.drive2_csv,
                    fieldnames=DRIVE2_FIELDNAMES,
                    row=base_row,
                    seen=self._seen_drive2,
                    key_field="link_url",
                    counter_key="drive2",
                )
            if link.kind == "other":
                return self._append_unique_row(
                    csv_path=self.other_links_csv,
                    fieldnames=OTHER_LINKS_FIELDNAMES,
                    row=base_row,
                    seen=self._seen_other,
                    key_field="link_url",
                    counter_key="other",
                )
        return False

    async def save_site_payload(
        self,
        *,
        target: CrawlTarget,
        pages: list[Any],
        prices: list[Any],
        specialists: list[Any],
        contacts: list[Any],
        booking_features: dict[str, str],
    ) -> None:
        domain = domain_slug(target.site_url)
        site_dir = self.output_dir / domain
        site_dir.mkdir(parents=True, exist_ok=True)
        write_pricing_csv(site_dir / "pricing.csv", prices)
        write_specialists_csv(site_dir / "specialists.csv", specialists)
        write_contacts_csv(site_dir / "contacts.csv", contacts)
        self._inserted_counts["sites"] += 1

        if booking_features.get("booking_mode", "none") == "none":
            return

        row = {
            "firm_id": target.firm.firm_id,
            "company_name": target.firm.company_name,
            "address_name": target.firm.address_name,
            "site_url": target.site_url,
            **booking_features,
        }
        async with self._lock:
            self._append_unique_row(
                csv_path=self.booking_csv,
                fieldnames=BOOKING_FIELDNAMES,
                row=row,
                seen=self._seen_booking,
                key_field="site_url",
                counter_key="booking",
            )

    def emit_summary(self) -> None:
        logger.info(
            "Inserted rows -> hh:%s telegram:%s max:%s dzen:%s social:%s video:%s drive2:%s other:%s booking:%s sites:%s",
            self._inserted_counts["hh"],
            self._inserted_counts["telegram"],
            self._inserted_counts["max"],
            self._inserted_counts["dzen"],
            self._inserted_counts["social"],
            self._inserted_counts["video"],
            self._inserted_counts["drive2"],
            self._inserted_counts["other"],
            self._inserted_counts["booking"],
            self._inserted_counts["sites"],
        )
        logger.info("Crawl status CSV: %s", self.crawl_status_csv)
        logger.info("HH links CSV: %s", self.hh_links_csv)
        logger.info("Telegram links CSV: %s", self.telegram_csv)
        logger.info("Max channels CSV: %s", self.max_channels_csv)
        logger.info("Dzen CSV: %s", self.dzen_csv)
        logger.info("VK/social CSV: %s", self.vk_csv)
        logger.info("Video hosting CSV: %s", self.rutube_csv)
        logger.info("Drive2 CSV: %s", self.drive2_csv)
        logger.info("Other external links CSV: %s", self.other_links_csv)
        logger.info("Booking CSV: %s", self.booking_csv)


class SiteCrawlerPipeline:
    def __init__(
        self,
        *,
        data_source: CrawlDataSource,
        repository: CrawlRepository,
        config: CrawlPipelineConfig,
    ) -> None:
        self._data_source = data_source
        self._repository = repository
        self._config = config
        self._short_url_cache: dict[str, Optional[str]] = {}
        self._site_redirect_cache: dict[str, Optional[str]] = {}
        self._seen_origin_domains: set[str] = set()
        self._failed_attempts: list[str] = []
        self._failed_seen: set[str] = set()

    async def run(self) -> None:
        firms = await self._data_source.list_firms()
        if not firms:
            return

        crawl_targets = await self._run_preprocess(firms)
        if not crawl_targets:
            self._print_failed_attempts()
            return

        await self._run_crawl(crawl_targets)
        self._print_failed_attempts()

    async def _run_preprocess(self, firms: list[FirmRecord]) -> list[CrawlTarget]:
        targets: list[CrawlTarget] = []
        for index, firm in enumerate(firms, start=1):
            logger.info("Preprocess firm %s/%s: id=%s name=%s", index, len(firms), firm.firm_id, firm.company_name or "-")
            try:
                sites = await asyncio.to_thread(get_firm_sites, self._config.city_code, firm.firm_id)
            except Exception as exc:
                logger.exception("Failed to resolve sites for firm_id=%s: %s", firm.firm_id, exc)
                await self._repository.record_status(
                    firm=firm,
                    stage="preprocess",
                    status="failed",
                    details=f"get_firm_sites_error: {exc}",
                )
                self._mark_failed(firm.firm_card_url)
                continue

            if not sites:
                await self._repository.record_status(
                    firm=firm,
                    stage="preprocess",
                    status="failed",
                    details="no_sites_from_get_firm_sites",
                )
                self._mark_failed(firm.firm_card_url)
                continue

            prepared = await self._prepare_targets_for_firm(firm, sites)
            targets.extend(prepared)
        return targets

    async def _prepare_targets_for_firm(self, firm: FirmRecord, sites: list[str]) -> list[CrawlTarget]:
        seen_in_firm: set[str] = set()
        crawl_candidates: list[str] = []

        for raw_site in sites:
            processed_site = raw_site
            if is_clck_url(raw_site):
                resolved = await self._resolve_short_url_cached(raw_site)
                if not resolved:
                    logger.info("Skip unresolved clck.ru from 2GIS card: firm_id=%s url=%s", firm.firm_id, to_display_url(raw_site))
                    continue
                logger.info("Resolved clck.ru from 2GIS card: %s -> %s", to_display_url(raw_site), to_display_url(resolved))
                processed_site = resolved

            if is_jivo_url(processed_site):
                logger.info("Skip jivo.chat from 2GIS card: firm_id=%s url=%s", firm.firm_id, to_display_url(processed_site))
                continue
            if is_reg_ru_host(split_http_like_url(processed_site).hostname or ""):
                logger.info("Skip reg.ru from 2GIS card: firm_id=%s url=%s", firm.firm_id, to_display_url(processed_site))
                continue

            direct_social = self._classify_social_link(
                firm=firm,
                site_url="",
                raw_url=processed_site,
                source="2gis_card",
            )
            if direct_social is not None:
                await self._repository.save_external_link(direct_social)
                continue

            normalized_site = normalize_site_origin_url(processed_site)
            if not normalized_site:
                continue

            resolved_site = await self._resolve_site_redirect_cached(normalized_site)
            if not resolved_site:
                continue
            if resolved_site != normalized_site:
                logger.info(
                    "Merged site by redirect for firm_id=%s: %s -> %s",
                    firm.firm_id,
                    to_display_url(normalized_site),
                    to_display_url(resolved_site),
                )

            redirected_social = self._classify_social_link(
                firm=firm,
                site_url="",
                raw_url=resolved_site,
                source="2gis_card_redirect",
            )
            if redirected_social is not None:
                await self._repository.save_external_link(redirected_social)
                continue

            if resolved_site in seen_in_firm:
                continue
            seen_in_firm.add(resolved_site)
            crawl_candidates.append(resolved_site)

        selected = crawl_candidates[: max(1, self._config.max_sites_per_firm)]
        prepared_targets: list[CrawlTarget] = []
        for site_url in selected:
            origin_domain = origin_domain_from_url(site_url)
            target = CrawlTarget(firm=firm, site_url=site_url)
            if origin_domain and origin_domain in self._seen_origin_domains:
                await self._repository.record_status(
                    firm=firm,
                    stage="preprocess",
                    status="skipped",
                    site_url=site_url,
                    details=f"duplicate_origin_domain={decode_host_from_idna(origin_domain)}",
                )
                continue
            if origin_domain:
                self._seen_origin_domains.add(origin_domain)
            prepared_targets.append(target)
            await self._repository.record_status(
                firm=firm,
                stage="preprocess",
                status="queued",
                site_url=site_url,
            )

        if not prepared_targets:
            await self._repository.record_status(
                firm=firm,
                stage="preprocess",
                status="failed",
                details="no_crawl_targets_after_filtering",
            )
            self._mark_failed(firm.firm_card_url)

        logger.info(
            "Firm %s preprocess result: raw_sites=%s queued_sites=%s",
            firm.firm_id,
            len(sites),
            len(prepared_targets),
        )
        return prepared_targets

    async def _run_crawl(self, crawl_targets: list[CrawlTarget]) -> None:
        browser_config = build_browser_config()
        run_config = build_run_config()
        site_semaphore = asyncio.Semaphore(max(1, self._config.site_concurrency))

        async with create_crawler(browser_config) as crawler:
            async def _crawl_one(target: CrawlTarget) -> None:
                async with site_semaphore:
                    await self._crawl_single_site(crawler=crawler, run_config=run_config, target=target)

            tasks = [asyncio.create_task(_crawl_one(target)) for target in crawl_targets]
            for task in tasks:
                await task

    async def _crawl_single_site(self, *, crawler: Any, run_config: Any, target: CrawlTarget) -> None:
        await self._repository.record_status(
            firm=target.firm,
            stage="crawl",
            status="running",
            site_url=target.site_url,
        )
        try:
            pages = await crawl_site_pages(
                crawler=crawler,
                start_url=target.site_url,
                max_pages=max(1, self._config.max_pages),
                run_config=run_config,
                verbose=self._config.verbose,
                page_concurrency=max(1, self._config.page_concurrency),
            )
        except Exception as exc:
            logger.exception("Crawler failed for site=%s: %s", to_display_url(target.site_url), exc)
            await self._repository.record_status(
                firm=target.firm,
                stage="crawl",
                status="failed",
                site_url=target.site_url,
                details=f"crawl_exception: {exc}",
            )
            self._mark_failed(target.site_url)
            return

        if not pages:
            await self._repository.record_status(
                firm=target.firm,
                stage="crawl",
                status="failed",
                site_url=target.site_url,
                details="no_pages_collected",
            )
            self._mark_failed(target.site_url)
            return

        domain = domain_slug(target.site_url)
        prices = extract_prices(pages, domain)
        specialists = extract_specialists(pages, domain)
        contacts = extract_contacts(pages, domain)
        booking_features = detect_booking_features(pages, target.site_url)
        await self._repository.save_site_payload(
            target=target,
            pages=pages,
            prices=prices,
            specialists=specialists,
            contacts=contacts,
            booking_features=booking_features,
        )

        link_counts = await self._extract_external_links(target=target, pages=pages)
        details = (
            f"pages={len(pages)} prices={len(prices)} specialists={len(specialists)} contacts={len(contacts)} "
            f"booking={booking_features.get('booking_mode', 'none')} links={dict(link_counts)}"
        )
        await self._repository.record_status(
            firm=target.firm,
            stage="crawl",
            status="success",
            site_url=target.site_url,
            details=details,
        )
        logger.info("SUCCESS: %s | %s", to_display_url(target.site_url), details)

    async def _extract_external_links(self, *, target: CrawlTarget, pages: list[Any]) -> dict[str, int]:
        counts: defaultdict[str, int] = defaultdict(int)
        for source_page_url, raw_link in extract_page_links(pages):
            processed_link = raw_link
            if is_clck_url(raw_link):
                resolved = await self._resolve_short_url_cached(raw_link)
                if not resolved:
                    logger.info("Skip unresolved clck.ru from site crawl: %s", to_display_url(raw_link))
                    continue
                processed_link = resolved

            if is_jivo_url(processed_link):
                continue
            if has_invalid_http_port(processed_link):
                logger.warning(
                    "Skip malformed link with invalid port: firm_id=%s site=%s source_page=%s link=%s",
                    target.firm.firm_id,
                    to_display_url(target.site_url),
                    to_display_url(source_page_url),
                    processed_link,
                )
                continue

            social_link = self._classify_social_link(
                firm=target.firm,
                site_url=target.site_url,
                raw_url=processed_link,
                source="site_crawl",
            )
            if social_link is not None:
                if await self._repository.save_external_link(social_link):
                    counts[social_link.kind] += 1
                continue

            normalized = normalize_http_url(processed_link)
            if not normalized:
                continue
            if is_internal_for_site(normalized, target.site_url):
                continue

            other_link = ExternalLinkRecord(
                firm=target.firm,
                site_url=target.site_url,
                link_url=normalized,
                link_host=normalize_host_for_url(urlsplit(normalized).hostname or ""),
                kind="other",
                source="site_crawl",
            )
            if await self._repository.save_external_link(other_link):
                counts["other"] += 1
        return dict(counts)

    def _classify_social_link(
        self,
        *,
        firm: FirmRecord,
        site_url: str,
        raw_url: str,
        source: str,
    ) -> Optional[ExternalLinkRecord]:
        hh_url = normalize_hh_employer_url(raw_url)
        if hh_url:
            return self._build_external_link(firm=firm, site_url=site_url, link_url=hh_url, kind="hh", source=source)

        max_url = normalize_max_channel_url(raw_url)
        if max_url:
            return self._build_external_link(firm=firm, site_url=site_url, link_url=max_url, kind="max", source=source)

        telegram_url = normalize_telegram_url(raw_url)
        if telegram_url:
            return self._build_external_link(
                firm=firm,
                site_url=site_url,
                link_url=telegram_url,
                kind="telegram",
                source=source,
            )

        dzen_url = normalize_dzen_url(raw_url)
        if dzen_url:
            return self._build_external_link(
                firm=firm,
                site_url=site_url,
                link_url=dzen_url,
                kind="dzen",
                source=source,
            )

        normalized = normalize_http_url(raw_url)
        if not normalized:
            return None
        platform_kind = classify_platform_url(normalized)
        if platform_kind == "drive2":
            return self._build_external_link(
                firm=firm,
                site_url=site_url,
                link_url=normalized,
                kind="drive2",
                source=source,
            )
        if platform_kind == "video_hosting":
            return self._build_external_link(
                firm=firm,
                site_url=site_url,
                link_url=normalized,
                kind="video",
                source=source,
            )
        if platform_kind == "social_network":
            return self._build_external_link(
                firm=firm,
                site_url=site_url,
                link_url=normalized,
                kind="social",
                source=source,
            )
        return None

    def _build_external_link(
        self,
        *,
        firm: FirmRecord,
        site_url: str,
        link_url: str,
        kind: str,
        source: str,
    ) -> ExternalLinkRecord:
        return ExternalLinkRecord(
            firm=firm,
            site_url=site_url,
            link_url=link_url,
            link_host=normalize_host_for_url(urlsplit(link_url).hostname or ""),
            kind=kind,
            source=source,
        )

    async def _resolve_short_url_cached(self, raw_url: str) -> Optional[str]:
        key = raw_url.strip()
        if key in self._short_url_cache:
            return self._short_url_cache[key]
        resolved = await asyncio.to_thread(resolve_short_url, key)
        self._short_url_cache[key] = resolved
        return resolved

    async def _resolve_site_redirect_cached(self, raw_url: str) -> Optional[str]:
        normalized = normalize_site_origin_url(raw_url)
        if not normalized:
            return None
        if normalized in self._site_redirect_cache:
            return self._site_redirect_cache[normalized]
        resolved = await asyncio.to_thread(resolve_site_redirect, normalized)
        self._site_redirect_cache[normalized] = resolved
        return resolved

    def _mark_failed(self, value: str) -> None:
        if value in self._failed_seen:
            return
        self._failed_seen.add(value)
        self._failed_attempts.append(value)

    def _print_failed_attempts(self) -> None:
        if self._failed_attempts:
            print("\nFailed attempts:")
            for value in self._failed_attempts:
                print(value)
            return
        print("\nFailed attempts: none")


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
    crawl_status_csv: Path,
    verbose: bool,
    site_concurrency: int,
    page_concurrency: int,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_source = ListFirmDataSource(firms=firms, city_code=city_code)
    repository = CsvCrawlRepository(
        output_dir=output_dir,
        hh_links_csv=hh_links_csv,
        telegram_csv=telegram_csv,
        max_channels_csv=max_channels_csv,
        dzen_csv=dzen_csv,
        rutube_csv=rutube_csv,
        vk_csv=vk_csv,
        drive2_csv=drive2_csv,
        other_links_csv=other_links_csv,
        booking_csv=booking_csv,
        crawl_status_csv=crawl_status_csv,
    )
    pipeline = SiteCrawlerPipeline(
        data_source=data_source,
        repository=repository,
        config=CrawlPipelineConfig(
            city_code=city_code,
            max_sites_per_firm=max(1, max_sites_per_firm),
            max_pages=max(1, max_pages),
            site_concurrency=max(1, site_concurrency),
            page_concurrency=max(1, page_concurrency),
            verbose=verbose,
        ),
    )
    await pipeline.run()
    repository.emit_summary()


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
    parser.add_argument(
        "--crawl-status-csv",
        default="output/crawl_status.csv",
        help="CSV file with queue/crawl statuses per site.",
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
            crawl_status_csv=Path(args.crawl_status_csv),
            verbose=args.verbose,
            site_concurrency=max(1, args.site_concurrency),
            page_concurrency=max(1, args.page_concurrency),
        )
    )


if __name__ == "__main__":
    main()
