# Playlisterr has no third-party Python dependencies, so there is nothing to pip
# install. The image is the stock slim Python plus this package; the only apt
# packages are the ones the PUID/PGID entrypoint needs (usermod/groupmod from
# passwd, setpriv from util-linux).
FROM python:3.12-slim

# Stamp the built version so the running container reports what it actually is
# rather than the source default. Pass with: docker build --build-arg VERSION=x
ARG VERSION=0.1.0

LABEL org.opencontainers.image.title="Playlisterr" \
      org.opencontainers.image.description="Personalized Plex recommendation rows chosen by a local LLM" \
      org.opencontainers.image.licenses="AGPL-3.0-or-later" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.source="https://github.com/JamesClayPatton/playlisterr"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYLISTERR_CONFIG_DIR=/config \
    PLAYLISTERR_VERSION=${VERSION} \
    TZ=Etc/UTC

RUN apt-get update \
    && apt-get install -y --no-install-recommends passwd util-linux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY playlisterr/ /app/playlisterr/
COPY README.md LICENSE /app/

# /config holds settings.json, state.json and logs — the only writable path.
VOLUME ["/config"]
EXPOSE 8484

# Runs as an unprivileged user by default. PUID/PGID are honoured by the
# entrypoint for hosts that need the bind mount owned by a specific user,
# which is the convention every *arr image follows.
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh \
    && groupadd -g 1000 playlisterr \
    && useradd -u 1000 -g 1000 -d /config -s /sbin/nologin playlisterr

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://localhost:8484/ping',timeout=3)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "playlisterr", "serve"]
