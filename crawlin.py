from __future__ import annotations

import argparse
import asyncio
import csv
import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urldefrag, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

# crawl4ai uses this env var at import-time to choose where it stores local sqlite/cache files.
os.environ.setdefault("CRAWL4_AI_BASE_DIRECTORY", str(Path(__file__).resolve().parent))

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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

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

PRICE_HINTS = (
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
)

SPECIALIST_HINTS = (
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
)

SPECIALIST_CARD_HINTS = (
    "doctor",
    "doctors",
    "team",
    "staff",
    "employee",
    "specialist",
    "врач",
    "команд",
    "персонал",
    "специал",
)

ROLE_WORDS = (
    "главный врач",
    "врач",
    "стоматолог",
    "терапевт",
    "хирург",
    "ортопед",
    "ортодонт",
    "имплантолог",
    "пародонтолог",
    "гигиенист",
    "детский стоматолог",
    "администратор",
    "ассистент",
    "челюстно-лицевой хирург",
    "prosthodontist",
    "orthodontist",
    "dentist",
    "doctor",
    "surgeon",
    "assistant",
)

SPECIALIST_PAGE_HINTS = (
    "врачи",
    "врач",
    "специалисты",
    "команда",
    "персонал",
    "doctors",
    "doctor",
    "team",
    "staff",
)

NON_PERSON_WORDS = {
    "клиника",
    "клиники",
    "стоматология",
    "стоматологии",
    "центр",
    "правительства",
    "постановление",
    "улица",
    "проспект",
    "карла",
    "маркса",
    "бердске",
    "южная",
    "корея",
    "гарантия",
    "шаг",
    "если",
    "вы",
    "телефон",
    "имя",
    "выберите",
}
AMOUNT_PATTERN = r"(?:\d{1,3}(?:[ \u00A0.,]\d{3})+|\d{2,7})"
PRICE_RANGE_RE = re.compile(
    rf"(?P<min>{AMOUNT_PATTERN})\s*[-–—]\s*(?P<max>{AMOUNT_PATTERN})\s*"
    r"(?:₽|руб(?:\.|лей|ля)?|р\b)",
    re.IGNORECASE,
)
PRICE_SINGLE_RE = re.compile(
    rf"(?P<prefix>\bот\b|\bдо\b)?\s*(?P<price>{AMOUNT_PATTERN})\s*(?:₽|руб(?:\.|лей|ля)?|р\b)",
    re.IGNORECASE,
)
PHONE_RE = re.compile(r"(?:\+7|8)\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
INN_LABELED_RE = re.compile(r"\bинн\b[^\d]{0,20}((?:\d[\s\u00A0]*){10,12})", re.IGNORECASE)
OGRN_LABELED_RE = re.compile(r"\bогрн\b[^\d]{0,20}((?:\d[\s\u00A0]*){13})", re.IGNORECASE)
OGRNIP_LABELED_RE = re.compile(r"\b(?:огрнип|огрн\s*ип)\b[^\d]{0,20}((?:\d[\s\u00A0]*){15})", re.IGNORECASE)
FOOTER_HINTS = ("footer", "подвал", "контакт", "contact", "requisite", "реквизит", "legal", "copyright")
RUS_NAME_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]{1,30}(?:-[А-ЯЁ][а-яё]{1,30})?\s+"
    r"[А-ЯЁ][а-яё]{1,30}(?:\s+[А-ЯЁ][а-яё]{1,30})?\b"
)
NAME_INITIALS_RE = re.compile(r"\b[А-ЯЁ][а-яё]{1,30}\s+[А-ЯЁ]\.[А-ЯЁ]\.\b")


@dataclass(frozen=True, slots=True)
class PageData:
    url: str
    title: str
    html: str
    markdown: str


@dataclass(frozen=True, slots=True)
class PriceEntry:
    domain: str
    source_url: str
    service: str
    price_raw: str
    price_min: str
    price_max: str
    currency: str


@dataclass(frozen=True, slots=True)
class SpecialistEntry:
    domain: str
    source_url: str
    full_name: str
    role: str
    phone: str
    email: str


@dataclass(frozen=True, slots=True)
class ContactEntry:
    domain: str
    source_url: str
    source_scope: str
    inn: str
    ogrn: str
    ogrnip: str
    phones: str
    emails: str


def compact_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_markdown_noise(text: str) -> str:
    cleaned = re.sub(r"[*_`>#\[\]\(\)]", "", text)
    return compact_spaces(cleaned)


