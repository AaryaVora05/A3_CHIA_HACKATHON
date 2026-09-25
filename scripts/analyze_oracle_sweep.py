#!/usr/bin/env python3

import csv
import re
import statistics
from pathlib import Path

ROOT = Path.home() / "a3-hackathon"
RESULTS = ROOT / "results" / "oracle"

# These are exploratory ambiguity thresholds.
# Do NOT treat them as final paper thresholds yet.
MIN_MEANINGFUL_RECOVERY = 5.0   # %
MIN_MARGIN_PP = 3.0             # percentage points
MIN_RELATIVE_GAP = 0.20         # second-best must be >=20% below best


CONFIGS = {
    "branch": "branch",
    "cache": "cache",
    "dram": "dram4",
}


ipc_pattern = re.compile(
    r"^CPU 0 cumulative IPC:\s+([0-9.]+)",
    re.MULTILINE
)


def extract_ipc(path):
    if not path.exists():
        return None

    text = path.read_text(errors="replace")

    vals = ipc_pattern.findall(text)

    if not vals:
        return None

    # Last occurrence corresponds to final ROI statistics
    return float(vals[-1])


def completed(path):
    if not path.exists():
        return False

    text = path.read_text(errors="replace")

    return "ChampSim completed all CPUs" in text


rows = []

for workload_dir in sorted(RESULTS.iterdir()):

    if not workload_dir.is_dir():
        continue

    workload = workload_dir.name

    baseline_file = workload_dir / "baseline.txt"

    files = {
        "branch": workload_dir / "branch.txt",
        "cache": workload_dir / "cache.txt",
        "dram": workload_dir / "dram4.txt",
    }

    all_files = [baseline_file] + list(files.values())

    if not all(completed(x) for x in all_files):
        print(f"[INCOMPLETE] {workload}")
        continue

    ipc0 = extract_ipc(baseline_file)

    if ipc0 is None:
        print(f"[NO IPC] {workload}")
        continue

    ipcs = {}

    for cls, path in files.items():
        ipcs[cls] = extract_ipc(path)

    if any(v is None for v in ipcs.values()):
        print(f"[MISSING IPC] {workload}")
        continue

    recovery = {
        cls: 100.0 * (ipc - ipc0) / ipc0
        for cls, ipc in ipcs.items()
    }

    ranking = sorted(
        recovery.items(),
        key=lambda x: x[1],
        reverse=True
    )

    best_class, best = ranking[0]
    second_class, second = ranking[1]
    third_class, third = ranking[2]

    margin = best - second

    if abs(best) > 1e-9:
        relative_gap = margin / abs(best)
    else:
        relative_gap = 0.0

    if second > 0:
        ratio = best / second
    else:
        ratio = float("inf")

    # Exploratory diagnostic status
    if best < MIN_MEANINGFUL_RECOVERY:
        status = "WEAK"

    elif margin < MIN_MARGIN_PP:
        status = "AMBIGUOUS_MARGIN"

    elif relative_gap < MIN_RELATIVE_GAP:
        status = "AMBIGUOUS_RELATIVE"

    else:
        status = "CLEAR"

    # Candidate for DRAM8:
    # - dram4 already wins, OR
    # - dram4 is close to the current winner, OR
    # - dram4 itself gives meaningful recovery
    dram8_candidate = (
        best_class == "dram"
        or recovery["dram"] >= MIN_MEANINGFUL_RECOVERY
        or (best - recovery["dram"]) <= 5.0
    )

    rows.append({
        "workload": workload,

        "ipc_baseline": ipc0,
        "ipc_branch": ipcs["branch"],
        "ipc_cache": ipcs["cache"],
        "ipc_dram4": ipcs["dram"],

        "branch_recovery_pct": recovery["branch"],
        "cache_recovery_pct": recovery["cache"],
        "dram4_recovery_pct": recovery["dram"],

        "best_class": best_class,
        "best_recovery_pct": best,

        "second_class": second_class,
        "second_recovery_pct": second,

        "third_class": third_class,
        "third_recovery_pct": third,

        "margin_pp": margin,
        "relative_gap": relative_gap,
        "best_second_ratio": ratio,

        "status": status,
        "dram8_candidate": dram8_candidate,
    })


