#!/usr/bin/env python3

import json
from pathlib import Path

ROOT = Path("results/v11_probes")

def arr(v):
    if isinstance(v, list):
        return sum(v)
    return v or 0

def cache_stats(obj):
    access = miss = 0

    for kind, x in obj.items():
        if kind in ("TRANSLATION", "miss latency",
                    "prefetch issued", "prefetch requested",
                    "useful prefetch", "useless prefetch"):
            continue

        if not isinstance(x, dict):
            continue

        hit = arr(x.get("hit", 0))
        m = arr(x.get("miss", 0))
        mm = arr(x.get("miss_merge", 0))

        access += hit + m + mm
        miss += m

    return access, miss

def get_result(case):
    p = ROOT / case / "champsim.json"
    with open(p) as f:
        d = json.load(f)[0]

    roi = d["roi"]
    core = roi["cores"][0]

    instructions = arr(core["instructions"])
    cycles = arr(core["cycles"])
    ipc = instructions / cycles if cycles else 0

    l1d_a, l1d_m = cache_stats(roi["cpu0_L1D"])
    l2_a,  l2_m  = cache_stats(roi["cpu0_L2C"])
    llc_a, llc_m = cache_stats(roi["LLC"])

    mp = core["mispredict"]
    if isinstance(mp, dict):
        br_misp = sum(arr(v) for v in mp.values())
    else:
        br_misp = arr(mp)

    scale = 1000 / instructions if instructions else 0

    return {
        "IPC": ipc,
        "cycles": cycles,
        "L1D MPKI": l1d_m * scale,
        "L2 MPKI": l2_m * scale,
        "LLC MPKI": llc_m * scale,
        "Br MPKI": br_misp * scale,
    }

cases = sorted(
    p.name for p in ROOT.iterdir()
    if p.is_dir() and (p / "champsim.json").exists()
)

print(
    f"{'case':<24} {'IPC':>7} {'cycles':>10} "
    f"{'L1D MPKI':>10} {'L2 MPKI':>9} "
    f"{'LLC MPKI':>10} {'Br MPKI':>9}"
)
print("-" * 88)

for case in cases:
    r = get_result(case)
    print(
        f"{case:<24} "
        f"{r['IPC']:>7.3f} "
        f"{r['cycles']:>10,} "
        f"{r['L1D MPKI']:>10.2f} "
        f"{r['L2 MPKI']:>9.2f} "
        f"{r['LLC MPKI']:>10.2f} "
        f"{r['Br MPKI']:>9.2f}"
    )
