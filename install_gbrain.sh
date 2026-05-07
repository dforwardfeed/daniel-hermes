#!/bin/bash
# Install or update the GBrain (Dbrain) CLI from a fork repo into a persistent
# location on the Railway volume, then `bun link` it so the `gbrain` binary is
# resolvable on PATH for the Hermes process.
#
# Controlled via env vars (defaults shown):
#   GBRAIN_ENABLED=false
#   GBRAIN_REPO_URL=https://github.com/dforwardfeed/Dbrain-hermes.git
#   GBRAIN_REF=main
#   GBRAIN_DIR=/data/gbrain
#   GBRAIN_REQUIRED=false
#
# If GBRAIN_REQUIRED=true, any failure exits non-zero (caller can decide to
# abort). Otherwise failures are logged as warnings and the script exits 0
# so Hermes can still start.

set -u

# Ensure the bun-linked CLI dir is on PATH even when this script is invoked
# directly (outside start.sh). Idempotent with start.sh's own export.
export BUN_INSTALL="${BUN_INSTALL:-/data/.bun}"
export PATH="$BUN_INSTALL/bin:/root/.bun/bin:$PATH"

GBRAIN_ENABLED="${GBRAIN_ENABLED:-false}"
GBRAIN_REPO_URL="${GBRAIN_REPO_URL:-https://github.com/dforwardfeed/Dbrain-hermes.git}"
GBRAIN_REF="${GBRAIN_REF:-main}"
GBRAIN_DIR="${GBRAIN_DIR:-/data/gbrain}"
GBRAIN_REQUIRED="${GBRAIN_REQUIRED:-false}"

log()  { echo "[install_gbrain] $*"; }
warn() { echo "[install_gbrain][WARN] $*" >&2; }
err()  { echo "[install_gbrain][ERROR] $*" >&2; }

# Lowercase a value for tolerant true/false comparison.
lc() { echo "${1:-}" | tr '[:upper:]' '[:lower:]'; }

fail() {
  err "$1"
  if [ "$(lc "$GBRAIN_REQUIRED")" = "true" ]; then
    err "GBRAIN_REQUIRED=true — aborting."
    exit 1
  fi
  warn "GBRAIN_REQUIRED=$GBRAIN_REQUIRED — continuing Hermes startup without GBrain."
  exit 0
}

if [ "$(lc "$GBRAIN_ENABLED")" != "true" ]; then
  log "GBRAIN_ENABLED=$GBRAIN_ENABLED — skipping GBrain install."
  exit 0
fi

log "Repo:     $GBRAIN_REPO_URL"
log "Ref:      $GBRAIN_REF"
log "Dir:      $GBRAIN_DIR"
log "Required: $GBRAIN_REQUIRED"

command -v git  >/dev/null 2>&1 || fail "git not found in PATH"
command -v bun  >/dev/null 2>&1 || fail "bun not found in PATH (image must install bun)"

# Make sure bun's global link target dir exists on the persistent volume.
mkdir -p "${BUN_INSTALL:-/data/.bun}/bin" || fail "could not create \$BUN_INSTALL/bin"

GBRAIN_PARENT="$(dirname "$GBRAIN_DIR")"
mkdir -p "$GBRAIN_PARENT" || fail "could not create $GBRAIN_PARENT"

if [ -d "$GBRAIN_DIR/.git" ]; then
  log "Existing checkout found — updating."
  git -C "$GBRAIN_DIR" remote set-url origin "$GBRAIN_REPO_URL" \
    || fail "git remote set-url failed"
  git -C "$GBRAIN_DIR" fetch --depth=1 origin "$GBRAIN_REF" \
    || fail "git fetch failed"
  # Force-reset to the fetched ref so a stale local branch never blocks updates.
  git -C "$GBRAIN_DIR" checkout -B "$GBRAIN_REF" FETCH_HEAD \
    || fail "git checkout failed"
elif [ -e "$GBRAIN_DIR" ]; then
  fail "$GBRAIN_DIR exists but is not a git checkout — refusing to overwrite"
else
  log "Cloning $GBRAIN_REPO_URL @ $GBRAIN_REF into $GBRAIN_DIR"
  git clone --depth=1 --branch "$GBRAIN_REF" "$GBRAIN_REPO_URL" "$GBRAIN_DIR" \
    || fail "git clone failed"
fi

log "----- git remote -v -----"
git -C "$GBRAIN_DIR" remote -v || true
log "----- git log -1 --oneline -----"
git -C "$GBRAIN_DIR" log -1 --oneline || true
log "-------------------------------"

log "Running 'bun install' in $GBRAIN_DIR"
( cd "$GBRAIN_DIR" && bun install ) || fail "bun install failed"

log "Running 'bun link' in $GBRAIN_DIR"
( cd "$GBRAIN_DIR" && bun link ) || fail "bun link failed"

log "----- which gbrain -----"
if command -v gbrain >/dev/null 2>&1; then
  which gbrain
else
  warn "gbrain not on PATH after bun link (BUN_INSTALL=${BUN_INSTALL:-unset})"
fi
log "----- gbrain --version -----"
gbrain --version || warn "gbrain --version failed"
log "----------------------------"

log "GBrain install complete."
exit 0
