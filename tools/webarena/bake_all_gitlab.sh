#!/bin/bash
# Bake each stack's (already booted + repointed) gitlab into webarena-ready/
# gitlab_sN so runtime refresh recreates from the baked image (docker run, no
# gitlab-ctl reconfigure — the HDD-slow step). Sequential: docker commit is
# disk-write heavy and 128's docker root is a spinning disk. Idempotent.
set -u
FARM="$HOME/skills_evolve/webarena/scripts/farm.sh"
N="${1:-12}"
for k in $(seq 1 "$N"); do
  st="s$k"
  if ! docker ps --format '{{.Names}}' | grep -q "^wa_gitlab_${st}\$"; then
    echo "[$st] gitlab not running, skip"; continue
  fi
  if docker image inspect "webarena-ready/gitlab_${st}:latest" >/dev/null 2>&1; then
    echo "[$st] already baked, skip"; continue
  fi
  echo "[$(date +%T)] baking $st (load=$(cut -d' ' -f1 /proc/loadavg))..."
  bash "$FARM" build-gitlab "$st"
done
echo "[$(date +%T)] ==== BAKE DONE ===="
docker images --format '{{.Repository}}:{{.Tag}} {{.Size}}' | grep "webarena-ready/gitlab" | sort
