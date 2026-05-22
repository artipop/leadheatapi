"""Helpers for HeadHunter OAuth tokens and vacancies API."""

import argparse
import html
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, TypedDict

import requests

# некоторое взято из проекта hh-research https://github.com/hukenovs/hh_research

TOKEN_URL = "https://api.hh.ru/token"
API_BASE_URL = "https://api.hh.ru"
VACANCIES_URL = f"{API_BASE_URL}/vacancies"
PROFESSIONAL_ROLE_SUGGESTS_URL = f"{API_BASE_URL}/suggests/professional_roles"
DEFAULT_USER_AGENT = "hh-research/1.0"
HTML_TAG_PATTERN = re.compile("<.*?>")
WHITESPACE_PATTERN = re.compile(r"\s+")


class HHApiError(Exception):
    """Raised when HeadHunter API returns an unsuccessful response."""

    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        request_id: Optional[str] = None,
        url: Optional[str] = None,
    ):
        self.status_code = status_code
        self.payload = payload
        self.request_id = request_id
        self.url = url
        super().__init__(
            f"HH API request failed: HTTP {status_code}; "
            f"request_id={request_id}; url={url}; response={payload}"
        )


@dataclass(frozen=True)
class VacancySummary:
    id: str
    name: str
    employer: str
    has_salary: bool
    salary_from: Optional[int]
    salary_to: Optional[int]
    experience: str
    schedule: str
    key_skills: list[str]
    description: str


class VacancyTextRow(TypedDict):
    employer: str
    text: str


def _headers(access_token: Optional[str], user_agent: Optional[str] = None) -> dict[str, str]:
    headers = {
        "HH-User-Agent": user_agent or os.getenv("HH_USER_AGENT", DEFAULT_USER_AGENT),
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


def _request_json(
    method: str,
    url: str,
    *,
    access_token: Optional[str],
    user_agent: Optional[str] = None,
    params: Optional[Mapping[str, Any] | list[tuple[str, Any]]] = None,
    data: Optional[Mapping[str, Any]] = None,
    timeout: int = 30,
    session: Any = requests,
) -> dict[str, Any]:
    response = session.request(
        method,
        url,
        params=params,
        data=data,
        headers=_headers(access_token, user_agent),
        timeout=timeout,
    )
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"HH API returned non-JSON response: HTTP {response.status_code}; url={response.url}"
        ) from exc

    if not response.ok:
        request_id = response.headers.get("X-Request-ID")
        if isinstance(payload, dict):
            request_id = payload.get("request_id") or request_id
        raise HHApiError(
            response.status_code,
            payload,
            request_id=request_id,
            url=response.url,
        )

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"HH API returned unexpected JSON payload: {payload!r}; url={response.url}"
        )

    return payload


def _normalize_search_params(params: Optional[Mapping[str, Any]]) -> list[tuple[str, Any]]:
    if not params:
        return []

    normalized: list[tuple[str, Any]] = []
    for key, value in params.items():
        param_key = "professional_role" if key == "professional_roles" else key
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            normalized.extend((param_key, item) for item in value if item is not None)
        else:
            normalized.append((param_key, value))
    return normalized


def fetch_application_token(
    client_id: str,
    client_secret: str,
    user_agent: Optional[str] = None,
    timeout: int = 30,
) -> Dict:
    """Request an application access token from HeadHunter OAuth."""
    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
        headers=_headers(None, user_agent),
        timeout=timeout,
    )
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"HH OAuth returned non-JSON response: HTTP {response.status_code}") from exc

    if not response.ok:
        raise RuntimeError(f"HH OAuth token request failed: HTTP {response.status_code}; response={data}")

    return data


