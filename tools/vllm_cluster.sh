#!/usr/bin/env bash
# vllm_cluster.sh — one-command manager for N vLLM replicas on one machine.
#
#   ./vllm_cluster.sh start     # launch all replicas, wait until healthy
#   ./vllm_cluster.sh stop      # graceful stop (TERM -> wait -> KILL), whole
#                               # process group per replica (no orphan workers)
#   ./vllm_cluster.sh restart
#   ./vllm_cluster.sh status    # pid / health / GPU memory table
#   ./vllm_cluster.sh probe     # end-to-end chat probe against every replica
#   ./vllm_cluster.sh logs 8888 # tail -f one replica's log
#
# Deployment decided 2026-07-03: 4x A100-80G -> 2 replicas x TP=2, 256K context.
# Per replica: weights ~70GB BF16 (35GB/GPU) + ~75-80GB KV pool (~780K tokens)
# => holds ~3 concurrent full-256K sequences, or hundreds of short-context
# agent streams. vLLM refuses to start if the KV pool cannot hold one
# max-model-len sequence, so a mis-sized config fails loudly at startup.
#
# Copy this file to the vLLM host and edit the Config block. Requires: bash,
# curl, nvidia-smi, and `vllm` on PATH (or set VLLM_BIN).
set -uo pipefail

# ── Config (EDIT ME — or override via env for per-host variants) ───────────
# New-host deployment: copy this file + set env overrides, no edits needed:
#   VLLM_REPLICA_GPUS="0,1;2,3"   semicolon-separated CUDA_VISIBLE_DEVICES sets
#   VLLM_REPLICA_PORTS="8888 8889" space-separated, aligned with GPU sets
#   VLLM_TP=2                      tensor-parallel size per replica
# e.g. 8-GPU host, 4 replicas x TP=2:
#   VLLM_REPLICA_GPUS="0,1;2,3;4,5;6,7" VLLM_REPLICA_PORTS="8888 8889 8890 8891" ./vllm_cluster.sh start
# e.g. FP8 single-GPU variant, 4 replicas x TP=1 (needs an FP8 checkpoint):
#   VLLM_MODEL_PATH=/path/to/FP8 VLLM_REPLICA_GPUS="0;1;2;3" VLLM_REPLICA_PORTS="8888 8889 8890 8891" VLLM_TP=1 ./vllm_cluster.sh start
MODEL_PATH="${VLLM_MODEL_PATH:-/data3/wushang/model/Qwen/Qwen3.6-35B-A3B}"
SERVED_NAME="qwen3.6-35b-a3b"
API_KEY="token-abc123"
HOST="0.0.0.0"
MAX_MODEL_LEN=262144          # 256K context (model config must support it,
                              # e.g. native or YaRN; same as the old TP=4 deploy)
GPU_UTIL=0.8
MAX_NUM_SEQS=256              # per replica
MAX_BATCHED_TOKENS=32768      # chunked-prefill budget per engine step
TP_SIZE="${VLLM_TP:-2}"       # tensor-parallel size per replica
IFS=';' read -r -a REPLICA_GPUS <<< "${VLLM_REPLICA_GPUS:-0,1;2,3}"
IFS=' ' read -r -a REPLICA_PORTS <<< "${VLLM_REPLICA_PORTS:-8888 8889}"
VLLM_BIN="${VLLM_BIN:-vllm}"
RUN_DIR="${VLLM_CLUSTER_HOME:-$HOME/vllm_cluster}"   # pidfiles + logs
HEALTH_TIMEOUT=900            # first start loads ~70GB weights; be patient
STOP_TIMEOUT=60
# Watchdog (auto-restart dead replicas; started by `start`, stopped by `stop`)
WATCH_INTERVAL=30             # seconds between health sweeps
WATCH_MAX_RESTARTS=3          # flap breaker: max auto-restarts per port ...
WATCH_FLAP_WINDOW=1800        # ... within this many seconds; then hold off
# Extra args every replica gets. Tool parser is REQUIRED by the Bird
# function-calling agent. Prefix caching MUST be explicit: the deployed vLLM
# defaulted it OFF (measured 0.1% hit rate vs 42% on the old deployment),
# which made 10K-token optimizer prompts fully re-prefill on every call.
EXTRA_ARGS=(--enable-auto-tool-choice --tool-call-parser qwen3_coder --reasoning-parser qwen3 --enable-prefix-caching)
# ────────────────────────────────────────────────────────────────────────────

mkdir -p "$RUN_DIR"

say()      { echo "[vllm-cluster] $*"; }
pidfile()  { echo "$RUN_DIR/replica_$1.pid"; }
logfile()  { echo "$RUN_DIR/replica_$1.log"; }

alive() {  # alive <port> — 0 if the recorded process group leader is running
    local pf; pf="$(pidfile "$1")"
    [[ -f "$pf" ]] && kill -0 "$(cat "$pf")" 2>/dev/null
}

