# leadheatapi

## Docker

Запуск контейнеров:

```bash
docker compose up
```

Запуск с пересборкой:

```bash
docker compose up --build
```

Остановка и удаление контейнеров:

```bash
docker compose down
```

## Миграции в Docker

Миграции катаются отдельной командой (по аналогии с `seapagan/fastapi-template`):

```bash
docker compose run --rm api uv run alembic upgrade head
```

Создание новой миграции:

```bash
docker compose up -d db
docker compose build api
docker compose run --rm api uv run alembic revision --autogenerate -m "Add some table"
```

Файлы миграций сохраняются в репозиторий в `app/migrations/versions` (каталог примонтирован в контейнер).
