# syntax=docker/dockerfile:1
# The sizeup HUB (docs/cloud.md): web UI + CAD in the cloud, fed by the station. No radios, no
# whisper, no voice server: Python + numpy only. Built for linux/arm64 (Fargate Graviton); amd64 works.
#   docker buildx build --platform linux/arm64 --build-arg SIZEUP_BUILD=$(git rev-parse HEAD) -t sizeup-hub .
# SIZEUP_BUILD: the full git sha (CI passes it); stations follow the hub's build (sizeup/cloud/autoupdate.py).
FROM python:3.14-slim

ARG TARGETARCH
ARG SIZEUP_BUILD=""
# Litestream (continuous SQLite backup to S3, deploy/hub/entrypoint.sh), pinned, checked against the
# release's checksums.txt: github.com/benbjohnson/litestream/releases/tag/v0.5.17
ARG LITESTREAM_VERSION=0.5.17
ARG LITESTREAM_SHA256_ARM64=f8ca4a050095c1efbda2c4365172e61bf9d955ea0d9ac42f448b52e51819baa5
ARG LITESTREAM_SHA256_AMD64=cfb371176d164437ae869f8351cfde49bd1804ae71c61923f75c9cba9c9c006d

# The slim image has no curl: Python fetches the tarball, sha256sum checks it.
RUN set -eu; \
    case "${TARGETARCH:-arm64}" in \
      arm64) arch=arm64;  sum="$LITESTREAM_SHA256_ARM64" ;; \
      amd64) arch=x86_64; sum="$LITESTREAM_SHA256_AMD64" ;; \
      *) echo "no Litestream build pinned for $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-${arch}.tar.gz"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$url" /tmp/litestream.tgz; \
    echo "$sum  /tmp/litestream.tgz" | sha256sum -c -; \
    tar -xzf /tmp/litestream.tgz -C /usr/local/bin litestream; \
    rm /tmp/litestream.tgz; \
    litestream version

# cloudflared, the Cloudflare connector, pinned, checked against the release's SHA256 list
# (github.com/cloudflare/cloudflared/releases/tag/2026.10.7). The task's "cloudflared" container runs it from this
# image, under deploy/hub/connector.sh: started only once this task is the hub's leader (GitHub #247).
ARG CLOUDFLARED_VERSION=2026.10.7
ARG CLOUDFLARED_SHA256_ARM64=3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775
ARG CLOUDFLARED_SHA256_AMD64=1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872
RUN set -eu; \
    case "${TARGETARCH:-arm64}" in \
      arm64) arch=arm64; sum="$CLOUDFLARED_SHA256_ARM64" ;; \
      amd64) arch=amd64; sum="$CLOUDFLARED_SHA256_AMD64" ;; \
      *) echo "no cloudflared build pinned for $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    url="https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-${arch}"; \
    python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])" "$url" /usr/local/bin/cloudflared; \
    echo "$sum  /usr/local/bin/cloudflared" | sha256sum -c -; \
    chmod 0755 /usr/local/bin/cloudflared; \
    cloudflared --version

RUN groupadd --system --gid 10001 sizeup \
    && useradd --system --uid 10001 --gid 10001 --home-dir /data --shell /usr/sbin/nologin sizeup \
    && mkdir -p /data && chown sizeup:sizeup /data \
    && mkdir -p /run/sizeup-shared && chown sizeup:sizeup /run/sizeup-shared

WORKDIR /app
COPY pyproject.toml ./
COPY sizeup ./sizeup
# [docs]: PDFium (pypdfium2) for a PDF floor plan's page (#459, sizeup/docfiles.py)
RUN pip install --no-cache-dir ".[docs]" && rm -rf build
COPY data ./data
COPY deploy/hub ./deploy/hub
COPY deploy/trunk-recorder/talkgroups.csv ./deploy/trunk-recorder/talkgroups.csv

# sizeup finds data/ next to its package (data/gazetteer.sqlite, cad_lists.json, ...), so it runs from
# /app (PYTHONPATH) rather than the site-packages copy; `pip install .` brings numpy.
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SIZEUP_CONFIG=/app/deploy/hub/config.hub.toml \
    SIZEUP_STATE_DIR=/data \
    SIZEUP_PORT=8090 \
    SIZEUP_BUILD=${SIZEUP_BUILD}

USER sizeup
# The leader file and the hub's heartbeat, shared with the connector container (deploy/aws/taskdef.json mounts a
# task volume here; ECS copies this folder, owner sizeup, into it).
VOLUME ["/run/sizeup-shared"]
EXPOSE 8090
# The supervisor's /healthz (port 8089, sizeup/handoff.py) when it runs (LITESTREAM_BUCKET), else the app's.
# ECS uses the task definition's own health check (the same control port).
HEALTHCHECK --interval=10s --timeout=5s --start-period=300s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port = '8089' if os.environ.get('LITESTREAM_BUCKET') else os.environ.get('SIZEUP_PORT', '8090'); urllib.request.urlopen('http://127.0.0.1:%s/healthz' % port, timeout=4)"]
ENTRYPOINT ["/app/deploy/hub/entrypoint.sh"]