healthy() {  # healthy <port> — 0 if /health returns 200
    curl -s -o /dev/null -w "%{http_code}" --connect-timeout 2 \
        "http://127.0.0.1:$1/health" 2>/dev/null | grep -q "^200$"
}

start_one() {  # start_one <index>
    local gpus="${REPLICA_GPUS[$1]}" port="${REPLICA_PORTS[$1]}"
    if healthy "$port"; then
        say "replica :$port already healthy — skipping"
        return 0
    fi
    if alive "$port"; then
        say "replica :$port has a live pid but is not healthy yet (still loading?)"
        return 0
    fi
    say "starting replica :$port on GPUs $gpus ..."
    {
        echo "===== $(date -Is) start :$port gpus=$gpus ====="
    } >> "$(logfile "$port")"
    # setsid => own process group; stop kills the whole group (TP workers too).
    CUDA_VISIBLE_DEVICES="$gpus" setsid nohup "$VLLM_BIN" serve "$MODEL_PATH" \
        --served-model-name "$SERVED_NAME" \
        --seed 1024 \
        --host "$HOST" --port "$port" \
        --tensor-parallel-size "$TP_SIZE" \
        --reasoning-parser qwen3 \
        --enable-auto-tool-choice \
        --tool-call-parser qwen3_coder \
        --language-model-only \
        --enable-prefix-caching \
        --max-model-len "$MAX_MODEL_LEN" \
        --gpu-memory-utilization "$GPU_UTIL" \
        --max-num-seqs "$MAX_NUM_SEQS" \
        --max-num-batched-tokens "$MAX_BATCHED_TOKENS" \
        --api-key "$API_KEY" \
        "${EXTRA_ARGS[@]}" \
        >> "$(logfile "$port")" 2>&1 &
    echo $! > "$(pidfile "$port")"
}

wait_healthy() {  # wait_healthy <port>
    local port="$1" waited=0
    while (( waited < HEALTH_TIMEOUT )); do
        if healthy "$port"; then
            say "replica :$port HEALTHY (${waited}s)"
            return 0
        fi
        if ! alive "$port"; then
            say "replica :$port DIED during startup — last log lines:"
            tail -5 "$(logfile "$port")" | sed 's/^/    /'
            return 1
        fi
        sleep 5; waited=$((waited + 5))
    done
    say "replica :$port NOT healthy after ${HEALTH_TIMEOUT}s — check $(logfile "$port")"
    return 1
}

stop_one() {  # stop_one <port>
    local port="$1" pf pid waited=0
    pf="$(pidfile "$port")"
    if [[ ! -f "$pf" ]]; then
        # Fallback: match by our exact launch signature.
        pkill -TERM -f "vllm serve .*--port $port" 2>/dev/null && \
            say "replica :$port TERM sent via pattern (no pidfile)"
        return 0
    fi
    pid="$(cat "$pf")"
    if ! kill -0 "$pid" 2>/dev/null; then
        say "replica :$port already stopped"; rm -f "$pf"; return 0
    fi
    say "stopping replica :$port (pgid $pid) ..."
    kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    while (( waited < STOP_TIMEOUT )); do
        kill -0 "$pid" 2>/dev/null || break
        sleep 2; waited=$((waited + 2))
    done
    if kill -0 "$pid" 2>/dev/null; then
        say "replica :$port did not exit in ${STOP_TIMEOUT}s — KILL"
        kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    fi
    rm -f "$pf"
    say "replica :$port stopped"
}

# ── Watchdog: self-healing for crashed replicas ────────────────────────────
wd_pidfile() { echo "$RUN_DIR/watchdog.pid"; }
wd_logfile() { echo "$RUN_DIR/watchdog.log"; }

wd_alive() {
    [[ -f "$(wd_pidfile)" ]] && kill -0 "$(cat "$(wd_pidfile)")" 2>/dev/null
}

wd_restart_budget_ok() {  # wd_restart_budget_ok <port> — flap breaker
    local f="$RUN_DIR/restarts_$1.log" now cutoff n
    now=$(date +%s); cutoff=$((now - WATCH_FLAP_WINDOW))
    [[ -f "$f" ]] || return 0
    n=$(awk -v c="$cutoff" '$1 >= c' "$f" | wc -l)
    (( n < WATCH_MAX_RESTARTS ))
}

