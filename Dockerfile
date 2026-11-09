# Playwright's official image ships Chromium + all system deps + Xvfb.
# Keep PLAYWRIGHT_VERSION in sync with the image tag so browsers match.
ARG PLAYWRIGHT_VERSION=1.63.0
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble

ARG PLAYWRIGHT_VERSION
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml README.md ./
COPY scrapebot ./scrapebot
RUN pip install . "playwright==${PLAYWRIGHT_VERSION}"

COPY examples ./examples
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && mkdir -p /app/data /app/config \
    && chown -R pwuser:pwuser /app

# Unprivileged user lets Chromium keep its sandbox enabled.
USER pwuser
ENTRYPOINT ["entrypoint.sh"]
CMD ["run", "-c", "config/config.yaml"]
