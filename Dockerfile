FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        libimage-exiftool-perl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir '.[webpage]' \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /ms-playwright

RUN groupadd --system --gid 10001 pico \
    && useradd --system --uid 10001 --gid pico --home-dir /app pico \
    && mkdir -p /app/data \
    && chown -R pico:pico /app/data

USER pico
RUN python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); b=p.chromium.launch(headless=True, args=['--no-proxy-server']); b.close(); p.stop()"

CMD ["python", "-m", "pico_photo_bot.deploy"]
