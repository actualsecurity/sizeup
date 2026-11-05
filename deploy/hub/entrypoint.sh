#!/bin/sh
# The hub container's entrypoint (Dockerfile, docs/cloud.md).
#   LITESTREAM_BUCKET set (Fargate): the supervisor, sizeup/handoff.py (GitHub #247): the single-writer lock in
#     the bucket; as a standby it restores cad / hub / audio .sqlite and the caches and follows the replica; as
#     the leader it runs `litestream replicate` and sizeup, and hands over on SIGTERM (docs/cloud.md "Deploys:
#     the hot/cold handoff"). Credentials: the ECS task role (no keys). Optional: LITESTREAM_PATH (prefix in the
#     bucket, default sizeup-hub), LITESTREAM_REGION (default AWS_REGION).
#   Unset (local dev, compose.yaml): just sizeup.
# The task's other container, the Cloudflare connector, runs deploy/hub/connector.sh from this image.
set -eu
CONFIG="${SIZEUP_CONFIG:-/app/deploy/hub/config.hub.toml}"
export SIZEUP_STATE_DIR="${SIZEUP_STATE_DIR:-/data}"
mkdir -p "$SIZEUP_STATE_DIR"
[ -n "${SIZEUP_STATION_TOKEN:-}" ] || echo "entrypoint: SIZEUP_STATION_TOKEN is not set: the hub will refuse the station" >&2

if [ -n "${LITESTREAM_BUCKET:-}" ]; then
  export LITESTREAM_PATH="${LITESTREAM_PATH:-sizeup-hub}"
  export LITESTREAM_REGION="${LITESTREAM_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
  export SIZEUP_RUN_DIR="${SIZEUP_RUN_DIR:-/tmp/sizeup-run}"
  export LITESTREAM_SOCKET="${LITESTREAM_SOCKET:-$SIZEUP_RUN_DIR/litestream.sock}"
  mkdir -p "$SIZEUP_RUN_DIR"
  exec python -m sizeup.handoff --config "$CONFIG" --litestream-config /app/deploy/hub/litestream.yml
fi
exec python -m sizeup --config "$CONFIG"
