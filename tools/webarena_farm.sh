#!/bin/bash
# WebArena stack farm manager. DEPLOYED COPY LIVES ON 162:
#   /data3/wushang/skills_evolve/webarena/scripts/farm.sh
# (infra script, mechanism-free; version-controlled here like vllm_cluster.sh)
#
# Stacks boot from golden warm commits (webarena-golden/<site>:warm) where the
# snapshot is trustworthy; reddit/gitlab run from the original images (their
# runtime snapshots restore dirty — reddit 500s; see webarena_PREP.md).
#   farm.sh start <s1|s2|s3>        start all four sites, wait until ready
#   farm.sh stop <stack>            stop+remove the stack's containers
#   farm.sh refresh <stack> <site>  recreate ONE site from source image and
#                                   BLOCK until it serves (lane reset — the
#                                   scheduler treats return as "lane usable")
#   farm.sh wait <stack>            block until every site of a stack serves
#   farm.sh status                  container table
# Port scheme: s1 = base ports; s2 = 1-prefixed; s3 = 2-prefixed.
#   site ports: shopping 7770, shopping_admin 7780, reddit 9999, gitlab 8023
#   env-ctrl:   shopping 8783, shopping_admin 8781, reddit 8782, gitlab 8784
#   (ctrl prefixes: s2=3, s3=4 — 18781 is taken by another tenant)
set -u
GOLDEN=webarena-golden
SITES="shopping shopping_admin reddit gitlab"
# gitlab boots from the original image: observed multi-minute readiness
# (502 for 4+ min during the 2026-07-06 smoke); generous ceiling, early exit.
READY_TIMEOUT="${READY_TIMEOUT:-1200}"

site_port() { case "$1" in shopping) echo 7770;; shopping_admin) echo 7780;;
              reddit) echo 9999;; gitlab) echo 8023;; esac; }
ctrl_port() { case "$1" in shopping) echo 8783;; shopping_admin) echo 8781;;
              reddit) echo 8782;; gitlab) echo 8784;; esac; }
inner_port() { case "$1" in gitlab) echo 8023;; *) echo 80;; esac; }
golden_img() { case "$1" in shopping_admin) echo "$GOLDEN/admin:warm";;
               reddit|gitlab) echo "am1n3e/webarena-verified-$1:latest";;
               *) echo "$GOLDEN/$1:warm";; esac; }
prefix() { case "$1" in s1) echo "";; s2) echo "1";; s3) echo "2";;
           *) echo "BAD"; return 1;; esac; }
ctrl_prefix() { case "$1" in s1) echo "";; s2) echo "3";; s3) echo "4";;
           *) echo "BAD"; return 1;; esac; }

start_site() {  # start_site <stack> <site>
    local stack="$1" site="$2" p cp name extra=""
    p="$(prefix "$stack")" || return 1
    cp="$(ctrl_prefix "$stack")" || return 1
    name="wa_${site}_${stack}"
    docker rm -f "$name" >/dev/null 2>&1
    [ "$site" = "gitlab" ] && extra="--shm-size=512m"
    docker run -d --name "$name" $extra \
        -p "${p}$(site_port "$site")":"$(inner_port "$site")" \
        -p "${cp}$(ctrl_port "$site")":8877 \
        "$(golden_img "$site")" >/dev/null && echo "started $name"
}

wait_ready() {  # wait_ready <stack> <site> — block until the site answers HTTP 2xx/3xx
    local stack="$1" site="$2" p port t0 code
    p="$(prefix "$stack")" || return 1
    port="${p}$(site_port "$site")"
    t0=$(date +%s)
    while :; do
        code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' \
               "http://localhost:${port}/") || code=000
        case "$code" in
            2*|3*) echo "ready ${site}_${stack} after $(( $(date +%s) - t0 ))s (http $code)"
                   return 0;;
        esac
        if [ $(( $(date +%s) - t0 )) -ge "$READY_TIMEOUT" ]; then
            echo "TIMEOUT ${site}_${stack} not ready after ${READY_TIMEOUT}s (last http $code)" >&2
            return 1
        fi
        sleep 5
    done
}

case "${1:-}" in
    start)
        for s in $SITES; do start_site "$2" "$s"; done
        for s in $SITES; do wait_ready "$2" "$s" || exit 1; done ;;
    stop)
        for s in $SITES; do docker rm -f "wa_${s}_$2" >/dev/null 2>&1; done
        echo "stopped $2" ;;
    refresh)
        start_site "$2" "$3" && wait_ready "$2" "$3" ;;
    wait)
        for s in $SITES; do wait_ready "$2" "$s" || exit 1; done ;;
    status)
        docker ps --format '{{.Names}}\t{{.Status}}' | grep -E '^wa_' | sort ;;
    *)
        echo "usage: $0 {start <stack>|stop <stack>|refresh <stack> <site>|wait <stack>|status}"; exit 1 ;;
esac