def normalize_host(host: str) -> str:
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


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
    if any(hint in joined for hint in PRICE_HINTS):
        score += 3
    if any(hint in joined for hint in SPECIALIST_HINTS):
        score += 2
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


def to_price_value(raw_number: str) -> str:
    digits = re.sub(r"[^\d]", "", raw_number)
    return digits if digits else ""


def strip_price_tokens(text: str) -> str:
    cleaned = PRICE_RANGE_RE.sub("", text)
    cleaned = PRICE_SINGLE_RE.sub("", cleaned)
    cleaned = re.sub(r"^[\-\–—:|/•*]+\s*", "", cleaned)
    cleaned = re.sub(r"\s*[\-\–—:|/•*]+\s*$", "", cleaned)
    return strip_markdown_noise(cleaned)


def parse_price_line(text: str, source_url: str, domain: str) -> PriceEntry | None:
    line = strip_markdown_noise(text)
    if len(line) < 4:
        return None

    range_match = PRICE_RANGE_RE.search(line)
    if range_match:
        min_value = to_price_value(range_match.group("min"))
        max_value = to_price_value(range_match.group("max"))
        service = strip_price_tokens(line) or line
        return PriceEntry(
            domain=domain,
            source_url=source_url,
            service=service,
            price_raw=range_match.group(0),
            price_min=min_value,
            price_max=max_value,
            currency="RUB",
        )

    single_match = PRICE_SINGLE_RE.search(line)
    if not single_match:
        return None

    value = to_price_value(single_match.group("price"))
    if not value:
        return None
    prefix = (single_match.group("prefix") or "").strip().lower()
    min_value = value if prefix != "до" else ""
    max_value = value if prefix == "до" else value
    service = strip_price_tokens(line) or line
    return PriceEntry(
        domain=domain,
        source_url=source_url,
        service=service,
        price_raw=single_match.group(0),
        price_min=min_value,
        price_max=max_value,
        currency="RUB",
    )


def unique_lines(lines: Iterable[str]) -> list[str]:
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


def extract_price_lines(page: PageData) -> list[str]:
    lines: list[str] = []
    if page.markdown:
        for raw_line in page.markdown.splitlines():
            line = compact_spaces(raw_line)
            if 6 <= len(line) <= 220 and (PRICE_RANGE_RE.search(line) or PRICE_SINGLE_RE.search(line)):
                lines.append(line)

    if page.html:
        soup = BeautifulSoup(page.html, "html.parser")
        for row in soup.find_all("tr"):
            cells = [
                compact_spaces(cell.get_text(" ", strip=True))
                for cell in row.find_all(["th", "td"])
                if compact_spaces(cell.get_text(" ", strip=True))
            ]
            if len(cells) < 2:
                continue
            row_text = " | ".join(cells)
            if PRICE_RANGE_RE.search(row_text) or PRICE_SINGLE_RE.search(row_text):
                lines.append(row_text)

        for item in soup.find_all(["li", "p"]):
            text = compact_spaces(item.get_text(" ", strip=True))
            if 8 <= len(text) <= 220 and (PRICE_RANGE_RE.search(text) or PRICE_SINGLE_RE.search(text)):
                lines.append(text)

    return unique_lines(lines)


