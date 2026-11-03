FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv

RUN pip install --no-cache-dir uv==0.11.28 \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /srv app \
    && mkdir -p /var/opsassist/blobs \
    && chown app:app /var/opsassist/blobs

WORKDIR /srv
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev --extra anthropic

ENV PATH="/opt/venv/bin:$PATH"
USER app
EXPOSE 8000
CMD ["uvicorn", "opsassist.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
