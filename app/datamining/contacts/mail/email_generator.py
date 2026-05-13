import itertools
import re
from collections.abc import Sequence
from urllib.parse import urlsplit


EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+\-]*@[a-z0-9.\-]+\.[a-z]{2,}$")

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
    "анастасия": ("anastasia", "anastasiya", "anastasiia"),
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
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def clean_name_token(token: str) -> str:
    token = token.strip().lower().replace("ё", "е")
    return re.sub(r"[^а-яa-z-]+", "", token)


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


def generate_email_candidates(
    domain: str,
    last_name: str,
    first_name: str,
    middle_name: str = "",
    *,
    max_name_variants: int = 8,
    patterns: Sequence[str] = LOCAL_PART_PATTERNS,
) -> list[str]:
    domain = normalize_domain(domain)
    if not domain or not clean_name_token(last_name) or not clean_name_token(first_name):
        return []

    max_name_variants = max(1, max_name_variants)
    first_variants = name_variants(first_name, is_first_name=True, max_variants=max_name_variants)
    last_variants = name_variants(last_name, is_first_name=False, max_variants=max_name_variants)
    middle_variants = name_variants(
        middle_name,
        is_first_name=False,
        max_variants=max(2, max_name_variants // 2),
    )
    if not middle_variants:
        middle_variants = [""]

    emails: list[str] = []
    seen: set[str] = set()
    for first, last, middle, pattern in itertools.product(
        first_variants,
        last_variants,
        middle_variants,
        patterns,
    ):
        local = render_pattern(pattern, first=first, last=last, middle=middle)
        if not local:
            continue
        local = re.sub(r"[._+\-]{2,}", ".", local).strip("._+-").lower()
        email = f"{local}@{domain}"
        if EMAIL_RE.match(email) and email not in seen:
            seen.add(email)
            emails.append(email)

    return sorted(emails)
