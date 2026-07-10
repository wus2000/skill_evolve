#!/bin/bash
# Bring up stacks 1..N in WAVES of size WAVE, waiting for each wave's stacks to be
# fully ready before the next. WAVE caps concurrent bring-up: fast sites are
# RAM-cheap but gitlab (HYBRID: on the shared HDD daemon) reconfigures are HDD-
# bound, and too many at once thrash the disk (all-at-once hit load 105 / 0
# progress, 2026-07-09). Each `farm.sh start` blocks on wait_ready, so peak
# concurrent gitlab reconfigure = WAVE. farm.sh routes each site to its daemon.
set -u
FARM="$HOME/skills_evolve/webarena/scripts/farm.sh"
LOGD="$HOME/skills_evolve/webarena/logs"
# gitlab lives on the rootless RAM daemon in the hybrid, so the "already up" probe
# and the final tally query THAT daemon explicitly (not any ambient DOCKER_HOST).
GITLAB_HOST="${WEBARENA_RAM_DOCKER_HOST:-unix:///run/user/1026/docker.sock}"
N="${1:-11}"; WAVE="${2:-4}"
mkdir -p "$LOGD"
k=1
while [ "$k" -le "$N" ]; do
  end=$(( k + WAVE - 1 )); [ "$end" -gt "$N" ] && end="$N"
  echo "[$(date +%T)] === WAVE s$k..s$end (load=$(cut -d' ' -f1 /proc/loadavg)) ==="
  pids=()
  for j in $(seq "$k" "$end"); do
    st="s$j"
    if docker -H "$GITLAB_HOST" ps --format '{{.Names}}' | grep -q "^wa_gitlab_${st}\$"; then
      echo "[$(date +%T)] [$st] already up, skip"; continue
    fi
    ( bash "$FARM" start "$st" > "$LOGD/start_${st}.log" 2>&1 ) &
    pids+=($!)
  done
  for p in "${pids[@]:-}"; do [ -n "$p" ] && wait "$p"; done
  echo "[$(date +%T)] === wave s$k..s$end done (load=$(cut -d' ' -f1 /proc/loadavg)) ==="
  k=$(( end + 1 ))
done
echo "[$(date +%T)] ==== ALL WAVES DONE ===="
docker -H "$GITLAB_HOST" ps --format '{{.Names}}' | grep -cE '^wa_gitlab_s'
