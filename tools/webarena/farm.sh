#!/bin/bash
# WebArena stack farm manager. DEPLOYED COPY LIVES ON 128 (zkgy-gpu; the site
# farm moved off the contended 162 on 2026-07-09):
#   /home/wushang/skills_evolve/webarena/scripts/farm.sh
# (infra script, mechanism-free; version-controlled in the repo like this copy)
#
# A "stack" is one replica of all four sites on a distinct port block. Stacks
# scale for concurrency; the port scheme is parameterized so the pool can grow
# past s1/s2/s3 without editing case statements.
#
# base_url correctness (2026-07-08): the golden/base images bake the s1 ports,
# so EVERY replica must be re-pointed at its own ports or its in-page links
# send the agent to s1 (silent cross-stack bleed — mutations land on the wrong
# replica, isolation is a lie). shopping/admin/reddit are re-pointed live with
# `env-ctrl init --base-url` (3-6s). gitlab boots from a per-stack baked image
# `webarena-ready/gitlab_<stack>` so its ~9-min cold boot reconfigures ONCE
# with the right external_url instead of twice.
#
#   farm.sh start   <stack>        start all four sites, re-point, wait ready
#   farm.sh stop    <stack>        stop+remove the stack's containers
#   farm.sh refresh <stack> <site> recreate ONE site clean, re-point, BLOCK
#                                  until it serves (lane reset)
#   farm.sh wait    <stack>        block until every site of a stack serves
#   farm.sh build-gitlab <stack>   bake webarena-ready/gitlab_<stack> from the
#                                  running (already re-pointed) gitlab
#   farm.sh status                 container table
#   farm.sh ready-pool <site> <n>  ensure n spare CLEAN replicas of <site> exist
#                                  as wa_<site>_pool<k> (warm pool; see below)
set -u
GOLDEN=webarena-golden
READY=webarena-ready
SITES="shopping shopping_admin reddit gitlab"
READY_TIMEOUT="${READY_TIMEOUT:-1200}"

# ── Port scheme (2026-07-09, farm on 128): each stack owns a contiguous 4-port
# block  host_port = BASE_PORT + (N-1)*STACK_STRIDE + site_offset.  The old
# "${N-1}${base}" concat capped the pool at s6 — s7 shopping = 67770 > 65535 and
# docker refused it ("invalid hostPort"). Blocks from 30000 stay below the 32768
# ephemeral floor and scale to ~50 stacks. host_port() is the SINGLE SOURCE OF
# TRUTH; launcher _stack_urls() and wa_forwards_ensure.sh mirror it exactly
# (BASE 30000, STRIDE 10, offsets shopping0/admin1/reddit2/gitlab3).
BASE_PORT="${WEBARENA_BASE_PORT:-30000}"
STACK_STRIDE=10
site_offset() { case "$1" in shopping) echo 0;; shopping_admin) echo 1;;
                reddit) echo 2;; gitlab) echo 3;; esac; }
stack_num()  { echo "${1#s}"; }
host_port()  { local n; n="$(stack_num "$1")"
               echo $(( BASE_PORT + (n-1)*STACK_STRIDE + $(site_offset "$2") )); }
# ctrl port (8877) is reached via `docker exec env-ctrl`, never published.
inner_port() { case "$1" in gitlab) echo 8023;; *) echo 80;; esac; }
ready_path() { case "$1" in gitlab) echo "/explore";; *) echo "/";; esac; }

golden_img() { case "$1" in shopping_admin) echo "$GOLDEN/admin:warm";;
               reddit) echo "am1n3e/webarena-verified-reddit:latest";;
               gitlab) echo "am1n3e/webarena-verified-gitlab:latest";;
               *) echo "$GOLDEN/$1:warm";; esac; }
# gitlab prefers its per-stack baked image when present (correct external_url,
# single boot reconfigure); falls back to the base image + live re-point.
stack_img() {  # stack_img <site> <stack>
    local site="$1" stack="$2"
    if [ "$site" = gitlab ] && docker image inspect "$READY/gitlab_${stack}:latest" >/dev/null 2>&1; then
        echo "$READY/gitlab_${stack}:latest"
    else
        golden_img "$site"
    fi
}

