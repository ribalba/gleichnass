FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GLEICHNASS_CONFIG=/data/gleichnass.yaml \
    GLEICHNASS_INTERVAL=300

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/gleichnass-entrypoint

RUN pip install --no-cache-dir . \
 && useradd --create-home --uid 10001 gleichnass \
 && chmod +x /usr/local/bin/gleichnass-entrypoint \
 # Created and owned here so a fresh named volume inherits the ownership from
 # the image and the unprivileged user can actually write to it.
 && mkdir -p /data \
 && chown gleichnass:gleichnass /data

# Config, users.d/ and the SQLite state all live here.
VOLUME /data
USER gleichnass

HEALTHCHECK --interval=5m --timeout=10s CMD gleichnass users >/dev/null || exit 1

ENTRYPOINT ["gleichnass-entrypoint"]

# The default role is the checker. Rules carry their own schedule and a grace
# window, so a plain sleep loop is enough: no cron anywhere, and a missed tick
# costs nothing. The site service overrides this with its own command.
CMD ["sh", "-c", "while true; do gleichnass -v run || true; sleep \"$GLEICHNASS_INTERVAL\"; done"]
