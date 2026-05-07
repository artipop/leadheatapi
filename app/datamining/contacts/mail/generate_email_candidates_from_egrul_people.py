from __future__ import annotations

import argparse
import csv
import itertools
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


INN_RE = re.compile(r"\b\d{10}(?:\d{2})?\b")
EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+\-]*@[a-z0-9.\-]+\.[a-z]{2,}$")

DEFAULT_SITE_MAP_CSVS = [
    "company_channel_inn_map.csv",
    "company_channel_people_review.csv",
    "company_channel_people_review_pairs.csv",
    "egrul_telegram_account_candidates.csv",
    "egrul_telegram_account_candidates_strict.csv",
    "egrul_telegram_account_candidates_with_profile_links.csv",
    "output/legal_info_recheck.csv",
    "output/testtest.csv",
]

DOMAIN_COLUMNS = (
    "company_domain",
    "domain",
    "site_domains",
    "source_site",
    "channel_source_site",
    "site_url",
    "site",
    "start_url",
    "website",
)
INN_COLUMNS = (
    "inn",
    "mapped_inn",
    "channel_mapped_inns",
    "egrul_inn",
)

SKIP_DOMAIN_EXACT = {
    "2gis.ru",
    "api.whatsapp.com",
    "facebook.com",
    "instagram.com",
    "maps.google.com",
    "t.me",
    "telegram.me",
    "vk.com",
    "web.telegram.org",
    "wa.me",
    "www.instagram.com",
    "www.facebook.com",
    "www.youtube.com",
    "youtube.com",
}
SKIP_DOMAIN_SUFFIXES = (
    ".2gis.ru",
    ".facebook.com",
    ".google.com",
    ".instagram.com",
    ".t.me",
    ".telegram.org",
    ".vk.com",
    ".youtube.com",
)
SKIP_PLATFORM_DOMAINS = {
    "taplink.cc",
    "onelink.me",
    "vkvideo.ru",
}

RU_CHAR_VARIANTS: dict[str, tuple[str, ...]] = {
    "а": ("a",),
    "б": ("b",),
    "в": ("v",),
    "г": ("g", "h"),
    "д": ("d",),
    "е": ("e", "ye"),
    "ё": ("e", "yo"),
    "ж": ("zh",),
    "з": ("z",),
    "и": ("i",),
    "й": ("y", "i"),
    "к": ("k",),
    "л": ("l",),
    "м": ("m",),
    "н": ("n",),
    "о": ("o",),
    "п": ("p",),
    "р": ("r",),
    "с": ("s",),
    "т": ("t",),
    "у": ("u",),
    "ф": ("f",),
    "х": ("kh", "h"),
    "ц": ("ts", "c"),
    "ч": ("ch",),
    "ш": ("sh",),
    "щ": ("shch", "sch"),
    "ъ": ("",),
    "ы": ("y", "i"),
    "ь": ("",),
    "э": ("e",),
    "ю": ("yu", "iu"),
    "я": ("ya", "ia"),
}

FIRST_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "александр": ("alexandr", "aleksandr", "alexander"),
    "александра": ("alexandra", "aleksandra"),
    "алексей": ("alexey", "aleksey", "alexei", "aleksei"),
    "алена": ("alena", "alyona"),
    "андрей": ("andrey", "andrei"),
    "анна": ("anna",),
    "антон": ("anton",),
    "артем": ("artem", "artyom"),
    "артём": ("artem", "artyom"),
    "артур": ("artur", "arthur"),
    "вадим": ("vadim",),
    "валентин": ("valentin",),
    "валентина": ("valentina",),
    "валерий": ("valeriy", "valery", "valerii"),
    "вера": ("vera",),
    "виктор": ("viktor", "victor"),
    "виктория": ("viktoria", "victoria"),
    "виталий": ("vitaliy", "vitaly", "vitalii"),
    "владимир": ("vladimir",),
    "вячеслав": ("vyacheslav", "viacheslav"),
    "георгий": ("georgiy", "georgy", "george"),
    "дарья": ("darya", "daria"),
    "денис": ("denis",),
    "дмитрий": ("dmitriy", "dmitry", "dmitrii", "dmitri"),
    "евгений": ("evgeniy", "evgeny", "evgenii", "eugene"),
    "евгения": ("evgeniya", "evgenia"),
    "екатерина": ("ekaterina", "katerina"),
    "елена": ("elena", "yelena"),
    "иван": ("ivan",),
    "игорь": ("igor", "igorj"),
    "илья": ("ilya", "ilia"),
    "ирина": ("irina",),
    "кирилл": ("kirill", "cyrill"),
    "константин": ("konstantin", "constantine"),
    "кристина": ("kristina", "christina"),
    "ксения": ("kseniya", "ksenia", "xenia"),
    "максим": ("maksim", "maxim"),
    "мария": ("mariya", "maria"),
    "михаил": ("mikhail", "mihail", "michael"),
    "наталья": ("natalya", "natalia", "nataliya"),
    "никита": ("nikita",),
    "николай": ("nikolay", "nikolai"),
    "оксана": ("oksana", "oxana"),
    "олег": ("oleg",),
    "ольга": ("olga",),
    "павел": ("pavel", "paul"),
    "петр": ("petr", "pyotr", "peter"),
    "пётр": ("petr", "pyotr", "peter"),
    "роман": ("roman",),
    "светлана": ("svetlana",),
    "сергей": ("sergey", "sergei"),
    "станислав": ("stanislav",),
    "татьяна": ("tatyana", "tatiana"),
    "юлия": ("yuliya", "yulia", "iulia", "julia"),
    "юрий": ("yuriy", "yury", "yuri"),
}

