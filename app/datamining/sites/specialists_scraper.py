import argparse
import asyncio
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from bs4 import BeautifulSoup

from app.datamining.sites.site_utils import build_browser_config
from app.datamining.sites.site_utils import build_run_config
from app.datamining.sites.site_utils import compact_spaces
from app.datamining.sites.site_utils import crawl_site_pages
from app.datamining.sites.site_utils import create_crawler
from app.datamining.sites.site_utils import domain_slug
from app.datamining.sites.site_utils import unique_lines

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
PHONE_RE = re.compile(r"(?:\+7|8)\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RUS_NAME_RE = re.compile(
    r"\b[А-ЯЁ][а-яё]{1,30}(?:-[А-ЯЁ][а-яё]{1,30})?\s+"
    r"[А-ЯЁ][а-яё]{1,30}(?:\s+[А-ЯЁ][а-яё]{1,30})?\b"
)
NAME_INITIALS_RE = re.compile(r"\b[А-ЯЁ][а-яё]{1,30}\s+[А-ЯЁ]\.[А-ЯЁ]\.\b")


@dataclass(frozen=True, slots=True)
class SpecialistEntry:
    domain: str
    source_url: str
    full_name: str
    role: str
    phone: str
    email: str


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


def element_has_specialist_hint(tag) -> bool:
    class_names = " ".join(tag.get("class", []))
    element_id = tag.get("id", "")
    payload = f"{class_names} {element_id}".lower()
    return any(hint in payload for hint in SPECIALIST_CARD_HINTS)


def is_specialist_page(page) -> bool:
    merged = f"{unquote(page.url)} {page.title}".lower().replace("ё", "е")
    if any(hint in merged for hint in SPECIALIST_PAGE_HINTS):
        return True
    markdown_head = page.markdown[:2500].lower().replace("ё", "е")
    return any(hint in markdown_head for hint in ("наши врачи", "специалисты", "команда врачей"))


def extract_specialists_from_html(page, domain: str) -> list[SpecialistEntry]:
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


def extract_specialists_from_text(page, domain: str) -> list[SpecialistEntry]:
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


def extract_specialists(pages, domain: str) -> list[SpecialistEntry]:
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


def write_specialists_csv(path: Path, specialists) -> None:
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


def specialists_from_pages(site: str, pages) -> list:
    return extract_specialists(pages, domain_slug(site))


async def collect_specialists(site: str, max_pages: int = 40) -> list:
    browser_config = build_browser_config()
    run_config = build_run_config()
    async with create_crawler(browser_config) as crawler:
        pages = await crawl_site_pages(
            crawler=crawler,
            start_url=site,
            max_pages=max(1, max_pages),
            run_config=run_config,
            verbose=False,
            page_concurrency=1,
        )
    return specialists_from_pages(site, pages)


def cli() -> None:
    parser = argparse.ArgumentParser(description="Extract specialists from a site.")
    parser.add_argument("--site", required=True)
    parser.add_argument("--max-pages", type=int, default=40)
    parser.add_argument("--output-csv", required=True)
    args = parser.parse_args()

    specialists = asyncio.run(collect_specialists(args.site, max_pages=max(1, args.max_pages)))
    output_path = Path(args.output_csv)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_specialists_csv(output_path, specialists)
    print(f"Saved {len(specialists)} rows to {output_path}")


if __name__ == "__main__":
    cli()
