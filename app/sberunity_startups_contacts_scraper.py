from __future__ import annotations

import argparse
import csv
import re
import time
import uuid

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse

STARTUPS_URL = "https://sberunity.ru/main/startups"
STARTUP_LINK_SELECTOR = 'a[data-test-id="startups:item"]'
STARTUP_FALLBACK_LINK_SELECTOR = 'a[href*="/main/startups/"]'
STARTUPS_LIST_API_PATH = "/sberx-gateway/v2/list"
STARTUP_VIEW_API_PATH = "/sberx-gateway/view"
DEFAULT_API_CLIENT_ID = "8385"
DEFAULT_API_LOCALE = "ru"
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")
URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
SHOW_BUTTON_RE = re.compile(r"^\s*(?:Показать|Show)\s*$", re.IGNORECASE)
IGNORED_EMAILS = {"sberunity@sberbank.ru"}
BULLET_MASK_CHARS = {"•", "*", "●", "◦", "·"}
LABELS = {
    "description": ("description", "описание"),
    "industries": ("industries", "индустрии", "отрасли"),
    "technologies": ("technologies", "технологии"),
    "website": ("website", "сайт"),
    "tin": ("taxpayer identification number", "tin", "инн"),
    "country": (
        "country of registration a legal entity or sole proprietorship",
        "country of registration legal entity or sole proprietorship",
        "страна регистрации",
    ),
    "legal_entity_name": (
        "legal entity or sole proprietorship",
        "юридическое лицо или ип",
        "юридическое лицо или индивидуальный предприниматель",
    ),
    "revenue": ("revenue", "выручка"),
    "contacts": ("contacts", "контакты"),
    "contact_name": (
        "full name of contact person",
        "full name contact person",
        "фио контактного лица",
        "контактное лицо",
    ),
    "contact_email": (
        "email of contact person",
        "email контактного лица",
        "электронная почта контактного лица",
    ),
    "contact_phone": (
        "mobile of contact person",
        "phone of contact person",
        "телефон контактного лица",
        "мобильный телефон контактного лица",
    ),
}
TOP_SECTION_KEYS = {"description", "industries", "technologies", "website", "revenue", "contacts"}
REVENUE_STOP_LABELS = (
    "pilots and contracts",
    "investment",
    "contacts",
    "пилоты",
    "инвестиции",
    "контакты",
)
CONTACTS_STOP_LABELS = ("product", "company", "revenue", "pilots and contracts", "investment")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)


class SberUnityScraperError(RuntimeError):
    """Raised when scraping flow cannot continue."""


@dataclass(frozen=True)
class StartupCard:
    name: str
    url: str


@dataclass(frozen=True)
class StartupProfile:
    startup_name: str
    startup_url: str
    tin: str
    country: str
    legal_entity_name: str
    industries: list[str]
    technologies: list[str]
    description: str
    website: str
    revenue_amount: str
    revenue_currency: str
    contact_name: str
    contact_email: str
    contact_phone: str
    reveal_clicks: int
    error: str


