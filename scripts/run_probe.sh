#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# V1.1 Probe Runner
#
# Usage:
#   ./scripts/run_probe.sh baseline
#   ./scripts/run_probe.sh branch_random
#   ./scripts/run_probe.sh small_ws
#   ./scripts/run_probe.sh all
#
# Pipeline:
#   semantic case -> gcc -> Pin 3.22 -> ChampSim M0
# ============================================================

ROOT="$HOME/a3-hackathon"

SRC="$ROOT/probes/template/probe.c"

PIN="$HOME/pin-3.22-reference/pin"
TRACER="$HOME/ChampSim-pin322-reference/tracer/pin/obj-intel64/champsim_tracer.so"
CHAMPSIM="$HOME/ChampSim/bin/champsim"

OUT="${OUT:-$ROOT/results/v11_probes}"
mkdir -p "$OUT"

# Small first-pass simulation.
TRACE_INSTRUCTIONS=1000000
WARMUP=100000
SIM=500000

# ------------------------------------------------------------
# V1.1 default semantic configuration
# ------------------------------------------------------------

B=0
BR_PATTERN=0
BR_PATTERN_PARAM=0
LBD=0

WSS_KB=64
STRIDE_LINES=1
RND_PCT=0
REUSE_PCT=0

MEM_DEP=0
CHAINS=1

ALU_OPS=0
DEP_DIST=1

# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------

die() {
    echo "ERROR: $*" >&2
    exit 1
}

compile_probe() {
    local dir="$1"
    local bin="$dir/probe"

    echo "[1/4] Compiling"

    gcc -O2 \
    -fno-pie \
    -no-pie \
    -fno-if-conversion \
    -fno-if-conversion2 \
    -DNBRANCH="$B" \
    -DBR_PATTERN="$BR_PATTERN" \
    -DLOAD_BR_DEP="$LBD" \
    -DWSS_KB="$WSS_KB" \
    -DSTRIDE_LINES="$STRIDE_LINES" \
    -DRANDOMNESS="$RND_PCT/100.0" \
    -DREUSE="$REUSE_PCT/100.0" \
    -DDEPENDENT="$MEM_DEP" \
    -DNCHAINS="$CHAINS" \
    -DALU_OPS="$ALU_OPS" \
    -DDEP_DIST="$DEP_DIST" \
    "$SRC" \
    -o "$bin"

    echo "      binary: $bin"
}

# ------------------------------------------------------------
# Named semantic cases
# ------------------------------------------------------------

set_case() {
    local name="$1"

    case "$name" in

        baseline)
            ;;

        branch_heavy)
            B=8
            ;;

        branch_random)
            B=4
            BR_PATTERN=3
            BR_PATTERN_PARAM=50
            ;;

        load_branch_dep)
            B=4
            BR_PATTERN=3
            BR_PATTERN_PARAM=50
            LBD=1
            ;;

        small_ws)
            WSS_KB=4
            ;;

        ws_16kb)
            WSS_KB=16
            ;;

        ws_32kb)
            WSS_KB=32
            ;;

        ws_256kb)
            WSS_KB=256
            ;;

        ws_1mb)
            WSS_KB=1024
            ;;

        ws_4mb)
            WSS_KB=4096
            ;;

        ws_16mb)
            WSS_KB=16384
            ;;

        large_ws)
            WSS_KB=65536
            ;;

        large_stride)
            STRIDE_LINES=64
            ;;

        random_memory)
            WSS_KB=65536
            RND_PCT=100
            ;;

        dependent_memory)
            WSS_KB=65536
            RND_PCT=100
            MEM_DEP=1
            ;;

        high_mlp)
            WSS_KB=65536
            RND_PCT=100
            CHAINS=8
            ;;

        serial_alu)
            ALU_OPS=32
            DEP_DIST=1
            ;;

        high_ilp)
            ALU_OPS=32
            DEP_DIST=8
            ;;

        high_reuse)
            WSS_KB=65536
            REUSE_PCT=90
            ;;

        # --------------------------------------------------------
        # Characterization: working-set coverage
        # --------------------------------------------------------

        ws_4kb)
            WSS_KB=4
            ;;

        ws_64kb)
            WSS_KB=64
            ;;

        ws_64mb)
            WSS_KB=65536
            ;;

        # --------------------------------------------------------
        # Characterization: randomness
        # --------------------------------------------------------

        random_25)
            WSS_KB=65536
            RND_PCT=25
            ;;

        random_50)
            WSS_KB=65536
            RND_PCT=50
            ;;

        random_75)
            WSS_KB=65536
            RND_PCT=75
            ;;

        random_100)
            WSS_KB=65536
            RND_PCT=100
            ;;

        # --------------------------------------------------------
        # Characterization: reuse
        # --------------------------------------------------------

        reuse_25)
            WSS_KB=65536
            REUSE_PCT=25
            ;;

        reuse_50)
            WSS_KB=65536
            REUSE_PCT=50
            ;;

        reuse_75)
            WSS_KB=65536
            REUSE_PCT=75
            ;;

        reuse_90)
            WSS_KB=65536
            REUSE_PCT=90
            ;;

        # --------------------------------------------------------
        # Characterization: stride
        # --------------------------------------------------------

        stride_1)
            WSS_KB=65536
            STRIDE_LINES=1
            ;;

        stride_4)
            WSS_KB=65536
            STRIDE_LINES=4
            ;;

        stride_16)
            WSS_KB=65536
            STRIDE_LINES=16
            ;;

        stride_64)
            WSS_KB=65536
            STRIDE_LINES=64
            ;;

        # --------------------------------------------------------
        # Characterization: MLP / memory dependency
        # --------------------------------------------------------

        independent_c1)
            WSS_KB=65536
            RND_PCT=100
            CHAINS=1
            ;;

        independent_c2)
            WSS_KB=65536
            RND_PCT=100
            CHAINS=2
            ;;

        independent_c4)
            WSS_KB=65536
            RND_PCT=100
            CHAINS=4
            ;;

        independent_c8)
            WSS_KB=65536
            RND_PCT=100
            CHAINS=8
            ;;

        dependent_c1)
            WSS_KB=65536
            RND_PCT=100
            MEM_DEP=1
            CHAINS=1
            ;;

        dependent_c4)
            WSS_KB=65536
            RND_PCT=100
            MEM_DEP=1
            CHAINS=4
            ;;

        # --------------------------------------------------------
        # Characterization: branch behavior
        # --------------------------------------------------------

        branch_low)
            B=2
            ;;

        br_0)
            B=0
            ;;

        br_2)
            B=2
            ;;

        br_4)
            B=4
            ;;

        br_8)
            B=8
            ;;

        br_16)
            B=16
            ;;

        br_32)
            B=32
            ;;

        br_64)
            B=64
            ;;

        branch_high)
            B=8
            ;;

        branch_alternating)
            B=4
            BR_PATTERN=1
            ;;

        branch_random)
            B=4
            BR_PATTERN=3
            BR_PATTERN_PARAM=50
            ;;

        # --------------------------------------------------------
        # Characterization: ALU dependency
        # --------------------------------------------------------

        alu_low)
            ALU_OPS=8
            DEP_DIST=8
            ;;

        alu_high_serial)
            ALU_OPS=32
            DEP_DIST=1
            ;;

        alu_high_ilp)
            ALU_OPS=32
            DEP_DIST=8
            ;;

        *)
            die "Unknown case '$name'"
            ;;
    esac
}

