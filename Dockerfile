FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install --no-install-recommends -y \
        ca-certificates \
        libimage-exiftool-perl \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN groupadd --system --gid 10001 pico \
    && useradd --system --uid 10001 --gid pico --home-dir /app pico \
    && mkdir -p /app/data \
    && chown -R pico:pico /app/data

USER pico

CMD ["python", "-m", "pico_photo_bot.deploy"]
