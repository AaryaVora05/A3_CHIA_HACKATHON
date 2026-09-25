#!/usr/bin/env python3
"""
Exhaustive parallel sweep over the probe knob space.
Runs in parallel across vCPUs without modifying agent/run_probe.py.
Outputs full observations (IPC, BR_MPKI, L1D, L2, LLC, DRAM_RQPI).
"""

import os
import sys
import json
import csv
import shutil
import subprocess
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

ROOT = Path("/home/avds_a3/a3-hackathon")
sys.path.insert(0, str(ROOT / "agent"))
sys.path.insert(0, str(ROOT / "clone"))
from semantic import SemanticProbe
from observation import extract_observation

OUT_DIR = ROOT / "results" / "v11_sweeps"
OUT_DIR.mkdir(parents=True, exist_ok=True)

PYTHON = str(ROOT / "agent" / ".venv" / "bin" / "python")
RUN_PROBE = str(ROOT / "agent" / "run_probe.py")
PIN_BIN = "/home/avds_a3/pin-3.22-reference/pin"
TRACER_SO = "/home/avds_a3/ChampSim-pin322-reference/tracer/pin/obj-intel64/champsim_tracer.so"
REAL_CHAMPSIM = "/home/avds_a3/ChampSim/bin/champsim"

def generate_cases():
    cases = []

    # 1. Branch Pattern & Frequency Sweeps
    for b in [1, 2, 4, 8]:
        cases.append((f"br_const_b{b}", {"branch_frequency": b, "branch_pattern": "constant", "branch_pattern_param": 0}))
        cases.append((f"br_alt_b{b}", {"branch_frequency": b, "branch_pattern": "alternating", "branch_pattern_param": 0}))
        for p in [3, 5, 8, 16, 32]:
            cases.append((f"br_per{p}_b{b}", {"branch_frequency": b, "branch_pattern": "periodic", "branch_pattern_param": p}))
        for t in [10, 25, 50, 75, 90]:
            cases.append((f"br_rnd{t}_b{b}", {"branch_frequency": b, "branch_pattern": "random", "branch_pattern_param": t}))

    # 2. Load-Branch Dependency (LBD)
    for b in [2, 4, 8]:
        cases.append((f"lbd_b{b}", {"branch_frequency": b, "branch_pattern": "random", "branch_pattern_param": 50, "load_branch_dependency": True}))
        cases.append((f"lbd_dep_b{b}", {"branch_frequency": b, "branch_pattern": "random", "branch_pattern_param": 50, "load_branch_dependency": True, "memory_dependency": "dependent"}))

    # 3. Dependent Pointer Chasing across Memory Hierarchy Tiers
    for wss in [4, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 16384, 65536]:
        cases.append((f"dep_c1_wss_{wss}k", {"working_set_kb": wss, "memory_dependency": "dependent", "independent_chains": 1, "randomness": 1.0}))
    for c in [2, 4, 8]:
        for wss in [32, 512, 2048, 65536]:
            cases.append((f"dep_c{c}_wss_{wss}k", {"working_set_kb": wss, "memory_dependency": "dependent", "independent_chains": c, "randomness": 1.0}))

    # 4. Independent Chains across Memory Hierarchy Tiers
    for c in [1, 2, 4, 8]:
        for wss in [4, 32, 256, 1024, 4096, 65536]:
            cases.append((f"ind_c{c}_wss_{wss}k", {"working_set_kb": wss, "memory_dependency": "independent", "independent_chains": c, "randomness": 1.0}))

    # 5. Stride sweeps across tiers
    for s in [1, 4, 16, 64, 256]:
        cases.append((f"stride_{s}_l2", {"working_set_kb": 256, "stride_lines": s}))
        cases.append((f"stride_{s}_llc", {"working_set_kb": 2048, "stride_lines": s}))
        cases.append((f"stride_{s}_dram", {"working_set_kb": 65536, "stride_lines": s}))

    # 6. Randomness & Reuse sweeps across tiers
    for rnd in [0.0, 0.25, 0.5, 0.75, 1.0]:
        cases.append((f"rnd_{int(rnd*100)}_l2", {"working_set_kb": 256, "randomness": rnd}))
        cases.append((f"rnd_{int(rnd*100)}_llc", {"working_set_kb": 2048, "randomness": rnd}))
    for u in [0.0, 0.25, 0.5, 0.75, 0.9]:
        cases.append((f"reuse_{int(u*100)}_l2", {"working_set_kb": 256, "reuse": u}))
        cases.append((f"reuse_{int(u*100)}_llc", {"working_set_kb": 2048, "reuse": u}))

    # 7. ALU Compute Intensity & Latency Distance
    for a in [0, 4, 8, 16, 32, 64]:
        for d in [1, 2, 4, 8]:
            if a % d == 0:
                cases.append((f"alu_a{a}_d{d}", {"alu_ops": a, "dependency_distance": d}))

    # 8. Cross-Interactions & Extreme Corners
    # Max IPC corner
    cases.append(("corner_max_ipc", {"working_set_kb": 4, "independent_chains": 8, "branch_frequency": 0, "alu_ops": 0}))
    # Min IPC corner
    cases.append(("corner_min_ipc", {"working_set_kb": 65536, "memory_dependency": "dependent", "independent_chains": 1, "branch_frequency": 8, "branch_pattern": "random", "branch_pattern_param": 50, "alu_ops": 64, "dependency_distance": 1}))
    # Max BR MPKI corner
    cases.append(("corner_max_br_mpki", {"working_set_kb": 4, "branch_frequency": 8, "branch_pattern": "random", "branch_pattern_param": 50, "alu_ops": 0}))
    # Max DRAM RQPI corner
    cases.append(("corner_max_dram_rqpi", {"working_set_kb": 65536, "stride_lines": 64, "randomness": 1.0, "independent_chains": 8, "reuse": 0.0, "branch_frequency": 0, "alu_ops": 0}))
    # Heavy branch + heavy memory
    cases.append(("x_br_heavy_dram", {"working_set_kb": 65536, "branch_frequency": 8, "branch_pattern": "random", "branch_pattern_param": 50, "randomness": 1.0}))
    cases.append(("x_br_heavy_l1", {"working_set_kb": 4, "branch_frequency": 8, "branch_pattern": "random", "branch_pattern_param": 50}))
    # Heavy compute + heavy memory
    cases.append(("x_alu_heavy_dram", {"working_set_kb": 65536, "alu_ops": 64, "dependency_distance": 1, "randomness": 1.0}))
    cases.append(("x_alu_heavy_l1", {"working_set_kb": 4, "alu_ops": 64, "dependency_distance": 1}))

    # Deduplicate by case name
    seen = set()
    unique_cases = []
    for name, params in cases:
        if name not in seen:
            seen.add(name)
            unique_cases.append((name, params))
    return unique_cases

