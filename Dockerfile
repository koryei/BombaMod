FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
RUN groupadd --system bombamod \
    && useradd --system --gid bombamod --create-home bombamod \
    && mkdir -p /app/data \
    && chown bombamod:bombamod /app/data
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .
USER bombamod

CMD ["bombamod"]
