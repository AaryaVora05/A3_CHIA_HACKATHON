#!/usr/bin/env python3
"""
clone/micrograd_cloner.py -- Fast MicroGrad-based Workload Cloner for A3/CHIA.

Implements the gradient-descent workload cloning methodology from:
  "MicroGrad: Microarchitecture-Directed Workload Cloning through Gradient Descent"
  (Ravi et al., arXiv:2009.04622)

Key Design Principles for Fast Convergence with Reduced Knobs:
  1. Reduced Knob Space (5 core knobs):
     - branch_frequency: controls BR_MPKI
     - working_set_kb: controls cache residency (L1 vs L2 vs LLC vs DRAM)
     - reuse: controls temporal locality and L1D/L2 hit rate
     - memory_dependency: toggles serial pointer chasing vs parallel memory streams
     - independent_chains: bounds memory-level parallelism (MLP)
  2. Physics-Informed Warm Start (Epoch 0):
     Directly projects the master fingerprint into the appropriate M0 architectural
     subsystem regions (L1D: 32KB, L2: 512KB, LLC: 2MB, DRAM: >2MB).
  3. Microarchitecture-Directed Gradient Steps (Epochs 1..3):
     Decouples perturbations along orthogonal hardware axes based on residual errors.
  4. Fast Parallel Execution:
     500k-instruction probes simulated in parallel across CPU cores.
"""

import argparse
import copy
import json
import math
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

# Add project root to sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
sys.path.insert(0, str(PROJECT_ROOT / "clone"))

from semantic import SemanticProbe
from observation import extract_observation
from distance import METRICS, METRIC_SCALES, compute_micrograd_loss, compute_normalized_errors
from execution_profile import EXEC_PROFILE

