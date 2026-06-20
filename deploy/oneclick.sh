#!/usr/bin/env bash
# One-click (re)deploy for sandbox-mcp on a Debian / Raspberry Pi host.
#
#   Fresh host     -> clone, build venv, install the systemd unit, start the service.
#   Existing host  -> "delete then update to latest": stop the service, WIPE the code
#                     tree, re-clone origin/<branch>, rebuild the venv, restart. Local
#                     state that is NOT in git (.env, frpc.toml, certs/) is preserved
#                     across the wipe, and the data dir is never touched.
#
# Quick start (bootstraps itself, works for both cases):
#   curl -fsSL https://raw.githubusercontent.com/GreenTeodoro839/sandbox-mcp/main/deploy/oneclick.sh | bash
#
# Run as a user with sudo (it manages a root-owned /opt install + systemd units).
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/GreenTeodoro839/sandbox-mcp.git}"
BRANCH="${BRANCH:-main}"
DIR="${DIR:-/opt/sandbox-mcp}"
DATA="${DATA:-/var/lib/sandbox-mcp}"
BASE_IMAGE="${BASE_IMAGE:-ghcr.io/greenteodoro839/sandbox-mcp-base:latest}"
PORT="${PORT:-8000}"
PRESERVE=(.env frpc.toml certs)   # local state, never committed; keep across redeploy

SUDO=sudo; [ "$(id -u)" = 0 ] && SUDO=
log(){ printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }

# --- prerequisites ----------------------------------------------------------
log "checking prerequisites"
if ! command -v git >/dev/null || ! python3 -m venv --help >/dev/null 2>&1; then
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq git python3 python3-venv
fi
if ! command -v docker >/dev/null; then
  log "installing docker"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO mkdir -p "$DATA"

# --- fetch code -------------------------------------------------------------
if [ -e "$DIR" ]; then
  log "existing install -> wiping to latest (preserving: ${PRESERVE[*]})"
  TMP=$($SUDO mktemp -d)
  for f in "${PRESERVE[@]}"; do
    if $SUDO test -e "$DIR/$f"; then $SUDO cp -a "$DIR/$f" "$TMP/" && echo "  saved $f"; fi
  done
  $SUDO systemctl stop sandbox-mcp 2>/dev/null || true
  $SUDO rm -rf "$DIR"
  $SUDO git clone -q --branch "$BRANCH" "$REPO_URL" "$DIR"
  for f in "${PRESERVE[@]}"; do
    if $SUDO test -e "$TMP/$f"; then $SUDO cp -a "$TMP/$f" "$DIR/" && echo "  restored $f"; fi
  done
  $SUDO rm -rf "$TMP"
else
  log "fresh install -> cloning"
  $SUDO git clone -q --branch "$BRANCH" "$REPO_URL" "$DIR"
fi
$SUDO git config --global --add safe.directory "$DIR" 2>/dev/null || true

# --- .env -------------------------------------------------------------------
NEED_ENV_EDIT=0
if ! $SUDO test -f "$DIR/.env"; then
  $SUDO cp "$DIR/.env.example" "$DIR/.env"
  $SUDO chmod 600 "$DIR/.env"
  NEED_ENV_EDIT=1
fi

# --- python venv ------------------------------------------------------------
log "building venv + installing package"
$SUDO python3 -m venv "$DIR/.venv"
$SUDO "$DIR/.venv/bin/pip" install -q --upgrade pip
$SUDO "$DIR/.venv/bin/pip" install -q -e "$DIR"

# --- sandbox base image (best-effort) ---------------------------------------
log "pulling sandbox base image (best-effort)"
$SUDO docker pull -q "$BASE_IMAGE" 2>/dev/null || echo "  (skipped; Docker will pull on first sandbox use)"

# --- systemd ----------------------------------------------------------------
log "installing systemd unit + (re)starting"
$SUDO cp "$DIR/deploy/sandbox-mcp.service" /etc/systemd/system/sandbox-mcp.service
$SUDO systemctl daemon-reload
$SUDO systemctl enable -q sandbox-mcp 2>/dev/null || true
$SUDO systemctl restart sandbox-mcp

# --- health -----------------------------------------------------------------
log "health check"
ok=0
for _ in $(seq 1 12); do
  if curl -fsS -m 5 "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then ok=1; break; fi
  sleep 1
done
echo "  commit:  $($SUDO git -C "$DIR" rev-parse --short HEAD)"
echo "  service: $($SUDO systemctl is-active sandbox-mcp)"
echo "  health:  $(curl -fsS -m 5 "http://127.0.0.1:${PORT}/healthz" 2>/dev/null || echo unreachable)"
if [ "$ok" != 1 ]; then
  echo
  echo "!! health check FAILED -- recent logs:"
  $SUDO journalctl -u sandbox-mcp -n 25 --no-pager || true
  exit 1
fi
if [ "$NEED_ENV_EDIT" = 1 ]; then
  echo
  echo ">> ACTION REQUIRED: edit $DIR/.env (set SMCP_TOKEN, SMCP_PUBLIC_BASE_URL),"
  echo "   then: sudo systemctl restart sandbox-mcp"
fi
log "done"
