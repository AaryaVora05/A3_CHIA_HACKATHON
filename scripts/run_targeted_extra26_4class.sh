#!/usr/bin/env bash

set -u
set -o pipefail

###############################################################################
# A3 / CHIA
#
# Targeted additional 26 SimPoints
#
# FINAL DIAGNOSTIC CLASSES:
#
#   BRANCH
#   CACHE
#   DRAM_BW   -> dram4
#   DRAM_LAT  -> latency
#
# Oracle additionally requires BASELINE.
#
# Therefore:
#
#   26 traces x 5 configurations = 130 simulations
#
# Existing successful simulations are reused automatically.
###############################################################################


ROOT="$HOME/a3-hackathon"

TRACE_DIR="$ROOT/workloads/dpc3"
RESULT_DIR="$ROOT/results/oracle"
BIN_DIR="$ROOT/configs/M0/bin"

MANIFEST="$TRACE_DIR/extra26_targeted.txt"

FAILED_FILE="$ROOT/results/extra26_4class_failed.tsv"
RUN_LOG="$ROOT/results/extra26_4class_run.log"

DPC3_URL="https://dpc3.compas.cs.stonybrook.edu/champsim-traces/speccpu"


###############################################################################
# EXPERIMENT PARAMETERS
###############################################################################

WARMUP=50000000
SIM=200000000

# Validated setting for the 8-vCPU host.
MAX_JOBS="${MAX_JOBS:-6}"

DOWNLOAD_JOBS="${DOWNLOAD_JOBS:-4}"


###############################################################################
# CREATE / FREEZE THE TARGETED 26-TRACE MANIFEST
###############################################################################

mkdir -p "$TRACE_DIR"
mkdir -p "$RESULT_DIR"


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


TRACE_COUNT="$(grep -cve '^[[:space:]]*$' "$MANIFEST")"


if [ "$TRACE_COUNT" -ne 26 ]; then
    echo "ERROR: Expected 26 traces, found $TRACE_COUNT"
    exit 1
fi


###############################################################################
# ONLY FIVE CONFIGURATIONS NOW
###############################################################################

CONFIG_NAMES=(
    baseline
    branch
    cache
    dram4
    latency
)


CONFIG_BINS=(
    "$BIN_DIR/m0_baseline"
    "$BIN_DIR/m0_branch"
    "$BIN_DIR/m0_cache"
    "$BIN_DIR/m0_dram4"
    "$BIN_DIR/m0_latency"
)