# Knob candidate domains
BRANCH_LEVELS = [0, 1, 2, 4, 8]
WSS_LEVELS = [16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
REUSE_LEVELS = [0.0, 0.2, 0.4, 0.6, 0.8]
CHAINS_LEVELS = [1, 2, 4, 8]
RANDOMNESS_LEVELS = [0.0, 0.25, 0.5, 0.75, 1.0]
STRIDE_LEVELS = [1, 2, 4, 8, 16, 64]

PYTHON = str(PROJECT_ROOT / "agent" / ".venv" / "bin" / "python")
RUN_PROBE = str(PROJECT_ROOT / "agent" / "run_probe.py")
PIN_BIN = "/home/avds_a3/pin-3.22-reference/pin"
TRACER_SO = "/home/avds_a3/ChampSim-pin322-reference/tracer/pin/obj-intel64/champsim_tracer.so"
REAL_CHAMPSIM = "/home/avds_a3/ChampSim/bin/champsim"
SIM_INSTR = EXEC_PROFILE["SIM_INSTR"]
WARMUP_MIN = EXEC_PROFILE["WARMUP_MIN"]


def compute_loss(candidate_fp, target_fp):
    """Canonical loss adapter calling distance.py compute_micrograd_loss."""
    return compute_micrograd_loss(candidate_fp, target_fp)



def snap_knobs(knobs):
    """Snap knobs to valid discrete values."""
    k = copy.deepcopy(knobs)
    k["branch_frequency"] = min(BRANCH_LEVELS, key=lambda x: abs(x - k.get("branch_frequency", 0)))
    k["working_set_kb"] = min(WSS_LEVELS, key=lambda x: abs(x - k.get("working_set_kb", 1024)))
    k["reuse"] = min(REUSE_LEVELS, key=lambda x: abs(x - k.get("reuse", 0.0)))
    k["independent_chains"] = min(CHAINS_LEVELS, key=lambda x: abs(x - k.get("independent_chains", 1)))
    k["memory_dependency"] = "dependent" if k.get("memory_dependency") == "dependent" else "independent"

    # Discrete auxiliary knobs
    k["branch_pattern"] = k.get("branch_pattern", "random")
    k["branch_pattern_param"] = k.get("branch_pattern_param", 50)
    k["load_branch_dependency"] = k.get("load_branch_dependency", False)
    
    stride = k.get("stride_lines", 1)
    k["stride_lines"] = min(STRIDE_LEVELS, key=lambda x: abs(x - stride))

    default_rand = 0.5 if k["working_set_kb"] > 32 else 0.0
    rand = k.get("randomness", default_rand)
    k["randomness"] = min(RANDOMNESS_LEVELS, key=lambda x: abs(x - rand))

    k["alu_ops"] = k.get("alu_ops", 0)
    k["dependency_distance"] = k.get("dependency_distance", 1)
    return k


def smart_warm_start(target_fp):
    """
    Epoch 0: Physics-informed microarchitectural mapping to target M0 regions.
    """
    br = target_fp.get("BR_MPKI", 0.0)
    if br < 1.0:
        b_freq = 0
    elif br < 6.0:
        b_freq = 1
    elif br < 15.0:
        b_freq = 2
    elif br < 25.0:
        b_freq = 4
    else:
        b_freq = 8

    llc = target_fp.get("LLC_MPKI", 0.0)
    dram = target_fp.get("DRAM_RQPI", 0.0)
    l2 = target_fp.get("L2_MPKI", 0.0)
    l1d = target_fp.get("L1D_MPKI", 0.0)

    # Determine cache hierarchy tier
    if llc > 5.0 or dram > 0.01:
        wss = 16384  # 16 MB: deep DRAM
    elif llc > 0.5 or dram > 0.002:
        wss = 4096   # 4 MB: LLC spill
    elif l2 > 2.0:
        wss = 1024   # 1 MB: LLC resident (2MB LLC)
    elif l1d > 5.0:
        wss = 256    # 256 KB: L2 resident (512KB L2)
    else:
        wss = 32     # 32 KB: L1D resident

    # Determine reuse
    if l1d < 5.0 and wss > 32:
        reuse = 0.8
    elif l1d < 20.0 and wss > 32:
        reuse = 0.4
    elif l1d < 50.0 and wss > 32:
        reuse = 0.2
    else:
        reuse = 0.0

    # Determine access pattern randomness from row-hit rate if available
    r_hit = target_fp.get("DRAM_row_hit_rate", None)
    if r_hit is not None:
        try:
            r_hit_val = float(r_hit.rstrip("%")) / 100.0 if isinstance(r_hit, str) else float(r_hit)
            if r_hit_val > 0.15:
                init_rand = 0.0
            elif r_hit_val > 0.05:
                init_rand = 0.25
            else:
                init_rand = 0.75 if wss > 32 else 0.0
        except Exception:
            init_rand = 0.5 if wss > 32 else 0.0
    else:
        init_rand = 0.5 if wss > 32 else 0.0

    # Determine concurrency / latency dependency
    ipc = target_fp.get("IPC", 1.0)
    if ipc < 0.4:
        dep = "dependent"
        chains = 1
    elif ipc < 0.9:
        dep = "dependent"
        chains = 2
    elif ipc < 1.5:
        dep = "independent"
        chains = 2
    else:
        dep = "independent"
        chains = 4

    return snap_knobs({
        "branch_frequency": b_freq,
        "working_set_kb": wss,
        "reuse": reuse,
        "randomness": init_rand,
        "memory_dependency": dep,
        "independent_chains": chains,
    })


def evaluate_probe_worker(task):
    """Worker process: compiles, traces, and simulates a candidate probe."""
    worker_id, name, knobs_dict, target_fp = task
    w_dir = Path(f"/tmp/micrograd_w{worker_id}")
    w_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = w_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    uid = f"{int(time.time()*1000)%1000000}_{name}"
    stats_json = w_dir / f"stats_{uid}.json"
    wrapper_sh = w_dir / f"wrap_{uid}.sh"

    wrapper_content = f"""#!/bin/bash
exec {REAL_CHAMPSIM} --json {stats_json} "$@"
"""
    wrapper_sh.write_text(wrapper_content)
    wrapper_sh.chmod(0o755)

    env = os.environ.copy()
    env["A3_ROOT"] = str(w_dir)
    env["PROBE_TEMPLATE"] = str(PROJECT_ROOT / "probes" / "template" / "probe.c")
    env["CHECK_STATIC"] = str(PROJECT_ROOT / "tools" / "check_static.py")
    env["PIN_BIN"] = PIN_BIN
    env["TRACER_SO"] = TRACER_SO
    env["CHAMPSIM_BIN"] = str(wrapper_sh)
    env["SIM_INSTR"] = SIM_INSTR
    env["WARMUP_MIN"] = WARMUP_MIN

    try:
        sem = SemanticProbe(**knobs_dict)
        probe_json_str = json.dumps(sem.model_dump())
        cmd = [PYTHON, RUN_PROBE, probe_json_str, "--semantic"]

        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=90)
        if res.returncode != 0:
            return {"name": name, "knobs": knobs_dict, "status": "FAIL", "error": res.stderr[-200:]}

        if not stats_json.exists():
            return {"name": name, "knobs": knobs_dict, "status": "FAIL", "error": "No stats JSON produced"}

        obs = extract_observation(stats_json)
        fp = obs["fingerprint"]
        loss, comp = compute_loss(fp, target_fp)

        return {
            "name": name,
            "knobs": sem.model_dump(),
            "fingerprint": fp,
            "loss": loss,
            "components": comp,
            "status": "OK"
        }
    except Exception as e:
        return {"name": name, "knobs": knobs_dict, "status": "FAIL", "error": str(e)}
    finally:
        wrapper_sh.unlink(missing_ok=True)
        stats_json.unlink(missing_ok=True)


