#!/bin/bash
# Bring up stacks sFROM..sTO in parallel. NO per-stack gitlab bake: on the idle
# 128, gitlab boots+repoints in ~30s (vs 162's 9-11min), so farm.sh start uses
# the am1n3e base image + live repoint for every stack; refresh recreates from
# base and resets to the image's baked-in WebArena data. Per-stack logs.
set -u
FARM="$HOME/skills_evolve/webarena/scripts/farm.sh"
LOGD="$HOME/skills_evolve/webarena/logs"
FROM="${1:?usage: bring_up_stacks.sh <from> <to>}"; TO="${2:?}"
mkdir -p "$LOGD"
pids=()
for k in $(seq "$FROM" "$TO"); do
  st="s$k"
  if docker ps --format '{{.Names}}' | grep -q "^wa_gitlab_${st}\$"; then
    echo "[$st] already up, skip"; continue
  fi
  ( bash "$FARM" start "$st" > "$LOGD/start_${st}.log" 2>&1 ) &
  pids+=($!)
done
for p in "${pids[@]:-}"; do [ -n "$p" ] && wait "$p"; done
echo "==== BRING-UP s${FROM}..s${TO} DONE ===="
bash "$FARM" status