repoint() {  # repoint <name> <site> <stack> — set the container's own base_url
    local name="$1" site="$2" stack="$3" url
    url="http://localhost:$(host_port "$stack" "$site")"
    docker exec "$name" env-ctrl init --base-url "$url" >/dev/null 2>&1 \
        && echo "  repointed $name -> $url"
}

start_site() {  # start_site <stack> <site>
    local stack="$1" site="$2" name extra="" img
    name="wa_${site}_${stack}"; img="$(stack_img "$site" "$stack")"
    docker rm -f "$name" >/dev/null 2>&1
    [ "$site" = gitlab ] && extra="--shm-size=512m"
    docker run -d --name "$name" $extra \
        -p "$(host_port "$stack" "$site")":"$(inner_port "$site")" \
        "$img" >/dev/null && echo "started $name ($img)"
    # gitlab from a baked per-stack image already carries the right external_url;
    # everything else is re-pointed live after it comes up.
    [ "$site" = gitlab ] && [ "$img" != "$(golden_img gitlab)" ] && return 0
    return 0
}

wait_ready() {  # wait_ready <stack> <site> — 3 consecutive rails-backed 2xx/3xx
    local stack="$1" site="$2" port t0 code ok=0
    port="$(host_port "$stack" "$site")"
    t0=$(date +%s)
    while :; do
        code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
               "http://localhost:${port}$(ready_path "$site")") || code=000
        case "$code" in
            2*|3*) ok=$((ok+1)); [ "$ok" -ge 3 ] && {
                       echo "ready ${site}_${stack} after $(( $(date +%s)-t0 ))s (http $code)"; return 0; };;
            *) ok=0;;
        esac
        [ $(( $(date +%s)-t0 )) -ge "$READY_TIMEOUT" ] && {
            echo "TIMEOUT ${site}_${stack} after ${READY_TIMEOUT}s (last $code)" >&2; return 1; }
        sleep 5
    done
}

start_stack() {  # start + repoint(non-gitlab, or gitlab-on-base) + wait
    local stack="$1" s img
    for s in $SITES; do start_site "$stack" "$s"; done
    for s in $SITES; do wait_ready "$stack" "$s" || return 1; done
    for s in shopping shopping_admin reddit; do repoint "wa_${s}_${stack}" "$s" "$stack"; done
    # gitlab only needs a live repoint if it booted from the base image
    img="$(stack_img gitlab "$stack")"
    if [ "$img" = "$(golden_img gitlab)" ]; then
        repoint "wa_gitlab_${stack}" gitlab "$stack"
        wait_ready "$stack" gitlab
    fi
}

case "${1:-}" in
    start)   start_stack "$2" ;;
    stop)    for s in $SITES; do docker rm -f "wa_${s}_$2" >/dev/null 2>&1; done
             echo "stopped $2" ;;
    refresh) start_site "$2" "$3" && wait_ready "$2" "$3" && {
                 img="$(stack_img "$3" "$2")"
                 if [ "$3" != gitlab ] || [ "$img" = "$(golden_img gitlab)" ]; then
                     repoint "wa_${3}_$2" "$3" "$2"
                     # if-fi (not `&& wait_ready`): a bare `[ x = gitlab ] &&`
                     # tail evaluates FALSE for non-gitlab sites, which made the
                     # whole refresh arm exit rc=1 on success (env.py then raised
                     # "refresh rc=1" and leaked the lane). if-fi returns 0 when
                     # the condition is false.
                     if [ "$3" = gitlab ]; then wait_ready "$2" "$3"; fi
                 fi; } ;;
    wait)    for s in $SITES; do wait_ready "$2" "$s" || exit 1; done ;;
    build-gitlab)  # bake the running (re-pointed) gitlab into a per-stack image
             name="wa_gitlab_$2"
             docker exec "$name" grep -E '^external_url' /etc/gitlab/gitlab.rb
             docker commit "$name" "$READY/gitlab_$2:latest" >/dev/null \
                 && echo "committed $READY/gitlab_$2:latest" ;;
    status)  docker ps --format '{{.Names}}\t{{.Status}}' | grep -E '^wa_' | sort ;;
    *) echo "usage: $0 {start <stack>|stop <stack>|refresh <stack> <site>|wait <stack>|build-gitlab <stack>|status}"; exit 1 ;;
esac
