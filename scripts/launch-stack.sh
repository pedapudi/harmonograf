#!/bin/bash
# Hardened launcher for the harmonograf dev stack.
#
# Launches three children:
#   - harmonograf_server (port 7531)
#   - frontend dev server (port 5173)
#   - ADK web (port 8080)
#
# Why this script exists (see internal task #224 / memory
# `feedback_verify_running_build.md`):
#
# The previous /tmp/launch-stack.sh used bare bash background subshells
# with `set -eu`. Subshell failures don't propagate up; if a port was
# already bound, the new child exited silently and the launcher kept
# running while the OLD process kept serving requests. That cost ~30
# minutes of wrong-direction diagnosis.
#
# Safeguards added here:
#   1. Pre-launch port check  -> fail loudly before starting anything.
#   2. Per-child TCP health probe with timeout (~30s) after launch.
#   3. `wait -n` so the FIRST child death tears the whole stack down
#      instead of being masked by the launcher's own `wait`.
#   4. Version stamps (harmonograf sha + goldfive submodule sha) printed
#      BEFORE children launch so the logs are unambiguous even if a child
#      dies immediately.
#   5. Robust cleanup trap on EXIT/INT/TERM.

set -euo pipefail

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
HARMONOGRAF_ROOT="${HARMONOGRAF_ROOT:-/home/sunil/git/harmonograf}"
LOG_DIR="${LOG_DIR:-/tmp}"
SERVER_LOG="${LOG_DIR}/stack-server.log"
FRONTEND_LOG="${LOG_DIR}/stack-frontend.log"
ADK_LOG="${LOG_DIR}/stack-adk.log"
PID_FILE="${LOG_DIR}/stack-pids.txt"

# ---------------------------------------------------------------------------
# Env defaults (only set if unset; allow caller override)
# ---------------------------------------------------------------------------
: "${HARMONOGRAF_SERVER:=127.0.0.1:7531}"
: "${OPENAI_API_KEY:=dummy}"
: "${GOLDFIVE_EXAMPLE_MODEL:=openai/Qwen3.6-35B-A3B-FP8}"
: "${GOLDFIVE_EXAMPLE_PLANNER_MODEL:=Qwen3.6-35B-A3B-FP8}"
: "${GOLDFIVE_STRICT_STATE_OWNERSHIP:=1}"
: "${USER_MODEL_NAME:=openai/Qwen3.6-35B-A3B-FP8}"
: "${OPENAI_API_BASE:=http://kossel.lan:8080/v1}"
: "${OPENAI_BASE_URL:=http://kossel.lan:8080/v1}"
export HARMONOGRAF_SERVER OPENAI_API_KEY GOLDFIVE_EXAMPLE_MODEL \
       GOLDFIVE_EXAMPLE_PLANNER_MODEL GOLDFIVE_STRICT_STATE_OWNERSHIP \
       USER_MODEL_NAME OPENAI_API_BASE OPENAI_BASE_URL

# Ports we will (eventually) bind. Override-able via env for tests.
SERVER_PORT="${SERVER_PORT:-7531}"
SERVER_ALT_PORT="${SERVER_ALT_PORT:-7532}"
ADK_PORT="${ADK_PORT:-8080}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
log() { printf '[launch-stack] %s\n' "$*"; }
die() { printf '[launch-stack] FATAL: %s\n' "$*" >&2; exit 1; }

# check_port_free PORT NAME -- die if PORT is already bound (TCP listen).
check_port_free() {
  local port="$1" name="$2"
  # ss is preferred; fall back to /proc-based check via lsof if available.
  if command -v ss >/dev/null 2>&1; then
    if ss -ltnH "sport = :${port}" 2>/dev/null | grep -q LISTEN; then
      local who
      who="$(ss -ltnpH "sport = :${port}" 2>/dev/null || true)"
      die "port ${port} (${name}) is already bound. Existing listener:
${who}
Refusing to launch -- a stale process is still running. Kill it first:
  fuser -k ${port}/tcp   # or find/kill the offending pid"
    fi
  elif command -v lsof >/dev/null 2>&1; then
    if lsof -iTCP:"${port}" -sTCP:LISTEN >/dev/null 2>&1; then
      die "port ${port} (${name}) is already bound (lsof). Kill the listener first."
    fi
  else
    log "WARN: neither ss nor lsof available; skipping port ${port} pre-check."
  fi
}

# wait_for_port PORT NAME [TIMEOUT_SECS] -- poll TCP until ready or fail loudly.
wait_for_port() {
  local port="$1" name="$2" timeout="${3:-30}"
  local deadline=$(( $(date +%s) + timeout ))
  local pid="" log_file=""
  log "waiting up to ${timeout}s for ${name} on port ${port}..."
  while (( $(date +%s) < deadline )); do
    # Use bash /dev/tcp to avoid an extra dependency.
    if (echo > "/dev/tcp/127.0.0.1/${port}") >/dev/null 2>&1; then
      log "${name} is accepting connections on port ${port}."
      return 0
    fi
    # Also fail fast if the child has already died.
    case "$name" in
      server)   pid="${SERVER_PID:-}";   log_file="${SERVER_LOG}" ;;
      frontend) pid="${FRONTEND_PID:-}"; log_file="${FRONTEND_LOG}" ;;
      adk)      pid="${ADK_PID:-}";      log_file="${ADK_LOG}" ;;
      *)        pid="";                  log_file="" ;;
    esac
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      die "${name} (pid ${pid}) exited before opening port ${port}. See ${log_file} (last lines below):
$(tail -n 40 "${log_file}" 2>/dev/null || echo '<no log>')"
    fi
    sleep 0.5
  done
  die "${name} did not open port ${port} within ${timeout}s. See ${log_file:-<unknown>} for details:
