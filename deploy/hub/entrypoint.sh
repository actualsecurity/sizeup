#!/bin/sh
# The hub container's entrypoint (Dockerfile, docs/cloud.md).
#   LITESTREAM_BUCKET set (Fargate): restore cad / hub / audio .sqlite from S3 when this task's disk
#     is new (a redeploy), make sure all three exist, then run sizeup under `litestream replicate`,
#     which ships every change to S3 within seconds. Credentials: the ECS task role (no keys).
#     Optional: LITESTREAM_PATH (prefix in the bucket, default sizeup-hub), LITESTREAM_REGION
#     (default AWS_REGION).
#   Unset (local dev, compose.yaml): just sizeup.
set -eu
CONFIG="${SIZEUP_CONFIG:-/app/deploy/hub/config.hub.toml}"
export SIZEUP_STATE_DIR="${SIZEUP_STATE_DIR:-/data}"
mkdir -p "$SIZEUP_STATE_DIR"
[ -n "${SIZEUP_STATION_TOKEN:-}" ] || echo "entrypoint: SIZEUP_STATION_TOKEN is not set: the hub will refuse the station" >&2

if [ -n "${LITESTREAM_BUCKET:-}" ]; then
  export LITESTREAM_PATH="${LITESTREAM_PATH:-sizeup-hub}"
  export LITESTREAM_REGION="${LITESTREAM_REGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-}}}"
  LS_CONFIG=/app/deploy/hub/litestream.yml
  for db in cad hub audio; do
    litestream restore -config "$LS_CONFIG" -if-db-not-exists -if-replica-exists "$SIZEUP_STATE_DIR/$db.sqlite"
  done
  python -m sizeup --config "$CONFIG" --init-state      # a first start: create them, so all three replicate
  exec litestream replicate -config "$LS_CONFIG" -exec "python -m sizeup --config '$CONFIG'"
fi
exec python -m sizeup --config "$CONFIG"
