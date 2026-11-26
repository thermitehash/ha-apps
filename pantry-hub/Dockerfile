# https://developers.home-assistant.io/docs/apps/dockerfile
ARG BUILD_FROM=ghcr.io/home-assistant/base-python:3.14-alpine3.24-2026.11.1
FROM ${BUILD_FROM}

WORKDIR /opt/pantry-hub

# config.yaml is copied because the package version is read from it.
COPY pyproject.toml config.yaml ./
COPY src ./src

# git fetches the pinned aiochainway tag; build-base covers any dependency
# without a musllinux wheel. Both are removed again in the same layer.
RUN apk add --no-cache --virtual .build-deps git build-base \
    && pip install --no-cache-dir . \
    && apk del .build-deps \
    && rm -rf /root/.cache

COPY rootfs /

LABEL \
    org.opencontainers.image.title="Home Assistant App: Pantry Hub" \
    org.opencontainers.image.description="RFID pantry inventory: Chainway UR4 reads become Grocy stock, with a barcode + RFID linking station." \
    org.opencontainers.image.source="https://github.com/thermitehash/ha-apps" \
    org.opencontainers.image.licenses="MIT"
