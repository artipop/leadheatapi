import unittest

from app.datamining.contacts.mail.email_generator import (
    generate_email_candidates,
)


class GenerateEmailCandidatesTest(unittest.TestCase):
    def test_generates_candidates_for_single_domain_and_name_parts(self) -> None:
        emails = generate_email_candidates(
            "Example.COM",
            "Иванов",
            "Иван",
            "Иванович",
            max_name_variants=1,
            patterns=("first.last", "f.last", "last.f", "first.middle"),
        )

        self.assertEqual(
            emails,
            [
                "i.ivanov@example.com",
                "ivan.ivanov@example.com",
                "ivan.ivanovich@example.com",
                "ivanov.i@example.com",
            ],
        )

    def test_normalizes_url_domain_without_skip_lists(self) -> None:
        self.assertEqual(
            generate_email_candidates(
                "https://www.t.me/some/path",
                "Петров",
                "Петр",
                max_name_variants=1,
                patterns=("first", "last.first"),
            ),
            [
                "petr@t.me",
                "petrov.petr@t.me",
            ],
        )

    def test_returns_empty_list_for_invalid_input(self) -> None:
        self.assertEqual(generate_email_candidates("example.com", "", "Иван"), [])
        self.assertEqual(generate_email_candidates("not a domain", "Иванов", "Иван"), [])


if __name__ == "__main__":
    unittest.main()