$(tail -n 40 "${log_file:-/dev/null}" 2>/dev/null || echo '<no log>')"
}

# Cleanup: kill any children we know about.
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  log "cleanup: tearing down children (rc=${rc})"
  for pid in "${ADK_PID:-}" "${FRONTEND_PID:-}" "${SERVER_PID:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null || true
    fi
  done
  # Give them a moment, then SIGKILL stragglers.
  sleep 1 2>/dev/null || true
  for pid in "${ADK_PID:-}" "${FRONTEND_PID:-}" "${SERVER_PID:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  done
  wait 2>/dev/null || true
  exit "$rc"
}
trap cleanup INT TERM EXIT

# ---------------------------------------------------------------------------
# Version stamps -- print BEFORE launching anything.
# ---------------------------------------------------------------------------
HARMONOGRAF_SHA="$(git -C "${HARMONOGRAF_ROOT}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
HARMONOGRAF_BRANCH="$(git -C "${HARMONOGRAF_ROOT}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GOLDFIVE_SHA="$(git -C "${HARMONOGRAF_ROOT}/third_party/goldfive" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GOLDFIVE_BRANCH="$(git -C "${HARMONOGRAF_ROOT}/third_party/goldfive" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"

log "================================================================"
log "harmonograf stack launcher"
log "  harmonograf : ${HARMONOGRAF_SHA} (${HARMONOGRAF_BRANCH})"
log "  goldfive    : ${GOLDFIVE_SHA} (${GOLDFIVE_BRANCH})"
log "  root        : ${HARMONOGRAF_ROOT}"
log "  log dir     : ${LOG_DIR}"
log "  ports       : server=${SERVER_PORT} frontend=${FRONTEND_PORT} adk=${ADK_PORT}"
log "  model api   : ${OPENAI_API_BASE}"
log "================================================================"

# ---------------------------------------------------------------------------
# Pre-launch port checks -- fail BEFORE we spawn anything.
# ---------------------------------------------------------------------------
check_port_free "${SERVER_PORT}"     "harmonograf-server"
check_port_free "${SERVER_ALT_PORT}" "harmonograf-server-alt"
check_port_free "${ADK_PORT}"        "adk-web"
check_port_free "${FRONTEND_PORT}"   "frontend-dev"

# ---------------------------------------------------------------------------
# Launch children
# ---------------------------------------------------------------------------
SERVER_PID=""
FRONTEND_PID=""
ADK_PID=""

log "starting harmonograf_server -> ${SERVER_LOG}"
(
  cd "${HARMONOGRAF_ROOT}/server" && \
  exec uv run python -m harmonograf_server \
    --store sqlite \
    --data-dir "${HARMONOGRAF_ROOT}/data" \
    --port "${SERVER_PORT}"
) > "${SERVER_LOG}" 2>&1 &
SERVER_PID=$!

log "starting frontend dev server -> ${FRONTEND_LOG}"
(
  cd "${HARMONOGRAF_ROOT}/frontend" && \
  exec pnpm dev --port "${FRONTEND_PORT}" --strictPort
) > "${FRONTEND_LOG}" 2>&1 &
FRONTEND_PID=$!

log "starting adk web -> ${ADK_LOG}"
(
  cd "${HARMONOGRAF_ROOT}" && \
  exec uv run --extra demo adk web \
    --host 127.0.0.1 --port "${ADK_PORT}" .demo-agents
) > "${ADK_LOG}" 2>&1 &
ADK_PID=$!

printf 'launcher pid=%s server=%s frontend=%s adk=%s\n' \
  "$$" "${SERVER_PID}" "${FRONTEND_PID}" "${ADK_PID}" > "${PID_FILE}"
log "pids: launcher=$$ server=${SERVER_PID} frontend=${FRONTEND_PID} adk=${ADK_PID}"

# ---------------------------------------------------------------------------
# Per-child health probes -- fail loudly if a child never opens its port.
# ---------------------------------------------------------------------------
wait_for_port "${SERVER_PORT}"   "server"   30
wait_for_port "${FRONTEND_PORT}" "frontend" 60
wait_for_port "${ADK_PORT}"      "adk"      60

log "all children healthy. URLs:"
log "  server   : http://127.0.0.1:${SERVER_PORT}"
log "  frontend : http://127.0.0.1:${FRONTEND_PORT}"
log "  adk      : http://127.0.0.1:${ADK_PORT}"

# ---------------------------------------------------------------------------
# Block until any child exits -- and tear down the rest.
# ---------------------------------------------------------------------------
log "stack is up. waiting on children (first death tears the stack down)."
# `wait -n` returns the exit status of the next child to terminate; the
# trap on EXIT will then clean up the survivors.
set +e
wait -n
rc=$?
set -e
log "a child exited with rc=${rc}; tearing down."
exit "${rc}"
