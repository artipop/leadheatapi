import csv
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

PRICING_FIELDNAMES = ("domain", "source_url", "service", "price_raw", "price_min", "price_max", "currency")
SPECIALISTS_FIELDNAMES = ("domain", "source_url", "full_name", "role", "phone", "email")
CONTACTS_FIELDNAMES = ("domain", "source_url", "source_scope", "inn", "ogrn", "ogrnip", "phones", "emails")
BOOKINGS_FIELDNAMES = (
    "site",
    "booking_mode",
    "has_booking_form",
    "has_booking_widget",
    "booking_widget_provider",
    "booking_widget_host",
    "booking_evidence",
)


class CsvWriteRepository:
    def __init__(
        self,
        path: str | Path,
        fieldnames: Sequence[str],
        row_factory: Callable[[object], dict[str, str]] | None = None,
    ) -> None:
        self.path = Path(path)
        self.fieldnames = tuple(fieldnames)
        self.row_factory = row_factory

    def save_many(self, items: Sequence[object]) -> None:
        self._write(items, append=False)

    def add_many(self, items: Sequence[object]) -> None:
        self._write(items, append=True)

    def _write(self, items: Sequence[object], append: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        file_exists = self.path.exists()
        mode = "a" if append else "w"
        with self.path.open(mode, encoding="utf-8", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=self.fieldnames)
            if not append or not file_exists:
                writer.writeheader()
            for item in items:
                row = self._to_row(item)
                writer.writerow({name: row.get(name, "") for name in self.fieldnames})

    def _to_row(self, item: object) -> dict[str, str]:
        if self.row_factory:
            return self.row_factory(item)
        if is_dataclass(item):
            return asdict(item)
        return dict(item)


class CsvSiteInnRepository:
    def __init__(
        self,
        path: str | Path,
        site_columns: Sequence[str] = ("site", "site_url", "website", "domain", "url"),
        inn_columns: Sequence[str] = ("inn", "ИНН", "company_inn"),
    ) -> None:
        self.path = Path(path)
        self.site_columns = tuple(site_columns)
        self.inn_columns = tuple(inn_columns)
        self._by_site = self._load()

    def get_inn_by_site(self, site: str) -> str:
        return self._by_site.get(site_key(site), "")

    def _load(self) -> dict[str, str]:
        result: dict[str, str] = {}
        if not self.path.exists():
            return result

        with self.path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            fieldnames = list(reader.fieldnames or [])
            site_column = first_existing_column(fieldnames, self.site_columns)
            inn_column = first_existing_column(fieldnames, self.inn_columns)
            if not site_column or not inn_column:
                return result
            for row in reader:
                inn = normalize_inn(row.get(inn_column) or "")
                if not inn:
                    continue
                for raw_site in split_values(row.get(site_column) or ""):
                    key = site_key(raw_site)
                    if key and key not in result:
                        result[key] = inn
        return result


class EmptySiteInnRepository:
    def get_inn_by_site(self, site: str) -> str:
        return ""


def first_existing_column(fieldnames: Sequence[str], candidates: Sequence[str]) -> str:
    lowered = {name.lower(): name for name in fieldnames}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return ""


def split_values(raw: str) -> list[str]:
    values: list[str] = []
    for part in raw.replace(",", ";").split(";"):
        value = part.strip()
        if value:
            values.append(value)
    return values


def normalize_inn(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) in {10, 12}:
        return digits
    return ""


def site_key(site: str) -> str:
    normalized = normalize_site(site)
    if "://" not in normalized:
        return normalized
    return normalized.split("://", 1)[1].split("/", 1)[0]


def normalize_site(site: str) -> str:
    value = site.strip()
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return urlunsplit(("https", host, "/", "", ""))


def price_entry_to_row(item: object) -> dict[str, str]:
    return {
        "domain": getattr(item, "domain", ""),
        "source_url": getattr(item, "source_url", ""),
        "service": getattr(item, "service", ""),
        "price_raw": getattr(item, "price_raw", ""),
        "price_min": getattr(item, "price_min", ""),
        "price_max": getattr(item, "price_max", ""),
        "currency": getattr(item, "currency", ""),
    }


def specialist_entry_to_row(item: object) -> dict[str, str]:
    return {
        "domain": getattr(item, "domain", ""),
        "source_url": getattr(item, "source_url", ""),
        "full_name": getattr(item, "full_name", ""),
        "role": getattr(item, "role", ""),
        "phone": getattr(item, "phone", ""),
        "email": getattr(item, "email", ""),
    }


def contact_entry_to_row(item: object) -> dict[str, str]:
    return {
        "domain": getattr(item, "domain", ""),
        "source_url": getattr(item, "source_url", ""),
        "source_scope": getattr(item, "source_scope", ""),
        "inn": getattr(item, "inn", ""),
        "ogrn": getattr(item, "ogrn", ""),
        "ogrnip": getattr(item, "ogrnip", ""),
        "phones": getattr(item, "phones", ""),
        "emails": getattr(item, "emails", ""),
    }
