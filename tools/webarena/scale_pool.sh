#!/bin/bash
# Grow the WebArena replica pool to N stacks. Runs ON the farm host (162).
# Idempotent: existing stacks are left alone; only missing sN are provisioned.
#
#   scale_pool.sh <N>
#
# For each new stack sK it: starts all four sites (farm.sh start, which
# re-points base_url and boots gitlab from — or bakes — its per-stack image),
# then bakes gitlab into webarena-ready/gitlab_sK so later refreshes are
# single-reconfigure. gitlab is the long pole (~9-11 min cold boot); stacks are
# provisioned in parallel. Login state (127) and forwards are handled by the
# caller (tools/webarena_login.py, wa_forwards_ensure.sh).
set -u
FARM=/data3/wushang/skills_evolve/webarena/scripts/farm.sh
N="${1:?usage: scale_pool.sh <N>}"

provision() {  # provision one stack, then bake its gitlab
    local st="$1"
    echo "[$st] starting all sites..."
    bash "$FARM" start "$st"
    # If gitlab booted from the base image (no baked sN yet), bake it now so
    # subsequent refreshes are single-reconfigure.
    if ! docker image inspect "webarena-ready/gitlab_${st}:latest" >/dev/null 2>&1; then
        echo "[$st] baking gitlab ready image..."
        bash "$FARM" build-gitlab "$st"
    fi
    echo "[$st] done"
}

pids=()
for k in $(seq 1 "$N"); do
    st="s$k"
    if docker ps --format '{{.Names}}' | grep -q "^wa_gitlab_${st}\$"; then
        echo "[$st] already up, skip"; continue
    fi
    provision "$st" &
    pids+=($!)
done
for p in "${pids[@]:-}"; do [ -n "$p" ] && wait "$p"; done
echo "=== pool status ==="
bash "$FARM" status
