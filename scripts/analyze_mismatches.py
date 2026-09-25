#!/usr/bin/env python3
"""
scripts/analyze_mismatches.py -- Automated inspection of CHIA diagnosis mismatches.
Pulls out: oracle margin, turn-by-turn probes, knob overrides, IPC deltas, and row-hit-rates.
"""

import json
import os
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
SUMMARY_FILE = ROOT / "results" / "clones" / "chia_diagnosis_summary.json"
ORACLE_FILE = ROOT / "results" / "oracle" / "oracle_analysis_6class.csv"
CLONES_DIR = ROOT / "results" / "clones"

def analyze():
    if not SUMMARY_FILE.exists():
        print(f"Summary file not found: {SUMMARY_FILE}")
        return

    with open(SUMMARY_FILE) as f:
        summary = json.load(f)

    oracle_df = pd.read_csv(ORACLE_FILE).set_index("workload") if ORACLE_FILE.exists() else None

    mismatches = [s for s in summary if not s.get("correct", False)]
    print(f"================================================================================")
    print(f" CHIA DIAGNOSIS MISMATCH INSPECTION REPORT ({len(mismatches)} / {len(summary)} workloads mismatched)")
    print(f"================================================================================\n")

    for idx, item in enumerate(mismatches, 1):
        wl = item["workload"]
        print(f"[{idx}/{len(mismatches)}] Workload: {wl}")
        print(f"  Predicted Diagnosis: {item['agent_diagnosis']} (Conf: {item.get('confidence', 'N/A')})")
        print(f"  Oracle Ground Truth: {item['oracle_class']} ({item.get('oracle_speedup', 'N/A')})")

        if oracle_df is not None and wl in oracle_df.index:
            r = oracle_df.loc[wl]
            print(f"  Oracle Relaxations: Cache: {r['cache_pct']:+5.1f}% | Latency: {r['dram_lat_pct']:+5.1f}% | DRAM_BW: {r['dram_bw_pct']:+5.1f}% | Branch: {r['branch_pct']:+5.1f}%")
            print(f"  Oracle Margin: {r['margin_pp']:.1f} pp gap between 1st ({r['best_class']}) and 2nd ({r['second_class']}) [{r['status']}]")

        diag_file = CLONES_DIR / f"{wl}_diagnosis.json"
        if diag_file.exists():
            with open(diag_file) as df_in:
                diag_data = json.load(df_in)

            master_fp = diag_data.get("master_fingerprint", {})
            clone_fp = diag_data.get("cloned_fingerprint", {})
            history = diag_data.get("history", [])

            print(f"  Master FP: IPC={master_fp.get('IPC', 0):.3f}, BR={master_fp.get('BR_MPKI', 0):.2f}, LLC={master_fp.get('LLC_MPKI', 0):.2f}, DRAM_RQPI={master_fp.get('DRAM_RQPI', 0):.5f}")
            print(f"  Clone  FP: IPC={clone_fp.get('IPC', 0):.3f}, BR={clone_fp.get('BR_MPKI', 0):.2f}, LLC={clone_fp.get('LLC_MPKI', 0):.2f}, DRAM_RQPI={clone_fp.get('DRAM_RQPI', 0):.5f}")
            print(f"  Turn-by-turn Probing Trace:")
            base_ipc = clone_fp.get("IPC", 0.001)

            for step in history:
                turn = step.get("turn")
                probe = step.get("probe", {})
                res = step.get("result", {})
                fp = res.get("fingerprint", {})
                sec = res.get("secondary", {})
                p_ipc = fp.get("IPC", 0.0)
                delta = ((p_ipc - base_ipc) / base_ipc) * 100.0 if base_ipc > 0 else 0.0
                row_hit = sec.get("DRAM_row_hit_rate", 0.0) * 100.0

                print(f"    Turn {turn} [{probe.get('hypothesis_tested', 'N/A').upper()}]: Overrides={probe.get('knob_overrides')} -> IPC={p_ipc:.3f} (Δ={delta:+6.1f}%), LLC_MPKI={fp.get('LLC_MPKI', 0):.2f}, Row_Hit={row_hit:.1f}%")

        print("-" * 80)

if __name__ == "__main__":
    analyze()
