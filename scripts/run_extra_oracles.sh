#!/usr/bin/env bash
set -euo pipefail

ROOT="$HOME/a3-hackathon"

TRACE_DIR="$ROOT/workloads/dpc3"
TRACE_LIST="$TRACE_DIR/maxweight_26.txt"

BIN_DIR="$ROOT/configs/M0/bin"

# Use the SAME result directories as the existing oracle sweep.
RESULT_ROOT="$ROOT/results/oracle"


# ============================================================
# MUST MATCH PREVIOUS ORACLE RUNS
# ============================================================

WARMUP=50000000
SIM=200000000

MAX_JOBS=4


CONFIG_NAMES=(
    latency
    mshr
    rob
)

CONFIG_BINS=(
    "$BIN_DIR/m0_latency"
    "$BIN_DIR/m0_mshr"
    "$BIN_DIR/m0_rob"
)


# ============================================================
# VALIDATION
# ============================================================

if [ ! -f "$TRACE_LIST" ]; then
    echo "ERROR: trace list missing:"
    echo "$TRACE_LIST"
    exit 1
fi

for bin in "${CONFIG_BINS[@]}"; do

    if [ ! -x "$bin" ]; then
        echo "ERROR: missing binary:"
        echo "$bin"
        exit 1
    fi

done


run_one () {

    local TRACE_NAME="$1"
    local CONFIG="$2"
    local BINARY="$3"

    local TRACE="$TRACE_DIR/$TRACE_NAME"
    local WORKLOAD="${TRACE_NAME%.champsimtrace.xz}"

    local OUTDIR="$RESULT_ROOT/$WORKLOAD"

    local LOG="$OUTDIR/${CONFIG}.txt"
    local JSON="$OUTDIR/${CONFIG}.json"
    local TIME="$OUTDIR/${CONFIG}.time"

    mkdir -p "$OUTDIR"


    if [ ! -f "$TRACE" ]; then
        echo "[ERROR] missing trace: $TRACE"
        return 1
    fi


    # Resume-safe
    if [ -s "$LOG" ] &&
       grep -q "ChampSim completed all CPUs" "$LOG"; then

        echo "[SKIP] $WORKLOAD / $CONFIG"
        return 0

    fi


    echo "[START] $WORKLOAD / $CONFIG"


    /usr/bin/time \
        -o "$TIME" \
        -f "elapsed_seconds=%e\nmax_rss_kb=%M\nexit_code=%x" \
        "$BINARY" \
        --warmup-instructions "$WARMUP" \
        --simulation-instructions "$SIM" \
        --json "$JSON" \
        "$TRACE" \
        > "$LOG" 2>&1


    if grep -q "ChampSim completed all CPUs" "$LOG"; then
        echo "[DONE]  $WORKLOAD / $CONFIG"
    else
        echo "[FAIL]  $WORKLOAD / $CONFIG"
        return 1
    fi
}


wait_for_slot () {

    while [ "$(jobs -rp | wc -l)" -ge "$MAX_JOBS" ]; do
        sleep 2
    done
}


while read -r TRACE_NAME; do

    [ -z "$TRACE_NAME" ] && continue

    for i in "${!CONFIG_NAMES[@]}"; do

        wait_for_slot

        run_one \
            "$TRACE_NAME" \
            "${CONFIG_NAMES[$i]}" \
            "${CONFIG_BINS[$i]}" &

    done

done < "$TRACE_LIST"


wait


echo
echo "=================================================="
echo "EXTRA ORACLE SWEEP COMPLETE"
echo "=================================================="
echo "26 workloads × 3 interventions = 78 runs"
