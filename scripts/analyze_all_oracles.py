#!/usr/bin/env python3

import csv
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path.home() / "a3-hackathon"
RESULTS = ROOT / "results" / "oracle"
TRACE_LIST = ROOT / "workloads" / "dpc3" / "maxweight_26.txt"

# ------------------------------------------------------------
# Exploratory thresholds
#
# These are useful for SCREENING.
# Do not yet present them as final paper thresholds.
# ------------------------------------------------------------

MIN_RECOVERY = 5.0       # best intervention must improve IPC >= 5%
MIN_MARGIN_PP = 3.0      # best must beat second-best by >= 3 percentage points
MIN_REL_GAP = 0.20       # margin >= 20% of best recovery


CONFIGS = {
    "branch":  "branch",
    "cache":   "cache",
    "dram_bw": "dram4",
    "dram_lat":"latency",
    "mshr":    "mshr",
    "rob":     "rob",
}


IPC_RE = re.compile(
    r"^CPU 0 cumulative IPC:\s+([0-9.]+)",
    re.MULTILINE
)


def get_ipc(path):

    if not path.exists():
        return None

    text = path.read_text(errors="replace")

    if "ChampSim completed all CPUs" not in text:
        return None

    vals = IPC_RE.findall(text)

    if not vals:
        return None

    return float(vals[-1])


def recovery(base, relaxed):

    if base is None or relaxed is None or base == 0:
        return None

    return 100.0 * (relaxed - base) / base


# ============================================================
# PROCESS ALL WORKLOADS
# ============================================================

rows = []

trace_names = [
    x.strip()
    for x in TRACE_LIST.read_text().splitlines()
    if x.strip()
]

for trace_name in trace_names:

    workload = trace_name.removesuffix(".champsimtrace.xz")

    d = RESULTS / workload

    base = get_ipc(d / "baseline.txt")

    if base is None:
        print(f"[ERROR] baseline missing/incomplete: {workload}")
        continue

    ipcs = {}
    gains = {}

    missing = []

    for label, filename in CONFIGS.items():

        value = get_ipc(d / f"{filename}.txt")

        if value is None:
            missing.append(filename)

        ipcs[label] = value
        gains[label] = recovery(base, value)

    if missing:
        print(
            f"[INCOMPLETE] {workload}: "
            + ", ".join(missing)
        )
        continue

    # --------------------------------------------------------
    # Rank interventions
    # --------------------------------------------------------

    ranking = sorted(
        gains.items(),
        key=lambda x: x[1],
        reverse=True
    )

    best_class, best = ranking[0]
    second_class, second = ranking[1]
    third_class, third = ranking[2]

    margin = best - second

    relative_gap = (
        margin / abs(best)
        if abs(best) > 1e-12
        else 0.0
    )

    # --------------------------------------------------------
    # Exploratory ambiguity classification
    # --------------------------------------------------------

    if best < MIN_RECOVERY:

        status = "WEAK"

    elif margin < MIN_MARGIN_PP:

        status = "AMBIGUOUS_MARGIN"

    elif relative_gap < MIN_REL_GAP:

        status = "AMBIGUOUS_RELATIVE"

    else:

        status = "CLEAR"


    row = {
        "workload": workload,

        "ipc_baseline": base,

        "ipc_branch": ipcs["branch"],
        "ipc_cache": ipcs["cache"],
        "ipc_dram_bw": ipcs["dram_bw"],
        "ipc_dram_lat": ipcs["dram_lat"],
        "ipc_mshr": ipcs["mshr"],
        "ipc_rob": ipcs["rob"],

        "branch_pct": gains["branch"],
        "cache_pct": gains["cache"],
        "dram_bw_pct": gains["dram_bw"],
        "dram_lat_pct": gains["dram_lat"],
        "mshr_pct": gains["mshr"],
        "rob_pct": gains["rob"],

        "best_class": best_class,
        "best_pct": best,

        "second_class": second_class,
        "second_pct": second,

        "third_class": third_class,
        "third_pct": third,

        "margin_pp": margin,
        "relative_gap": relative_gap,

        "status": status,
    }

    rows.append(row)


# ============================================================
# FULL TABLE
# ============================================================

print()
print("=" * 150)

