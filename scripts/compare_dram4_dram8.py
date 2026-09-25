#!/usr/bin/env python3

import re
from pathlib import Path

ROOT = Path.home() / "a3-hackathon/results/oracle"

WORKLOADS = [
    "619.lbm_s-4268B",
    "470.lbm-1274B",
    "649.fotonik3d_s-1176B",
    "433.milc-127B",
    "462.libquantum-1343B",
]

ipc_re = re.compile(
    r"^CPU 0 cumulative IPC:\s+([0-9.]+)",
    re.MULTILINE
)

def get_ipc(path):
    if not path.exists():
        return None

    text = path.read_text(errors="replace")
    vals = ipc_re.findall(text)

    if not vals:
        return None

    return float(vals[-1])


print()
print(
    f"{'WORKLOAD':28s}"
    f"{'BASE':>9s}"
    f"{'DRAM4':>9s}"
    f"{'DRAM8':>9s}"
    f"{'D4%':>9s}"
    f"{'D8%':>9s}"
    f"{'4->8':>9s}"
    f"{'CAPTURE':>10s}"
)

print("-" * 101)

for w in WORKLOADS:

    d = ROOT / w

    base  = get_ipc(d / "baseline.txt")
    d4    = get_ipc(d / "dram4.txt")
    d8    = get_ipc(d / "dram8.txt")

    if None in [base, d4, d8]:
        print(f"{w:28s} MISSING RESULT")
        continue

    rec4 = 100.0 * (d4 - base) / base
    rec8 = 100.0 * (d8 - base) / base
    inc  = 100.0 * (d8 - d4) / d4

    # Fraction of the 8-channel recovery already obtained with 4 channels.
    capture = rec4 / rec8 if rec8 > 0 else float("nan")

    print(
        f"{w:28s}"
        f"{base:9.4f}"
        f"{d4:9.4f}"
        f"{d8:9.4f}"
        f"{rec4:9.2f}"
        f"{rec8:9.2f}"
        f"{inc:9.2f}"
        f"{capture:10.2f}"
    )
