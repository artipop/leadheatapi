#!/usr/bin/env python3
"""
whois_lookup.py — определяет тип владельца домена (юр. или физ. лицо)
по данным WHOIS.

Использование:
    python whois_lookup.py example.com
    python whois_lookup.py example.com google.com github.io
    python whois_lookup.py -f domains.txt
    python whois_lookup.py --json example.com
"""

import sys
import re
import csv
import socket
import argparse
import json
from datetime import datetime

# ─── попытка импортировать python-whois ───────────────────────────────────────
try:
    import whois as whois_lib
    WHOIS_LIB = "python-whois"
except ImportError:
    whois_lib = None
    WHOIS_LIB = None

# ─── ANSI-цвета ───────────────────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"

# ─── Ключевые слова, характерные для юридических лиц ─────────────────────────
LEGAL_KEYWORDS = [
    # EN
    "llc", "ltd", "inc", "corp", "corporation", "limited", "gmbh", "s.a.",
    "s.r.o.", "bv", "nv", "ag", "plc", "llp", "lp", "co.", "company",
    "group", "holding", "holdings", "enterprises", "solutions", "technologies",
    "services", "systems", "software", "networks", "media", "communications",
    "bank", "financial", "insurance", "foundation", "association", "institute",
    "university", "college", "school", "hospital", "clinic", "government",
    # RU / CIS
    "ооо", "оао", "зао", "пао", "ао", "ип", "нко", "фгуп", "гуп", "муп",
    "тов", "тдв", "пат", "пбоюл", "пк", "снт", "нпо", "нии",
    "фонд", "банк", "страховая", "страховое", "холдинг",
    # Маркеры privacy-proxy (скрывает реального владельца)
    "privacy", "redacted", "withheld", "protected", "proxy", "whoisguard",
    "domainprotect", "contactprivacy", "anonymize", "private registration",
    "data protected",
]

PERSON_PATTERNS = [
    # Типичная структура «Имя Фамилия» или «Фамилия Имя»
    r"^[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+$",
    r"^[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+\s+[A-ZА-ЯЁ][a-zа-яё]+$",
]


# ─── Определение типа владельца ───────────────────────────────────────────────
def classify_owner(registrant: str | None, org: str | None, raw_text: str = "") -> dict:
    """
    Возвращает dict:
        type        : "legal" | "person" | "privacy" | "unknown"
        confidence  : "high" | "medium" | "low"
        reason      : str
        matched     : str | None  (какое ключевое слово/поле сработало)
    """
    combined = " ".join(filter(None, [registrant, org, raw_text])).lower()

    # 1. Privacy / редактированные данные
    privacy_words = ["privacy", "redacted", "withheld", "protected", "proxy",
                     "whoisguard", "domainprotect", "contactprivacy", "anonymize"]
    for pw in privacy_words:
        if pw in combined:
            return {"type": "privacy", "confidence": "high",
                    "reason": "Данные скрыты privacy-сервисом", "matched": pw}

    # 2. По полю org
    if org:
        org_clean = org.strip()
        org_lower = org_clean.lower()
        for kw in LEGAL_KEYWORDS:
            if kw in org_lower:
                return {"type": "legal", "confidence": "high",
                        "reason": f"Поле org содержит признак юр. лица",
                        "matched": f"org='{org_clean}' кл.слово='{kw}'"}
        # Org заполнено, но ключевых слов нет — скорее всего юр. лицо
        return {"type": "legal", "confidence": "medium",
                "reason": "Поле org заполнено (без явных ключевых слов)",
                "matched": f"org='{org_clean}'"}

    # 3. По полю registrant / name
    if registrant:
        reg_clean = registrant.strip()
        reg_lower = reg_clean.lower()
        for kw in LEGAL_KEYWORDS:
            if kw in reg_lower:
                return {"type": "legal", "confidence": "high",
                        "reason": "Имя регистранта содержит признак юр. лица",
                        "matched": f"registrant='{reg_clean}' кл.слово='{kw}'"}
        # Проверяем паттерн физ. лица
        for pat in PERSON_PATTERNS:
            if re.match(pat, reg_clean, re.IGNORECASE):
                return {"type": "person", "confidence": "high",
                        "reason": "Имя соответствует шаблону ФИО физ. лица",
                        "matched": f"registrant='{reg_clean}'"}
        # Не похоже ни на то, ни на другое
        return {"type": "person", "confidence": "low",
                "reason": "Поле org пусто, имя не опознано как юр. лицо",
                "matched": f"registrant='{reg_clean}'"}

    return {"type": "unknown", "confidence": "low",
            "reason": "Нет данных о регистранте", "matched": None}


# ─── Извлечение ИНН/code из сырого WHOIS ─────────────────────────────────────
_CODE_RE = re.compile(
    r"(?:taxpayer-id|org-inn|inn|tax-id|taxid|vat-id|vat_id|code)\s*[:\s]+\s*(\d{10,12})",
    re.IGNORECASE,
)

