#!/usr/bin/env bash
set -euo pipefail

ROOT="$HOME/a3-hackathon"

BIN_DIR="$ROOT/configs/M0/bin"
WORKLOAD_DIR="$ROOT/workloads/dpc3"
RESULT_ROOT="$ROOT/results/pilot"

WARMUP=5000000
SIM=20000000


if [ "$#" -ne 1 ]; then
    echo "Usage:"
    echo "  $0 TRACE_FILENAME"
    exit 1
fi


TRACE_NAME="$1"
TRACE="$WORKLOAD_DIR/$TRACE_NAME"

if [ ! -f "$TRACE" ]; then
    echo "ERROR: Trace does not exist:"
    echo "$TRACE"
    exit 1
fi


WORKLOAD="${TRACE_NAME%.champsimtrace.xz}"

RESULT_DIR="$RESULT_ROOT/$WORKLOAD"

mkdir -p "$RESULT_DIR"


run_one () {

    CONFIG="$1"
    BINARY="$2"

    echo "Starting $CONFIG"

    /usr/bin/time \
        -o "$RESULT_DIR/${CONFIG}.time" \
        -f "elapsed_seconds=%e\nmax_rss_kb=%M\nexit_code=%x" \
        "$BINARY" \
        --warmup-instructions "$WARMUP" \
        --simulation-instructions "$SIM" \
        --json "$RESULT_DIR/${CONFIG}.json" \
        "$TRACE" \
        > "$RESULT_DIR/${CONFIG}.txt" 2>&1
}


run_one baseline "$BIN_DIR/m0_baseline" &
PID_BASE=$!

run_one branch "$BIN_DIR/m0_branch" &
PID_BRANCH=$!

run_one cache "$BIN_DIR/m0_cache" &
PID_CACHE=$!

run_one dram4 "$BIN_DIR/m0_dram4" &
PID_DRAM=$!


echo
echo "Processes:"
echo "baseline : $PID_BASE"
echo "branch   : $PID_BRANCH"
echo "cache    : $PID_CACHE"
echo "dram4    : $PID_DRAM"

echo
echo "Waiting for all four..."


wait "$PID_BASE"
wait "$PID_BRANCH"
wait "$PID_CACHE"
wait "$PID_DRAM"


echo
echo "All four simulations finished."
echo "Results:"
echo "$RESULT_DIR"