show_config() {
    echo
    echo "Configuration:"
    echo "  branches             = $B"
    echo "  branch pattern       = $BR_PATTERN"
    echo "  branch pattern param = $BR_PATTERN_PARAM"
    echo "  load-branch dep      = $LBD"
    echo "  working set (KB)     = $WSS_KB"
    echo "  stride (lines)       = $STRIDE_LINES"
    echo "  randomness (%)       = $RND_PCT"
    echo "  reuse (%)            = $REUSE_PCT"
    echo "  memory dependency    = $MEM_DEP"
    echo "  independent chains   = $CHAINS"
    echo "  ALU ops              = $ALU_OPS"
    echo "  dependency distance  = $DEP_DIST"
    echo
}

run_one() {
    local name="$1"

    # Reset semantic parameters.
    B=0
    BR_PATTERN=0
    BR_PATTERN_PARAM=0
    LBD=0

    WSS_KB=64
    STRIDE_LINES=1
    RND_PCT=0
    REUSE_PCT=0

    MEM_DEP=0
    CHAINS=1

    ALU_OPS=0
    DEP_DIST=1

    set_case "$name"

    local dir="$OUT/$name"
    local bin="$dir/probe"
    local trace="$dir/trace.champsimtrace"
    local trace_xz="$trace.xz"
    local stats="$dir/champsim.json"
    local config="$dir/config.txt"

    mkdir -p "$dir"

    echo
    echo "============================================================"
    echo "Probe: $name"
    echo "============================================================"

    show_config

    cat > "$config" <<CFG
case=$name
branches=$B
branch_pattern=$BR_PATTERN
branch_pattern_param=$BR_PATTERN_PARAM
load_branch_dependency=$LBD
working_set_kb=$WSS_KB
stride_lines=$STRIDE_LINES
randomness_pct=$RND_PCT
reuse_pct=$REUSE_PCT
memory_dependency=$MEM_DEP
independent_chains=$CHAINS
alu_ops=$ALU_OPS
dependency_distance=$DEP_DIST
CFG

    compile_probe "$dir"

    echo
    echo "[2/4] Generating trace"

    rm -f "$trace" "$trace_xz"

    "$PIN" \
        -t "$TRACER" \
        -o "$trace" \
        -s 0 \
        -t "$TRACE_INSTRUCTIONS" \
        -- "$bin"

    echo
    echo "[3/4] Compressing trace"

    xz -T0 "$trace"

    echo
    echo "[4/4] Running ChampSim"

    "$CHAMPSIM" \
        --warmup-instructions "$WARMUP" \
        --simulation-instructions "$SIM" \
        --json "$stats" \
        "$trace_xz"

    echo
    echo "Result:"
    echo "  $stats"
}

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------

if [[ $# -ne 1 ]]; then
    cat <<USAGE
Usage:
  $0 <case>
  $0 all

Cases:
  baseline
  branch_heavy
  branch_random
  load_branch_dep
  small_ws
  large_ws
  large_stride
  random_memory
  dependent_memory
  high_mlp
  serial_alu
  high_ilp
  high_reuse
  all
USAGE
    exit 1
fi

CASE="$1"

if [[ "$CASE" == "all" ]]; then
    for c in \
        baseline \
        branch_heavy \
        branch_random \
        load_branch_dep \
        small_ws \
        large_ws \
        large_stride \
        random_memory \
        dependent_memory \
        high_mlp \
        serial_alu \
        high_ilp \
        high_reuse
    do
        run_one "$c"
    done
else
    run_one "$CASE"
fi

echo
echo "============================================================"
echo "DONE"
echo "============================================================"