LOCAL_PART_PATTERNS = (
    "first.last",
    "last.first",
    "first_last",
    "last_first",
    "first-last",
    "last-first",
    "firstlast",
    "lastfirst",
    "f.last",
    "last.f",
    "f_last",
    "last_f",
    "f-last",
    "last-f",
    "flast",
    "lastf",
    "first.l",
    "l.first",
    "first_l",
    "l_first",
    "first-l",
    "l-first",
    "firstl",
    "lfirst",
    "first",
    "last",
    "f.l",
    "l.f",
    "fl",
    "lf",
    "first.middle",
    "last.middle",
    "first.m",
    "last.m",
    "f.m.last",
    "last.f.m",
    "fmlast",
    "lastfm",
    "first.last.middle",
    "last.first.middle",
    "first.middle.last",
    "firstlastm",
    "lastfirstm",
)


@dataclass(frozen=True)
class Person:
    inn: str
    company_name: str
    fio: str
    person_type: str


def split_multi_value(value: str) -> list[str]:
    parts = re.split(r"[;\n,|]+", value or "")
    return [part.strip() for part in parts if part.strip()]


def normalize_domain(raw: str) -> str:
    value = (raw or "").strip().strip("()[]{}<>\"'")
    if not value:
        return ""
    if "@" in value and "://" not in value:
        value = value.rsplit("@", 1)[-1]
    if "://" in value:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
    else:
        host = value.split("/", 1)[0].split("?", 1)[0].strip()
        if ":" in host and not host.startswith("["):
            host = host.split(":", 1)[0]
    host = host.lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or any(ch.isspace() for ch in host):
        return ""
    if host in SKIP_DOMAIN_EXACT or host in SKIP_PLATFORM_DOMAINS:
        return ""
    if any(host.endswith(suffix) for suffix in SKIP_DOMAIN_SUFFIXES):
        return ""
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return host


def extract_domains(row: dict[str, str]) -> set[str]:
    domains: set[str] = set()
    for column in DOMAIN_COLUMNS:
        raw = row.get(column) or ""
        for part in split_multi_value(raw):
            domain = normalize_domain(part)
            if domain:
                domains.add(domain)
    return domains


def extract_inns(row: dict[str, str]) -> set[str]:
    inns: set[str] = set()
    for column in INN_COLUMNS:
        raw = row.get(column) or ""
        inns.update(INN_RE.findall(raw))
    return inns


def read_inn_domain_map(paths: list[Path]) -> tuple[dict[str, set[str]], dict[tuple[str, str], set[str]]]:
    by_inn: dict[str, set[str]] = defaultdict(set)
    sources: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                inns = extract_inns(row)
                domains = extract_domains(row)
                if not inns or not domains:
                    continue
                for inn in inns:
                    for domain in domains:
                        by_inn[inn].add(domain)
                        sources[(inn, domain)].add(str(path))
    return by_inn, sources


def read_people(path: Path) -> list[Person]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        people = [
            Person(
                inn=(row.get("inn") or "").strip(),
                company_name=(row.get("company_name") or "").strip(),
                fio=(row.get("person_fio") or "").strip(),
                person_type=(row.get("person_type") or "").strip(),
            )
            for row in reader
        ]
    seen: set[Person] = set()
    unique: list[Person] = []
    for person in people:
        if not person.inn or not person.fio or person in seen:
            continue
        seen.add(person)
        unique.append(person)
    return unique


def clean_name_token(token: str) -> str:
    token = token.strip().lower().replace("ё", "е")
    return re.sub(r"[^а-яa-z-]+", "", token)


def parse_fio(fio: str) -> tuple[str, str, str]:
    parts = [clean_name_token(part) for part in fio.split()]
    parts = [part for part in parts if part]
    if len(parts) < 2:
        return "", "", ""
    last = parts[0]
    first = parts[1]
    middle = parts[2] if len(parts) >= 3 else ""
    return last, first, middle


