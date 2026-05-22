import re
from dataclasses import dataclass
from typing import TypedDict
from urllib.parse import unquote

from bs4 import BeautifulSoup

from app.datamining.contracts import SiteScrapeConfig
from app.datamining.contracts import WriteRepository
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


class SpecialistRow(TypedDict):
    domain: str
    source_url: str
    full_name: str
    role: str
    phone: str
    email: str


@dataclass(frozen=True, slots=True)
class SpecialistsScraperConfig(SiteScrapeConfig):
    pass


class SpecialistsScraper:
    def __init__(
        self,
        config: SpecialistsScraperConfig | None = None,
        repository: WriteRepository[SpecialistRow] | None = None,
    ) -> None:
        self.config = config or SpecialistsScraperConfig()
        self.repository = repository

    async def scrape(self, site: str) -> list[SpecialistRow]:
        pages = await crawl_pages(site, self.config)
        items = specialist_rows_from_pages(site, pages)
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


def specialists_from_pages(site: str, pages) -> list[SpecialistEntry]:
    return extract_specialists(pages, domain_slug(site))


def specialist_entry_to_row(item: SpecialistEntry) -> SpecialistRow:
    return {
        "domain": item.domain,
        "source_url": item.source_url,
        "full_name": item.full_name,
        "role": item.role,
        "phone": item.phone,
        "email": item.email,
    }


def specialist_rows_from_pages(site: str, pages) -> list[SpecialistRow]:
    return [specialist_entry_to_row(item) for item in specialists_from_pages(site, pages)]


async def collect_specialists(site: str, max_pages: int = 40) -> list[SpecialistRow]:
    scraper = SpecialistsScraper(SpecialistsScraperConfig(max_pages=max_pages))
    return await scraper.scrape(site)
