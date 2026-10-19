FROM python:3.12.7-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HEALTH_DATA_SCHEMA_PATH=/app/sql/schema.sql

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY sql ./sql

RUN python -m pip install --no-cache-dir .

USER 65532:65532

ENTRYPOINT ["python", "-m", "health_data.cli"]
CMD ["bootstrap"]
