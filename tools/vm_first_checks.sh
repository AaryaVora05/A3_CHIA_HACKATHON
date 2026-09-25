#!/usr/bin/env bash
# First checks ON THE VM (Pin 3.22 + ChampSim tracer). Run before any characterization.
#   export PIN_BIN=... TRACER_SO=...        then:   bash tools/vm_first_checks.sh
# Verifies C1 (-s skip lands in the loop), C2 (per-iteration record counts), C3 (XMM
# register IDs and op i -> op i-D chains survive the tracer), C4 (branch_taken sequences
# identical for LOAD_BR_DEP=0/1), C5 (load addresses in span/hot).
# If C3 fails: set ALU_XMM=0 in run_probe.py KNOB_SPEC defaults (GPR fallback; see README).
set -euo pipefail
cd "$(dirname "$0")/.."
: "${PIN_BIN:?set PIN_BIN}"; : "${TRACER_SO:?set TRACER_SO}"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
COMMON="-DNCHAINS=2 -DALU_OPS=12 -DDEP_DIST=3 -DNBRANCH=4 -DBR_PATTERN=3 -DBR_TAKEN=0.5 -DRANDOMNESS=0.4 -DREUSE=0.3 -DWSS_KB=1024"
for L in 0 1; do
  gcc -O2 -static -fno-pie -no-pie $COMMON -DLOAD_BR_DEP=$L probes/template/probe.c -o "$T/p$L" -lm
  python3 tools/check_static.py "$T/p$L" | tail -1
  # init length: trace --dry-run into a FIFO and count 64-byte records
  mkfifo "$T/f$L"
  ( wc -c < "$T/f$L" > "$T/n$L" ) &
  "$PIN_BIN" -t "$TRACER_SO" -o "$T/f$L" -s 0 -t 1000000000000 -- "$T/p$L" --dry-run >/dev/null 2>&1
  wait
  SKIP=$(( $(cat "$T/n$L") / 64 ))
  echo "LOAD_BR_DEP=$L: init length (skip) = $SKIP instructions"
  "$PIN_BIN" -t "$TRACER_SO" -o "$T/t$L.champsimtrace" -s "$SKIP" -t 200000 -- "$T/p$L" --iters 100000 >/dev/null 2>&1
done
python3 tools/check_champsim_trace.py "$T/p0" "$T/t0.champsimtrace" --compare "$T/p1" "$T/t1.champsimtrace"