def generate_micrograd_perturbations(curr_knobs, curr_fp, target_fp, comp_loss):
    """
    Generate decoupled 1D microarchitectural gradient perturbations
    targeting the dominant loss contributors.
    """
    perturbations = []

    # 1. Branch MPKI gradient
    br_err = curr_fp["BR_MPKI"] - target_fp["BR_MPKI"]
    b_curr = curr_knobs["branch_frequency"]
    idx_b = BRANCH_LEVELS.index(b_curr) if b_curr in BRANCH_LEVELS else 0
    if br_err < -1.5 and idx_b < len(BRANCH_LEVELS) - 1:
        c = copy.deepcopy(curr_knobs)
        c["branch_frequency"] = BRANCH_LEVELS[idx_b + 1]
        perturbations.append(("br_step_up", snap_knobs(c)))
    elif br_err > 1.5 and idx_b > 0:
        c = copy.deepcopy(curr_knobs)
        c["branch_frequency"] = BRANCH_LEVELS[idx_b - 1]
        perturbations.append(("br_step_dn", snap_knobs(c)))

    # 2. Cache Tier / Working Set gradient (LLC MPKI or DRAM RQPI)
    llc_err = curr_fp["LLC_MPKI"] - target_fp["LLC_MPKI"]
    dram_err = curr_fp["DRAM_RQPI"] - target_fp["DRAM_RQPI"]
    wss_curr = curr_knobs["working_set_kb"]
    idx_w = WSS_LEVELS.index(wss_curr) if wss_curr in WSS_LEVELS else 6
    if (llc_err < -1.0 or dram_err < -0.003) and idx_w < len(WSS_LEVELS) - 1:
        c = copy.deepcopy(curr_knobs)
        c["working_set_kb"] = WSS_LEVELS[idx_w + 1]
        perturbations.append(("wss_step_up", snap_knobs(c)))
    elif (llc_err > 2.0 or dram_err > 0.005) and idx_w > 0:
        c = copy.deepcopy(curr_knobs)
        c["working_set_kb"] = WSS_LEVELS[idx_w - 1]
        perturbations.append(("wss_step_dn", snap_knobs(c)))

    # 3. Reuse gradient (L1D / L2 MPKI fine-tuning)
    l1d_err = curr_fp["L1D_MPKI"] - target_fp["L1D_MPKI"]
    reuse_curr = curr_knobs["reuse"]
    idx_r = REUSE_LEVELS.index(reuse_curr) if reuse_curr in REUSE_LEVELS else 0
    if l1d_err > 3.0 and idx_r < len(REUSE_LEVELS) - 1:
        c = copy.deepcopy(curr_knobs)
        c["reuse"] = REUSE_LEVELS[idx_r + 1]
        perturbations.append(("reuse_step_up", snap_knobs(c)))
    elif l1d_err < -3.0 and idx_r > 0:
        c = copy.deepcopy(curr_knobs)
        c["reuse"] = REUSE_LEVELS[idx_r - 1]
        perturbations.append(("reuse_step_dn", snap_knobs(c)))

    # 4. IPC / Concurrency gradient
    ipc_err = curr_fp["IPC"] - target_fp["IPC"]
    chains_curr = curr_knobs["independent_chains"]
    idx_c = CHAINS_LEVELS.index(chains_curr) if chains_curr in CHAINS_LEVELS else 0
    if ipc_err < -0.15:
        # Increase throughput
        if curr_knobs["memory_dependency"] == "dependent":
            c = copy.deepcopy(curr_knobs)
            c["memory_dependency"] = "independent"
            perturbations.append(("dep_to_indep", snap_knobs(c)))
        elif idx_c < len(CHAINS_LEVELS) - 1:
            c = copy.deepcopy(curr_knobs)
            c["independent_chains"] = CHAINS_LEVELS[idx_c + 1]
            perturbations.append(("chains_step_up", snap_knobs(c)))
    elif ipc_err > 0.15:
        # Decrease throughput
        if curr_knobs["memory_dependency"] == "independent":
            c = copy.deepcopy(curr_knobs)
            c["memory_dependency"] = "dependent"
            perturbations.append(("indep_to_dep", snap_knobs(c)))
        elif idx_c > 0:
            c = copy.deepcopy(curr_knobs)
            c["independent_chains"] = CHAINS_LEVELS[idx_c - 1]
            perturbations.append(("chains_step_dn", snap_knobs(c)))

    # 5. Access pattern / Randomness gradient
    rand_curr = curr_knobs.get("randomness", 0.5)
    idx_rand = RANDOMNESS_LEVELS.index(rand_curr) if rand_curr in RANDOMNESS_LEVELS else 2
    if dram_err > 0.008 and idx_rand > 0:
        c = copy.deepcopy(curr_knobs)
        c["randomness"] = RANDOMNESS_LEVELS[idx_rand - 1]
        perturbations.append(("rand_step_dn", snap_knobs(c)))
    elif dram_err < -0.008 and idx_rand < len(RANDOMNESS_LEVELS) - 1:
        c = copy.deepcopy(curr_knobs)
        c["randomness"] = RANDOMNESS_LEVELS[idx_rand + 1]
        perturbations.append(("rand_step_up", snap_knobs(c)))

    return perturbations