def _extract_code(raw: str) -> str | None:
    m = _CODE_RE.search(raw)
    return m.group(1) if m else None


_TCINET_TLDS = {".ru", ".рф", ".su"}

def _fetch_tcinet_raw(domain: str) -> str:
    """Прямой WHOIS-запрос к whois.tcinet.ru:43 (без доп. зависимостей)."""
    try:
        with socket.create_connection(("whois.tcinet.ru", 43), timeout=10) as s:
            s.sendall(f"{domain}\r\n".encode())
            chunks: list[bytes] = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ─── WHOIS-запрос ─────────────────────────────────────────────────────────────
def do_whois(domain: str) -> dict:
    """Делает WHOIS-запрос и возвращает нормализованный словарь."""
    result = {
        "domain": domain,
        "registrant": None,
        "org": None,
        "code": None,
        "registrar": None,
        "creation_date": None,
        "expiration_date": None,
        "emails": [],
        "name_servers": [],
        "raw": "",
        "error": None,
    }

    if whois_lib is None:
        result["error"] = "Библиотека python-whois не установлена. Запустите: pip install python-whois"
        return result

    try:
        w = whois_lib.whois(domain)
        result["raw"] = str(w) if w else ""

        def first(val):
            if isinstance(val, list):
                return val[0] if val else None
            return val

        result["registrant"]     = first(getattr(w, "name", None)) or first(getattr(w, "registrant_name", None))
        result["org"]            = first(getattr(w, "org", None))
        tld = "." + domain.rsplit(".", 1)[-1].lower()
        if tld in _TCINET_TLDS:
            code = _extract_code(_fetch_tcinet_raw(domain))
        else:
            code = _extract_code(result["raw"])
        result["code"] = code
        result["registrar"]      = first(getattr(w, "registrar", None))
        result["creation_date"]  = first(getattr(w, "creation_date", None))
        result["expiration_date"]= first(getattr(w, "expiration_date", None))
        result["emails"]         = list({e for e in (getattr(w, "emails", None) or []) if e})
        ns = getattr(w, "name_servers", None) or []
        result["name_servers"]   = sorted({n.lower() for n in ns if n})[:4]

    except whois_lib.exceptions.WhoisError as e:
        result["error"] = f"WHOIS ошибка: {e}"
    except socket.gaierror:
        result["error"] = "Нет сети или домен не существует"
    except Exception as e:
        result["error"] = f"Ошибка: {e}"

    return result


# ─── Форматирование вывода ────────────────────────────────────────────────────
TYPE_ICONS = {
    "legal":   ("🏢", C.BLUE,   "Юридическое лицо"),
    "person":  ("👤", C.GREEN,  "Физическое лицо"),
    "privacy": ("🔒", C.YELLOW, "Данные скрыты"),
    "unknown": ("❓", C.GRAY,   "Неизвестно"),
}

CONF_COLORS = {
    "high":   C.GREEN,
    "medium": C.YELLOW,
    "low":    C.RED,
}


def fmt_date(d) -> str:
    if d is None:
        return "—"
    if isinstance(d, datetime):
        return d.strftime("%Y-%m-%d")
    return str(d)[:10]


def print_result(info: dict, classification: dict, no_color: bool = False):
    def c(color, text):
        return text if no_color else f"{color}{text}{C.RESET}"

    icon, color, label = TYPE_ICONS.get(classification["type"], ("?", C.GRAY, "?"))
    conf_c = CONF_COLORS.get(classification["confidence"], C.GRAY)

    print(f"\n{c(C.BOLD + C.WHITE, '━' * 56)}")
    print(f"  {c(C.BOLD, info['domain'])}")
    print(f"{c(C.BOLD + C.WHITE, '━' * 56)}")

    if info["error"]:
        print(f"  {c(C.RED, '✗')} {info['error']}")
        return

    # Тип владельца
    print(f"  {icon}  {c(color + C.BOLD, label)}"
          f"  {c(conf_c, '[' + classification['confidence'] + ']')}")
    print(f"  {c(C.DIM, classification['reason'])}")
    if classification["matched"]:
        print(f"  {c(C.DIM, '→ ' + classification['matched'])}")

    print()

    # Детали
    rows = [
        ("Регистрант",  info["registrant"]   or "—"),
        ("Организация", info["org"]          or "—"),
        ("ИНН/code",    info["code"]         or "—"),
        ("Регистратор", info["registrar"]    or "—"),
        ("Создан",      fmt_date(info["creation_date"])),
        ("Истекает",    fmt_date(info["expiration_date"])),
    ]
    for label_r, value in rows:
        print(f"  {c(C.GRAY, label_r + ':'): <24}{value}")

    if info["emails"]:
        print(f"  {c(C.GRAY, 'Email:'): <24}{', '.join(info['emails'][:3])}")
    if info["name_servers"]:
        print(f"  {c(C.GRAY, 'NS:'): <24}{', '.join(info['name_servers'])}")


