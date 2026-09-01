FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps for TA/numba and postgres
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./alembic.ini

RUN pip install --upgrade pip && pip install -e ".[postgres]"

# Create non-root user
RUN useradd -m trader && chown -R trader:trader /app
USER trader

# Default command runs migrations then bot
CMD ["sh", "-c", "alembic upgrade head && python scripts/run_bot.py"]
