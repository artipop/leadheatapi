import csv
import re
from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup

from app.datamining.sites.site_utils import compact_spaces
from app.datamining.sites.site_utils import unique_values

PHONE_RE = re.compile(r"(?:\+7|8)\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
INN_LABELED_RE = re.compile(r"\bинн\b[^\d]{0,20}((?:\d[\s\u00A0]*){10,12})", re.IGNORECASE)
OGRN_LABELED_RE = re.compile(r"\bогрн\b[^\d]{0,20}((?:\d[\s\u00A0]*){13})", re.IGNORECASE)
OGRNIP_LABELED_RE = re.compile(r"\b(?:огрнип|огрн\s*ип)\b[^\d]{0,20}((?:\d[\s\u00A0]*){15})", re.IGNORECASE)
FOOTER_HINTS = ("footer", "подвал", "контакт", "contact", "requisite", "реквизит", "legal", "copyright")


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


def tag_has_footer_hint(tag) -> bool:
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


def extract_full_page_text(page) -> str:
    chunks: list[str] = []
    if page.markdown:
        chunks.append(page.markdown)
    if page.html:
        soup = BeautifulSoup(page.html, "html.parser")
        chunks.append(soup.get_text(" ", strip=True))
    return compact_spaces(" ".join(chunks))


def build_contact_entry(domain: str, source_url: str, source_scope: str, contact_data: dict[str, list[str]]) -> ContactEntry:
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


def extract_contacts_from_page(page, domain: str) -> ContactEntry | None:
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


def extract_contacts(pages, domain: str) -> list[ContactEntry]:
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


def write_contacts_csv(path: Path, contacts) -> None:
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