# ============================================================
# PRINT FULL TABLE
# ============================================================

print()
print("=" * 125)

print(
    f"{'WORKLOAD':34s}"
    f"{'BR%':>9s}"
    f"{'CACHE%':>9s}"
    f"{'DRAM4%':>9s}"
    f"{'BEST':>9s}"
    f"{'2ND':>9s}"
    f"{'MARGIN':>9s}"
    f"{'STATUS':>22s}"
)

print("=" * 125)

for r in rows:

    print(
        f"{r['workload'][:34]:34s}"
        f"{r['branch_recovery_pct']:9.2f}"
        f"{r['cache_recovery_pct']:9.2f}"
        f"{r['dram4_recovery_pct']:9.2f}"
        f"{r['best_class']:>9s}"
        f"{r['second_class']:>9s}"
        f"{r['margin_pp']:9.2f}"
        f"{r['status']:>22s}"
    )


# ============================================================
# SUMMARY COUNTS
# ============================================================

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)

print(f"Complete workloads: {len(rows)}")

for cls in ["branch", "cache", "dram"]:
    wins = [r for r in rows if r["best_class"] == cls]
    clear = [
        r for r in wins
        if r["status"] == "CLEAR"
    ]

    print(
        f"{cls:8s}: "
        f"{len(wins):2d} raw wins, "
        f"{len(clear):2d} clear wins"
    )


status_counts = {}

for r in rows:
    status_counts[r["status"]] = (
        status_counts.get(r["status"], 0) + 1
    )

print()

for status, count in sorted(status_counts.items()):
    print(f"{status:22s}: {count}")


# ============================================================
# INTERVENTION STRENGTH SUMMARY
# ============================================================

print()
print("=" * 70)
print("INTERVENTION STRENGTH ACROSS ALL WORKLOADS")
print("=" * 70)

for cls, key in [
    ("branch", "branch_recovery_pct"),
    ("cache", "cache_recovery_pct"),
    ("dram4", "dram4_recovery_pct"),
]:

    vals = [r[key] for r in rows]

    print(
        f"{cls:8s} "
        f"median={statistics.median(vals):7.2f}%   "
        f"mean={statistics.mean(vals):7.2f}%   "
        f"max={max(vals):7.2f}%"
    )


# ============================================================
# DRAM8 CANDIDATES
# ============================================================

dram_candidates = [
    r for r in rows
    if r["dram8_candidate"]
]

dram_candidates.sort(
    key=lambda r: r["dram4_recovery_pct"],
    reverse=True
)

print()
print("=" * 95)
print("WORKLOADS WORTH CONSIDERING FOR DRAM8")
print("=" * 95)

for r in dram_candidates:

    print(
        f"{r['workload'][:38]:38s} "
        f"dram4={r['dram4_recovery_pct']:7.2f}%  "
        f"best={r['best_class']:6s} "
        f"best_gain={r['best_recovery_pct']:7.2f}%  "
        f"gap_to_best="
        f"{r['best_recovery_pct'] - r['dram4_recovery_pct']:6.2f} pp"
    )


# ============================================================
# SAVE CSV
# ============================================================

csv_path = RESULTS / "oracle_analysis.csv"

if rows:
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0].keys())
        )

        writer.writeheader()
        writer.writerows(rows)

print()
print(f"Saved full results to:")
print(csv_path)


# ============================================================
# SAVE DRAM8 CANDIDATE LIST
# ============================================================

candidate_path = RESULTS / "dram8_candidates.txt"

with candidate_path.open("w") as f:
    for r in dram_candidates:
        f.write(r["workload"] + "\n")

print()
print(f"Saved DRAM8 candidate list to:")
print(candidate_path)