def run_worker_probe(task):
    case_name, params, worker_id = task
    target_json = OUT_DIR / f"{case_name}.json"
    if target_json.exists():
        try:
            with open(target_json) as f:
                d = json.load(f)
            return case_name, d["fingerprint"], True
        except Exception:
            pass

    worker_dir = Path(f"/tmp/a3_worker_{worker_id}")
    worker_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = worker_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Create wrapper script for ChampSim to emit JSON
    stats_json = worker_dir / f"stats_{case_name}.json"
    wrapper_sh = worker_dir / f"wrapper_{case_name}.sh"
    wrapper_sh.write_text(f"""#!/bin/bash
{REAL_CHAMPSIM} "$@" --json {stats_json}
""")
    wrapper_sh.chmod(0o755)

    # Build semantic probe object
    probe = SemanticProbe(**params)
    probe_json_str = probe.model_dump_json()

    env = os.environ.copy()
    env["A3_ROOT"] = str(worker_dir)
    env["PROBE_TEMPLATE"] = str(ROOT / "probes" / "template" / "probe.c")
    env["CHECK_STATIC"] = str(ROOT / "tools" / "check_static.py")
    env["PIN_BIN"] = PIN_BIN
    env["TRACER_SO"] = TRACER_SO
    env["CHAMPSIM_BIN"] = str(wrapper_sh)
    env["SIM_INSTR"] = "1000000"
    env["WARMUP_MIN"] = "200000"

    cmd = [PYTHON, RUN_PROBE, probe_json_str, "--semantic"]
    try:
        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            print(f"[{case_name}] FAILED: {res.stderr[-300:]}")
            return case_name, None, False

        # Extract observation from ChampSim JSON
        if not stats_json.exists():
            print(f"[{case_name}] JSON not produced")
            return case_name, None, False

        obs = extract_observation(stats_json)
        fp = obs["fingerprint"]
        sec = obs["secondary"]

        result_record = {
            "case": case_name,
            "params": params,
            "fingerprint": fp,
            "secondary": sec,
        }
        with open(target_json, "w") as f:
            json.dump(result_record, f, indent=2)

        return case_name, fp, True
    except Exception as e:
        print(f"[{case_name}] EXCEPTION: {e}")
        return case_name, None, False
    finally:
        wrapper_sh.unlink(missing_ok=True)
        stats_json.unlink(missing_ok=True)

def main():
    cases = generate_cases()
    print(f"Total cases to sweep: {len(cases)}")

    num_workers = 6
    tasks = [(name, params, i % num_workers) for i, (name, params) in enumerate(cases)]

    start_t = time.time()
    results = {}
    completed = 0

    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(run_worker_probe, t): t[0] for t in tasks}
        for future in as_completed(futures):
            case_name, fp, ok = future.result()
            completed += 1
            if ok and fp:
                results[case_name] = fp
                print(f"[{completed}/{len(tasks)}] {case_name:<24} IPC: {fp['IPC']:6.3f} BR: {fp['BR_MPKI']:6.2f} L1D: {fp['L1D_MPKI']:6.2f} L2: {fp['L2_MPKI']:6.2f} LLC: {fp['LLC_MPKI']:6.2f} DRAM: {fp['DRAM_RQPI']:8.5f}")
            else:
                print(f"[{completed}/{len(tasks)}] {case_name:<24} FAILED")

    elapsed = time.time() - start_t
    print(f"\nAll {len(cases)} cases processed in {elapsed:.1f}s ({elapsed/60:.2f}m)!")

if __name__ == "__main__":
    main()