watchdog_loop() {  # internal: runs in its own process group
    declare -A grace_until
    local now port
    echo "===== $(date -Is) watchdog up (interval=${WATCH_INTERVAL}s) =====" >> "$(wd_logfile)"
    while true; do
        for i in "${!REPLICA_PORTS[@]}"; do
            port="${REPLICA_PORTS[$i]}"; now=$(date +%s)
            if healthy "$port"; then
                grace_until[$port]=0
                continue
            fi
            if alive "$port"; then
                # Alive but unhealthy: loading or wedged. Allow HEALTH_TIMEOUT
                # of grace from first observation before declaring it wedged.
                if [[ "${grace_until[$port]:-0}" == "0" ]]; then
                    grace_until[$port]=$((now + HEALTH_TIMEOUT))
                    echo "$(date -Is) :$port alive-but-unhealthy; grace ${HEALTH_TIMEOUT}s" >> "$(wd_logfile)"
                    continue
                fi
                (( now < grace_until[$port] )) && continue
                echo "$(date -Is) :$port WEDGED past grace — force restart" >> "$(wd_logfile)"
                stop_one "$port" >> "$(wd_logfile)" 2>&1
            else
                echo "$(date -Is) :$port DEAD (no process)" >> "$(wd_logfile)"
            fi
            if wd_restart_budget_ok "$port"; then
                date +%s >> "$RUN_DIR/restarts_$port.log"
                echo "$(date -Is) :$port auto-restarting" >> "$(wd_logfile)"
                start_one "$i" >> "$(wd_logfile)" 2>&1
                grace_until[$port]=$(( $(date +%s) + HEALTH_TIMEOUT ))
            else
                echo "$(date -Is) :$port FLAPPING (>=${WATCH_MAX_RESTARTS} restarts in ${WATCH_FLAP_WINDOW}s) — holding off; investigate $(logfile "$port")" >> "$(wd_logfile)"
            fi
        done
        sleep "$WATCH_INTERVAL"
    done
}

watchdog_start() {
    if wd_alive; then say "watchdog already running (pid $(cat "$(wd_pidfile)"))"; return 0; fi
    setsid nohup "$0" __watchdog >> "$(wd_logfile)" 2>&1 &
    echo $! > "$(wd_pidfile)"
    say "watchdog started (pid $!, interval ${WATCH_INTERVAL}s, flap breaker ${WATCH_MAX_RESTARTS}/${WATCH_FLAP_WINDOW}s)"
}

watchdog_stop() {
    if wd_alive; then
        kill -TERM -- "-$(cat "$(wd_pidfile)")" 2>/dev/null || kill -TERM "$(cat "$(wd_pidfile)")" 2>/dev/null
        say "watchdog stopped"
    fi
    rm -f "$(wd_pidfile)"
}

status() {
    if wd_alive; then say "watchdog: RUNNING (pid $(cat "$(wd_pidfile)"))"; else say "watchdog: not running"; fi
    printf "%-8s %-8s %-8s %-9s %s\n" PORT GPUS PID ALIVE HEALTH
    for i in "${!REPLICA_PORTS[@]}"; do
        local port="${REPLICA_PORTS[$i]}" pid="-" al="no" he="down"
        [[ -f "$(pidfile "$port")" ]] && pid="$(cat "$(pidfile "$port")")"
        alive "$port" && al="yes"
        healthy "$port" && he="ok"
        printf "%-8s %-8s %-8s %-9s %s\n" ":$port" "${REPLICA_GPUS[$i]}" "$pid" "$al" "$he"
    done
    command -v nvidia-smi >/dev/null && \
        nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu \
                   --format=csv,noheader | sed 's/^/    GPU /'
}

probe() {
    for port in "${REPLICA_PORTS[@]}"; do
        say "probe :$port ..."
        curl -s --max-time 120 "http://127.0.0.1:$port/v1/chat/completions" \
            -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
            -d "{\"model\":\"$SERVED_NAME\",\"messages\":[{\"role\":\"user\",\"content\":\"Say OK.\"}],\"max_tokens\":8,\"temperature\":0,\"chat_template_kwargs\":{\"enable_thinking\":false}}" \
            | head -c 300; echo
    done
}

case "${1:-}" in
    start)
        for i in "${!REPLICA_PORTS[@]}"; do start_one "$i"; done
        rc=0
        for port in "${REPLICA_PORTS[@]}"; do wait_healthy "$port" || rc=1; done
        watchdog_start
        status
        exit "$rc"
        ;;
    stop)
        watchdog_stop   # first — or it would resurrect what we stop below
        for port in "${REPLICA_PORTS[@]}"; do stop_one "$port"; done
        ;;
    __watchdog) watchdog_loop ;;
    watchdog-start) watchdog_start ;;
    watchdog-stop)  watchdog_stop ;;
    restart)
        "$0" stop && sleep 3 && exec "$0" start
        ;;
    status)  status ;;
    probe)   probe ;;
    logs)
        [[ -n "${2:-}" ]] || { say "usage: $0 logs <port>"; exit 1; }
        exec tail -f "$(logfile "$2")"
        ;;
    *)
        echo "usage: $0 {start|stop|restart|status|probe|logs <port>|watchdog-start|watchdog-stop}"
        exit 1
        ;;
esac
