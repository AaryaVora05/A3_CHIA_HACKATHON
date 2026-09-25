#!/usr/bin/env python3

import copy
import json
from pathlib import Path

ROOT = Path.home() / "a3-hackathon"
CONFIG_DIR = ROOT / "configs" / "M0"

BASELINE = CONFIG_DIR / "baseline.json"

with BASELINE.open() as f:
    base = json.load(f)


def write(name, cfg):
    path = CONFIG_DIR / name

    with path.open("w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")

    print(f"Wrote {path}")


# ============================================================
# 1. DRAM TIMING / LATENCY RELAXATION
#
# Scale the principal DRAM timing constraints by 2x.
# This should be described in the paper as a
# "DRAM timing/access-latency relaxation".
# ============================================================

latency = copy.deepcopy(base)

for field in ["tCAS", "tRCD", "tRP", "tRAS"]:
    if field not in latency["physical_memory"]:
        raise KeyError(
            f"{field} not found in physical_memory configuration"
        )

    old = latency["physical_memory"][field]
    latency["physical_memory"][field] = old / 2


# ============================================================
# 2. MISS-HANDLING / MSHR CAPACITY RELAXATION
#
# Data-cache hierarchy only.
# Do NOT modify I-cache/TLB MSHRs.
# ============================================================

mshr = copy.deepcopy(base)

for level in ["L1D", "L2C", "LLC"]:
    if "mshr_size" not in mshr[level]:
        raise KeyError(f"{level}.mshr_size not found")

    mshr[level]["mshr_size"] *= 4


# ============================================================
# 3. ROB / OOO WINDOW RELAXATION
# ============================================================

rob = copy.deepcopy(base)

old_rob = rob["ooo_cpu"][0]["rob_size"]
rob["ooo_cpu"][0]["rob_size"] = old_rob * 2


write("latency.json", latency)
write("mshr.json", mshr)
write("rob.json", rob)

print()
print("Generated candidate oracle relaxations.")
