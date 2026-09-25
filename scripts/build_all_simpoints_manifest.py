#!/usr/bin/env python3

import csv
from pathlib import Path

ROOT = Path.home() / "a3-hackathon"
DPC3 = ROOT / "workloads" / "dpc3"
META = DPC3 / "simpoint_metadata"

# The original 26 applications
APP_FILE = DPC3 / "wanted_spec.txt"

# If your file has a different name, change only this line.
AVAILABLE_FILE = DPC3 / "dpc3_available.txt"

CSV_OUT = DPC3 / "all_simpoints_26.csv"
TRACE_OUT = DPC3 / "all_simpoints_26.txt"
MISSING_OUT = DPC3 / "missing_simpoints_26.txt"


apps = [
    x.strip()
    for x in APP_FILE.read_text().splitlines()
    if x.strip()
]

available = set(
    x.strip()
    for x in AVAILABLE_FILE.read_text().splitlines()
    if x.strip()
)


def read_simpoints(path):

    out = {}

    for line in path.read_text().splitlines():

        if not line.strip():
            continue

        interval, cluster = line.split()[:2]

        out[int(cluster)] = int(interval)

    return out


def read_weights(path):

    out = {}

    for line in path.read_text().splitlines():

        if not line.strip():
            continue

        weight, cluster = line.split()[:2]

        out[int(cluster)] = float(weight)

    return out


rows = []
valid_traces = []
missing_traces = []


for app in apps:

    directory = META / app

    sim_file = directory / "simpoints.out"
    weight_file = directory / "weights.out"

    if not sim_file.exists() or not weight_file.exists():
        print(f"[NO METADATA] {app}")
        continue

    simpoints = read_simpoints(sim_file)
    weights = read_weights(weight_file)

    common = set(simpoints) & set(weights)

    app_rows = []

    for cluster in common:

        interval = simpoints[cluster]
        weight = weights[cluster]

        trace = f"{app}-{interval}B.champsimtrace.xz"

        row = {
            "application": app,
            "cluster": cluster,
            "interval": interval,
            "weight": weight,
            "trace": trace,
            "available": trace in available,
            "downloaded": (DPC3 / trace).exists(),
        }

        app_rows.append(row)

    # Rank within application by weight
    app_rows.sort(
        key=lambda x: x["weight"],
        reverse=True
    )

    for rank, row in enumerate(app_rows, 1):

        row["weight_rank"] = rank
        rows.append(row)

        if row["available"]:
            valid_traces.append(row["trace"])
        else:
            missing_traces.append(row["trace"])


# ------------------------------------------------------------
# Save
# ------------------------------------------------------------

fields = [
    "application",
    "weight_rank",
    "cluster",
    "interval",
    "weight",
    "trace",
    "available",
    "downloaded",
]

with CSV_OUT.open("w", newline="") as f:

    writer = csv.DictWriter(
        f,
        fieldnames=fields
    )

    writer.writeheader()
    writer.writerows(rows)


TRACE_OUT.write_text(
    "\n".join(valid_traces) + "\n"
)

MISSING_OUT.write_text(
    "\n".join(missing_traces) + "\n"
)


# ------------------------------------------------------------
# Summary
# ------------------------------------------------------------

print()
print("=" * 70)
print("ALL-SIMPOINT EXPERIMENT SIZE")
print("=" * 70)

print(f"Applications:            {len(apps)}")
print(f"Metadata SimPoints:      {len(rows)}")
print(f"Available DPC3 traces:   {len(valid_traces)}")
print(f"Missing DPC3 traces:     {len(missing_traces)}")

print()
print("Simulations required:")
print(f"  4 original oracle configs : {len(valid_traces) * 4}")
print(f"  6 bottleneck configs + M0 : {len(valid_traces) * 7}")

already_downloaded = sum(
    1
    for r in rows
    if r["available"] and r["downloaded"]
)

need_download = len(valid_traces) - already_downloaded

print()
print(f"Already downloaded:      {already_downloaded}")
print(f"Need downloading:        {need_download}")

print()
print(f"Manifest: {TRACE_OUT}")
print(f"Metadata: {CSV_OUT}")