def fetch_vacancies_page(
    access_token: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    professional_roles: Optional[Iterable[int]] = None,
    text: Optional[str] = None,
    page: int = 0,
    per_page: int = 100,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> dict[str, Any]:
    """Fetch one page from the HeadHunter vacancies search endpoint."""
    if page < 0:
        raise ValueError("page must be greater than or equal to 0")
    if per_page < 1 or per_page > 100:
        raise ValueError("per_page must be between 1 and 100")

    search_params: dict[str, Any] = dict(params or {})
    if professional_roles is not None:
        search_params["professional_roles"] = professional_roles
    if text:
        search_params["text"] = text

    query = [
        (key, value)
        for key, value in _normalize_search_params(search_params)
        if key not in {"page", "per_page"}
    ]
    query.extend(
        [
            ("page", page),
            ("per_page", per_page),
        ]
    )
    return _request_json(
        "GET",
        VACANCIES_URL,
        access_token=access_token,
        user_agent=user_agent,
        params=query,
        timeout=timeout,
        session=session,
    )


def iter_vacancy_pages(
    access_token: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    professional_roles: Optional[Iterable[int]] = None,
    text: Optional[str] = None,
    per_page: int = 100,
    max_pages: Optional[int] = None,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> Iterator[dict[str, Any]]:
    """Iterate through pages from the vacancies search endpoint."""
    if max_pages is not None and max_pages <= 0:
        return

    page = 0
    while True:
        payload = fetch_vacancies_page(
            access_token,
            params,
            professional_roles=professional_roles,
            text=text,
            page=page,
            per_page=per_page,
            user_agent=user_agent,
            timeout=timeout,
            session=session,
        )
        yield payload

        total_pages = int(payload.get("pages") or 0)
        if not payload.get("items"):
            break
        if page + 1 >= total_pages:
            break
        if max_pages is not None and page + 1 >= max_pages:
            break
        page += 1


def fetch_all_vacancies(
    access_token: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    professional_roles: Optional[Iterable[int]] = None,
    text: Optional[str] = None,
    per_page: int = 100,
    max_pages: Optional[int] = None,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> list[dict[str, Any]]:
    """Fetch all vacancy search items from available pages."""
    vacancies: list[dict[str, Any]] = []
    for payload in iter_vacancy_pages(
        access_token,
        params,
        professional_roles=professional_roles,
        text=text,
        per_page=per_page,
        max_pages=max_pages,
        user_agent=user_agent,
        timeout=timeout,
        session=session,
    ):
        vacancies.extend(payload.get("items", []))
    return vacancies


def fetch_professional_role_suggestions(
    text: str,
    access_token: Optional[str] = None,
    *,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> dict[str, Any]:
    query = text.strip()
    if not query:
        raise ValueError("text must not be empty")
    return _request_json(
        "GET",
        PROFESSIONAL_ROLE_SUGGESTS_URL,
        access_token=access_token,
        user_agent=user_agent,
        params={"text": query},
        timeout=timeout,
        session=session,
    )


def fetch_vacancy(
    access_token: str,
    vacancy_id: str | int,
    *,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> dict[str, Any]:
    """Fetch full data for one vacancy by ID."""
    return _request_json(
        "GET",
        f"{VACANCIES_URL}/{vacancy_id}",
        access_token=access_token,
        user_agent=user_agent,
        timeout=timeout,
        session=session,
    )


def fetch_vacancy_details(
    access_token: str,
    vacancy_ids: Iterable[str | int],
    *,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> list[dict[str, Any]]:
    """Fetch full data for several vacancies by IDs."""
    return [
        fetch_vacancy(
            access_token,
            vacancy_id,
            user_agent=user_agent,
            timeout=timeout,
            session=session,
        )
        for vacancy_id in vacancy_ids
    ]


def clean_tags(html_text: str) -> str:
    """Remove HTML tags from a vacancy description."""
    text = HTML_TAG_PATTERN.sub(" ", html_text)
    text = html.unescape(text)
    return WHITESPACE_PATTERN.sub(" ", text).strip()


def vacancy_summary(
    vacancy: Mapping[str, Any],
    exchange_rates: Optional[Mapping[str, float]] = None,
) -> VacancySummary:
    """Extract the common research fields from full vacancy JSON."""
    salary = vacancy.get("salary")
    salary_from: Optional[int] = None
    salary_to: Optional[int] = None
    if isinstance(salary, Mapping):
        currency = salary.get("currency")
        rate = exchange_rates.get(currency) if exchange_rates and currency else None
        multiplier = 0.87 if salary.get("gross") else 1
        for source_key, target_key in (("from", "salary_from"), ("to", "salary_to")):
            value = salary.get(source_key)
            if value is None:
                continue
            converted = int(multiplier * value / rate) if rate else int(multiplier * value)
            if target_key == "salary_from":
                salary_from = converted
            else:
                salary_to = converted

    return VacancySummary(
        id=str(vacancy.get("id", "")),
        name=str(vacancy.get("name", "")),
        employer=str((vacancy.get("employer") or {}).get("name", "")),
        has_salary=salary is not None,
        salary_from=salary_from,
        salary_to=salary_to,
        experience=str((vacancy.get("experience") or {}).get("name", "")),
        schedule=str((vacancy.get("schedule") or {}).get("name", "")),
        key_skills=[
            str(skill.get("name", ""))
            for skill in vacancy.get("key_skills", [])
            if isinstance(skill, Mapping)
        ],
        description=clean_tags(str(vacancy.get("description", ""))),
    )


def vacancy_text(vacancy: Mapping[str, Any]) -> VacancyTextRow:
    """Extract only employer and vacancy text from full vacancy JSON."""
    name = str(vacancy.get("name", "")).strip()
    description = clean_tags(str(vacancy.get("description", ""))).strip()
    text_parts = [part for part in (name, description) if part]
    return {
        "employer": str((vacancy.get("employer") or {}).get("name", "")),
        "text": "\n\n".join(text_parts),
    }


def collect_vacancy_texts(
    access_token: str,
    query: Optional[Mapping[str, Any]] = None,
    *,
    professional_roles: Optional[Iterable[int]] = None,
    text: Optional[str] = None,
    per_page: int = 100,
    max_pages: Optional[int] = None,
    user_agent: Optional[str] = None,
    timeout: int = 30,
    session: Any = requests,
) -> list[VacancyTextRow]:
    """Search vacancies and return only employer and cleaned vacancy text."""
    items = fetch_all_vacancies(
        access_token,
        query,
        professional_roles=professional_roles,
        text=text,
        per_page=per_page,
        max_pages=max_pages,
        user_agent=user_agent,
        timeout=timeout,
        session=session,
    )
    vacancy_ids = [item["id"] for item in items if item.get("id")]
    return [
        vacancy_text(vacancy)
        for vacancy in fetch_vacancy_details(
            access_token,
            vacancy_ids,
            user_agent=user_agent,
            timeout=timeout,
            session=session,
        )
    ]


def main():
    parser = argparse.ArgumentParser(description="Get HeadHunter application OAuth token")
    parser.add_argument("--client-id", default=os.getenv("HH_CLIENT_ID"), help="HH application client_id")
    parser.add_argument("--client-secret", default=os.getenv("HH_CLIENT_SECRET"), help="HH application client_secret")
    parser.add_argument("--user-agent", default=os.getenv("HH_USER_AGENT"), help="HH-User-Agent value")
    parser.add_argument("--shell", action="store_true", help="Print shell export command instead of token only")
    parser.add_argument(
        "--professional-role",
        "--professional-roles",
        dest="professional_roles",
        action="append",
        type=int,
        default=None,
        help="HH professional_role filter. Repeat to pass several roles; omitted by default.",
    )
    args = parser.parse_args()

    token = os.getenv("HH_ACCESS_TOKEN")
    if token is None:
        if not args.client_id or not args.client_secret:
            raise SystemExit("Set HH_CLIENT_ID and HH_CLIENT_SECRET or pass --client-id/--client-secret")
        token_data = fetch_application_token(args.client_id, args.client_secret, args.user_agent)
        token = token_data["access_token"]
        if args.shell:
            print(f"export HH_ACCESS_TOKEN='{token}'")
        else:
            print(token)
    vacancies = collect_vacancy_texts(
        token,
        {
            "area": 1,
        },
        text="FPGA",
        professional_roles=args.professional_roles,
        per_page=50,
        max_pages=1,
    )
    print(f"Total vacancies fetched: {len(vacancies)}")
    if vacancies:
        print(vacancies[0])


if __name__ == "__main__":
    main()
