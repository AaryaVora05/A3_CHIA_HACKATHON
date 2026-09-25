#!/usr/bin/env bash

set -u
set -o pipefail

ROOT="$HOME/a3-hackathon"

TRACE_DIR="$ROOT/workloads/dpc3"
RESULT_DIR="$ROOT/results/oracle"
BIN_DIR="$ROOT/configs/M0/bin"

MANIFEST="$TRACE_DIR/extra26_targeted.txt"
FAILED_FILE="$ROOT/results/extra26_targeted_failed.tsv"

DPC3_URL="https://dpc3.compas.cs.stonybrook.edu/champsim-traces/speccpu"

WARMUP=50000000
SIM=200000000

# 8-vCPU VM
MAX_JOBS="${MAX_JOBS:-6}"
DOWNLOAD_JOBS="${DOWNLOAD_JOBS:-4}"

mkdir -p "$TRACE_DIR"
mkdir -p "$RESULT_DIR"

###############################################################################
# TARGETED 26 ADDITIONAL SIMPOINTS
###############################################################################

cat > "$MANIFEST" <<'EOF'
641.leela_s-149B.champsimtrace.xz
641.leela_s-334B.champsimtrace.xz
648.exchange2_s-72B.champsimtrace.xz
648.exchange2_s-353B.champsimtrace.xz
458.sjeng-31B.champsimtrace.xz
445.gobmk-17B.champsimtrace.xz
473.astar-42B.champsimtrace.xz
429.mcf-22B.champsimtrace.xz
429.mcf-184B.champsimtrace.xz
437.leslie3d-134B.champsimtrace.xz
437.leslie3d-232B.champsimtrace.xz
482.sphinx3-417B.champsimtrace.xz
620.omnetpp_s-141B.champsimtrace.xz
450.soplex-92B.champsimtrace.xz
605.mcf_s-472B.champsimtrace.xz
605.mcf_s-484B.champsimtrace.xz
605.mcf_s-782B.champsimtrace.xz
605.mcf_s-994B.champsimtrace.xz
619.lbm_s-2676B.champsimtrace.xz
619.lbm_s-2677B.champsimtrace.xz
619.lbm_s-3766B.champsimtrace.xz
649.fotonik3d_s-1B.champsimtrace.xz
433.milc-274B.champsimtrace.xz
433.milc-337B.champsimtrace.xz
EOF

TRACE_COUNT=$(wc -l < "$MANIFEST")

if [ "$TRACE_COUNT" -ne 26 ]; then
    echo "ERROR: expected 26 traces, found $TRACE_COUNT"
    exit 1
fi

###############################################################################
# CONFIGURATIONS
###############################################################################

CONFIG_NAMES=(
    baseline
    branch
    cache
    dram4
    latency
    mshr
    rob
)

CONFIG_BINS=(
    "$BIN_DIR/m0_baseline"
    "$BIN_DIR/m0_branch"
    "$BIN_DIR/m0_cache"
    "$BIN_DIR/m0_dram4"
    "$BIN_DIR/m0_latency"
    "$BIN_DIR/m0_mshr"
    "$BIN_DIR/m0_rob"
)

echo
echo "============================================================"
echo "TARGETED EXTRA-26 ORACLE SWEEP"
echo "============================================================"
echo "vCPUs:       $(nproc)"
echo "Traces:      $TRACE_COUNT"
echo "Configs:     ${#CONFIG_NAMES[@]}"
echo "Total runs:  $((TRACE_COUNT * ${#CONFIG_NAMES[@]}))"
echo "Concurrency: $MAX_JOBS"
echo "Warmup:      $WARMUP"
echo "ROI:         $SIM"
echo "============================================================"
echo

###############################################################################
# VERIFY BINARIES
###############################################################################

for i in "${!CONFIG_NAMES[@]}"; do
    cfg="${CONFIG_NAMES[$i]}"
    bin="${CONFIG_BINS[$i]}"

    if [ ! -x "$bin" ]; then
        echo "ERROR: missing/non-executable binary:"
        echo "  $cfg -> $bin"
        exit 1
    fi
done

echo "All seven ChampSim binaries found."

###############################################################################
# DOWNLOAD MISSING TRACES
###############################################################################

DOWNLOAD_LIST="$TRACE_DIR/extra26_targeted_missing.txt"
: > "$DOWNLOAD_LIST"

while read -r trace; do
    [ -z "$trace" ] && continue

    if [ ! -s "$TRACE_DIR/$trace" ]; then
        echo "$trace" >> "$DOWNLOAD_LIST"
    fi
done < "$MANIFEST"

MISSING=$(wc -l < "$DOWNLOAD_LIST")

echo
echo "Traces already present: $((26 - MISSING))"
echo "Traces to download:     $MISSING"
echo

if [ "$MISSING" -gt 0 ]; then
    cd "$TRACE_DIR" || exit 1

    xargs -P "$DOWNLOAD_JOBS" -I{} \
        wget \
        --continue \
        --tries=5 \
        --timeout=90 \
        "$DPC3_URL/{}" \
        < "$DOWNLOAD_LIST"
