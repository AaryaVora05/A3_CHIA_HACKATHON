import json
import sys

path = sys.argv[1]

with open(path) as f:
    data = json.load(f)

phase = next((x for x in data if x.get("name") == "Simulation"), data[-1])
roi = phase["roi"]

core = roi["cores"][0]

instructions = core["instructions"]
cycles = core["cycles"]
ipc = instructions / cycles

mispredicts = sum(core.get("mispredict", {}).values())
branch_mpki = 1000.0 * mispredicts / instructions

def find_cache(tag):
    for name, stats in roi.items():
        if tag in name and isinstance(stats, dict):
            return name, stats
    return None, None

def misses(stats):
    if not stats:
        return 0

    total = 0
    for typ in ("LOAD", "RFO", "WRITE", "TRANSLATION"):
        if typ in stats:
            m = stats[typ].get("miss", [])
            if isinstance(m, list):
                total += sum(m)
            else:
                total += m
    return total

def mpki(n):
    return 1000.0 * n / instructions

l1_name, l1 = find_cache("L1D")
l2_name, l2 = find_cache("L2C")
llc_name, llc = find_cache("LLC")

l1_miss = misses(l1)
l2_miss = misses(l2)
llc_miss = misses(llc)

dram = roi.get("DRAM", [])
if isinstance(dram, dict):
    dram = [dram]

dram_reads = sum(
    x.get("RQ ROW_BUFFER_HIT", 0) +
    x.get("RQ ROW_BUFFER_MISS", 0)
    for x in dram
)

dram_row_misses = sum(
    x.get("RQ ROW_BUFFER_MISS", 0)
    for x in dram
)

print(f"IPC              : {ipc:.4f}")
print(f"Instructions     : {instructions}")
print(f"Cycles           : {cycles}")
print(f"Branch MPKI      : {branch_mpki:.4f}")
print(f"L1D misses       : {l1_miss}")
print(f"L1D MPKI         : {mpki(l1_miss):.4f}")
print(f"L2C misses       : {l2_miss}")
print(f"L2C MPKI         : {mpki(l2_miss):.4f}")
print(f"LLC misses       : {llc_miss}")
print(f"LLC MPKI         : {mpki(llc_miss):.4f}")
print(f"DRAM reads       : {dram_reads}")
print(f"DRAM row misses  : {dram_row_misses}")
