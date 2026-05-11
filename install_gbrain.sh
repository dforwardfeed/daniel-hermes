#!/bin/bash
# Install or update the GBrain (Dbrain) CLI into a persistent location on the
# Railway volume, then `bun link` it so the `gbrain` binary is resolvable on
# PATH for the Hermes process.
#
# Two source modes (controlled by GBRAIN_SOURCE):
#
#   remote  (default, legacy)  — git clone/update from GBRAIN_REPO_URL @ GBRAIN_REF
#                                into GBRAIN_DIR. This is the historical behavior;
#                                Hermes prod runs this today.
#
#   local                      — copy/sync GBRAIN_LOCAL_SOURCE_DIR (the vendored
#                                ./gbrain/ tree inside the image) into GBRAIN_DIR.
#                                No network, no git. Requires the Dockerfile to
#                                COPY gbrain/ into the image at GBRAIN_LOCAL_SOURCE_DIR.
#
# Both modes converge on the same `bun install` + `bun link` finalization.
#
# Controlled via env vars (defaults shown):
#   GBRAIN_ENABLED=false
#   GBRAIN_SOURCE=remote                       # remote | local
#   GBRAIN_REPO_URL=https://github.com/dforwardfeed/Dbrain-hermes.git
#   GBRAIN_REF=main
#   GBRAIN_DIR=/data/gbrain
#   GBRAIN_LOCAL_SOURCE_DIR=/app/gbrain        # only used when GBRAIN_SOURCE=local
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
GBRAIN_SOURCE="${GBRAIN_SOURCE:-remote}"
GBRAIN_REPO_URL="${GBRAIN_REPO_URL:-https://github.com/dforwardfeed/Dbrain-hermes.git}"
GBRAIN_REF="${GBRAIN_REF:-main}"
GBRAIN_DIR="${GBRAIN_DIR:-/data/gbrain}"
GBRAIN_LOCAL_SOURCE_DIR="${GBRAIN_LOCAL_SOURCE_DIR:-/app/gbrain}"
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

# Normalize source mode and log it loudly so the boot log makes it obvious
# whether we're pulling from GitHub or using the in-image vendored copy.
GBRAIN_SOURCE_LC="$(lc "$GBRAIN_SOURCE")"
case "$GBRAIN_SOURCE_LC" in
  remote|local) ;;
  *)
    warn "GBRAIN_SOURCE='$GBRAIN_SOURCE' is not 'remote' or 'local' — falling back to 'remote'."
    GBRAIN_SOURCE_LC="remote"
    ;;
esac

log "Source:   $GBRAIN_SOURCE_LC"
log "Dir:      $GBRAIN_DIR"
log "Required: $GBRAIN_REQUIRED"

command -v bun  >/dev/null 2>&1 || fail "bun not found in PATH (image must install bun)"

# Make sure bun's global link target dir exists on the persistent volume.
mkdir -p "${BUN_INSTALL:-/data/.bun}/bin" || fail "could not create \$BUN_INSTALL/bin"

GBRAIN_PARENT="$(dirname "$GBRAIN_DIR")"
mkdir -p "$GBRAIN_PARENT" || fail "could not create $GBRAIN_PARENT"

# ── Source: remote (git clone/update) ─────────────────────────────────────────
install_from_remote() {
  log "Repo:     $GBRAIN_REPO_URL"
  log "Ref:      $GBRAIN_REF"

  command -v git >/dev/null 2>&1 || fail "git not found in PATH"

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
}

# ── Source: local (vendored ./gbrain/ inside the image) ───────────────────────
install_from_local() {
  log "Local source: $GBRAIN_LOCAL_SOURCE_DIR"

  if [ ! -d "$GBRAIN_LOCAL_SOURCE_DIR" ]; then
    fail "GBRAIN_LOCAL_SOURCE_DIR=$GBRAIN_LOCAL_SOURCE_DIR does not exist — \
the image is missing the vendored gbrain/ tree (Dockerfile must COPY it)."
  fi
  if [ ! -f "$GBRAIN_LOCAL_SOURCE_DIR/package.json" ]; then
    fail "$GBRAIN_LOCAL_SOURCE_DIR has no package.json — refusing to sync \
what looks like a partial / wrong source tree."
  fi

  command -v rsync >/dev/null 2>&1 || fail "rsync not found in PATH \
(image must apt-get install rsync for GBRAIN_SOURCE=local)"

  # Pin the source SHA visibly in the boot log. The vendored tree is a git
  # subtree of Dbrain-hermes; .source-ref is written by the Dockerfile at
  # build time (next step). If it's absent we don't fail — just log it.
  if [ -f "$GBRAIN_LOCAL_SOURCE_DIR/.source-ref" ]; then
    log "Source ref: $(cat "$GBRAIN_LOCAL_SOURCE_DIR/.source-ref")"
  else
    log "Source ref: (no .source-ref file in $GBRAIN_LOCAL_SOURCE_DIR)"
  fi

  mkdir -p "$GBRAIN_DIR" || fail "could not create $GBRAIN_DIR"

  log "rsync $GBRAIN_LOCAL_SOURCE_DIR/  →  $GBRAIN_DIR/"
  # --delete:   if a file was removed from the in-image source, remove it
  #             from the destination too — keeps /data/gbrain in lock-step
  #             with the vendored tree across image upgrades.
  # --exclude=node_modules / admin/node_modules:
  #             let the bun install cache on the persistent volume survive
  #             between deploys. The next `bun install` below will refresh
  #             whatever needs refreshing based on the new lockfile.
  # --exclude=.git:
  #             if /data/gbrain already has a .git from a previous remote-mode
  #             run, leave it alone (it's harmless and removing it during
  #             rsync of a different source could confuse a future remote-mode
  #             rollback). A user wanting a clean switch can manually rm -rf.
  rsync -a --delete \
    --exclude='node_modules/' \
    --exclude='admin/node_modules/' \
    --exclude='.git/' \
    "$GBRAIN_LOCAL_SOURCE_DIR/" "$GBRAIN_DIR/" \
    || fail "rsync from $GBRAIN_LOCAL_SOURCE_DIR to $GBRAIN_DIR failed"
}

# ── Dispatch ─────────────────────────────────────────────────────────────────
if [ "$GBRAIN_SOURCE_LC" = "local" ]; then
  install_from_local
else
  install_from_remote
fi

# ── Finalize (same for both modes) ───────────────────────────────────────────
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

log "GBrain install complete (source=$GBRAIN_SOURCE_LC)."
exit 0
