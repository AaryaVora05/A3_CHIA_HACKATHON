#!/usr/bin/env python3

import copy
import json
from pathlib import Path

HOME = Path.home()

SOURCE = HOME / "ChampSim" / "champsim_config.json"
OUT = HOME / "a3-hackathon" / "configs" / "M0"

OUT.mkdir(parents=True, exist_ok=True)

with SOURCE.open() as f:
    base = json.load(f)

# ============================================================
# M0 BASELINE
# ============================================================

base["num_cores"] = 1

base["ooo_cpu"][0]["branch_predictor"] = "bimodal"

# LLC = 2048 sets * 16 ways * 64 B = 2 MiB
base["LLC"]["sets"] = 2048
base["LLC"]["ways"] = 16
base["LLC"]["replacement"] = "lru"

# Single-channel memory
base["physical_memory"]["channels"] = 1

# Prefetchers disabled
for level in ["L1I", "L1D", "L2C", "LLC"]:
    base[level]["prefetcher"] = "no"


# ============================================================
# M_branch
# ============================================================

branch = copy.deepcopy(base)
branch["ooo_cpu"][0]["branch_predictor"] = "hashed_perceptron"


# ============================================================
# M_cache
# 32768 * 16 * 64 B = 32 MiB
# ============================================================

cache = copy.deepcopy(base)
cache["LLC"]["sets"] = 32768


# ============================================================
# M_dram candidate
# pure bandwidth relaxation
# ============================================================

dram4 = copy.deepcopy(base)
dram4["physical_memory"]["channels"] = 4


configs = {
    "baseline.json": base,
    "branch.json": branch,
    "cache.json": cache,
    "dram4.json": dram4,
}

for name, cfg in configs.items():
    path = OUT / name

    with path.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

    print(f"Wrote {path}")