def transliterate_token(token: str, max_variants: int) -> list[str]:
    token = clean_name_token(token)
    if not token:
        return []
    if re.fullmatch(r"[a-z-]+", token):
        return [token.replace("-", "")]

    variants = [""]
    for ch in token:
        if ch == "-":
            char_variants = ("",)
        else:
            char_variants = RU_CHAR_VARIANTS.get(ch, ("",))
        variants = [prefix + suffix for prefix in variants for suffix in char_variants]
        if len(variants) > max_variants * 4:
            variants = variants[: max_variants * 4]

    out = []
    seen: set[str] = set()
    for item in variants:
        item = re.sub(r"[^a-z0-9]+", "", item.lower())
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= max_variants:
            break
    return out


def name_variants(token: str, *, is_first_name: bool, max_variants: int) -> list[str]:
    cleaned = clean_name_token(token)
    variants: list[str] = []
    if is_first_name:
        variants.extend(FIRST_NAME_ALIASES.get(cleaned, ()))
    variants.extend(transliterate_token(cleaned, max_variants=max_variants))

    out: list[str] = []
    seen: set[str] = set()
    for item in variants:
        item = re.sub(r"[^a-z0-9]+", "", item.lower())
        if item and item not in seen:
            seen.add(item)
            out.append(item)
        if len(out) >= max_variants:
            break
    return out


def render_pattern(pattern: str, *, first: str, last: str, middle: str) -> str:
    values = {
        "first": first,
        "last": last,
        "middle": middle,
        "f": first[:1],
        "l": last[:1],
        "m": middle[:1],
    }
    token_names = sorted(values, key=len, reverse=True)
    rendered = ""
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char in "._+-":
            rendered += char
            index += 1
            continue
        token = next((name for name in token_names if pattern.startswith(name, index)), "")
        if token:
            if not values[token]:
                return ""
            rendered += values[token]
            index += len(token)
        else:
            return ""
    return rendered.strip("._+-")


