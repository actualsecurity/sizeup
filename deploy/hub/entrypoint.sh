#!/bin/sh
# The hub container's entrypoint (Dockerfile, docs/cloud.md).
#   LITESTREAM_BUCKET set (Fargate): restore cad / hub .sqlite from S3 when this task's disk is new
#     (a redeploy), make sure both exist, then run sizeup under `litestream replicate`, which ships every
#     change to S3 within seconds. audio.sqlite: the app, after the CAD serves (SIZEUP_AUDIO_LITESTREAM,
#     deploy/hub/litestream-audio.yml). Credentials: the ECS task role (no keys).
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
  for db in cad hub; do
    litestream restore -config "$LS_CONFIG" -if-db-not-exists -if-replica-exists "$SIZEUP_STATE_DIR/$db.sqlite"
  done
  # audio.sqlite (the biggest, and not needed to serve the CAD): the app restores it in the background, then
  # replicates it with its own Litestream (sizeup/cloud/lateaudio.py, #251)
  export SIZEUP_AUDIO_LITESTREAM=/app/deploy/hub/litestream-audio.yml
  python -m sizeup --config "$CONFIG" --init-state      # a first start: create cad and hub, so both replicate
  exec litestream replicate -config "$LS_CONFIG" -exec "python -m sizeup --config '$CONFIG'"
fi
exec python -m sizeup --config "$CONFIG"
