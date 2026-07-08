#!/usr/bin/env bash
# llm_tunnel_guard.sh — self-healing local forwards for the remote vLLM arms.
#
#   127.0.0.1:8888 -> 173.0.69.2:8888   (via jump yaozhiming@183.174.61.200)
#   127.0.0.1:8889 -> 173.0.69.2:8889
#
# Guarantee: as long as the remote vLLM server is actually up, the local
# forwards stay usable. The guard probes END-TO-END (an HTTP request through
# the local forward, not just "is ssh alive") every PROBE_INTERVAL seconds;
# any failed probe tears the tunnel down and rebuilds it. If the probe still
# fails after a rebuild, the vLLM side itself is likely down — the guard
# keeps probing with backoff and the forwards recover the moment vLLM does.
# A cron line (installed by `install-cron`) re-runs `start` every 5 minutes,
# so even a killed guard (or a reboot) self-heals; `start` is idempotent
# (a lock file keeps exactly one guard alive).
#
# RATE-LIMIT WARNING (inherited from the retired llm_tunnels_ensure.sh): a
# previous per-port 5s-loop guard got rate-limit-banned by the relay's sshd.
# This guard is safe by construction — healthy probes only curl the LOCAL
# forwards (no ssh connections), and rebuilds happen only on failure with
# 15->120s backoff (worst case ~1 connection per 2 minutes while the far
# side is down). Do NOT "tune" PROBE_INTERVAL into a reconnect interval.
#
# Usage:
#   ~/llm_tunnel_guard.sh start          # one-key start (idempotent)
#   ~/llm_tunnel_guard.sh stop           # stop guard + tunnel
#   ~/llm_tunnel_guard.sh status         # health + processes + log tail
#   ~/llm_tunnel_guard.sh probe          # one end-to-end probe (exit code)
#   ~/llm_tunnel_guard.sh install-cron   # add the */5 cron self-heal line
set -u

JUMP="yaozhiming@183.174.61.200"
TARGET="173.0.69.2"
PORTS=(8888 8889)
API_KEY="token-abc123"

BASE_DIR="$HOME/.llm_tunnel_guard"
LOG="$BASE_DIR/guard.log"
GUARD_PID_FILE="$BASE_DIR/guard.pid"
SSH_PID_FILE="$BASE_DIR/ssh.pid"
LOCK="$BASE_DIR/guard.lock"

PROBE_INTERVAL=15      # seconds between healthy probes
PROBE_TIMEOUT=6        # per-port curl timeout
MAX_BACKOFF=120        # cap while the vLLM side itself is down

mkdir -p "$BASE_DIR"

log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }

rotate_log() {
    local sz
    sz=$(stat -c%s "$LOG" 2>/dev/null || echo 0)
    if [ "$sz" -gt 5000000 ]; then
        tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
    fi
}

probe_port() {  # $1 = port; end-to-end through the local forward
    curl -s -m "$PROBE_TIMEOUT" -H "Authorization: Bearer $API_KEY" \
         "http://127.0.0.1:$1/v1/models" 2>/dev/null | grep -q '"id"'
}

probe_all() {
    local p
    for p in "${PORTS[@]}"; do
        probe_port "$p" || return 1
    done
    return 0
}

ssh_pid() { [ -f "$SSH_PID_FILE" ] && cat "$SSH_PID_FILE" 2>/dev/null; }

ssh_alive() {
    local pid
    pid=$(ssh_pid) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}

kill_foreign_tunnels() {
    # Any OTHER ssh holding our local forwards (e.g. a hand-started one)
    # blocks the rebuild via ExitOnForwardFailure — take it over. pgrep
    # selects; kill receives numeric pids only.
    local own pids p
    own=$(ssh_pid || true)
    pids=$(pgrep -f "ssh .*-L *(127\.0\.0\.1:)?${PORTS[0]}:${TARGET}:${PORTS[0]}" || true)
    for p in $pids; do
        [ "$p" = "${own:-}" ] && continue
        log "taking over foreign tunnel pid $p"
        kill "$p" 2>/dev/null
    done
}

