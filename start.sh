#!/bin/bash
set -e

# Mirror dashboard-ref-only's startup: create every directory hermes expects
# and seed a default config.yaml if the volume is empty. Without these,
# `hermes dashboard` endpoints that hit logs/, sessions/, cron/, etc. can fail
# with opaque errors even though no auth is actually involved.
mkdir -p /data/.hermes/cron /data/.hermes/sessions /data/.hermes/logs \
         /data/.hermes/memories /data/.hermes/skills /data/.hermes/pairing \
         /data/.hermes/hooks /data/.hermes/image_cache /data/.hermes/audio_cache \
         /data/.hermes/workspace

if [ ! -f /data/.hermes/config.yaml ] && [ -f /opt/hermes-agent/cli-config.yaml.example ]; then
  cp /opt/hermes-agent/cli-config.yaml.example /data/.hermes/config.yaml
fi

[ ! -f /data/.hermes/.env ] && touch /data/.hermes/.env

# Clear any stale gateway PID file left over from the previous container.
# `hermes gateway` writes /data/.hermes/gateway.pid on start but does not
# remove it on SIGTERM. Since /data is a persistent volume, the file
# survives container restarts and causes every subsequent boot to exit with
# "ERROR gateway.run: PID file race lost to another gateway instance".
# No hermes process can be running at this point (we're pre-exec in a fresh
# container), so removing the file unconditionally is safe.
rm -f /data/.hermes/gateway.pid

# Optionally install/refresh GBrain (Dbrain) from a fork repo into
# /data/gbrain and `bun link` it so the `gbrain` CLI is available to Hermes.
# The script honors GBRAIN_REQUIRED: when false, install failures are warned
# and the script exits 0 so Hermes still starts. We disable -e around the
# call defensively in case the script itself fails before reaching its own
# error handlers.
mkdir -p /data/.bun/bin
set +e
/app/install_gbrain.sh
gbrain_rc=$?
set -e
if [ "$gbrain_rc" -ne 0 ]; then
  if [ "$(printf '%s' "${GBRAIN_REQUIRED:-false}" | tr '[:upper:]' '[:lower:]')" = "true" ]; then
    echo "[start.sh] install_gbrain.sh failed (rc=$gbrain_rc) and GBRAIN_REQUIRED=true — aborting." >&2
    exit "$gbrain_rc"
  fi
  echo "[start.sh] install_gbrain.sh exited rc=$gbrain_rc — continuing without GBrain." >&2
fi

exec python /app/server.py
