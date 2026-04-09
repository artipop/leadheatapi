FROM python:3.13-slim

# Install uv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy the application into the container.
COPY . /app

# Install the application dependencies.
WORKDIR /app
RUN uv sync --frozen --no-cache

# Run DB migrations, then start the application.
CMD ["/bin/sh", "-c", "/app/.venv/bin/alembic upgrade head && /app/.venv/bin/fastapi run app/main.py --port 80 --host 0.0.0.0"]