def build_email_candidates(
    *,
    fio: str,
    domain: str,
    max_name_variants: int,
    patterns: tuple[str, ...],
) -> tuple[list[str], dict[str, str]]:
    last_ru, first_ru, middle_ru = parse_fio(fio)
    if not last_ru or not first_ru:
        return [], {
            "first_name_ru": first_ru,
            "last_name_ru": last_ru,
            "middle_name_ru": middle_ru,
            "first_name_variants_lat": "",
            "last_name_variants_lat": "",
            "middle_name_variants_lat": "",
            "patterns_used": "",
        }

    first_variants = name_variants(first_ru, is_first_name=True, max_variants=max_name_variants)
    last_variants = name_variants(last_ru, is_first_name=False, max_variants=max_name_variants)
    middle_variants = name_variants(middle_ru, is_first_name=False, max_variants=max(2, max_name_variants // 2))
    if not middle_variants:
        middle_variants = [""]

    emails: list[str] = []
    seen: set[str] = set()
    used_patterns: set[str] = set()
    for first, last, middle, pattern in itertools.product(first_variants, last_variants, middle_variants, patterns):
        local = render_pattern(pattern, first=first, last=last, middle=middle)
        if not local:
            continue
        local = re.sub(r"[._+\-]{2,}", ".", local).strip("._+-").lower()
        email = f"{local}@{domain}"
        if not EMAIL_RE.match(email):
            continue
        if email in seen:
            continue
        seen.add(email)
        used_patterns.add(pattern)
        emails.append(email)

    return sorted(emails), {
        "first_name_ru": first_ru.upper(),
        "last_name_ru": last_ru.upper(),
        "middle_name_ru": middle_ru.upper(),
        "first_name_variants_lat": "; ".join(first_variants),
        "last_name_variants_lat": "; ".join(last_variants),
        "middle_name_variants_lat": "; ".join(v for v in middle_variants if v),
        "patterns_used": "; ".join(pattern for pattern in patterns if pattern in used_patterns),
    }


def write_csv(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate corporate email candidates from EGRUL people and INN-to-domain CSV mappings."
    )
    parser.add_argument("--people", default="output/inn_people_from_egrul.csv", help="EGRUL people CSV.")
    parser.add_argument(
        "--site-map-csv",
        action="append",
        default=[],
        help="CSV with INN and site/domain columns. Can be repeated. Defaults to known local mapping files.",
    )
    parser.add_argument(
        "--flat-output",
        default="output/generated_email_candidates_from_egrul_people_flat_extended.csv",
        help="Flat candidate CSV output.",
    )
    parser.add_argument(
        "--summary-output",
        default="output/generated_email_candidates_from_egrul_people_summary_extended.csv",
        help="Per person/domain summary CSV output.",
    )
    parser.add_argument(
        "--unresolved-output",
        default="output/generated_email_candidates_unresolved_companies_extended.csv",
        help="CSV with EGRUL people whose INN has no mapped domain.",
    )
    parser.add_argument(
        "--max-name-variants",
        type=int,
        default=8,
        help="Max transliteration variants per first/last name token.",
    )
    parser.add_argument(
        "--max-candidates-per-person-domain",
        type=int,
        default=0,
        help="Optional cap per person/domain (0 = no cap).",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    people_path = Path(args.people)
    if not people_path.exists():
        raise SystemExit(f"People CSV not found: {people_path}")

    site_map_paths = [Path(path) for path in (args.site_map_csv or DEFAULT_SITE_MAP_CSVS)]
    people = read_people(people_path)
    inn_domain_map, mapping_sources = read_inn_domain_map(site_map_paths)

    flat_rows: list[dict[str, str]] = []
    summary_rows: list[dict[str, str]] = []
    unresolved_rows: list[dict[str, str]] = []
    global_seen: set[tuple[str, str, str, str, str, str]] = set()

    for person in people:
        domains = sorted(inn_domain_map.get(person.inn, set()))
        if not domains:
            unresolved_rows.append(
                {
                    "inn": person.inn,
                    "company_name": person.company_name,
                    "person_fio": person.fio,
                    "person_type": person.person_type,
                    "reason": "no_domain_for_inn",
                }
            )
            continue

        for domain in domains:
            emails, details = build_email_candidates(
                fio=person.fio,
                domain=domain,
                max_name_variants=max(1, args.max_name_variants),
                patterns=LOCAL_PART_PATTERNS,
            )
            if args.max_candidates_per_person_domain > 0:
                emails = emails[: args.max_candidates_per_person_domain]
            if not emails:
                unresolved_rows.append(
                    {
                        "inn": person.inn,
                        "company_name": person.company_name,
                        "person_fio": person.fio,
                        "person_type": person.person_type,
                        "reason": "name_could_not_be_transliterated",
                    }
                )
                continue

            for email in emails:
                key = (domain, person.inn, person.company_name, person.fio, person.person_type, email)
                if key in global_seen:
                    continue
                global_seen.add(key)
                flat_rows.append(
                    {
                        "company_domain": domain,
                        "inn": person.inn,
                        "company_name": person.company_name,
                        "person_fio": person.fio,
                        "person_type": person.person_type,
                        "email_candidate": email,
                    }
                )

            summary_rows.append(
                {
                    "company_domain": domain,
                    "inn": person.inn,
                    "company_name": person.company_name,
                    "person_fio": person.fio,
                    "person_type": person.person_type,
                    **details,
                    "mapping_sources": "; ".join(sorted(mapping_sources.get((person.inn, domain), set()))),
                    "generated_email_count": str(len(emails)),
                    "generated_emails": "; ".join(emails),
                }
            )

    flat_rows.sort(key=lambda row: (row["company_domain"], row["inn"], row["person_fio"], row["email_candidate"]))
    summary_rows.sort(key=lambda row: (row["company_domain"], row["inn"], row["person_fio"]))
    unresolved_rows.sort(key=lambda row: (row["inn"], row["person_fio"]))

    write_csv(
        Path(args.flat_output),
        flat_rows,
        ["company_domain", "inn", "company_name", "person_fio", "person_type", "email_candidate"],
    )
    write_csv(
        Path(args.summary_output),
        summary_rows,
        [
            "company_domain",
            "inn",
            "company_name",
            "person_fio",
            "person_type",
            "first_name_ru",
            "last_name_ru",
            "middle_name_ru",
            "first_name_variants_lat",
            "last_name_variants_lat",
            "middle_name_variants_lat",
            "patterns_used",
            "mapping_sources",
            "generated_email_count",
            "generated_emails",
        ],
    )
    write_csv(
        Path(args.unresolved_output),
        unresolved_rows,
        ["inn", "company_name", "person_fio", "person_type", "reason"],
    )

    mapped_people = len({(row["inn"], row["person_fio"], row["person_type"]) for row in flat_rows})
    mapped_domains = len({row["company_domain"] for row in flat_rows})
    print(f"People loaded: {len(people)}")
    print(f"INNs with domains: {len(inn_domain_map)}")
    print(f"Mapped people: {mapped_people}")
    print(f"Mapped domains: {mapped_domains}")
    print(f"Generated candidates: {len(flat_rows)}")
    print(f"Unresolved people rows: {len(unresolved_rows)}")
    print(f"Saved flat CSV: {args.flat_output}")
    print(f"Saved summary CSV: {args.summary_output}")
    print(f"Saved unresolved CSV: {args.unresolved_output}")


if __name__ == "__main__":
    main()
