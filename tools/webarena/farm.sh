#!/bin/bash
# WebArena stack farm manager. DEPLOYED COPY LIVES ON 162:
#   /data3/wushang/skills_evolve/webarena/scripts/farm.sh
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

site_port()  { case "$1" in shopping) echo 7770;; shopping_admin) echo 7780;;
               reddit) echo 9999;; gitlab) echo 8023;; esac; }
ctrl_port()  { case "$1" in shopping) echo 8783;; shopping_admin) echo 8781;;
               reddit) echo 8782;; gitlab) echo 8784;; esac; }
inner_port() { case "$1" in gitlab) echo 8023;; *) echo 80;; esac; }
ready_path() { case "$1" in gitlab) echo "/explore";; *) echo "/";; esac; }

# Port prefix per stack: s1="" s2=1 s3=2 s4=3 ... (sN -> N-1). ctrl prefix
# avoids the 18781 collision with another tenant by using 3,4,5,...
stack_num()   { echo "${1#s}"; }
prefix()      { local n; n="$(stack_num "$1")"; [ "$n" = 1 ] && echo "" || echo "$((n-1))"; }
ctrl_prefix() { local n; n="$(stack_num "$1")"; [ "$n" = 1 ] && echo "" || echo "$((n+1))"; }

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
    local name="$1" site="$2" stack="$3" p url
    p="$(prefix "$stack")"; url="http://localhost:${p}$(site_port "$site")"
    [ "$site" = shopping_admin ] && url="${url}"   # env-ctrl handles the /admin area
    docker exec "$name" env-ctrl init --base-url "$url" >/dev/null 2>&1 \
        && echo "  repointed $name -> $url"
}

start_site() {  # start_site <stack> <site>
    local stack="$1" site="$2" p cp name extra="" img
    p="$(prefix "$stack")"; cp="$(ctrl_prefix "$stack")"
    name="wa_${site}_${stack}"; img="$(stack_img "$site" "$stack")"
    docker rm -f "$name" >/dev/null 2>&1
    [ "$site" = gitlab ] && extra="--shm-size=512m"
    docker run -d --name "$name" $extra \
        -p "${p}$(site_port "$site")":"$(inner_port "$site")" \
        -p "${cp}$(ctrl_port "$site")":8877 \
        "$img" >/dev/null && echo "started $name ($img)"
    # gitlab from a baked per-stack image already carries the right external_url;
    # everything else is re-pointed live after it comes up.
    [ "$site" = gitlab ] && [ "$img" != "$(golden_img gitlab)" ] && return 0
    return 0
}

wait_ready() {  # wait_ready <stack> <site> — 3 consecutive rails-backed 2xx/3xx
    local stack="$1" site="$2" p port t0 code ok=0
    p="$(prefix "$stack")"; port="${p}$(site_port "$site")"
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
                     [ "$3" = gitlab ] && wait_ready "$2" "$3"
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
