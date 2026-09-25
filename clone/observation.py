#!/usr/bin/env python3

import json
from pathlib import Path


def _sum(x):
    """Sum a ChampSim statistic that may be scalar or a list."""
    if isinstance(x, list):
        return sum(x)
    return x


def _load_stats(cache):
    """Return raw demand-load statistics for a cache."""
    load = cache["LOAD"]

    return {
        "hit": _sum(load["hit"]),
        "miss": _sum(load["miss"]),
        "miss_merge": _sum(load["miss_merge"]),
    }


def _demand_misses(cache):
    """
    Actual demand misses generating lower-level cache traffic.

    miss_merge is retained separately but is NOT counted as another
    cache miss in the primary fingerprint.
    """
    return _load_stats(cache)["miss"]


def _total_branch_mispredicts(core):
    return sum(_sum(v) for v in core["mispredict"].values())


def extract_observation(path):
    """
    Extract the V1 clone observation from a ChampSim JSON.

    Primary fingerprint:
        IPC
        BR_MPKI
        L1D_MPKI
        L2_MPKI
        LLC_MPKI
        DRAM_RQPI

    Also returns raw statistics for auditing/debugging.
    """

    path = Path(path)

    with path.open() as f:
        data = json.load(f)

    roi = data[0]["roi"]
    core = roi["cores"][0]

    instructions = _sum(core["instructions"])
    cycles = _sum(core["cycles"])

    if instructions <= 0:
        raise ValueError("ChampSim reported zero instructions")

    if cycles <= 0:
        raise ValueError("ChampSim reported zero cycles")

    # ------------------------------------------------------------
    # Core
    # ------------------------------------------------------------

    branch_mispredicts = _total_branch_mispredicts(core)

    ipc = instructions / cycles
    br_mpki = 1000.0 * branch_mispredicts / instructions

    # ------------------------------------------------------------
    # Cache hierarchy
    # ------------------------------------------------------------

    l1d = _load_stats(roi["cpu0_L1D"])
    l2 = _load_stats(roi["cpu0_L2C"])
    llc = _load_stats(roi["LLC"])

    l1d_mpki = 1000.0 * l1d["miss"] / instructions
    l2_mpki = 1000.0 * l2["miss"] / instructions
    llc_mpki = 1000.0 * llc["miss"] / instructions
    raw_miss_lat = roi["LLC"].get("miss latency")
    llc_miss_latency = float(raw_miss_lat) if raw_miss_lat is not None else 0.0

    # ------------------------------------------------------------
    # DRAM
    # ------------------------------------------------------------

    dram_read_hits = 0
    dram_read_misses = 0
    dram_write_hits = 0
    dram_write_misses = 0
    dbus_congestion_list = []

    for bank in roi["DRAM"]:
        dram_read_hits += _sum(bank["RQ ROW_BUFFER_HIT"])
        dram_read_misses += _sum(bank["RQ ROW_BUFFER_MISS"])
        dram_write_hits += _sum(bank["WQ ROW_BUFFER_HIT"])
        dram_write_misses += _sum(bank["WQ ROW_BUFFER_MISS"])
        if bank.get("AVG DBUS CONGESTED CYCLE") is not None:
            dbus_congestion_list.append(float(bank["AVG DBUS CONGESTED CYCLE"]))

    dram_read_requests = dram_read_hits + dram_read_misses
    dram_write_requests = dram_write_hits + dram_write_misses

    dram_rqpi = dram_read_requests / instructions

    total_dram_requests = dram_read_requests + dram_write_requests
    avg_dbus_congestion = (
        sum(dbus_congestion_list) / len(dbus_congestion_list)
        if dbus_congestion_list
        else 0.0
    )

    # ------------------------------------------------------------
    # Primary fingerprint
    # ------------------------------------------------------------

    fingerprint = {
        "IPC": ipc,
        "BR_MPKI": br_mpki,
        "L1D_MPKI": l1d_mpki,
        "L2_MPKI": l2_mpki,
        "LLC_MPKI": llc_mpki,
        "DRAM_RQPI": dram_rqpi,
    }

    # ------------------------------------------------------------
    # Raw / secondary statistics
    # ------------------------------------------------------------

    secondary = {
        "instructions": instructions,
        "cycles": cycles,

        "branch_mispredicts": branch_mispredicts,

        "L1D_load_hit": l1d["hit"],
        "L1D_load_miss": l1d["miss"],
        "L1D_load_miss_merge": l1d["miss_merge"],

        "L2_load_hit": l2["hit"],
        "L2_load_miss": l2["miss"],
        "L2_load_miss_merge": l2["miss_merge"],

        "LLC_load_hit": llc["hit"],
        "LLC_load_miss": llc["miss"],
        "LLC_load_miss_merge": llc["miss_merge"],
        "LLC_miss_latency": llc_miss_latency,

        "DRAM_read_row_hit": dram_read_hits,
        "DRAM_read_row_miss": dram_read_misses,
        "DRAM_read_requests": dram_read_requests,

        "DRAM_write_row_hit": dram_write_hits,
        "DRAM_write_row_miss": dram_write_misses,
        "DRAM_write_requests": dram_write_requests,

        "DRAM_total_requests": total_dram_requests,
        "DRAM_dbus_congestion": avg_dbus_congestion,

        "DRAM_row_hit_rate": (
            dram_read_hits / dram_read_requests
            if dram_read_requests > 0
            else 0.0
        ),
    }

    return {
        "fingerprint": fingerprint,
        "secondary": secondary,
    }


def print_observation(obs):
    x = obs["fingerprint"]
    s = obs["secondary"]

    print("PRIMARY FINGERPRINT")
    print(f"IPC:       {x['IPC']:.6f}")
    print(f"BR MPKI:   {x['BR_MPKI']:.6f}")
    print(f"L1D MPKI:  {x['L1D_MPKI']:.6f}")
    print(f"L2 MPKI:   {x['L2_MPKI']:.6f}")
    print(f"LLC MPKI:  {x['LLC_MPKI']:.6f}")
    print(f"DRAM RQPI: {x['DRAM_RQPI']:.8f}")

    print("\nSECONDARY / RAW")
    print(f"Instructions:       {s['instructions']}")
    print(f"Cycles:             {s['cycles']}")
    print(f"Branch mispredicts: {s['branch_mispredicts']}")

    print(f"L1D miss:           {s['L1D_load_miss']}")
    print(f"L1D miss merge:     {s['L1D_load_miss_merge']}")
    print(f"L2 miss:            {s['L2_load_miss']}")
    print(f"L2 miss merge:      {s['L2_load_miss_merge']}")
    print(f"LLC miss:           {s['LLC_load_miss']}")
    print(f"LLC miss merge:     {s['LLC_load_miss_merge']}")

    print(f"DRAM read requests: {s['DRAM_read_requests']}")
    print(f"DRAM write requests:{s['DRAM_write_requests']}")
    print(f"DRAM row hit rate:  {s['DRAM_row_hit_rate']:.6f}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("champsim_json")
    args = parser.parse_args()

    print_observation(extract_observation(args.champsim_json))