def extract_prices(pages: Iterable[PageData], domain: str) -> list[PriceEntry]:
    items: list[PriceEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for page in pages:
        for line in extract_price_lines(page):
            parsed = parse_price_line(line, page.url, domain)
            if not parsed:
                continue
            key = (parsed.source_url, parsed.service.lower(), parsed.price_raw.lower())
            if key in seen:
                continue
            seen.add(key)
            items.append(parsed)
    return items


def find_name(text: str) -> str:
    match = RUS_NAME_RE.search(text)
    if match:
        return match.group(0)
    match = NAME_INITIALS_RE.search(text)
    if match:
        return match.group(0)
    return ""


def is_probable_person_name(name: str) -> bool:
    words = [word.lower() for word in name.split()]
    if len(words) < 2:
        return False
    if any(word in NON_PERSON_WORDS for word in words):
        return False
    if len(words) == 2 and any(len(word) < 3 for word in words):
        return False
    return True


def find_role(text: str) -> str:
    lowered = text.lower().replace("ё", "е")
    for role in sorted(ROLE_WORDS, key=len, reverse=True):
        if role in lowered:
            return role
    return ""


def extract_contact_value(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return compact_spaces(match.group(0)) if match else ""


def unique_values(values: Iterable[str]) -> list[str]:
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


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = f"7{digits[1:]}"
    if len(digits) == 11 and digits.startswith("7"):
        return f"+{digits}"
    return compact_spaces(phone)


def extract_labeled_digits(pattern: re.Pattern[str], text: str, allowed_lengths: set[int]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in pattern.finditer(text):
        digits = re.sub(r"\D", "", match.group(1))
        if len(digits) not in allowed_lengths:
            continue
        if digits in seen:
            continue
        seen.add(digits)
        values.append(digits)
    return values


def parse_contact_block(text: str) -> dict[str, list[str]]:
    phones = unique_values(normalize_phone(match.group(0)) for match in PHONE_RE.finditer(text))
    emails = unique_values(match.group(0).lower() for match in EMAIL_RE.finditer(text))
    inns = extract_labeled_digits(INN_LABELED_RE, text, allowed_lengths={10, 12})
    ogrns = extract_labeled_digits(OGRN_LABELED_RE, text, allowed_lengths={13})
    ogrnips = extract_labeled_digits(OGRNIP_LABELED_RE, text, allowed_lengths={15})
    return {
        "phones": phones,
        "emails": emails,
        "inns": inns,
        "ogrns": ogrns,
        "ogrnips": ogrnips,
    }


def has_any_contact_data(contact_data: dict[str, list[str]]) -> bool:
    return any(contact_data[field] for field in ("phones", "emails", "inns", "ogrns", "ogrnips"))


def tag_has_footer_hint(tag: Any) -> bool:
    if tag.name == "footer":
        return True
    payload_parts = [
        str(tag.get("id", "")),
        str(tag.get("role", "")),
        str(tag.get("aria-label", "")),
        " ".join(tag.get("class", [])),
    ]
    payload = " ".join(payload_parts).lower()
    return any(hint in payload for hint in FOOTER_HINTS)


def extract_footer_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    chunks: list[str] = []
    seen: set[str] = set()

    for tag in soup.find_all(["footer", "div", "section", "aside"]):
        if not tag_has_footer_hint(tag):
            continue
        text = compact_spaces(tag.get_text(" ", strip=True))
        if len(text) < 20:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        chunks.append(text)

    return " ".join(chunks)


def extract_full_page_text(page: PageData) -> str:
    chunks: list[str] = []
    if page.markdown:
        chunks.append(page.markdown)
    if page.html:
        soup = BeautifulSoup(page.html, "html.parser")
        chunks.append(soup.get_text(" ", strip=True))
    return compact_spaces(" ".join(chunks))


def build_contact_entry(
    domain: str,
    source_url: str,
    source_scope: str,
    contact_data: dict[str, list[str]],
) -> ContactEntry:
    return ContactEntry(
        domain=domain,
        source_url=source_url,
        source_scope=source_scope,
        inn="; ".join(contact_data["inns"]),
        ogrn="; ".join(contact_data["ogrns"]),
        ogrnip="; ".join(contact_data["ogrnips"]),
        phones="; ".join(contact_data["phones"]),
        emails="; ".join(contact_data["emails"]),
    )


def extract_contacts_from_page(page: PageData, domain: str) -> ContactEntry | None:
    footer_text = extract_footer_text(page.html)
    if footer_text:
        footer_contact_data = parse_contact_block(footer_text)
        if has_any_contact_data(footer_contact_data):
            return build_contact_entry(
                domain=domain,
                source_url=page.url,
                source_scope="footer",
                contact_data=footer_contact_data,
            )

    page_text = extract_full_page_text(page)
    if not page_text:
        return None
    page_contact_data = parse_contact_block(page_text)
    if not has_any_contact_data(page_contact_data):
        return None
    return build_contact_entry(
        domain=domain,
        source_url=page.url,
        source_scope="page",
        contact_data=page_contact_data,
    )


def extract_contacts(pages: Iterable[PageData], domain: str) -> list[ContactEntry]:
    entries: list[ContactEntry] = []
    seen: set[tuple[str, str, str, str, str, str]] = set()

    for page in pages:
        entry = extract_contacts_from_page(page, domain)
        if not entry:
            continue
        key = (
            entry.source_scope,
            entry.inn,
            entry.ogrn,
            entry.ogrnip,
            entry.phones,
            entry.emails,
        )
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    return entries


def element_has_specialist_hint(tag: Any) -> bool:
    class_names = " ".join(tag.get("class", []))
    element_id = tag.get("id", "")
    payload = f"{class_names} {element_id}".lower()
    return any(hint in payload for hint in SPECIALIST_CARD_HINTS)


def is_specialist_page(page: PageData) -> bool:
    merged = f"{unquote(page.url)} {page.title}".lower().replace("ё", "е")
    if any(hint in merged for hint in SPECIALIST_PAGE_HINTS):
        return True
    markdown_head = page.markdown[:2500].lower().replace("ё", "е")
    return any(hint in markdown_head for hint in ("наши врачи", "специалисты", "команда врачей"))


def extract_specialists_from_html(page: PageData, domain: str) -> list[SpecialistEntry]:
    if not page.html:
        return []

    soup = BeautifulSoup(page.html, "html.parser")
    found: list[SpecialistEntry] = []
    seen: set[tuple[str, str, str, str]] = set()

    containers = [
        tag for tag in soup.find_all(["article", "section", "div", "li"]) if element_has_specialist_hint(tag)
    ]
    for container in containers:
        text = compact_spaces(container.get_text(" ", strip=True))
        if len(text) < 8:
            continue
        name = find_name(text)
        role = find_role(text)
        phone = extract_contact_value(PHONE_RE, text)
        email = extract_contact_value(EMAIL_RE, text)

        if not name:
            continue
        if not is_probable_person_name(name):
            continue
        key = (name.lower(), role.lower(), phone, email)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            SpecialistEntry(
                domain=domain,
                source_url=page.url,
                full_name=name,
                role=role,
                phone=phone,
                email=email,
            )
        )
    return found


def extract_specialists_from_text(page: PageData, domain: str) -> list[SpecialistEntry]:
    if not is_specialist_page(page):
        return []

    lines = unique_lines(page.markdown.splitlines() if page.markdown else [])
    found: list[SpecialistEntry] = []
    seen: set[tuple[str, str, str, str]] = set()

    for index, line in enumerate(lines):
        if len(line) > 180:
            continue
        neighbors = [line]
        if index > 0:
            neighbors.append(lines[index - 1])
        if index + 1 < len(lines):
            neighbors.append(lines[index + 1])
        chunk = compact_spaces(" ".join(neighbors))

        name = find_name(chunk)
        role = find_role(chunk)
        phone = extract_contact_value(PHONE_RE, chunk)
        email = extract_contact_value(EMAIL_RE, chunk)
        if not name:
            continue
        if not is_probable_person_name(name):
            continue

        key = (name.lower(), role.lower(), phone, email)
        if key in seen:
            continue
        seen.add(key)
        found.append(
            SpecialistEntry(
                domain=domain,
                source_url=page.url,
                full_name=name,
                role=role,
                phone=phone,
                email=email,
            )
        )
    return found


def extract_specialists(pages: Iterable[PageData], domain: str) -> list[SpecialistEntry]:
    combined: list[SpecialistEntry] = []
    seen: set[tuple[str, str, str, str]] = set()
    for page in pages:
        candidates = extract_specialists_from_html(page, domain)
        candidates.extend(extract_specialists_from_text(page, domain))
        for item in candidates:
            key = (item.full_name.lower(), item.role.lower(), item.phone, item.email)
            if key in seen:
                continue
            seen.add(key)
            combined.append(item)

    names_with_role = {item.full_name.lower() for item in combined if item.role}
    filtered: list[SpecialistEntry] = []
    for item in combined:
        if not item.role and item.full_name.lower() in names_with_role:
            continue
        filtered.append(item)
    return filtered


def domain_slug(url: str) -> str:
    host = normalize_host(urlparse(url).netloc)
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", host)


def write_pricing_csv(path: Path, prices: Iterable[PriceEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["domain", "source_url", "service", "price_raw", "price_min", "price_max", "currency"],
        )
        writer.writeheader()
        for item in prices:
            writer.writerow(
                {
                    "domain": item.domain,
                    "source_url": item.source_url,
                    "service": item.service,
                    "price_raw": item.price_raw,
                    "price_min": item.price_min,
                    "price_max": item.price_max,
                    "currency": item.currency,
                }
            )


def write_specialists_csv(path: Path, specialists: Iterable[SpecialistEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["domain", "source_url", "full_name", "role", "phone", "email"],
        )
        writer.writeheader()
        for item in specialists:
            writer.writerow(
                {
                    "domain": item.domain,
                    "source_url": item.source_url,
                    "full_name": item.full_name,
                    "role": item.role,
                    "phone": item.phone,
                    "email": item.email,
                }
            )


def write_contacts_csv(path: Path, contacts: Iterable[ContactEntry]) -> None:
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(
            file,
            fieldnames=["domain", "source_url", "source_scope", "inn", "ogrn", "ogrnip", "phones", "emails"],
        )
        writer.writeheader()
        for item in contacts:
            writer.writerow(
                {
                    "domain": item.domain,
                    "source_url": item.source_url,
                    "source_scope": item.source_scope,
                    "inn": item.inn,
                    "ogrn": item.ogrn,
                    "ogrnip": item.ogrnip,
                    "phones": item.phones,
                    "emails": item.emails,
                }
            )


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

    candidates = []
    if CacheMode is not None and hasattr(CacheMode, "BYPASS"):
        candidates.append(
            {
                "cache_mode": CacheMode.BYPASS,
                "remove_overlay_elements": True,
                "word_count_threshold": 1,
            }
        )

    candidates.extend(
        [
            {"bypass_cache": True, "remove_overlay_elements": True, "word_count_threshold": 1},
            {"bypass_cache": True},
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


async def crawl_site_pages(
    crawler: Any, start_url: str, max_pages: int, run_config: Any, verbose: bool
) -> list[PageData]:
    normalized_start = normalize_start_url(start_url)
    root_host = normalize_host(urlparse(normalized_start).netloc)
    origin_domain = extract_origin_domain(root_host)
    queue: deque[str] = deque([normalized_start])
    enqueued: set[str] = {normalized_start}
    visited: set[str] = set()
    pages: list[PageData] = []

    while queue and len(visited) < max_pages:
        current_url = queue.popleft()
        enqueued.discard(current_url)
        if current_url in visited:
            continue
        visited.add(current_url)

        if verbose:
            print(f"[crawl] {current_url}")

        result = await crawl_page(crawler=crawler, url=current_url, run_config=run_config)
        if not result.success:
            if verbose:
                print(f"[skip] {current_url} -> {result.error_message}")
            continue

        html = result.cleaned_html or result.html or ""
        markdown = extract_markdown_text(result.markdown)
        pages.append(PageData(url=result.url or current_url, title=extract_title(html), html=html, markdown=markdown))

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

            normalized = normalize_internal_url(raw_href, result.url or current_url, origin_domain)
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


def load_urls(args: argparse.Namespace) -> list[str]:
    urls = [value for value in (args.url or []) if value.strip()]
    if args.url_file:
        for line in Path(args.url_file).read_text(encoding="utf-8").splitlines():
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


async def run(urls: list[str], max_pages: int, output_dir: Path, verbose: bool) -> None:
    if AsyncWebCrawler is None:
        raise SystemExit(
            "crawl4ai не установлен. Установите зависимости: "
            "pip install crawl4ai && python -m playwright install"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    browser_config = build_browser_config()
    run_config = build_run_config()

    async with create_crawler(browser_config) as crawler:
        for url in urls:
            domain = domain_slug(url)
            site_dir = output_dir / domain
            site_dir.mkdir(parents=True, exist_ok=True)

            pages = await crawl_site_pages(
                crawler=crawler,
                start_url=url,
                max_pages=max_pages,
                run_config=run_config,
                verbose=verbose,
            )
            prices = extract_prices(pages, domain)
            specialists = extract_specialists(pages, domain)
            contacts = extract_contacts(pages, domain)

            write_pricing_csv(site_dir / "pricing.csv", prices)
            write_specialists_csv(site_dir / "specialists.csv", specialists)
            write_contacts_csv(site_dir / "contacts.csv", contacts)

            print(
                f"[done] {url} | pages={len(pages)} prices={len(prices)} "
                f"specialists={len(specialists)} contacts={len(contacts)} -> {site_dir}"
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl dental websites with crawl4ai and export pricing/specialists/contacts to CSV."
    )
    parser.add_argument("--url", action="append", help="Website URL (can be repeated).")
    parser.add_argument("--url-file", help="Path to text file with URLs (one per line).")
    parser.add_argument("--max-pages", type=int, default=40, help="Max pages to crawl per site.")
    parser.add_argument("--output-dir", default="output", help="Directory for CSV output.")
    parser.add_argument("--verbose", action="store_true", help="Print crawling progress.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    urls = load_urls(args)
    if not urls:
        raise SystemExit("Передайте хотя бы один URL через --url или --url-file")
    asyncio.run(
        run(
            urls=urls,
            max_pages=max(1, args.max_pages),
            output_dir=Path(args.output_dir),
            verbose=args.verbose,
        )
    )


if __name__ == "__main__":
    main()