def clone_workload(target_name, target_fp, epochs=3, num_workers=4, output_dir=None):
    """
    Run the MicroGrad workload cloner on a single target fingerprint.
    """
    print("\n" + "=" * 80)
    print(f" MicroGrad Workload Cloner: Target '{target_name}'")
    print("=" * 80)
    print("Target 6-D Microarchitectural Fingerprint:")
    print(f"  IPC:       {target_fp['IPC']:6.3f}   |   BR_MPKI:   {target_fp['BR_MPKI']:6.2f}")
    print(f"  L1D_MPKI:  {target_fp['L1D_MPKI']:6.2f}   |   L2_MPKI:   {target_fp['L2_MPKI']:6.2f}")
    print(f"  LLC_MPKI:  {target_fp['LLC_MPKI']:6.2f}   |   DRAM_RQPI: {target_fp['DRAM_RQPI']:8.5f}\n")

    # Epoch 0: Smart Physics-Informed Warm Start
    t_start = time.time()
    init_knobs = smart_warm_start(target_fp)
    print(f"[Epoch 0: Smart Warm-Start] Initial Knobs: "
          f"branch={init_knobs['branch_frequency']}, "
          f"wss={init_knobs['working_set_kb']}KB, "
          f"reuse={init_knobs['reuse']}, "
          f"dep={init_knobs['memory_dependency']}, "
          f"chains={init_knobs['independent_chains']}")

    base_eval = evaluate_probe_worker((0, "epoch0_init", init_knobs, target_fp))
    if base_eval["status"] != "OK":
        print(f"  Error evaluating initial probe: {base_eval.get('error')}")
        return None

    best_knobs = copy.deepcopy(init_knobs)
    best_loss = base_eval["loss"]
    best_fp = base_eval["fingerprint"]
    best_comps = base_eval["components"]

    print(f"  Initial Loss: {best_loss:.4f}")
    print(f"  Observed FP: IPC={best_fp['IPC']:.2f}, BR={best_fp['BR_MPKI']:.2f}, "
          f"L1D={best_fp['L1D_MPKI']:.1f}, L2={best_fp['L2_MPKI']:.1f}, "
          f"LLC={best_fp['LLC_MPKI']:.1f}, DRAM={best_fp['DRAM_RQPI']:.5f}\n")

    # Gradient Descent Optimization (Epochs 1..N)
    for epoch in range(1, epochs + 1):
        print(f"--- Epoch {epoch}/{epochs} ---")
        perturbations = generate_micrograd_perturbations(best_knobs, best_fp, target_fp, best_comps)

        # Deduplicate candidates
        unique_tasks = []
        seen = set()
        for idx, (pname, pknobs) in enumerate(perturbations):
            key = (pknobs["branch_frequency"], pknobs["working_set_kb"], pknobs["reuse"],
                   pknobs["memory_dependency"], pknobs["independent_chains"])
            if key not in seen:
                seen.add(key)
                w_id = idx % num_workers
                unique_tasks.append((w_id, pname, pknobs, target_fp))

        if not unique_tasks:
            print("  [Convergence] Gradient magnitude below threshold (no further gradient directions).")
            break

        print(f"  Dispatching {len(unique_tasks)} gradient directions in parallel across {min(num_workers, len(unique_tasks))} workers...")
        epoch_best = None
        epoch_best_loss = best_loss

        with ProcessPoolExecutor(max_workers=min(num_workers, len(unique_tasks))) as executor:
            futures = [executor.submit(evaluate_probe_worker, task) for task in unique_tasks]
            for f in as_completed(futures):
                res = f.result()
                if res["status"] == "OK":
                    pname = res["name"]
                    loss = res["loss"]
                    fp = res["fingerprint"]
                    delta = loss - best_loss
                    tag = "IMPROVED" if delta < 0 else "higher"
                    print(f"    [{pname:<15}] Loss: {loss:.4f} ({tag:8} {delta:+.4f}) | "
                          f"IPC={fp['IPC']:.2f}, BR={fp['BR_MPKI']:.1f}, "
                          f"L1D={fp['L1D_MPKI']:.1f}, LLC={fp['LLC_MPKI']:.1f}")
                    if loss < epoch_best_loss:
                        epoch_best_loss = loss
                        epoch_best = res

        if epoch_best and epoch_best_loss < best_loss:
            gain = best_loss - epoch_best_loss
            print(f"  => Epoch {epoch} Step Accepted: Loss reduced by {gain:.4f} -> New Loss: {epoch_best_loss:.4f}\n")
            best_loss = epoch_best_loss
            best_knobs = copy.deepcopy(epoch_best["knobs"])
            best_fp = epoch_best["fingerprint"]
            best_comps = epoch_best["components"]
        else:
            print(f"  => Step plateau reached. Optimization finished.\n")
            break

    total_time = time.time() - t_start

    # Final Report using Unified Engineering Tolerances
    composite_L, z_scores, passes, details = compute_normalized_errors(best_fp, target_fp)
    max_z = max(z_scores.values()) if z_scores else 0.0
    passed_gate = bool(max_z <= 2.5)

    print("-" * 85)
    print(f" Final Cloning Summary for '{target_name}' (Finished in {total_time:.1f}s)")
    print("-" * 85)
    print(f"{'Metric':<12} | {'Target':>10} | {'Clone':>10} | {'Abs Error':>12} | {'Z-Score':>10} | {'Tolerance':>10}")
    print("-" * 85)
    for m in METRICS:
        d = details.get(m, {})
        vt = target_fp.get(m, 0.0)
        vc = best_fp.get(m, 0.0)
        err = abs(vc - vt)
        z_val = z_scores.get(m, 0.0)
        stat = "PASS" if z_val <= 1.0 else ("BORDER" if z_val <= 2.5 else "FAIL")
        print(f"{m:<12} | {vt:10.3f} | {vc:10.3f} | {err:12.3f} | {z_val:9.2f}z | {stat:>10}")

    print("-" * 85)
    print(f"Composite Normalized Error (L): {composite_L:.2f}z   |   Max Error (z_max): {max_z:.2f}z   |   Fidelity Gate: {'PASSED' if passed_gate else 'FAILED'}")
    print("Final Active Semantic Knobs:")
    active_knobs_summary = {
        "branch_frequency": best_knobs["branch_frequency"],
        "working_set_kb": best_knobs["working_set_kb"],
        "reuse": best_knobs["reuse"],
        "memory_dependency": best_knobs["memory_dependency"],
        "independent_chains": best_knobs["independent_chains"]
    }
    print(f"  {json.dumps(active_knobs_summary)}")

    result_data = {
        "workload": target_name,
        "execution_profile": EXEC_PROFILE,
        "target_fingerprint": target_fp,
        "cloned_fingerprint": best_fp,
        "loss": best_loss,
        "composite_error": round(composite_L, 3),
        "max_error": round(max_z, 3),
        "z_scores": {k: round(v, 2) for k, v in z_scores.items()},
        "passed_gate": passed_gate,
        "active_knobs": active_knobs_summary,
        "full_knobs": best_knobs,
        "tuning_time_sec": total_time
    }

    if output_dir:
        out_p = Path(output_dir)
        out_p.mkdir(parents=True, exist_ok=True)
        out_f = out_p / f"{target_name}_clone.json"
        with open(out_f, "w") as f:
            json.dump(result_data, f, indent=2)
        print(f"Results saved to: {out_f}")

    return result_data


