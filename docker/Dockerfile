FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /uvx /bin/
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev && uv run --locked playwright install-deps firefox && uv run --locked camoufox fetch
COPY . .
CMD ["uv", "run", "--locked", "app/deploy.py"]
