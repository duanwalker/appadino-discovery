FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./
COPY alembic ./alembic

RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "discovery.cli"]
CMD ["ingest"]
