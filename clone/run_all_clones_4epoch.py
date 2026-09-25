#!/usr/bin/env python3
"""
clone/run_all_clones_4epoch.py -- Run 4-epoch MicroGrad cloner across all 26 workloads.
"""
import csv
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
sys.path.insert(0, str(PROJECT_ROOT / "clone"))

from observation import extract_observation
from micrograd_cloner import clone_workload


def main():
    maxweight_csv = PROJECT_ROOT / "workloads" / "dpc3" / "maxweight_26.csv"
    with open(maxweight_csv) as f:
        traces = [row["trace"].replace(".champsimtrace.xz", "") for row in csv.DictReader(f)]

    print("=" * 96)
    print(f" Starting Canonical 4-Epoch Cloner Tuning for all {len(traces)} Workloads (Unified 1M/500k)")
    print("=" * 96)

    results = []
    t_start_all = time.time()
    for idx, w in enumerate(traces, 1):
        baseline_json = PROJECT_ROOT / "results" / "oracle" / w / "baseline.json"
        if not baseline_json.exists():
            print(f"[{idx}/{len(traces)}] Skipping {w} (no baseline.json)")
            continue

        print(f"\n>>> [{idx}/{len(traces)}] Optimizing clone for '{w}' (4 epochs, 4 workers)...")
        obs = extract_observation(baseline_json)
        target_fp = obs["fingerprint"]
        out_dir = PROJECT_ROOT / "results" / "clones"
        res = clone_workload(w, target_fp, epochs=4, num_workers=4, output_dir=str(out_dir))
        if res:
            results.append(res)

    total_time = time.time() - t_start_all
    print("\n" + "=" * 96)
    print(f" 26-Workload Canonical 4-Epoch Cloning Summary (Finished in {total_time/60:.1f} mins)")
    print("=" * 96)
    print(f"{'Workload':<22} | {'Loss':>8} | {'Composite':>10} | {'Max Error':>10} | {'Gate':>8} | {'Time':>6}")
    print("-" * 96)
    passed_count = 0
    for r in results:
        gate_str = "PASS" if r.get("passed_gate") else "FAIL"
        if r.get("passed_gate"):
            passed_count += 1
        print(f"{r['workload']:<22} | {r['loss']:8.4f} | {r['composite_error']:9.2f}z | {r['max_error']:9.2f}z | {gate_str:>8} | {r['tuning_time_sec']:5.1f}s")
    print("-" * 96)
    cov_pct = (passed_count / max(len(results), 1)) * 100.0
    print(f"Total Clones Accepted (Coverage): {passed_count}/{len(results)} ({cov_pct:.1f}%)\n")


if __name__ == "__main__":
    main()
