#!/bin/bash
# Bring up stacks 1..N in WAVES of size WAVE, waiting for each wave's stacks to be
# fully ready before the next. Caps concurrent Magento (shopping+admin) cold
# warmups: bringing up all 12 at once thrashed 128 to load 105 with zero progress
# (2026-07-09). Each `farm.sh start` blocks on wait_ready, so peak concurrent
# warmup = WAVE stacks. No per-stack gitlab bake (base+repoint, ~30s on 128).
set -u
FARM="$HOME/skills_evolve/webarena/scripts/farm.sh"
LOGD="$HOME/skills_evolve/webarena/logs"
N="${1:-12}"; WAVE="${2:-4}"
mkdir -p "$LOGD"
k=1
while [ "$k" -le "$N" ]; do
  end=$(( k + WAVE - 1 )); [ "$end" -gt "$N" ] && end="$N"
  echo "[$(date +%T)] === WAVE s$k..s$end (load=$(cut -d' ' -f1 /proc/loadavg)) ==="
  pids=()
  for j in $(seq "$k" "$end"); do
    ( bash "$FARM" start "s$j" > "$LOGD/start_s${j}.log" 2>&1 ) &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p"; done
  echo "[$(date +%T)] === wave s$k..s$end done (load=$(cut -d' ' -f1 /proc/loadavg)) ==="
  k=$(( end + 1 ))
done
echo "[$(date +%T)] ==== ALL WAVES DONE ===="
docker ps --format '{{.Names}}' | grep -cE '^wa_gitlab_s'
