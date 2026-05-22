from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.datamining.contracts import SiteScrapeConfig
from app.datamining.contracts import WriteRepository
from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import compact_spaces
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import decode_idna_host
from app.datamining.sites.site_utils import is_reg_ru_host

BOOKING_FIELDNAMES = (
    "site",
    "booking_mode",
    "has_booking_form",
    "has_booking_widget",
    "booking_widget_provider",
    "booking_widget_host",
    "booking_evidence",
)
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
BOOKING_PROVIDER_DOMAINS = {
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


@dataclass(frozen=True, slots=True)
class BookingResult:
    site: str
    booking_mode: str
    has_booking_form: str
    has_booking_widget: str
    booking_widget_provider: str
    booking_widget_host: str
    booking_evidence: str


class BookingRow(TypedDict):
    site: str
    booking_mode: str
    has_booking_form: str
    has_booking_widget: str
    booking_widget_provider: str
    booking_widget_host: str
    booking_evidence: str


@dataclass(frozen=True, slots=True)
class BookingsScraperConfig(SiteScrapeConfig):
    pass


class BookingsScraper:
    def __init__(
        self,
        config: BookingsScraperConfig | None = None,
        repository: WriteRepository[BookingRow] | None = None,
    ) -> None:
        self.config = config or BookingsScraperConfig()
        self.repository = repository

    async def scrape(self, site: str) -> list[BookingRow]:
        pages = await crawl_pages(site, self.config)
        items = booking_rows_from_pages(site, pages)
        if self.repository:
            self.repository.add_many(items)
        return items


async def crawl_pages(site: str, config: SiteScrapeConfig):
    browser_config = build_browser_config()
    run_config = build_run_config()
    async with create_crawler(browser_config) as crawler:
        return await crawl_site_pages(
            crawler=crawler,
            start_url=site,
            max_pages=max(1, config.max_pages),
            run_config=run_config,
            verbose=config.verbose,
            page_concurrency=max(1, config.page_concurrency),
        )


def normalize_host_for_url(host: str) -> str:
    value = (host or "").strip().lower()
    if value.startswith("www."):
        value = value[4:]
    if not value:
        return ""
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def normalize_http_url(url: str) -> str | None:
    resolved, _ = urldefrag(url.strip())
    parsed = urlsplit(resolved)
    if parsed.scheme not in {"http", "https"}:
        return None

    host = normalize_host_for_url(parsed.hostname or "")
    if not host or is_reg_ru_host(host):
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


def compact_match_text(text: str) -> str:
    return compact_spaces((text or "").lower().replace("ё", "е"))


def contains_any_fragment(text: str, fragments: tuple[str, ...]) -> bool:
    compact = compact_match_text(text)
    return any(fragment in compact for fragment in fragments)


def host_matches_domain_list(host: str, domains: set[str]) -> bool:
    normalized = normalize_host_for_url(host)
    if not normalized:
        return False
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in domains)


def url_has_booking_hint(url: str) -> bool:
    normalized = normalize_http_url(url)
    if not normalized:
        return False
    parsed = urlsplit(normalized)
    normalized_parts = compact_match_text(f"{parsed.netloc}{parsed.path}?{parsed.query}")
    return any(hint in normalized_parts for hint in BOOKING_URL_HINTS)


def booking_provider_by_host(host: str) -> str | None:
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


def is_internal_for_site(url: str, site_url: str) -> bool:
    url_host = normalize_host_for_url(urlsplit(url).hostname or "")
    site_host = normalize_host_for_url(urlsplit(site_url).hostname or "")
    return bool(url_host and site_host and (url_host == site_host or url_host.endswith(f".{site_host}")))


def to_display_url(url: str) -> str:
    parsed = urlsplit(url)
    host = decode_idna_host(parsed.hostname or "")
    if not host:
        return url
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


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
                    provider_names.add(provider or f"external:{decode_idna_host(action_host)}")
                    add_evidence("form_action", page.url, decode_idna_host(action_host))
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
                has_booking_hint = url_has_booking_hint(link_url) or contains_any_fragment(tag_text, BOOKING_TEXT_HINTS)
                provider = booking_provider_by_host(host)
                if provider and is_external:
                    has_booking_widget = True
                    provider_names.add(provider)
                    provider_hosts.add(host)
                    add_evidence("widget", page.url, decode_idna_host(host))
                    continue
                if is_external and has_booking_hint:
                    has_booking_widget = True
                    provider_hosts.add(host)
                    provider_names.add(f"external:{decode_idna_host(host)}")
                    add_evidence("widget_link", page.url, decode_idna_host(host))
                    continue
                if tag.name == "a" and not is_external and has_booking_hint:
                    has_booking_form = True
                    add_evidence("booking_link", page.url, to_display_url(link_url))

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
        "booking_widget_host": "; ".join(sorted(decode_idna_host(host) for host in provider_hosts)),
        "booking_evidence": " | ".join(evidence[:6]),
    }


def bookings_from_pages(site: str, pages) -> BookingResult:
    result = detect_booking_features(pages, site)
    return BookingResult(
        site=site,
        booking_mode=result.get("booking_mode", "none"),
        has_booking_form=result.get("has_booking_form", "0"),
        has_booking_widget=result.get("has_booking_widget", "0"),
        booking_widget_provider=result.get("booking_widget_provider", ""),
        booking_widget_host=result.get("booking_widget_host", ""),
        booking_evidence=result.get("booking_evidence", ""),
    )


def booking_result_to_row(item: BookingResult) -> BookingRow:
    return {
        "site": item.site,
        "booking_mode": item.booking_mode,
        "has_booking_form": item.has_booking_form,
        "has_booking_widget": item.has_booking_widget,
        "booking_widget_provider": item.booking_widget_provider,
        "booking_widget_host": item.booking_widget_host,
        "booking_evidence": item.booking_evidence,
    }


def booking_rows_from_pages(site: str, pages) -> list[BookingRow]:
    return [booking_result_to_row(bookings_from_pages(site, pages))]


async def collect_bookings(site: str, max_pages: int = 40) -> list[BookingRow]:
    scraper = BookingsScraper(BookingsScraperConfig(max_pages=max_pages))
    return await scraper.scrape(site)
