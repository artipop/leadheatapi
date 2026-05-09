import unittest

from app.datamining.api.hh_api import (
    collect_vacancy_texts,
    fetch_all_vacancies,
    fetch_vacancy,
    fetch_vacancies_page,
    vacancy_summary,
    vacancy_text,
)


class FakeResponse:
    def __init__(self, payload, *, ok=True, status_code=200, url="https://api.hh.ru/test"):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.url = url
        self.headers = {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError("No fake response left")
        return self.responses.pop(0)


class HHApiTest(unittest.TestCase):
    def test_fetch_vacancies_page_passes_token_and_repeated_params(self) -> None:
        session = FakeSession([FakeResponse({"items": [], "pages": 1})])

        payload = fetch_vacancies_page(
            "token-123",
            {"text": "python", "professional_roles": [96, 156], "area": None},
            page=2,
            per_page=50,
            user_agent="leadheat-test",
            session=session,
        )

        self.assertEqual(payload, {"items": [], "pages": 1})
        method, url, kwargs = session.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://api.hh.ru/vacancies")
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer token-123")
        self.assertEqual(kwargs["headers"]["HH-User-Agent"], "leadheat-test")
        self.assertEqual(
            kwargs["params"],
            [
                ("text", "python"),
                ("professional_role", 96),
                ("professional_role", 156),
                ("page", 2),
                ("per_page", 50),
            ],
        )

    def test_fetch_all_vacancies_collects_pages(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"items": [{"id": "1"}], "pages": 2}),
                FakeResponse({"items": [{"id": "2"}], "pages": 2}),
            ]
        )

        self.assertEqual(
            fetch_all_vacancies("token", {"text": "sales"}, per_page=1, session=session),
            [{"id": "1"}, {"id": "2"}],
        )
        self.assertEqual(session.calls[0][2]["params"][-2:], [("page", 0), ("per_page", 1)])
        self.assertEqual(session.calls[1][2]["params"][-2:], [("page", 1), ("per_page", 1)])

    def test_fetch_vacancy_uses_vacancy_endpoint(self) -> None:
        session = FakeSession([FakeResponse({"id": "42"})])

        self.assertEqual(fetch_vacancy("token", 42, session=session), {"id": "42"})
        self.assertEqual(session.calls[0][1], "https://api.hh.ru/vacancies/42")

    def test_vacancy_summary_extracts_research_fields(self) -> None:
        summary = vacancy_summary(
            {
                "id": "10",
                "name": "Python developer",
                "employer": {"name": "Example"},
                "salary": {"from": 100000, "to": 150000, "currency": "RUR", "gross": True},
                "experience": {"name": "1-3 года"},
                "schedule": {"name": "Удаленная работа"},
                "key_skills": [{"name": "Python"}, {"name": "SQL"}],
                "description": "<p>Build APIs</p>",
            },
            exchange_rates={"RUR": 1.0},
        )

        self.assertEqual(summary.id, "10")
        self.assertEqual(summary.employer, "Example")
        self.assertTrue(summary.has_salary)
        self.assertEqual(summary.salary_from, 87000)
        self.assertEqual(summary.salary_to, 130500)
        self.assertEqual(summary.key_skills, ["Python", "SQL"])
        self.assertEqual(summary.description, "Build APIs")

    def test_vacancy_text_extracts_only_employer_and_text(self) -> None:
        self.assertEqual(
            vacancy_text(
                {
                    "name": "Python developer",
                    "employer": {"name": "Example"},
                    "description": "<p>Build &quot;APIs&quot;</p>",
                    "salary": {"from": 100000},
                }
            ),
            {"employer": "Example", "text": 'Python developer\n\nBuild "APIs"'},
        )

    def test_collect_vacancy_texts_fetches_details_after_search(self) -> None:
        session = FakeSession(
            [
                FakeResponse({"items": [{"id": "1"}, {"id": "2"}], "pages": 1}),
                FakeResponse({"name": "One", "employer": {"name": "A"}, "description": "<p>Alpha</p>"}),
                FakeResponse({"name": "Two", "employer": {"name": "B"}, "description": "<p>Beta</p>"}),
            ]
        )

        self.assertEqual(
            collect_vacancy_texts("token", {"text": "python"}, max_pages=1, session=session),
            [
                {"employer": "A", "text": "One\n\nAlpha"},
                {"employer": "B", "text": "Two\n\nBeta"},
            ],
        )
        self.assertEqual(session.calls[1][1], "https://api.hh.ru/vacancies/1")
        self.assertEqual(session.calls[2][1], "https://api.hh.ru/vacancies/2")


if __name__ == "__main__":
    unittest.main()