fi

###############################################################################
# VERIFY DOWNLOADS
###############################################################################

BAD=0

while read -r trace; do
    [ -z "$trace" ] && continue

    if [ ! -s "$TRACE_DIR/$trace" ]; then
        echo "MISSING TRACE: $trace"
        BAD=$((BAD + 1))
    fi
done < "$MANIFEST"

if [ "$BAD" -ne 0 ]; then
    echo "ERROR: $BAD required traces are missing."
    exit 1
fi

echo "All 26 required traces are present."

###############################################################################
# PROGRESS
###############################################################################

progress () {
    local grand=0

    echo
    echo "============================================================"
    echo "PROGRESS"
    echo "============================================================"

    for cfg in "${CONFIG_NAMES[@]}"; do
        local done_count=0

        while read -r trace; do
            [ -z "$trace" ] && continue

            workload="${trace%.champsimtrace.xz}"
            log="$RESULT_DIR/$workload/${cfg}.txt"

            if [ -s "$log" ] &&
               grep -q "ChampSim completed all CPUs" "$log"; then
                done_count=$((done_count + 1))
            fi
        done < "$MANIFEST"

        grand=$((grand + done_count))

        printf "%-10s %2d / 26\n" "$cfg" "$done_count"
    done

    echo "------------------------------------------------------------"
    echo "TOTAL      $grand / 182"
    echo "============================================================"
    echo
}

progress

###############################################################################
# RUN ONE SIMULATION
###############################################################################

run_one () {
    local trace_name="$1"
    local cfg="$2"
    local binary="$3"

    local trace="$TRACE_DIR/$trace_name"
    local workload="${trace_name%.champsimtrace.xz}"

    local outdir="$RESULT_DIR/$workload"

    local logfile="$outdir/${cfg}.txt"
    local jsonfile="$outdir/${cfg}.json"
    local timefile="$outdir/${cfg}.time"

    mkdir -p "$outdir"

    # Resume safety
    if [ -s "$logfile" ] &&
       grep -q "ChampSim completed all CPUs" "$logfile"; then
        echo "[$(date '+%H:%M:%S')] [SKIP]  $workload / $cfg"
        return 0
    fi

    echo "[$(date '+%H:%M:%S')] [START] $workload / $cfg"

    rm -f "$jsonfile"

    /usr/bin/time \
        -o "$timefile" \
        -f "elapsed_seconds=%e\nmax_rss_kb=%M\nexit_code=%x" \
        "$binary" \
        --warmup-instructions "$WARMUP" \
        --simulation-instructions "$SIM" \
        --json "$jsonfile" \
        "$trace" \
        > "$logfile" 2>&1

    rc=$?

    if [ "$rc" -eq 0 ] &&
       grep -q "ChampSim completed all CPUs" "$logfile"; then
        echo "[$(date '+%H:%M:%S')] [DONE]  $workload / $cfg"
        return 0
    fi

    echo "[$(date '+%H:%M:%S')] [FAIL]  $workload / $cfg rc=$rc"
    return "$rc"
}

###############################################################################
# CONCURRENCY
###############################################################################

wait_for_slot () {
    while [ "$(jobs -rp | wc -l)" -ge "$MAX_JOBS" ]; do
        wait -n || true
    done
}

###############################################################################
# SCHEDULE
#
# Trace-major ordering keeps all seven configs for a trace close together.
###############################################################################

echo
echo "Starting remaining simulations..."
echo

while read -r trace; do
    [ -z "$trace" ] && continue

    for i in "${!CONFIG_NAMES[@]}"; do
        wait_for_slot

        run_one \
            "$trace" \
            "${CONFIG_NAMES[$i]}" \
            "${CONFIG_BINS[$i]}" &
    done

done < "$MANIFEST"

wait || true

###############################################################################
# FINAL CHECK
###############################################################################

echo
echo "All scheduled jobs have exited."

progress

: > "$FAILED_FILE"

for cfg in "${CONFIG_NAMES[@]}"; do
    while read -r trace; do
        [ -z "$trace" ] && continue

        workload="${trace%.champsimtrace.xz}"
        logfile="$RESULT_DIR/$workload/${cfg}.txt"

        if ! (
            [ -s "$logfile" ] &&
            grep -q "ChampSim completed all CPUs" "$logfile"
        ); then
            printf "%s\t%s\n" "$trace" "$cfg" >> "$FAILED_FILE"
        fi
    done < "$MANIFEST"
done

FAILED=$(wc -l < "$FAILED_FILE")

echo
echo "============================================================"

if [ "$FAILED" -eq 0 ]; then
    echo "SUCCESS"
    echo "26 traces x 7 configurations = 182 / 182 completed."
else
    echo "INCOMPLETE"
    echo "$FAILED runs did not complete."
    echo
    cat "$FAILED_FILE"
    echo
    echo "Rerun this same script; completed runs will be skipped."
fi

echo "============================================================"
