from __future__ import annotations

import argparse
import asyncio
import csv
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import AsyncSessionLocal
from app.models import Company, CompanySocialProfile, Pricing, Specialist


CONTACT_FILES = ("contacts.csv", "pricing.csv", "specialists.csv")
SOCIAL_FILES = (
    "dzen.csv",
    "hh_links_review.csv",
    "max_channels.csv",
    "rutube.csv",
    "telegram.csv",
    "vk.csv",
)


def resolve_social_csv_path(output_dir: Path, filename: str) -> Path:
    primary = output_dir / filename
    if primary.exists():
        return primary
    fallback = output_dir / "000_socials" / filename
    return fallback


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def split_semicolon_values(raw: str) -> list[str]:
    values = []
    for part in (raw or "").split(";"):
        value = part.strip()
        if value:
            values.append(value)
    return values


def merge_unique(values: list[str]) -> str:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return "; ".join(result)


def normalize_domain(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""

    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    host = host.rstrip(".")

    try:
        host = host.encode("ascii").decode("idna")
    except UnicodeError:
        pass
    return host


def to_idna_ascii(host: str) -> str:
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def company_domain_keys(domain: str) -> set[str]:
    normalized = normalize_domain(domain)
    if not normalized:
        return set()
    return {normalized, to_idna_ascii(normalized)}


def parse_int_or_none(raw: str) -> Optional[int]:
    digits = re.sub(r"[^\d]", "", raw or "")
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def first_non_empty(rows: list[dict[str, str]], field: str) -> Optional[str]:
    for row in rows:
        value = (row.get(field) or "").strip()
        if value:
            return value
    return None


def merge_contact_field(rows: list[dict[str, str]], field: str) -> Optional[str]:
    values: list[str] = []
    for row in rows:
        raw = (row.get(field) or "").strip()
        if not raw:
            continue
        values.extend(split_semicolon_values(raw))
    merged = merge_unique(values)
    return merged or None


def detect_social_field(url: str, explicit_field: Optional[str] = None) -> Optional[str]:
    if explicit_field:
        return explicit_field

    host = normalize_domain(url)
    if not host:
        return None
    if host == "hh.ru" or host.endswith(".hh.ru"):
        return "hh_profile"
    if host in {"t.me", "telegram.me"}:
        return "tg_profile"
    if host == "max.ru" or host.endswith(".max.ru"):
        return "max_profile"
    if host == "vk.com" or host.endswith(".vk.com"):
        return "vk_profile"
    if host == "dzen.ru" or host.endswith(".dzen.ru") or host == "zen.yandex.ru":
        return "dzen_profile"
    if host == "rutube.ru" or host.endswith(".rutube.ru"):
        return "rutube_profile"
    if host == "instagram.com" or host.endswith(".instagram.com"):
        return "instagram_profile"
    if host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com"):
        return "youtube_profile"
    return None


def should_skip_obvious_share_link(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if host == "drive2.ru" or host.endswith(".drive2.ru"):
        return True
    if host in {"t.me", "telegram.me"} and path.startswith("/share"):
        return True
    if host == "vk.com" and path.startswith("/share"):
        return True
    if host in {"x.com", "twitter.com"} and path.startswith("/intent"):
        return True
    return False


async def load_existing_company_index(session: AsyncSession) -> dict[str, Company]:
    result = await session.execute(select(Company))
    companies = result.scalars().all()
    index: dict[str, Company] = {}
    for company in companies:
        for key in company_domain_keys(company.domain):
            index[key] = company
    return index


async def get_or_create_company(session: AsyncSession, index: dict[str, Company], domain: str) -> Company:
    keys = company_domain_keys(domain)
    for key in keys:
        company = index.get(key)
        if company is not None:
            return company

    company = Company(domain=domain)
    session.add(company)
    await session.flush()
    for key in keys:
        index[key] = company
    return company


async def import_company_folder(session: AsyncSession, index: dict[str, Company], folder: Path) -> dict[str, int]:
    contacts_path = folder / "contacts.csv"
    pricing_path = folder / "pricing.csv"
    specialists_path = folder / "specialists.csv"
    if not any(path.exists() for path in (contacts_path, pricing_path, specialists_path)):
        return {"companies": 0, "pricing": 0, "specialists": 0}

    domain = folder.name
    company = await get_or_create_company(session, index=index, domain=domain)

    contacts_rows = read_csv_rows(contacts_path)
    if contacts_rows:
        company.source_url = first_non_empty(contacts_rows, "source_url")
        company.source_scope = first_non_empty(contacts_rows, "source_scope")
        company.inn = merge_contact_field(contacts_rows, "inn")
        company.ogrn = merge_contact_field(contacts_rows, "ogrn")
        company.ogrnip = merge_contact_field(contacts_rows, "ogrnip")
        company.phones = merge_contact_field(contacts_rows, "phones")
        company.emails = merge_contact_field(contacts_rows, "emails")
        await session.flush()

    pricing_count = 0
    await session.execute(delete(Pricing).where(Pricing.company_id == company.id))
    for row in read_csv_rows(pricing_path):
        source_url = (row.get("source_url") or "").strip()
        service = (row.get("service") or "").strip()
        if not source_url and not service:
            continue
        pricing_count += 1
        session.add(
            Pricing(
                company_id=company.id,
                source_url=source_url,
                service=service,
                price_raw=(row.get("price_raw") or "").strip() or None,
                price_min=parse_int_or_none((row.get("price_min") or "").strip()),
                price_max=parse_int_or_none((row.get("price_max") or "").strip()),
                currency=(row.get("currency") or "").strip() or None,
            )
        )

    specialists_count = 0
    await session.execute(delete(Specialist).where(Specialist.company_id == company.id))
    for row in read_csv_rows(specialists_path):
        source_url = (row.get("source_url") or "").strip()
        full_name = (row.get("full_name") or "").strip()
        if not source_url and not full_name:
            continue
        specialists_count += 1
        session.add(
            Specialist(
                company_id=company.id,
                source_url=source_url,
                full_name=full_name,
                role=(row.get("role") or "").strip() or None,
                phone=(row.get("phone") or "").strip() or None,
                email=(row.get("email") or "").strip() or None,
            )
        )

    return {"companies": 1, "pricing": pricing_count, "specialists": specialists_count}


async def import_social_profiles(session: AsyncSession, output_dir: Path, index: dict[str, Company]) -> dict[str, int]:
    await session.execute(delete(CompanySocialProfile))

    inserted = 0
    skipped_no_company = 0
    skipped_unknown_type = 0
    skipped_invalid = 0
    dedup: set[tuple[int, str, str]] = set()

    def add_profile(company_id: int, field: str, url: str) -> None:
        nonlocal inserted
        key = (company_id, field, url)
        if key in dedup:
            return
        dedup.add(key)
        kwargs = {
            "hh_profile": None,
            "tg_profile": None,
            "max_profile": None,
            "vk_profile": None,
            "dzen_profile": None,
            "rutube_profile": None,
            "instagram_profile": None,
            "youtube_profile": None,
        }
        kwargs[field] = url
        session.add(CompanySocialProfile(company_id=company_id, **kwargs))
        inserted += 1

    def process_rows(path: Path, profile_column: str, explicit_field: Optional[str]) -> None:
        nonlocal skipped_no_company, skipped_unknown_type, skipped_invalid

        for row in read_csv_rows(path):
            site_url = (row.get("site_url") or "").strip()
            profile_url = (row.get(profile_column) or "").strip()
            if not site_url or not profile_url:
                skipped_invalid += 1
                continue
            if should_skip_obvious_share_link(profile_url):
                skipped_invalid += 1
                continue

            domain = normalize_domain(site_url)
            if not domain:
                skipped_invalid += 1
                continue

            company = None
            for key in company_domain_keys(domain):
                company = index.get(key)
                if company is not None:
                    break
            if company is None:
                skipped_no_company += 1
                continue

            field = detect_social_field(profile_url, explicit_field=explicit_field)
            if field is None:
                skipped_unknown_type += 1
                continue
            add_profile(company.id, field, profile_url)

    process_rows(resolve_social_csv_path(output_dir, "dzen.csv"), "dzen_url", "dzen_profile")
    process_rows(resolve_social_csv_path(output_dir, "hh_links_review.csv"), "hh_employer_url", "hh_profile")
    process_rows(resolve_social_csv_path(output_dir, "max_channels.csv"), "max_channel_url", "max_profile")
    process_rows(resolve_social_csv_path(output_dir, "telegram.csv"), "telegram_url", "tg_profile")
    process_rows(resolve_social_csv_path(output_dir, "rutube.csv"), "link_url", None)
    process_rows(resolve_social_csv_path(output_dir, "vk.csv"), "link_url", None)

    return {
        "social_inserted": inserted,
        "social_skipped_no_company": skipped_no_company,
        "social_skipped_unknown_type": skipped_unknown_type,
        "social_skipped_invalid": skipped_invalid,
    }


async def run_import(output_dir: Path, skip_social: bool) -> None:
    folder_stats = {"companies": 0, "pricing": 0, "specialists": 0}

    async with AsyncSessionLocal() as session:
        domain_index = await load_existing_company_index(session)

        folders = sorted([path for path in output_dir.iterdir() if path.is_dir()])
        for folder in folders:
            if folder.name.startswith("_"):
                continue
            if any(ch in folder.name for ch in ["/", "\\"]):
                continue
            stats = await import_company_folder(session, index=domain_index, folder=folder)
            folder_stats["companies"] += stats["companies"]
            folder_stats["pricing"] += stats["pricing"]
            folder_stats["specialists"] += stats["specialists"]
        await session.commit()

        social_stats = {
            "social_inserted": 0,
            "social_skipped_no_company": 0,
            "social_skipped_unknown_type": 0,
            "social_skipped_invalid": 0,
        }
        if not skip_social:
            social_stats = await import_social_profiles(session, output_dir=output_dir, index=domain_index)
            await session.commit()

    print("Import finished.")
    print(f"Companies upserted: {folder_stats['companies']}")
    print(f"Pricing rows imported: {folder_stats['pricing']}")
    print(f"Specialists rows imported: {folder_stats['specialists']}")
    if not skip_social:
        print(f"Social rows imported: {social_stats['social_inserted']}")
        print(f"Social skipped (no company): {social_stats['social_skipped_no_company']}")
        print(f"Social skipped (unknown type): {social_stats['social_skipped_unknown_type']}")
        print(f"Social skipped (invalid/share/empty): {social_stats['social_skipped_invalid']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import output CSV files into database.")
    parser.add_argument("--output-dir", default="output", help="Directory with company folders and social CSV files.")
    parser.add_argument("--skip-social", action="store_true", help="Skip import of social profile CSV files.")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        raise SystemExit(f"Output directory not found: {output_dir}")

    missing_social = [name for name in SOCIAL_FILES if not resolve_social_csv_path(output_dir, name).exists()]
    if not args.skip_social and missing_social:
        print(f"Warning: missing social CSV files: {', '.join(missing_social)}")

    asyncio.run(run_import(output_dir=output_dir, skip_social=args.skip_social))


if __name__ == "__main__":
    main()
