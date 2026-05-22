## Общая инфа

https://api.hh.ru/openapi/redoc

### Ключи

OAuth

#### professional roles
stomatologi
[15, 24, 29]

pogromisty
[96, 10]

- https://api.hh.ru/professional_roles
- https://api.hh.ru/suggests/professional_roles

### Контракты

- `fetch_vacancies_page(access_token, professional_roles: Iterable[int] | None, text: str | None, page: int, per_page: int)`
- `fetch_professional_role_suggestions(text: str, access_token: str | None = None)`
