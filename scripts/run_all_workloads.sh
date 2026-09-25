#!/usr/bin/env bash
set -euo pipefail

ROOT="$HOME/a3-hackathon"

TRACE_DIR="$ROOT/workloads/dpc3"
TRACE_LIST="$TRACE_DIR/maxweight_26.txt"

BIN_DIR="$ROOT/configs/M0/bin"
RESULT_ROOT="$ROOT/results/oracle"

# ============================================================
# EXPERIMENT LENGTH
# ============================================================

WARMUP=50000000
SIM=200000000

# For an 8-vCPU VM, start conservatively with 4.
MAX_JOBS=4


# ============================================================
# CONFIGURATIONS
# ============================================================

CONFIG_NAMES=(
    baseline
    branch
    cache
    dram4
)

CONFIG_BINS=(
    "$BIN_DIR/m0_baseline"
    "$BIN_DIR/m0_branch"
    "$BIN_DIR/m0_cache"
    "$BIN_DIR/m0_dram4"
)


mkdir -p "$RESULT_ROOT"


# ============================================================
# BASIC VALIDATION
# ============================================================

if [ ! -f "$TRACE_LIST" ]; then
    echo "ERROR: workload list not found:"
    echo "$TRACE_LIST"
    exit 1
fi

for bin in "${CONFIG_BINS[@]}"; do
    if [ ! -x "$bin" ]; then
        echo "ERROR: binary missing or not executable:"
        echo "$bin"
        exit 1
    fi
done


# ============================================================
# RUN ONE SIMULATION
# ============================================================

run_one() {

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
        echo "[ERROR] Missing trace: $TRACE"
        return 1
    fi


    # Resume support:
    # skip simulations that already completed successfully.
    if [ -s "$LOG" ] && grep -q "ChampSim completed all CPUs" "$LOG"; then
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


# ============================================================
# JOB THROTTLING
# ============================================================

wait_for_slot() {

    while [ "$(jobs -rp | wc -l)" -ge "$MAX_JOBS" ]; do
        sleep 2
    done
}


# ============================================================
# GENERATE ALL 26 × 4 RUNS
# ============================================================

while read -r TRACE_NAME; do

    # Ignore blank lines.
    [ -z "$TRACE_NAME" ] && continue

    for i in "${!CONFIG_NAMES[@]}"; do

        wait_for_slot

        run_one \
            "$TRACE_NAME" \
            "${CONFIG_NAMES[$i]}" \
            "${CONFIG_BINS[$i]}" &

    done

done < "$TRACE_LIST"


# Wait for everything remaining.
wait


echo
echo "============================================================"
echo "FULL SWEEP FINISHED"
echo "Warmup instructions    : $WARMUP"
echo "Simulation instructions: $SIM"
echo "Maximum parallel jobs  : $MAX_JOBS"
echo "Results directory      : $RESULT_ROOT"
echo "============================================================"