start_tunnel() {
    local old args p
    old=$(ssh_pid || true)
    if [ -n "${old:-}" ]; then
        kill "$old" 2>/dev/null
        sleep 1
    fi
    kill_foreign_tunnels
    args=()
    for p in "${PORTS[@]}"; do
        args+=(-L "127.0.0.1:${p}:${TARGET}:${p}")
    done
    ssh -N "${args[@]}" \
        -o BatchMode=yes -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
        -o TCPKeepAlive=yes -o ConnectTimeout=10 \
        -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
        -o LogLevel=ERROR \
        "$JUMP" &
    echo $! > "$SSH_PID_FILE"
    log "tunnel started (ssh pid $!)"
}

guard_loop() {
    echo $$ > "$GUARD_PID_FILE"
    trap 'log "guard stopping (pid $$)"; kill "$(ssh_pid)" 2>/dev/null; exit 0' TERM INT
    log "guard started (pid $$, ports ${PORTS[*]} -> ${TARGET} via ${JUMP})"
    local fails=0 backoff=$PROBE_INTERVAL alive
    start_tunnel
    sleep 4
    while :; do
        if probe_all; then
            [ "$fails" -gt 0 ] && log "recovered after $fails failed probe(s)"
            fails=0
            backoff=$PROBE_INTERVAL
        else
            fails=$((fails + 1))
            alive="dead"; ssh_alive && alive="alive"
            log "probe FAILED (#$fails, ssh=$alive) — rebuilding tunnel"
            start_tunnel
            sleep 4
            if probe_all; then
                log "rebuild OK"
                fails=0
                backoff=$PROBE_INTERVAL
            else
                # Tunnel is fresh yet the probe still fails: the vLLM side
                # (or the jump host) is down. Keep guarding with backoff —
                # the forwards recover the moment the far side does.
                log "still failing after rebuild — far side likely down;" \
                    "guarding with ${backoff}s backoff"
                backoff=$(( backoff * 2 > MAX_BACKOFF ? MAX_BACKOFF : backoff * 2 ))
            fi
        fi
        rotate_log
        sleep "$backoff"
    done
}

case "${1:-start}" in
    _guard)
        exec 9>"$LOCK"
        flock -n 9 || exit 0   # exactly one guard; extra starts are no-ops
        guard_loop
        ;;
    start)
        if exec 9>"$LOCK"; flock -n 9; then
            flock -u 9
            nohup setsid bash "$0" _guard >> "$LOG" 2>&1 &
            sleep 5
        fi
        if probe_all; then
            echo "OK: guard running (pid $(cat "$GUARD_PID_FILE" 2>/dev/null)), both forwards healthy"
        else
            echo "guard launched; probe not healthy yet — check: $0 status"
        fi
        ;;
    stop)
        for f in "$GUARD_PID_FILE" "$SSH_PID_FILE"; do
            if [ -f "$f" ]; then
                kill "$(cat "$f")" 2>/dev/null
                rm -f "$f"
            fi
        done
        echo "stopped"
        ;;
    status)
        if probe_all; then echo "probe: HEALTHY (both ports end-to-end)"; else echo "probe: FAILING"; fi
        echo "guard pid: $(cat "$GUARD_PID_FILE" 2>/dev/null || echo '-') | ssh pid: $(cat "$SSH_PID_FILE" 2>/dev/null || echo '-')"
        echo "--- last log lines:"
        tail -n 8 "$LOG" 2>/dev/null
        ;;
    probe)
        if probe_all; then echo OK; exit 0; else echo FAIL; exit 1; fi
        ;;
    install-cron)
        line="*/5 * * * * $HOME/llm_tunnel_guard.sh start >/dev/null 2>&1"
        (crontab -l 2>/dev/null | grep -vF "llm_tunnel_guard.sh"; echo "$line") | crontab -
        echo "cron installed: $line"
        ;;
    *)
        echo "usage: $0 {start|stop|status|probe|install-cron}"
        exit 2
        ;;
esac
