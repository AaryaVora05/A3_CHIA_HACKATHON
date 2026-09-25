#!/usr/bin/env bash
# run_all_diagnoses.sh -- Autonomous CHIA Loop Runner for All Master Traces
# A3/CHIA Hackathon Pipeline
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "================================================================================"
echo " A3/CHIA Hackathon: Autonomous Bottleneck Diagnosis Runner"
echo "================================================================================"

# 1. Activate Python virtual environment
if [ -f "agent/.venv/bin/activate" ]; then
    source agent/.venv/bin/activate
else
    echo "Error: agent/.venv not found. Please set up the environment."
    exit 1
fi

# 2. Export required Pin & ChampSim simulator paths
export PIN_BIN="${PIN_BIN:-$HOME/pin-3.22-reference/pin}"
export TRACER_SO="${TRACER_SO:-$HOME/ChampSim-pin322-reference/tracer/pin/obj-intel64/champsim_tracer.so}"
export CHAMPSIM_BIN="${CHAMPSIM_BIN:-$HOME/ChampSim/bin/champsim}"

# 3. Clean stale Ray session locks to ensure fresh initialization
rm -rf /tmp/ray

# 4. Prepare logs directory
mkdir -p results/clones
LOG_FILE="results/clones/all_diagnoses.log"

# 5. Default arguments if none passed: run all master traces with 3 turns
ARGS=("$@")
if [ ${#ARGS[@]} -eq 0 ]; then
    ARGS=("--all" "--turns" "3")
fi

echo "Simulator Binary:   $CHAMPSIM_BIN"
echo "Tracer SO:          $TRACER_SO"
echo "Arguments:          ${ARGS[*]}"
echo "Logging output to:  $LOG_FILE"
echo "================================================================================"

# 6. Execute CHIA Loop Runner
python -u agent/chia_diagnostic_loop.py "${ARGS[@]}" 2>&1 | tee "$LOG_FILE"

echo ""
echo "================================================================================"
echo " Diagnosis complete! Results saved in:"
echo "   - JSON Report:     results/clones/chia_diagnosis_summary.json"
echo "   - Individual Runs: results/clones/*_diagnosis.json"
echo "   - Full Log:        $LOG_FILE"
echo "================================================================================"
