#!/bin/bash
# !!! ABANDONED — DO NOT USE (kept as a record of a dead end, 2026-07-09) !!!
# Idea: bake each stack's repointed gitlab into webarena-ready/gitlab_sN so
# refresh could docker-run it with no reconfigure. IT DOES NOT WORK: a committed
# gitlab re-run SKIPS gitlab-ctl reconfigure (env-ctrl sees the baked external_url
# already correct) and boots with broken nginx->puma wiring -> instant 502. Use
# base + live repoint for refresh instead (farm.sh does this when no baked image
# exists; 163s solo on 128). Left here so nobody re-tries baking.
#
# (original intent below)
# Bake each stack's (already booted + repointed) gitlab into webarena-ready/
# gitlab_sN so runtime refresh recreates from the baked image (docker run, no
# gitlab-ctl reconfigure — the HDD-slow step). Sequential: docker commit is
# disk-write heavy and 128's docker root is a spinning disk. Idempotent.
echo "bake_all_gitlab.sh is ABANDONED (baked gitlab re-run 502s). Aborting." >&2; exit 2
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
