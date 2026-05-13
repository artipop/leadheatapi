### Контракт

`generate_email_candidates(domain: str, last_name: str, first_name: str, middle_name: str = "")`

`domain` принимает FQDN строкой. `https://` можно передать, но не нужно: функция сама нормализует домен через `normalize_domain`.