# ─── CSV ──────────────────────────────────────────────────────────────────────
CSV_FIELDS = [
    "domain", "error", "owner_type", "owner_label", "confidence",
    "registrant", "org", "code", "registrar",
    "creation_date", "expiration_date", "emails", "name_servers",
]

def to_csv_row(info: dict, classification: dict) -> dict:
    return {
        "domain":          info["domain"],
        "error":           info["error"] or "",
        "owner_type":      classification["type"],
        "owner_label":     TYPE_ICONS[classification["type"]][2],
        "confidence":      classification["confidence"],
        "registrant":      info["registrant"] or "",
        "org":             info["org"] or "",
        "code":            info["code"] or "",
        "registrar":       info["registrar"] or "",
        "creation_date":   fmt_date(info["creation_date"]),
        "expiration_date": fmt_date(info["expiration_date"]),
        "emails":          "; ".join(info["emails"]),
        "name_servers":    "; ".join(info["name_servers"]),
    }

def write_csv(rows: list[dict], path: str):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{C.GREEN}✓ CSV сохранён: {path}{C.RESET}")


# ─── Вывод JSON ───────────────────────────────────────────────────────────────
def to_json_output(info: dict, classification: dict) -> dict:
    return {
        "domain":         info["domain"],
        "error":          info["error"],
        "owner_type":     classification["type"],
        "owner_label":    TYPE_ICONS[classification["type"]][2],
        "confidence":     classification["confidence"],
        "reason":         classification["reason"],
        "matched":        classification["matched"],
        "registrant":     info["registrant"],
        "org":            info["org"],
        "code":           info["code"],
        "registrar":      info["registrar"],
        "creation_date":  fmt_date(info["creation_date"]),
        "expiration_date":fmt_date(info["expiration_date"]),
        "emails":         info["emails"],
        "name_servers":   info["name_servers"],
    }


# ─── CLI ──────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser(
        description="Определяет тип владельца домена (юр./физ. лицо) по WHOIS",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Примеры:
  python whois_lookup.py google.com
  python whois_lookup.py google.com github.com wikipedia.org
  python whois_lookup.py -f domains.txt
  python whois_lookup.py --json google.com
  python whois_lookup.py --csv out.csv -f domains.txt
  python whois_lookup.py --no-color example.com
""",
    )
    p.add_argument("domains", nargs="*", help="Домены для проверки")
    p.add_argument("-f", "--file", help="Файл со списком доменов (по одному на строку)")
    p.add_argument("--json", action="store_true", help="Вывод в формате JSON")
    p.add_argument("--csv", metavar="FILE", help="Сохранить результат в CSV-файл")
    p.add_argument("--no-color", action="store_true", help="Отключить цвета")
    return p.parse_args()


def check_deps():
    if whois_lib is None:
        print(f"{C.YELLOW}⚠  Библиотека python-whois не найдена.{C.RESET}")
        print(f"   Установите: {C.CYAN}pip install python-whois{C.RESET}\n")
        sys.exit(1)


def main():
    args = parse_args()

    domains = list(args.domains)
    if args.file:
        try:
            with open(args.file) as f:
                domains += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        except FileNotFoundError:
            print(f"{C.RED}Файл не найден: {args.file}{C.RESET}")
            sys.exit(1)

    if not domains:
        print(f"{C.YELLOW}Укажите хотя бы один домен.{C.RESET}")
        print("Пример: python whois_lookup.py google.com")
        sys.exit(0)

    check_deps()

    silent = bool(args.csv) and not args.json
    if not args.json and not silent:
        print(f"\n{C.BOLD}{C.CYAN}  WHOIS Domain Owner Classifier{C.RESET}")
        print(f"  {C.DIM}python-whois · {datetime.now():%Y-%m-%d %H:%M}{C.RESET}")

    json_results = []
    csv_rows = []

    for domain in domains:
        domain = domain.strip().lower()
        # Убираем протокол, если вдруг передали URL
        domain = re.sub(r"^https?://", "", domain).split("/")[0]

        info = do_whois(domain)
        classification = classify_owner(
            info["registrant"], info["org"], info["raw"]
        )

        if args.json:
            json_results.append(to_json_output(info, classification))
        else:
            if not silent:
                print_result(info, classification, no_color=args.no_color)
        if args.csv:
            csv_rows.append(to_csv_row(info, classification))

    if args.json:
        print(json.dumps(json_results if len(json_results) > 1 else json_results[0],
                         ensure_ascii=False, indent=2))
    elif not args.no_color and not silent:
        print(f"\n{C.BOLD}{C.WHITE}{'━' * 56}{C.RESET}\n")

    if args.csv:
        write_csv(csv_rows, args.csv)


if __name__ == "__main__":
    main()