def compact_spaces(value: str) -> str:
    return " ".join((value or "").replace("\u00A0", " ").split()).strip()


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in values:
        value = compact_spaces(raw)
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def normalize_phone(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    return compact_spaces(raw_phone)


def is_likely_phone_value(phone: str) -> bool:
    value = compact_spaces(phone)
    digits = re.sub(r"\D", "", value)
    if value.startswith("+") and 10 <= len(digits) <= 15:
        return True
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return True
    return False


def _resolve_startup_url(base_url: str, href: str) -> str | None:
    resolved = urljoin(base_url, href or "")
    parsed = urlparse(resolved)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    if "/main/startups/" not in parsed.path:
        return None
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


def startup_cards_count(page) -> int:
    for selector in (STARTUP_LINK_SELECTOR, STARTUP_FALLBACK_LINK_SELECTOR):
        try:
            count = page.locator(selector).count()
        except Exception:
            count = 0
        if count > 0:
            return count
    return 0


def wait_for_startup_cards(page, timeout_seconds: int) -> bool:
    deadline = time.monotonic() + max(1, timeout_seconds)
    while time.monotonic() < deadline:
        if startup_cards_count(page) > 0:
            return True
        page.wait_for_timeout(1000)
    return startup_cards_count(page) > 0


def collect_startup_cards(
    page,
    max_scrolls: int,
    scroll_pause_ms: int,
    stable_scroll_rounds: int,
    max_startups: int | None,
) -> list[StartupCard]:
    discovered: dict[str, str] = {}
    stable_rounds = 0
    previous_count = -1

    for _ in range(max(1, max_scrolls)):
        raw_cards = page.eval_on_selector_all(
            STARTUP_FALLBACK_LINK_SELECTOR,
            """(links) => links.map((link) => {
                const href = link.getAttribute("href") || "";
                const titleNode = link.querySelector("h1,h2,h3,h4,h5,h6");
                const name = (titleNode?.textContent || "").replace(/\\s+/g, " ").trim();
                return { href, name };
            })""",
        )
        for item in raw_cards:
            startup_url = _resolve_startup_url(page.url, str(item.get("href", "")))
            if not startup_url:
                continue
            startup_name = compact_spaces(str(item.get("name", "")))
            if startup_url not in discovered:
                discovered[startup_url] = startup_name
            elif startup_name and not discovered[startup_url]:
                discovered[startup_url] = startup_name
            if max_startups and len(discovered) >= max_startups:
                break

        if max_startups and len(discovered) >= max_startups:
            break

        if len(discovered) == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        previous_count = len(discovered)
        if stable_rounds >= max(1, stable_scroll_rounds):
            break

        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        page.wait_for_timeout(max(100, scroll_pause_ms))
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass

    cards: list[StartupCard] = []
    for index, (url, name) in enumerate(discovered.items(), start=1):
        cards.append(StartupCard(name=name or f"startup_{index}", url=url))
    return cards


def _is_uuid(value: str) -> bool:
    return bool(UUID_RE.search(value or ""))


def _extract_next_token(payload: object) -> int | None:
    if isinstance(payload, dict):
        for key in ("nextPageToken", "next_token", "nextToken", "next", "nextPage"):
            value = payload.get(key)
            if isinstance(value, int):
                return value
            if isinstance(value, str) and value.strip().isdigit():
                return int(value.strip())
        for nested_key in ("meta", "pagination", "page", "result"):
            nested = payload.get(nested_key)
            next_token = _extract_next_token(nested)
            if next_token is not None:
                return next_token
    elif isinstance(payload, list):
        for item in payload:
            next_token = _extract_next_token(item)
            if next_token is not None:
                return next_token
    return None


def _collect_startup_candidates(payload: object, start_url: str) -> list[StartupCard]:
    parsed_start = urlparse(start_url)
    base = f"{parsed_start.scheme}://{parsed_start.netloc}"

    discovered: dict[str, str] = {}

    def try_add(url_value: str, name_value: str | None = None) -> None:
        startup_url = _resolve_startup_url(start_url, url_value)
        if not startup_url:
            return
        name = compact_spaces(name_value or "")
        if startup_url not in discovered:
            discovered[startup_url] = name
        elif name and not discovered[startup_url]:
            discovered[startup_url] = name

    def walk(node: object) -> None:
        if isinstance(node, dict):
            startup_name = ""
            for key in ("name", "title", "startupName", "projectName", "companyName", "shortName"):
                value = node.get(key)
                if isinstance(value, str) and compact_spaces(value):
                    startup_name = compact_spaces(value)
                    break

            for key in ("href", "url", "link", "path", "publicUrl"):
                value = node.get(key)
                if isinstance(value, str):
                    try_add(value, startup_name)
                    if _is_uuid(value):
                        startup_id_match = UUID_RE.search(value)
                        if startup_id_match:
                            try_add(f"{base}/main/startups/{startup_id_match.group(0)}", startup_name)

            for key in ("id", "uuid", "startupId", "questionnaireId", "entityId", "profileId"):
                value = node.get(key)
                if isinstance(value, str) and _is_uuid(value):
                    startup_id_match = UUID_RE.search(value)
                    if startup_id_match:
                        try_add(f"{base}/main/startups/{startup_id_match.group(0)}", startup_name)

            for value in node.values():
                walk(value)

        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            if "/main/startups/" in node:
                try_add(node, "")
            if _is_uuid(node):
                startup_id_match = UUID_RE.search(node)
                if startup_id_match:
                    try_add(f"{base}/main/startups/{startup_id_match.group(0)}", "")

    walk(payload)
    cards: list[StartupCard] = []
    for index, (url, name) in enumerate(discovered.items(), start=1):
        cards.append(StartupCard(name=name or f"startup_{index}", url=url))
    return cards


def _build_startups_api_url(start_url: str, page_token: int, row_count: int) -> str:
    parsed = urlparse(start_url)
    query = urlencode(
        {
            "type": 0,
            "state": 20004,
            "filters": "startup_unity",
            "rowCount": max(1, row_count),
            "pageToken": max(0, page_token),
        }
    )
    return f"{parsed.scheme}://{parsed.netloc}{STARTUPS_LIST_API_PATH}?{query}"


def _build_startup_view_api_url(start_url: str, startup_uuid: str) -> str:
    parsed = urlparse(start_url)
    query = urlencode(
        {
            "type": 0,
            "uuid": startup_uuid,
            "action": 1,
        }
    )
    return f"{parsed.scheme}://{parsed.netloc}{STARTUP_VIEW_API_PATH}?{query}"


def _build_api_headers(api_client_id: str, api_locale: str) -> dict[str, str]:
    return {
        "Client-id": api_client_id.strip(),
        "requestid": str(uuid.uuid4()),
        "locale": api_locale.strip() or DEFAULT_API_LOCALE,
    }


def collect_startup_cards_via_api(
    context,
    start_url: str,
    api_start_token: int,
    api_row_count: int,
    api_max_pages: int,
    api_client_id: str,
    api_locale: str,
    max_startups: int | None,
    verbose: bool,
) -> list[StartupCard]:
    discovered: dict[str, str] = {}
    page_token = max(0, api_start_token)
    pages_read = 0
    empty_pages = 0
    seen_page_tokens: set[int] = set()

    while pages_read < max(1, api_max_pages):
        if page_token in seen_page_tokens:
            break
        seen_page_tokens.add(page_token)

        api_url = _build_startups_api_url(start_url=start_url, page_token=page_token, row_count=api_row_count)
        try:
            response = context.request.get(
                api_url,
                headers=_build_api_headers(api_client_id=api_client_id, api_locale=api_locale),
            )
        except Exception:
            if verbose:
                print(f"[api] request failed: {api_url}")
            break

        if response.status >= 400:
            if verbose:
                try:
                    print(f"[api] status={response.status} body={response.text()[:300]}")
                except Exception:
                    print(f"[api] status={response.status} body=<unreadable>")
            break
        try:
            payload = response.json()
        except Exception:
            if verbose:
                try:
                    print(f"[api] non-json body={response.text()[:300]}")
                except Exception:
                    print("[api] non-json body=<unreadable>")
            break

        page_cards = _collect_startup_candidates(payload=payload, start_url=start_url)
        new_on_page = 0
        for card in page_cards:
            if card.url in discovered:
                continue
            discovered[card.url] = card.name
            new_on_page += 1
            if max_startups and len(discovered) >= max_startups:
                break

        pages_read += 1
        if verbose:
            print(
                f"[api] pageToken={page_token} collected_total={len(discovered)} "
                f"new_on_page={new_on_page} status={response.status}"
            )
        if max_startups and len(discovered) >= max_startups:
            break

        if new_on_page == 0:
            empty_pages += 1
        else:
            empty_pages = 0
        if empty_pages >= 2:
            break

        next_token = _extract_next_token(payload)
        if next_token is not None and next_token != page_token:
            page_token = next_token
        else:
            page_token += max(1, api_row_count)

    cards: list[StartupCard] = []
    for index, (url, name) in enumerate(discovered.items(), start=1):
        cards.append(StartupCard(name=name or f"startup_{index}", url=url))
    return cards


def extract_startup_uuid(url: str) -> str:
    match = UUID_RE.search(url or "")
    return match.group(0) if match else ""


def is_masked_value(value: str) -> bool:
    text = compact_spaces(value)
    if not text:
        return False
    return any(char in BULLET_MASK_CHARS for char in text)


def parse_revenue_amount_currency(value: str) -> tuple[str, str]:
    text = compact_spaces(value)
    if not text:
        return "", ""
    match = re.search(
        r"(?P<amount>\d[\d\s\u00A0.,]*)\s*(?P<currency>RUB|USD|EUR|₽|руб(?:\.|лей|ля)?|\$|€)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return "", ""
    amount_digits = re.sub(r"[^\d]", "", match.group("amount"))
    return amount_digits, normalize_currency(match.group("currency"))


def value_to_text_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, dict):
                if "name" in item and isinstance(item["name"], str):
                    result.append(compact_spaces(item["name"]))
                elif "value" in item and isinstance(item["value"], str):
                    result.append(compact_spaces(item["value"]))
                elif "metric_value" in item and isinstance(item["metric_value"], str):
                    result.append(compact_spaces(item["metric_value"]))
            else:
                item_text = compact_spaces(str(item))
                if item_text:
                    result.append(item_text)
        return unique_values(result)
    if isinstance(value, dict):
        if "value" in value:
            return value_to_text_list(value["value"])
        return [compact_spaces(str(value))]
    text = compact_spaces(str(value))
    return [text] if text else []


def parse_profile_from_view_payload(
    payload: dict[str, object],
    card: StartupCard,
    reveal_clicks: int = 0,
) -> StartupProfile:
    forms_root = payload.get("forms")
    modules: list[dict[str, object]] = []
    if isinstance(forms_root, list):
        for form_block in forms_root:
            if not isinstance(form_block, dict):
                continue
            inner_form = form_block.get("form")
            if not isinstance(inner_form, list):
                continue
            for module in inner_form:
                if isinstance(module, dict):
                    modules.append(module)

    fields_by_sysname: dict[str, dict[str, object]] = {}
    for module in modules:
        fields = module.get("fields")
        if not isinstance(fields, list):
            continue
        for field in fields:
            if not isinstance(field, dict):
                continue
            sys_name = compact_spaces(str(field.get("sysName", "")))
            if not sys_name:
                continue
            if sys_name not in fields_by_sysname:
                fields_by_sysname[sys_name] = field

    def field_value(sys_name: str) -> object:
        field = fields_by_sysname.get(sys_name, {})
        if not isinstance(field, dict):
            return None
        return field.get("value")

    def field_text(sys_name: str) -> str:
        values = value_to_text_list(field_value(sys_name))
        return values[0] if values else ""

    description = field_text("project_note")
    industries = value_to_text_list(field_value("project_industry"))
    technologies = value_to_text_list(field_value("project_technology"))
    website = field_text("questionnaire_site")

    tin = ""
    tin_raw = field_text("questionnaire_inn")
    if tin_raw:
        digits = re.sub(r"\D", "", tin_raw)
        if len(digits) in {10, 12}:
            tin = digits

    country_values = value_to_text_list(field_value("questionnaire_registrationCountry"))
    country = country_values[0] if country_values else ""
    legal_entity_name = field_text("questionnaire_fullName") or compact_spaces(str(payload.get("fullName", "")))

    contact_name = field_text("questionnaire_inviteFio")
    contact_email = field_text("questionnaire_email").lower()
    contact_phone = normalize_phone(field_text("questionnaire_phoneNumber"))

    if contact_email in IGNORED_EMAILS or is_masked_value(contact_email):
        contact_email = ""
    if not is_likely_phone_value(contact_phone) or is_masked_value(contact_phone):
        contact_phone = ""
    if is_masked_value(contact_name):
        contact_name = ""

    revenue_amount = ""
    revenue_currency = ""
    revenue_value = field_value("questionnaire_metricRevenue")
    revenue_candidates = value_to_text_list(revenue_value)
    for candidate in revenue_candidates:
        amount, currency = parse_revenue_amount_currency(candidate)
        if amount:
            revenue_amount, revenue_currency = amount, currency
            break
    if not revenue_amount and isinstance(revenue_value, list):
        for entry in revenue_value:
            if not isinstance(entry, dict):
                continue
            metric_value = compact_spaces(str(entry.get("metric_value", "")))
            amount, currency = parse_revenue_amount_currency(metric_value)
            if amount:
                revenue_amount, revenue_currency = amount, currency
                break

    return StartupProfile(
        startup_name=card.name,
        startup_url=card.url,
        tin=tin,
        country=country,
        legal_entity_name=legal_entity_name,
        industries=industries,
        technologies=technologies,
        description=description,
        website=website,
        revenue_amount=revenue_amount,
        revenue_currency=revenue_currency,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        reveal_clicks=reveal_clicks,
        error="",
    )


def fetch_startup_view_payload(
    context,
    start_url: str,
    startup_uuid: str,
    api_client_id: str,
    api_locale: str,
) -> tuple[dict[str, object] | None, str]:
    if not startup_uuid:
        return None, "missing_uuid"
    request_url = _build_startup_view_api_url(start_url=start_url, startup_uuid=startup_uuid)
    try:
        response = context.request.get(
            request_url,
            headers=_build_api_headers(api_client_id=api_client_id, api_locale=api_locale),
        )
    except Exception as exc:
        return None, f"view_request_failed: {exc}"
    if response.status >= 400:
        return None, f"view_status_{response.status}"
    try:
        payload = response.json()
    except Exception as exc:
        return None, f"view_non_json: {exc}"
    if not isinstance(payload, dict):
        return None, "view_invalid_payload"
    return payload, ""


def reveal_hidden_contacts(page, click_timeout_ms: int, max_clicks: int = 20) -> int:
    selectors = [
        "button[data-test-id='Button']:has-text('Показать')",
        "button[data-test-id='Button']:has-text('Show')",
        "button:has-text('Показать')",
        "button:has-text('Show')",
    ]
    clicks = 0
    for _ in range(max(1, max_clicks)):
        clicked = False
        candidate = None

        for selector in selectors:
            locator = page.locator(selector)
            try:
                if locator.count() == 0:
                    continue
                candidate = locator.first
                break
            except Exception:
                continue

        if candidate is None:
            role_locator = page.get_by_role("button", name=SHOW_BUTTON_RE)
            try:
                if role_locator.count() > 0:
                    candidate = role_locator.first
            except Exception:
                candidate = None

        if candidate is None:
            break

        try:
            candidate.scroll_into_view_if_needed(timeout=click_timeout_ms)
        except Exception:
            pass

        try:
            candidate.click(timeout=click_timeout_ms)
            page.wait_for_timeout(300)
            clicks += 1
            clicked = True
        except Exception:
            clicked = False

        if not clicked:
            break

    return clicks


def normalize_label(line: str) -> str:
    return compact_spaces(line).lower().replace("`", "'").replace("’", "'")


def line_matches_aliases(line: str, aliases: tuple[str, ...]) -> bool:
    norm = normalize_label(line)
    for alias in aliases:
        alias_norm = normalize_label(alias)
        if norm == alias_norm or norm.startswith(f"{alias_norm}:") or norm.startswith(alias_norm):
            return True
    return False


def try_extract_inline_value(line: str, aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        pattern = re.compile(rf"^\s*{re.escape(alias)}\s*[:\-]?\s*(.+?)\s*$", re.IGNORECASE)
        match = pattern.match(line)
        if not match:
            continue
        value = compact_spaces(match.group(1))
        if value:
            return value
    return ""


def line_matches_any_key(line: str, keys: set[str]) -> bool:
    return any(line_matches_aliases(line, LABELS[key]) for key in keys if key in LABELS)


def find_label_index(lines: list[str], key: str, start: int = 0) -> int | None:
    aliases = LABELS.get(key, ())
    if not aliases:
        return None
    for index in range(max(0, start), len(lines)):
        if line_matches_aliases(lines[index], aliases):
            return index
    return None


def collect_section_lines(lines: list[str], key: str, stop_keys: set[str] | None = None) -> list[str]:
    label_index = find_label_index(lines, key)
    if label_index is None:
        return []

    section: list[str] = []
    inline_value = try_extract_inline_value(lines[label_index], LABELS[key])
    if inline_value:
        section.append(inline_value)

    effective_stop_keys = stop_keys if stop_keys is not None else set(LABELS.keys()) - {key}
    for line in lines[label_index + 1:]:
        if line.startswith("1997—"):
            break
        if line_matches_any_key(line, effective_stop_keys):
            break
        norm = normalize_label(line)
        if norm in {normalize_label(value) for value in REVENUE_STOP_LABELS}:
            break
        section.append(line)
    return [compact_spaces(item) for item in section if compact_spaces(item)]


def extract_labeled_values(
    lines: list[str],
    key: str,
    stop_keys: set[str] | None = None,
    max_items: int | None = None,
) -> list[str]:
    section = collect_section_lines(lines=lines, key=key, stop_keys=stop_keys)
    values = [value for value in section if not line_matches_any_key(value, set(LABELS.keys()))]
    values = unique_values(values)
    if max_items is not None:
        return values[: max(0, max_items)]
    return values


def normalize_currency(raw_currency: str) -> str:
    value = raw_currency.strip().upper()
    if value in {"₽", "РУБ", "RUR"}:
        return "RUB"
    if value in {"$", "USDT"}:
        return "USD"
    if value == "€":
        return "EUR"
    if value.startswith("РУБ"):
        return "RUB"
    return value


def extract_revenue(lines: list[str]) -> tuple[str, str]:
    section_lines = collect_section_lines(
        lines=lines,
        key="revenue",
        stop_keys={"contacts", "description", "industries", "technologies", "website"},
    )
    if not section_lines:
        return "", ""

    section_text = " ".join(section_lines)
    revenue_match = re.search(
        r"(?P<amount>\d[\d\s\u00A0.,]*)\s*(?P<currency>RUB|USD|EUR|₽|руб(?:\.|лей|ля)?|\$|€)",
        section_text,
        re.IGNORECASE,
    )
    if not revenue_match:
        return "", ""

    amount_digits = re.sub(r"[^\d]", "", revenue_match.group("amount"))
    currency = normalize_currency(revenue_match.group("currency"))
    return amount_digits, currency


def extract_website(lines: list[str]) -> str:
    website_values = extract_labeled_values(lines=lines, key="website", stop_keys=set(LABELS.keys()), max_items=3)
    for value in website_values:
        match = URL_RE.search(value)
        if match:
            return compact_spaces(match.group(0))
        if "." in value and " " not in value:
            candidate = value.strip("/")
            if candidate and candidate.count(".") >= 1:
                return f"https://{candidate}" if not candidate.startswith("http") else candidate
    return ""


def extract_tin(lines: list[str]) -> str:
    values = extract_labeled_values(lines=lines, key="tin", stop_keys=set(LABELS.keys()), max_items=2)
    for value in values:
        digits = re.sub(r"\D", "", value)
        if len(digits) in {10, 12}:
            return digits
    return ""


def extract_contacts(lines: list[str]) -> tuple[str, str, str]:
    contacts_section = collect_section_lines(lines=lines, key="contacts", stop_keys=set())
    if contacts_section:
        pruned: list[str] = []
        for line in contacts_section:
            norm = normalize_label(line)
            if norm.startswith("1997—"):
                break
            if norm in {normalize_label(value) for value in CONTACTS_STOP_LABELS}:
                break
            pruned.append(line)
        contacts_section = pruned

    if not contacts_section:
        return "", "", ""

    name_values = extract_labeled_values(lines=contacts_section, key="contact_name", stop_keys=set(LABELS.keys()), max_items=1)
    email_values = extract_labeled_values(
        lines=contacts_section, key="contact_email", stop_keys=set(LABELS.keys()), max_items=2
    )
    phone_values = extract_labeled_values(
        lines=contacts_section, key="contact_phone", stop_keys=set(LABELS.keys()), max_items=2
    )

    contact_name = name_values[0] if name_values else ""
    contact_email = ""
    for value in email_values:
        email_match = EMAIL_RE.search(value)
        if not email_match:
            continue
        email = email_match.group(0).lower()
        if email in IGNORED_EMAILS:
            continue
        contact_email = email
        break

    contact_phone = ""
    for value in phone_values:
        phone_match = PHONE_RE.search(value)
        if not phone_match:
            continue
        phone = normalize_phone(phone_match.group(0))
        if is_likely_phone_value(phone):
            contact_phone = phone
            break

    section_text = " ".join(contacts_section)
    if not contact_email:
        for match in EMAIL_RE.finditer(section_text):
            email = match.group(0).lower()
            if email in IGNORED_EMAILS:
                continue
            contact_email = email
            break
    if not contact_phone:
        for match in PHONE_RE.finditer(section_text):
            phone = normalize_phone(match.group(0))
            if not is_likely_phone_value(phone):
                continue
            contact_phone = phone
            break
    if not contact_name:
        for value in contacts_section:
            if line_matches_any_key(value, set(LABELS.keys())):
                continue
            if EMAIL_RE.search(value) or PHONE_RE.search(value):
                continue
            contact_name = value
            break

    return contact_name, contact_email, contact_phone


def extract_profile_from_page(page, card: StartupCard, reveal_clicks: int) -> StartupProfile:
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=8000)
    except Exception:
        pass
    lines = [compact_spaces(line) for line in (body_text or "").splitlines() if compact_spaces(line)]
    if not lines:
        return StartupProfile(
            startup_name=card.name,
            startup_url=card.url,
            tin="",
            country="",
            legal_entity_name="",
            industries=[],
            technologies=[],
            description="",
            website="",
            revenue_amount="",
            revenue_currency="",
            contact_name="",
            contact_email="",
            contact_phone="",
            reveal_clicks=reveal_clicks,
            error="empty_page",
        )

    description = " ".join(extract_labeled_values(lines=lines, key="description", stop_keys=set(LABELS.keys())))
    industries = extract_labeled_values(lines=lines, key="industries", stop_keys=set(LABELS.keys()))
    technologies = extract_labeled_values(lines=lines, key="technologies", stop_keys=set(LABELS.keys()))
    website = extract_website(lines=lines)
    tin = extract_tin(lines=lines)
    country_values = extract_labeled_values(lines=lines, key="country", stop_keys=set(LABELS.keys()), max_items=1)
    legal_entity_values = extract_labeled_values(
        lines=lines, key="legal_entity_name", stop_keys=set(LABELS.keys()), max_items=1
    )
    revenue_amount, revenue_currency = extract_revenue(lines=lines)
    contact_name, contact_email, contact_phone = extract_contacts(lines=lines)

    return StartupProfile(
        startup_name=card.name,
        startup_url=card.url,
        tin=tin,
        country=country_values[0] if country_values else "",
        legal_entity_name=legal_entity_values[0] if legal_entity_values else "",
        industries=industries,
        technologies=technologies,
        description=description,
        website=website,
        revenue_amount=revenue_amount,
        revenue_currency=revenue_currency,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_phone=contact_phone,
        reveal_clicks=reveal_clicks,
        error="",
    )


def scrape_startup_profile(
    page,
    context,
    card: StartupCard,
    start_url: str,
    timeout_ms: int,
    api_client_id: str,
    api_locale: str,
    verbose: bool = False,
) -> StartupProfile:
    startup_uuid = extract_startup_uuid(card.url)
    payload, payload_error = fetch_startup_view_payload(
        context=context,
        start_url=start_url,
        startup_uuid=startup_uuid,
        api_client_id=api_client_id,
        api_locale=api_locale,
    )

    api_profile: StartupProfile | None = None
    if payload is not None:
        api_profile = parse_profile_from_view_payload(payload=payload, card=card, reveal_clicks=0)
        # Fast path: API provided contacts and core fields.
        if api_profile.contact_email or api_profile.contact_phone:
            return api_profile
    elif verbose and payload_error:
        print(f"[view] {card.url} -> {payload_error}")

    try:
        page.goto(card.url, wait_until="domcontentloaded", timeout=timeout_ms)
        page.wait_for_timeout(600)
    except Exception as exc:
        if api_profile is not None:
            return StartupProfile(
                startup_name=api_profile.startup_name,
                startup_url=api_profile.startup_url,
                tin=api_profile.tin,
                country=api_profile.country,
                legal_entity_name=api_profile.legal_entity_name,
                industries=api_profile.industries,
                technologies=api_profile.technologies,
                description=api_profile.description,
                website=api_profile.website,
                revenue_amount=api_profile.revenue_amount,
                revenue_currency=api_profile.revenue_currency,
                contact_name=api_profile.contact_name,
                contact_email=api_profile.contact_email,
                contact_phone=api_profile.contact_phone,
                reveal_clicks=api_profile.reveal_clicks,
                error=payload_error or f"navigation_failed: {exc}",
            )
        return StartupProfile(
            startup_name=card.name,
            startup_url=card.url,
            tin="",
            country="",
            legal_entity_name="",
            industries=[],
            technologies=[],
            description="",
            website="",
            revenue_amount="",
            revenue_currency="",
            contact_name="",
            contact_email="",
            contact_phone="",
            reveal_clicks=0,
            error=f"navigation_failed: {exc}",
        )

    if "/auth" in (page.url or ""):
        return StartupProfile(
            startup_name=card.name,
            startup_url=card.url,
            tin=api_profile.tin if api_profile else "",
            country=api_profile.country if api_profile else "",
            legal_entity_name=api_profile.legal_entity_name if api_profile else "",
            industries=api_profile.industries if api_profile else [],
            technologies=api_profile.technologies if api_profile else [],
            description=api_profile.description if api_profile else "",
            website=api_profile.website if api_profile else "",
            revenue_amount=api_profile.revenue_amount if api_profile else "",
            revenue_currency=api_profile.revenue_currency if api_profile else "",
            contact_name=api_profile.contact_name if api_profile else "",
            contact_email=api_profile.contact_email if api_profile else "",
            contact_phone=api_profile.contact_phone if api_profile else "",
            reveal_clicks=0,
            error="auth_required",
        )

    reveal_clicks = reveal_hidden_contacts(page=page, click_timeout_ms=min(timeout_ms, 12_000))
    page.wait_for_timeout(400)
    dom_profile = extract_profile_from_page(page=page, card=card, reveal_clicks=reveal_clicks)

    if api_profile is not None:
        merged = StartupProfile(
            startup_name=api_profile.startup_name or dom_profile.startup_name,
            startup_url=api_profile.startup_url or dom_profile.startup_url,
            tin=api_profile.tin or dom_profile.tin,
            country=api_profile.country or dom_profile.country,
            legal_entity_name=api_profile.legal_entity_name or dom_profile.legal_entity_name,
            industries=api_profile.industries or dom_profile.industries,
            technologies=api_profile.technologies or dom_profile.technologies,
            description=api_profile.description or dom_profile.description,
            website=api_profile.website or dom_profile.website,
            revenue_amount=api_profile.revenue_amount or dom_profile.revenue_amount,
            revenue_currency=api_profile.revenue_currency or dom_profile.revenue_currency,
            contact_name=api_profile.contact_name or dom_profile.contact_name,
            contact_email=api_profile.contact_email or dom_profile.contact_email,
            contact_phone=api_profile.contact_phone or dom_profile.contact_phone,
            reveal_clicks=reveal_clicks,
            error="",
        )
    else:
        merged = dom_profile

    if (
        not merged.contact_email
        and not merged.contact_phone
        and "/for-startups/startup" in (page.url or "")
        and not merged.error
    ):
        return StartupProfile(
            startup_name=merged.startup_name,
            startup_url=merged.startup_url,
            tin=merged.tin,
            country=merged.country,
            legal_entity_name=merged.legal_entity_name,
            industries=merged.industries,
            technologies=merged.technologies,
            description=merged.description,
            website=merged.website,
            revenue_amount=merged.revenue_amount,
            revenue_currency=merged.revenue_currency,
            contact_name=merged.contact_name,
            contact_email=merged.contact_email,
            contact_phone=merged.contact_phone,
            reveal_clicks=merged.reveal_clicks,
            error="auth_required_or_hidden_contacts",
        )
    return merged


def write_results_csv(path: Path, records: list[StartupProfile]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=[
                "startup_name",
                "startup_url",
                "tin",
                "country",
                "legal_entity_name",
                "industries",
                "technologies",
                "description",
                "website",
                "revenue_amount",
                "revenue_currency",
                "contact_name",
                "contact_email",
                "contact_phone",
                "reveal_clicks",
                "error",
            ],
        )
        writer.writeheader()
        for row in records:
            writer.writerow(
                {
                    "startup_name": row.startup_name,
                    "startup_url": row.startup_url,
                    "tin": row.tin,
                    "country": row.country,
                    "legal_entity_name": row.legal_entity_name,
                    "industries": "; ".join(row.industries),
                    "technologies": "; ".join(row.technologies),
                    "description": row.description,
                    "website": row.website,
                    "revenue_amount": row.revenue_amount,
                    "revenue_currency": row.revenue_currency,
                    "contact_name": row.contact_name,
                    "contact_email": row.contact_email,
                    "contact_phone": row.contact_phone,
                    "reveal_clicks": row.reveal_clicks,
                    "error": row.error,
                }
            )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Scroll SberUnity startups page, open each startup card, "
            "reveal hidden contacts via 'Показать/Show' button and save contacts to CSV."
        )
    )
    parser.add_argument("--start-url", default=STARTUPS_URL, help="Startups list URL.")
    parser.add_argument(
        "--user-data-dir",
        default=".playwright/sberunity-profile",
        help="Persistent Playwright profile directory (stores login session).",
    )
    parser.add_argument("--output", default="output/sberunity_startups_contacts.csv", help="Path to output CSV file.")
    parser.add_argument("--headless", action="store_true", help="Run browser in headless mode.")
    parser.add_argument(
        "--cdp-endpoint",
        help=(
            "Optional CDP endpoint (for already opened Chrome session), "
            "for example http://127.0.0.1:9222."
        ),
    )
    parser.add_argument("--timeout", type=int, default=30, help="Page navigation timeout in seconds.")
    parser.add_argument("--login-wait", type=int, default=120, help="Seconds to wait for manual login if needed.")
    parser.add_argument("--max-startups", type=int, help="Optional limit for startup cards.")
    parser.add_argument(
        "--pagination-mode",
        choices=("auto", "api", "scroll"),
        default="auto",
        help="How to collect startups list: API, UI scroll, or auto (API then scroll fallback).",
    )
    parser.add_argument("--api-start-token", type=int, default=0, help="Start pageToken for API pagination.")
    parser.add_argument("--api-row-count", type=int, default=100, help="rowCount value for API pagination.")
    parser.add_argument("--api-max-pages", type=int, default=500, help="Safety cap for API pages to fetch.")
    parser.add_argument(
        "--api-client-id",
        default=DEFAULT_API_CLIENT_ID,
        help="Required Client-id header for /sberx-gateway API.",
    )
    parser.add_argument(
        "--api-locale",
        default=DEFAULT_API_LOCALE,
        help="locale header for /sberx-gateway API.",
    )
    parser.add_argument("--max-scrolls", type=int, default=250, help="Maximum scroll iterations on list page.")
    parser.add_argument("--scroll-pause-ms", type=int, default=1200, help="Pause between scrolls on list page.")
    parser.add_argument(
        "--stable-scroll-rounds",
        type=int,
        default=5,
        help="Stop scrolling after this many rounds with no new cards.",
    )
    parser.add_argument(
        "--delay-between-startups-ms",
        type=int,
        default=300,
        help="Delay between startup profile requests.",
    )
    parser.add_argument("--verbose", action="store_true", help="Print progress logs.")
    return parser.parse_args()


