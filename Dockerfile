FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        tesseract-ocr \
        tesseract-ocr-rus \
        tesseract-ocr-eng \
        libglib2.0-0 \
        libgl1 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

COPY pyproject.toml uv.lock ./
# Кэш uv переживает пересборки: torch и компания не качаются заново каждый раз,
# когда меняется состав зависимостей.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --extra ocr

# Прогреваем модели EasyOCR в образ, чтобы бот не качал их при первом фото.
RUN uv run --frozen python -c "import easyocr; easyocr.Reader(['ru'], gpu=False, verbose=False)"

COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
COPY web_configurator ./web_configurator
COPY README.md ./

RUN mkdir -p /app/runtime

CMD ["uv", "run", "--frozen", "-m", "src.main"]