TOTAL_REQUIRED=$((TRACE_COUNT * ${#CONFIG_NAMES[@]}))


###############################################################################
# VERIFY BINARIES
###############################################################################

echo
echo "============================================================"
echo " A3 TARGETED FOUR-CLASS ORACLE SWEEP"
echo "============================================================"
echo "Host vCPUs:          $(nproc)"
echo "Traces:              $TRACE_COUNT"
echo "Oracle configs:      ${#CONFIG_NAMES[@]}"
echo "Total required:      $TOTAL_REQUIRED"
echo "Concurrent jobs:     $MAX_JOBS"
echo "Warmup:              $WARMUP"
echo "Simulation ROI:      $SIM"
echo
echo "Diagnostic classes:"
echo "  BRANCH"
echo "  CACHE"
echo "  DRAM_BW  (dram4)"
echo "  DRAM_LAT (latency)"
echo "============================================================"
echo


for i in "${!CONFIG_NAMES[@]}"; do

    cfg="${CONFIG_NAMES[$i]}"
    bin="${CONFIG_BINS[$i]}"

    if [ ! -x "$bin" ]; then

        echo "ERROR: Missing simulator binary:"
        echo "  $cfg -> $bin"

        exit 1

    fi

done


echo "All five simulator binaries found."


###############################################################################
# DOWNLOAD ANY MISSING TRACES
###############################################################################

DOWNLOAD_LIST="$TRACE_DIR/extra26_4class_missing_downloads.txt"

: > "$DOWNLOAD_LIST"


while read -r trace; do

    [ -z "$trace" ] && continue

    if [ ! -s "$TRACE_DIR/$trace" ]; then
        echo "$trace" >> "$DOWNLOAD_LIST"
    fi

done < "$MANIFEST"


MISSING="$(wc -l < "$DOWNLOAD_LIST")"


echo
echo "Trace files already present: $((TRACE_COUNT - MISSING))"
echo "Trace files to download:     $MISSING"


if [ "$MISSING" -gt 0 ]; then

    cd "$TRACE_DIR" || exit 1

    xargs \
        -P "$DOWNLOAD_JOBS" \
        -I{} \
        wget \
            --continue \
            --tries=5 \
            --timeout=90 \
            "$DPC3_URL/{}" \
        < "$DOWNLOAD_LIST"

fi


###############################################################################
# VERIFY TRACES
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

    echo "ERROR: $BAD required traces unavailable."

    exit 1

fi


###############################################################################
# PROGRESS FUNCTION
###############################################################################

progress () {

    local grand=0

    echo
    echo "============================================================"
    echo " FOUR-CLASS ORACLE PROGRESS"
    echo "============================================================"


    for cfg in "${CONFIG_NAMES[@]}"; do

        local done_count=0


        while read -r trace; do

            [ -z "$trace" ] && continue

            workload="${trace%.champsimtrace.xz}"

            logfile="$RESULT_DIR/$workload/${cfg}.txt"


            if [ -s "$logfile" ] &&
               grep -q "ChampSim completed all CPUs" "$logfile"; then

                done_count=$((done_count + 1))

            fi

        done < "$MANIFEST"


        grand=$((grand + done_count))


        printf "%-10s %2d / %2d\n" \
            "$cfg" \
            "$done_count" \
            "$TRACE_COUNT"

    done


    pct="$(
        awk \
          -v done="$grand" \
          -v total="$TOTAL_REQUIRED" \
          'BEGIN { printf "%.1f", 100*done/total }'
    )"


    echo "------------------------------------------------------------"
    echo "TOTAL      $grand / $TOTAL_REQUIRED"
    echo "PROGRESS   $pct%"
    echo "============================================================"
    echo

}


###############################################################################
# SHOW EXISTING COMPLETED WORK
###############################################################################

progress


###############################################################################
# COUNT REMAINING RUNS
###############################################################################

remaining=0


for cfg in "${CONFIG_NAMES[@]}"; do

    while read -r trace; do

        [ -z "$trace" ] && continue

        workload="${trace%.champsimtrace.xz}"

        logfile="$RESULT_DIR/$workload/${cfg}.txt"


        if ! (
            [ -s "$logfile" ] &&
            grep -q "ChampSim completed all CPUs" "$logfile"
        ); then

            remaining=$((remaining + 1))

        fi

    done < "$MANIFEST"

done


echo "Relevant simulations remaining: $remaining"
echo


if [ "$remaining" -eq 0 ]; then

    echo "Everything needed for the four-class oracle is already complete."

    exit 0

fi


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


    ###########################################################################
    # RESUME SAFETY
    ###########################################################################

    if [ -s "$logfile" ] &&
       grep -q "ChampSim completed all CPUs" "$logfile"; then

        echo "[$(date '+%H:%M:%S')] [SKIP]  $workload / $cfg"

        return 0

    fi


    echo "[$(date '+%H:%M:%S')] [START] $workload / $cfg"


    # Remove stale partial JSON left behind by an interrupted old run.
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
# CONCURRENCY LIMIT
###############################################################################

wait_for_slot () {

    while [ "$(jobs -rp | wc -l)" -ge "$MAX_JOBS" ]; do

        wait -n || true

    done

}


###############################################################################
# RUN ONLY MISSING RELEVANT SIMULATIONS
###############################################################################

echo
echo "Starting $remaining remaining relevant simulations..."
echo


#
# Trace-major scheduling.
#
# This is useful because configurations operating on the same compressed
# trace execute close together and can benefit from filesystem/page cache.
#

while read -r trace; do

    [ -z "$trace" ] && continue


    for i in "${!CONFIG_NAMES[@]}"; do

        cfg="${CONFIG_NAMES[$i]}"
        bin="${CONFIG_BINS[$i]}"

        workload="${trace%.champsimtrace.xz}"

        logfile="$RESULT_DIR/$workload/${cfg}.txt"


        # Do not even create a background process for completed work.
        if [ -s "$logfile" ] &&
           grep -q "ChampSim completed all CPUs" "$logfile"; then

            continue

        fi


        wait_for_slot


        run_one \
            "$trace" \
            "$cfg" \
            "$bin" &

    done

done < "$MANIFEST"


###############################################################################
# WAIT UNTIL ALL JOBS FINISH
###############################################################################

wait || true


echo
echo "All scheduled simulations exited."


###############################################################################
# FINAL COMPLETENESS CHECK
###############################################################################

progress


###############################################################################
# BUILD FAILED/MISSING REPORT
###############################################################################

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

            printf "%s\t%s\n" \
                "$trace" \
                "$cfg" \
                >> "$FAILED_FILE"

        fi

    done < "$MANIFEST"

done


FAILED="$(wc -l < "$FAILED_FILE")"


echo
echo "============================================================"


if [ "$FAILED" -eq 0 ]; then

    echo "SUCCESS"
    echo
    echo "All required four-class oracle simulations completed."
    echo
    echo "26 traces x 5 configs = 130 / 130"

else

    echo "INCOMPLETE"
    echo
    echo "$FAILED relevant simulations remain incomplete:"
    echo

    cat "$FAILED_FILE"

    echo
    echo "Simply rerun this script."
    echo "All completed cases will automatically be skipped."

fi


echo "============================================================"
echo