def main():
    parser = argparse.ArgumentParser(description="MicroGrad Workload Cloner")
    parser.add_argument("--workloads", nargs="+", default=["403.gcc-17B", "458.sjeng-1088B", "450.soplex-247B"],
                        help="List of master trace workload names to clone")
    parser.add_argument("--epochs", type=int, default=3, help="Max tuning epochs per workload")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel workers")
    parser.add_argument("--output-dir", type=str, default=str(PROJECT_ROOT / "results" / "clones"),
                        help="Directory to save clone results")

    args = parser.parse_args()

    results = []
    for w in args.workloads:
        baseline_json = PROJECT_ROOT / "results" / "oracle" / w / "baseline.json"
        if not baseline_json.exists():
            print(f"Warning: Baseline JSON for {w} does not exist at {baseline_json}. Skipping.")
            continue

        obs = extract_observation(baseline_json)
        target_fp = obs["fingerprint"]
        res = clone_workload(w, target_fp, epochs=args.epochs, num_workers=args.workers, output_dir=args.output_dir)
        if res:
            results.append(res)

    print("\n" + "=" * 92)
    print(" Multi-Workload MicroGrad Cloning Summary")
    print("=" * 92)
    print(f"{'Workload':<22} | {'Loss':>8} | {'Composite':>10} | {'Max Z':>8} | {'Gate':>8} | {'Time':>6} | {'Active Knobs'}")
    print("-" * 92)
    for r in results:
        k = r["active_knobs"]
        k_str = f"br={k['branch_frequency']} wss={k['working_set_kb']}k reuse={k['reuse']} {k['memory_dependency']}(c={k['independent_chains']})"
        gate_str = "PASS" if r.get("passed_gate") else "FAIL"
        print(f"{r['workload']:<22} | {r['loss']:8.4f} | {r['composite_error']:9.2f}z | {r['max_error']:7.2f}z | {gate_str:>8} | {r['tuning_time_sec']:5.1f}s | {k_str}")


if __name__ == "__main__":
    main()