def run() -> int:
    args = parse_args()

    try:
        from playwright.sync_api import sync_playwright, ViewportSize
    except ImportError:
        print("Playwright is not installed. Install with: uv add playwright && uv run playwright install chromium")
        return 1

    timeout_ms = max(1, args.timeout) * 1000
    output_path = Path(args.output)
    user_data_dir = Path(args.user_data_dir).expanduser().resolve()
    user_data_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        own_context = False
        context = None
        list_page = None
        detail_page = None
        try:
            if args.cdp_endpoint:
                try:
                    browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint, timeout=timeout_ms)
                except TypeError:
                    browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint)
                if not browser.contexts:
                    raise SberUnityScraperError("Connected via CDP but no browser context is available.")
                context = browser.contexts[0]
            else:
                context = playwright.chromium.launch_persistent_context(
                    user_data_dir=str(user_data_dir),
                    headless=args.headless,
                    user_agent=USER_AGENT,
                    locale="ru-RU",
                    viewport=ViewportSize(width=1440, height=900),
                )
                own_context = True

            list_page = context.new_page()
            list_page.goto(args.start_url, wait_until="domcontentloaded", timeout=timeout_ms)

            ui_cards_visible = wait_for_startup_cards(list_page, timeout_seconds=10)
            if not ui_cards_visible and args.pagination_mode == "scroll":
                if not args.headless and args.login_wait > 0:
                    print(
                        "Startup cards are not visible yet. "
                        "If login is required, complete it in the open browser window."
                    )
                    login_deadline = time.monotonic() + max(1, args.login_wait)
                    cards_visible = False
                    while time.monotonic() < login_deadline:
                        if startup_cards_count(list_page) > 0:
                            cards_visible = True
                            break
                        try:
                            if "/main/startups" not in (list_page.url or ""):
                                list_page.goto(args.start_url, wait_until="domcontentloaded", timeout=timeout_ms)
                        except Exception:
                            pass
                        list_page.wait_for_timeout(2000)
                    if not cards_visible:
                        raise SberUnityScraperError("No startup cards found after waiting for login.")
                else:
                    raise SberUnityScraperError(
                        "No startup cards found. Run in non-headless mode and login first, or reuse user-data-dir."
                    )
            elif not ui_cards_visible and args.verbose:
                print("UI cards are not visible. Continuing with API pagination.")

            cards: list[StartupCard] = []
            startup_limit = max(1, args.max_startups) if args.max_startups else None

            if args.pagination_mode in {"auto", "api"}:
                cards = collect_startup_cards_via_api(
                    context=context,
                    start_url=args.start_url,
                    api_start_token=max(0, args.api_start_token),
                    api_row_count=max(1, args.api_row_count),
                    api_max_pages=max(1, args.api_max_pages),
                    api_client_id=str(args.api_client_id),
                    api_locale=str(args.api_locale),
                    max_startups=startup_limit,
                    verbose=args.verbose,
                )
                if args.verbose:
                    print(f"Collected via API: {len(cards)}")

            if args.pagination_mode in {"auto", "scroll"} and not cards:
                cards = collect_startup_cards(
                    page=list_page,
                    max_scrolls=max(1, args.max_scrolls),
                    scroll_pause_ms=max(100, args.scroll_pause_ms),
                    stable_scroll_rounds=max(1, args.stable_scroll_rounds),
                    max_startups=startup_limit,
                )
                if args.verbose:
                    print(f"Collected via scroll: {len(cards)}")

            if not cards:
                raise SberUnityScraperError("No startup links were collected (API and scroll returned empty).")

            if args.verbose:
                print(f"Collected startup cards: {len(cards)}")

            detail_page = context.new_page()
            records: list[StartupProfile] = []

            for index, card in enumerate(cards, start=1):
                if args.verbose:
                    print(f"[{index}/{len(cards)}] {card.name or card.url}")
                record = scrape_startup_profile(
                    page=detail_page,
                    context=context,
                    card=card,
                    start_url=args.start_url,
                    timeout_ms=timeout_ms,
                    api_client_id=str(args.api_client_id),
                    api_locale=str(args.api_locale),
                    verbose=args.verbose,
                )
                records.append(record)
                detail_page.wait_for_timeout(max(0, args.delay_between_startups_ms))

            write_results_csv(output_path, records)
            ok_count = sum(1 for row in records if not row.error)
            auth_failed_count = sum(
                1
                for row in records
                if row.error in {"auth_required", "auth_required_or_hidden_contacts"}
            )
            print(
                f"Done. Total={len(records)}, success={ok_count}, failed={len(records) - ok_count}, "
                f"output={output_path.resolve()}"
            )
            if records and auth_failed_count == len(records):
                print(
                    "Hint: all startup pages require authorized access for contacts. "
                    "Run in non-headless mode and login in the opened browser profile."
                )
            return 0
        except SberUnityScraperError as exc:
            print(f"Error: {exc}")
            return 1
        except Exception as exc:
            print(f"Error: {exc}")
            return 1
        finally:
            for page_obj in (detail_page, list_page):
                if page_obj is None:
                    continue
                try:
                    page_obj.close()
                except Exception:
                    pass
            if own_context and context is not None:
                context.close()


if __name__ == "__main__":
    raise SystemExit(run())