print(
    f"{'WORKLOAD':30s}"
    f"{'BP%':>9s}"
    f"{'CACHE%':>9s}"
    f"{'BW%':>9s}"
    f"{'LAT%':>9s}"
    f"{'MSHR%':>9s}"
    f"{'ROB%':>9s}"
    f"{'BEST':>11s}"
    f"{'2ND':>11s}"
    f"{'MARGIN':>9s}"
    f"{'STATUS':>22s}"
)

print("=" * 150)

for r in rows:

    print(
        f"{r['workload'][:30]:30s}"

        f"{r['branch_pct']:9.2f}"
        f"{r['cache_pct']:9.2f}"
        f"{r['dram_bw_pct']:9.2f}"
        f"{r['dram_lat_pct']:9.2f}"
        f"{r['mshr_pct']:9.2f}"
        f"{r['rob_pct']:9.2f}"

        f"{r['best_class']:>11s}"
        f"{r['second_class']:>11s}"
        f"{r['margin_pp']:9.2f}"
        f"{r['status']:>22s}"
    )


# ============================================================
# WIN COUNTS
# ============================================================

print()
print("=" * 80)
print("WIN COUNTS")
print("=" * 80)

raw_counts = Counter(
    r["best_class"]
    for r in rows
)

clear_counts = Counter(
    r["best_class"]
    for r in rows
    if r["status"] == "CLEAR"
)

for cls in CONFIGS:

    print(
        f"{cls:10s}: "
        f"{raw_counts[cls]:2d} raw wins, "
        f"{clear_counts[cls]:2d} clear wins"
    )


# ============================================================
# STATUS COUNTS
# ============================================================

print()
print("=" * 80)
print("AMBIGUITY / QUALITY")
print("=" * 80)

status_counts = Counter(
    r["status"]
    for r in rows
)

for status, count in status_counts.items():
    print(f"{status:22s}: {count}")


# ============================================================
# INTERVENTION STRENGTH
# ============================================================

print()
print("=" * 80)
print("INTERVENTION STRENGTH ACROSS ALL 26 WORKLOADS")
print("=" * 80)

for cls in CONFIGS:

    key = f"{cls}_pct"

    vals = [
        r[key]
        for r in rows
    ]

    print(
        f"{cls:10s} "
        f"median={statistics.median(vals):8.2f}%   "
        f"mean={statistics.mean(vals):8.2f}%   "
        f"min={min(vals):8.2f}%   "
        f"max={max(vals):8.2f}%"
    )


# ============================================================
# CLEAR WINNERS PER CLASS
# ============================================================

print()
print("=" * 80)
print("CLEAR WINNERS BY BOTTLENECK")
print("=" * 80)

for cls in CONFIGS:

    candidates = [
        r for r in rows
        if (
            r["best_class"] == cls
            and r["status"] == "CLEAR"
        )
    ]

    candidates.sort(
        key=lambda r: r["margin_pp"],
        reverse=True
    )

    print()
    print(f"--- {cls.upper()} ---")

    if not candidates:
        print("NONE")
        continue

    for r in candidates:

        print(
            f"{r['workload']:32s} "
            f"best={r['best_pct']:8.2f}%   "
            f"second={r['second_class']:10s} "
            f"{r['second_pct']:8.2f}%   "
            f"margin={r['margin_pp']:7.2f}"
        )


# ============================================================
# AMBIGUOUS / WEAK CASES
# ============================================================

print()
print("=" * 80)
print("AMBIGUOUS / WEAK WORKLOADS")
print("=" * 80)

problematic = [
    r for r in rows
    if r["status"] != "CLEAR"
]

if not problematic:

    print("None")

else:

    for r in problematic:

        print(
            f"{r['workload']:32s} "
            f"{r['best_class']:10s} "
            f"{r['best_pct']:7.2f}% vs "
            f"{r['second_class']:10s} "
            f"{r['second_pct']:7.2f}% "
            f"margin={r['margin_pp']:6.2f} "
            f"[{r['status']}]"
        )


# ============================================================
# SAVE CSV
# ============================================================

csv_path = RESULTS / "oracle_analysis_6class.csv"

if rows:

    with csv_path.open("w", newline="") as f:

        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0].keys())
        )

        writer.writeheader()
        writer.writerows(rows)

print()
print(f"Saved:")
print(csv_path)
