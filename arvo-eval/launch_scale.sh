#!/usr/bin/env bash
# Detached learn_loop run over the full mruby set (resume skips bugs already in
# that pass's ledger). Fully detached via setsid+nohup so a controlling-session
# teardown cannot SIGKILL it.
#
# Usage: ./launch_scale.sh [treatment|control]   (default: treatment)
set -euo pipefail
cd "$(dirname "$0")"

PASS="${1:-treatment}"
TS=$(date +%y%m%d_%H%M%S)
LOG="results/learn/campaign_${PASS}_${TS}.log"

setsid nohup env \
  ARVO_DB_PATH=arvo_new.db \
  LEARN_PASS="$PASS" \
  LEARN_MAX_ATTEMPTS=1 \
  OSS_CRS_CHECK_PATCH=1 \
  OSS_CRS_RUN_TIMEOUT=7200 \
  OSS_CRS_ORIENT=1 \
  OSS_CRS_FORCE_EDIT="${OSS_CRS_FORCE_EDIT:-0}" \
  OSS_CRS_RECON_TIMEOUT="${OSS_CRS_RECON_TIMEOUT:-1800}" \
  LLM_BACKEND=claude_cli \
  systemd-inhibit --what=idle:sleep --why="panacea ${PASS} run" \
  python3 learn_loop.py \
  > "$LOG" 2>&1 < /dev/null &

echo "launched pass=$PASS PID $! -> $LOG"
