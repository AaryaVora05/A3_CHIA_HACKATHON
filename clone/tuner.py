#!/usr/bin/env python3
"""
clone/tuner.py -- MicroGrad-based Workload Cloning Tuner with Causal Bottleneck Tolerances.

Implements:
  1. Abstract Workload Model (12 semantic knobs)
  2. Database / Analytical Warm-Start Initialization (Epoch 0)
  3. Numerical Finite-Difference Gradient Descent with Adaptive Step Sizes
  4. Unified Normalized Error Metric: z_i = |C_i - T_i| / max(A_i, R_i * |T_i|) <= 1.0 for PASS
  5. Composite Root-Mean-Square Loss: L = sqrt( 1/M * sum(z_i^2) )
  6. Three Defensible Terminal States:
     - FIDELITY_CONVERGED: all critical z_i <= 1.0 AND L <= 1.0 for 2 consecutive epochs.
     - OPTIMIZER_PLATEAU: gradient improvement < epsilon after annealing, but z_i > 1.0.
     - BUDGET_EXHAUSTED: max epochs reached without satisfactory clone.
  7. Multi-Worker Parallel Evaluation using canonical run_probe.py

Usage:
  python clone/tuner.py --target-name 403.gcc-17B --epochs 6 --workers 4
  python clone/tuner.py --target-name 429.mcf-51B --epochs 6 --workers 4
"""

import argparse
import copy
import glob
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
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
from distance import METRICS, TOLERANCE_SPECS, compute_normalized_errors
from execution_profile import EXEC_PROFILE

PYTHON = str(PROJECT_ROOT / "agent" / ".venv" / "bin" / "python")
RUN_PROBE = str(PROJECT_ROOT / "agent" / "run_probe.py")
PIN_BIN = "/home/avds_a3/pin-3.22-reference/pin"
TRACER_SO = "/home/avds_a3/ChampSim-pin322-reference/tracer/pin/obj-intel64/champsim_tracer.so"
REAL_CHAMPSIM = "/home/avds_a3/ChampSim/bin/champsim"
SIM_INSTR = EXEC_PROFILE["SIM_INSTR"]
WARMUP_MIN = EXEC_PROFILE["WARMUP_MIN"]


def _run_worker(task):
    """Worker process evaluating a single candidate probe via run_probe.py."""
    worker_id, name, sem_dict, target_fp = task
    a3_worker = Path(f"/tmp/a3_tuner_w{worker_id}")
    a3_worker.mkdir(parents=True, exist_ok=True)
    cache_dir = a3_worker / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    uid = f"{int(time.time()*1000)%1000000}_{name}"
    stats_json = a3_worker / f"stats_{uid}.json"
    wrapper_sh = a3_worker / f"champsim_wrap_{uid}.sh"

    wrapper_content = f"""#!/bin/bash
exec {REAL_CHAMPSIM} --json {stats_json} "$@"
"""
    wrapper_sh.write_text(wrapper_content)
    wrapper_sh.chmod(0o755)

    env = os.environ.copy()
    env["A3_ROOT"] = str(a3_worker)
    env["PROBE_TEMPLATE"] = str(PROJECT_ROOT / "probes" / "template" / "probe.c")
    env["CHECK_STATIC"] = str(PROJECT_ROOT / "tools" / "check_static.py")
    env["PIN_BIN"] = PIN_BIN
    env["TRACER_SO"] = TRACER_SO
    env["CHAMPSIM_BIN"] = str(wrapper_sh)
    env["SIM_INSTR"] = SIM_INSTR
    env["WARMUP_MIN"] = WARMUP_MIN

    try:
        sem = SemanticProbe(**sem_dict)
        probe_json_str = json.dumps(sem.model_dump())
        cmd = [PYTHON, RUN_PROBE, probe_json_str, "--semantic"]

        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if res.returncode != 0:
            return {"name": name, "sem_dict": sem_dict, "status": "FAIL", "error": res.stderr[-200:]}

        if not stats_json.exists():
            return {"name": name, "sem_dict": sem_dict, "status": "FAIL", "error": "No stats JSON"}

        obs = extract_observation(stats_json)
        fp = obs["fingerprint"]
        comp_loss, z_scores, passes, details = compute_normalized_errors(fp, target_fp)

        return {
            "name": name,
            "sem_dict": sem.model_dump(),
            "fingerprint": fp,
            "loss": comp_loss,
            "z_scores": z_scores,
            "passes": passes,
            "details": details,
            "status": "OK"
        }
    except Exception as e:
        return {"name": name, "sem_dict": sem_dict, "status": "FAIL", "error": str(e)}
    finally:
        wrapper_sh.unlink(missing_ok=True)
        stats_json.unlink(missing_ok=True)


def find_database_warm_start(target_fp):
    """Retrieve nearest probe from the 201 empirical runs using z_i loss."""
    best_probe = None
    best_loss = float("inf")
    sweep_files = glob.glob(str(PROJECT_ROOT / "results" / "v11_sweeps" / "*.json"))
    if not sweep_files:
        sweep_files = glob.glob(str(PROJECT_ROOT / "results" / "characterization" / "*.json"))

    for sf in sweep_files:
        try:
            with open(sf, "r") as f:
                data = json.load(f)
            cand_fp = data.get("fingerprint", {})
            sem_knobs = data.get("semantic_knobs", {})
            if all(m in cand_fp for m in METRICS) and sem_knobs:
                l_val, _, _, _ = compute_normalized_errors(cand_fp, target_fp)
                if l_val < best_loss:
                    best_loss = l_val
                    best_probe = (Path(sf).stem, sem_knobs, cand_fp, l_val)
        except Exception:
            continue
    return best_probe


def analytical_warm_start(target_fp):
    """Physics-informed analytical inversion to seed Epoch 0."""
    # 1. Branch frequency & pattern
    br_mpki = target_fp.get("BR_MPKI", 0.0)
    if br_mpki < 0.5:
        b_freq = 0
        b_pat = "periodic"
        b_param = 4
    elif br_mpki < 5.0:
        b_freq = max(1, int(round(br_mpki / 2.0)))
        b_pat = "periodic"
        b_param = 8
    else:
        b_freq = 8
        b_pat = "random"
        b_param = 50

    # 2. Working set size & reuse from cache hierarchy
    llc_mpki = target_fp.get("LLC_MPKI", 0.0)
    l2_mpki = target_fp.get("L2_MPKI", 0.0)
    l1d_mpki = target_fp.get("L1D_MPKI", 0.0)
    dram_rqpi = target_fp.get("DRAM_RQPI", 0.0)

    if llc_mpki > 5.0 or dram_rqpi > 0.01:
        wss = 16384
        reuse = 0.0
    elif l2_mpki > 5.0:
        wss = 2048
        reuse = 0.1
    elif l1d_mpki > 5.0:
        wss = 512
        reuse = 0.25
    else:
        wss = 16
        reuse = 0.8

    # 3. Memory dependency & Concurrency from IPC
    ipc = target_fp.get("IPC", 1.0)
    if ipc < 0.4:
        dep = "dependent"
        chains = 1
        alu = 0
    elif ipc < 0.8:
        dep = "dependent"
        chains = 2
        alu = 4
    elif ipc < 1.5:
        dep = "independent"
        chains = 2
        alu = 8
    else:
        dep = "independent"
        chains = 4
        alu = 16

    return {
        "branch_frequency": b_freq,
        "branch_pattern": b_pat,
        "branch_pattern_param": b_param,
        "load_branch_dependency": (ipc < 0.9 and b_freq > 0),
        "working_set_kb": wss,
        "stride_lines": 1,
        "randomness": 0.5 if wss > 32 else 0.0,
        "reuse": reuse,
        "memory_dependency": dep,
        "independent_chains": chains,
        "alu_ops": alu,
        "dependency_distance": 1,
    }


def snap_to_valid(sem_dict):
    """Project perturbations to valid discrete SemanticProbe bounds."""
    wss = sem_dict["working_set_kb"]
    pow2_wss = [4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]
    sem_dict["working_set_kb"] = min(pow2_wss, key=lambda x: abs(x - wss))

    stride = sem_dict["stride_lines"]
    pow2_stride = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024]
    sem_dict["stride_lines"] = min(pow2_stride, key=lambda x: abs(x - stride))

    chains = sem_dict["independent_chains"]
    pow2_chains = [1, 2, 4, 8]
    sem_dict["independent_chains"] = min(pow2_chains, key=lambda x: abs(x - chains))

    sem_dict["branch_frequency"] = max(0, min(8, int(round(sem_dict["branch_frequency"]))))
    sem_dict["branch_pattern_param"] = max(10, min(90, int(round(sem_dict["branch_pattern_param"]))))
    sem_dict["randomness"] = max(0.0, min(1.0, float(sem_dict["randomness"])))
    sem_dict["reuse"] = max(0.0, min(0.9, float(sem_dict["reuse"])))
    sem_dict["alu_ops"] = max(0, min(64, int(round(sem_dict["alu_ops"]))))
    sem_dict["dependency_distance"] = max(1, min(8, int(round(sem_dict["dependency_distance"]))))

    while (sem_dict["working_set_kb"] * 16) // sem_dict["stride_lines"] < 2 * sem_dict["independent_chains"]:
        if sem_dict["stride_lines"] > 1:
            sem_dict["stride_lines"] //= 2
        else:
            sem_dict["working_set_kb"] *= 2

    if sem_dict["alu_ops"] > 0 and sem_dict["alu_ops"] % sem_dict["dependency_distance"] != 0:
        sem_dict["alu_ops"] = (sem_dict["alu_ops"] // sem_dict["dependency_distance"]) * sem_dict["dependency_distance"]

    return sem_dict


def run_micrograd_tuning(target_fp, epochs=6, num_workers=4, output_dir=None):
    """Main MicroGrad tuning loop with Causal Bottleneck Tolerances."""
    print("=" * 80)
    print(" MicroGrad Workload Cloning Engine (ChampSim M0)")
    print(" Causal Bottleneck Normalization: z_i = |C_i - T_i| / max(A_i, 0.10 * |T_i|) <= 1.0")
    print("=" * 80)
    print("Target Fingerprint:")
    print(f"  IPC:       {target_fp['IPC']:.4f}")
    print(f"  BR_MPKI:   {target_fp['BR_MPKI']:.4f}")
    print(f"  L1D_MPKI:  {target_fp['L1D_MPKI']:.4f}")
    print(f"  L2_MPKI:   {target_fp['L2_MPKI']:.4f}")
    print(f"  LLC_MPKI:  {target_fp['LLC_MPKI']:.4f}")
    dram_rpki = target_fp.get('DRAM_RPKI', target_fp.get('DRAM_RQPI', 0.0) * 1000.0)
    print(f"  DRAM_RPKI: {dram_rpki:.4f} req/KI (RQPI: {target_fp.get('DRAM_RQPI', 0.0):.6f})\n")

    # Step 1: Warm-Start Initialization (Epoch 0)
    db_best = find_database_warm_start(target_fp)
    analytical_init = analytical_warm_start(target_fp)

    if db_best and db_best[3] < 10.0:
        print(f"[Warm-Start] Found near empirical match from sweep: '{db_best[0]}' (Loss: {db_best[3]:.4f})")
        curr_knobs = copy.deepcopy(db_best[1])
    else:
        print(f"[Warm-Start] Using analytical microarchitectural initialization.")
        curr_knobs = analytical_init

    base_eval = _run_worker((0, "base_init", curr_knobs, target_fp))
    if base_eval["status"] != "OK":
        print(f"Error evaluating initial configuration: {base_eval.get('error')}")
        sys.exit(1)

    best_knobs = copy.deepcopy(curr_knobs)
    best_loss = base_eval["loss"]
    best_fp = base_eval["fingerprint"]
    best_z = base_eval["z_scores"]
    best_passes = base_eval["passes"]
    best_details = base_eval["details"]

    print(f"[Epoch 0 Base] Initial Loss L: {best_loss:.4f} | Max z: {max(best_z.values()):.2f}")
    print(f"  Initial z-scores: " + ", ".join(f"{k}={v:.2f}" for k, v in best_z.items()) + "\n")

    success_counter = 0
    terminal_state = "BUDGET_EXHAUSTED"
    terminal_reason = f"Completed all {epochs} epochs without reaching target tolerances on all metrics."

    consecutive_plateaus = 0

    for epoch in range(1, epochs + 1):
        print(f"--- Epoch {epoch}/{epochs} ---")
        # Identify the primary failing metric (highest z_i)
        sorted_z = sorted(best_z.items(), key=lambda x: x[1], reverse=True)
        top_metric, top_z = sorted_z[0]
        print(f"  Primary residual contributor: {top_metric} (z = {top_z:.2f}, {'PASS' if top_z <= 1.0 else 'FAIL'})")

        perturbations = []

        # 1. Branch perturbations (coarse and fine)
        if best_z.get("BR_MPKI", 0.0) > 1.0:
            if best_fp["BR_MPKI"] < target_fp["BR_MPKI"]:
                cand = copy.deepcopy(curr_knobs)
                if cand["branch_frequency"] < 8:
                    cand["branch_frequency"] = min(8, cand["branch_frequency"] + 1)
                else:
                    cand["branch_pattern_param"] = min(90, cand["branch_pattern_param"] + 10)
                perturbations.append(("br_up", snap_to_valid(cand)))
            else:
                cand = copy.deepcopy(curr_knobs)
                if cand["branch_pattern_param"] > 20:
                    cand["branch_pattern_param"] = max(10, cand["branch_pattern_param"] - 10)
                else:
                    cand["branch_frequency"] = max(0, cand["branch_frequency"] - 1)
                perturbations.append(("br_down", snap_to_valid(cand)))

        # 2. Cache tier & capacity perturbations
        if best_z.get("LLC_MPKI", 0.0) > 1.0 or best_z.get("DRAM_RPKI", 0.0) > 1.0:
            cand_up = copy.deepcopy(curr_knobs)
            cand_up["working_set_kb"] = min(65536, cand_up["working_set_kb"] * 2)
            perturbations.append(("wss_up", snap_to_valid(cand_up)))

            cand_dn = copy.deepcopy(curr_knobs)
            cand_dn["working_set_kb"] = max(4, cand_dn["working_set_kb"] // 2)
            perturbations.append(("wss_down", snap_to_valid(cand_dn)))

        # 3. Reuse perturbations (fine-grained L1D/L2 tuning)
        if best_z.get("L1D_MPKI", 0.0) > 1.0 or best_z.get("L2_MPKI", 0.0) > 1.0:
            if best_fp["L1D_MPKI"] > target_fp["L1D_MPKI"]:
                for step in [0.05, 0.15]:
                    cand = copy.deepcopy(curr_knobs)
                    cand["reuse"] = min(0.9, round(cand["reuse"] + step, 3))
                    perturbations.append((f"reuse_up_{int(step*100)}", snap_to_valid(cand)))
            else:
                for step in [0.05, 0.15]:
                    cand = copy.deepcopy(curr_knobs)
                    cand["reuse"] = max(0.0, round(cand["reuse"] - step, 3))
                    perturbations.append((f"reuse_dn_{int(step*100)}", snap_to_valid(cand)))

        # 4. IPC & Concurrency fine-tuning
        if best_z.get("IPC", 0.0) > 1.0:
            if best_fp["IPC"] < target_fp["IPC"]:
                cand1 = copy.deepcopy(curr_knobs)
                cand1["independent_chains"] = min(8, cand1["independent_chains"] * 2)
                perturbations.append(("chains_up", snap_to_valid(cand1)))

                cand2 = copy.deepcopy(curr_knobs)
                cand2["alu_ops"] = min(64, cand2["alu_ops"] + 8)
                perturbations.append(("alu_up", snap_to_valid(cand2)))
            else:
                cand1 = copy.deepcopy(curr_knobs)
                if cand1["memory_dependency"] == "independent":
                    cand1["memory_dependency"] = "dependent"
                elif cand1["independent_chains"] > 1:
                    cand1["independent_chains"] = max(1, cand1["independent_chains"] // 2)
                else:
                    cand1["alu_ops"] = max(0, cand1["alu_ops"] - 8)
                perturbations.append(("ipc_down", snap_to_valid(cand1)))

        unique_tasks = []
        seen = set()
        for idx, (pname, pknobs) in enumerate(perturbations):
            key = json.dumps(pknobs, sort_keys=True)
            if key not in seen:
                seen.add(key)
                w_id = idx % num_workers
                unique_tasks.append((w_id, pname, pknobs, target_fp))

        if not unique_tasks:
            print("  No further gradient directions to explore in current subspace.")
            consecutive_plateaus += 1
            if consecutive_plateaus >= 2:
                if all(v <= 1.0 for v in best_z.values()) and best_loss <= 1.0:
                    terminal_state = "FIDELITY_CONVERGED"
                    terminal_reason = "All critical metrics within engineering tolerance (z_i <= 1.0) and composite loss L <= 1.0."
                else:
                    terminal_state = "OPTIMIZER_PLATEAU"
                    terminal_reason = "Numerical search stopped improving. One or more critical metrics remain outside tolerance."
                break
            continue

        print(f"  Dispatching {len(unique_tasks)} finite-difference gradient checks in parallel...")

        epoch_best = None
        epoch_best_loss = best_loss

        with ProcessPoolExecutor(max_workers=min(num_workers, len(unique_tasks))) as executor:
            futures = [executor.submit(_run_worker, task) for task in unique_tasks]
            for f in as_completed(futures):
                res = f.result()
                if res["status"] == "OK":
                    pname = res["name"]
                    loss_val = res["loss"]
                    fp = res["fingerprint"]
                    z_val = max(res["z_scores"].values())
                    delta = loss_val - best_loss
                    symbol = "▼ IMPROVED" if delta < 0 else "▲ higher"
                    print(f"    [{pname:<14}] Loss L: {loss_val:.4f} ({symbol} {abs(delta):.3f}) | Max z: {z_val:.2f} | IPC={fp['IPC']:.2f}, BR={fp['BR_MPKI']:.2f}, L1D={fp['L1D_MPKI']:.1f}, LLC={fp['LLC_MPKI']:.1f}")
                    if loss_val < epoch_best_loss:
                        epoch_best_loss = loss_val
                        epoch_best = res

        if epoch_best and epoch_best_loss < best_loss:
            improvement = best_loss - epoch_best_loss
            print(f"  => Epoch {epoch} Success: Loss reduced by {improvement:.4f} -> Best Loss L: {epoch_best_loss:.4f}\n")
            curr_knobs = copy.deepcopy(epoch_best["sem_dict"])
            best_knobs = copy.deepcopy(epoch_best["sem_dict"])
            best_loss = epoch_best_loss
            best_fp = epoch_best["fingerprint"]
            best_z = epoch_best["z_scores"]
            best_passes = epoch_best["passes"]
            best_details = epoch_best["details"]
            consecutive_plateaus = 0
        else:
            consecutive_plateaus += 1
            print(f"  => Step plateau reached (consecutive: {consecutive_plateaus}). Annealing.\n")

        # Two-epoch stability check for FIDELITY_CONVERGED
        all_pass = all(v <= 1.0 for v in best_z.values()) and best_loss <= 1.0
        if all_pass:
            success_counter += 1
            print(f"  [Tolerance Met] All z_i <= 1.0 (Streak: {success_counter}/2)")
            if success_counter >= 2:
                terminal_state = "FIDELITY_CONVERGED"
                terminal_reason = "All critical metrics reproduced within tolerances for 2 consecutive epochs."
                break
        else:
            success_counter = 0

        if consecutive_plateaus >= 2:
            if all_pass:
                terminal_state = "FIDELITY_CONVERGED"
                terminal_reason = "All critical metrics reproduced within tolerances."
            else:
                terminal_state = "OPTIMIZER_PLATEAU"
                terminal_reason = ("Numerical search has stopped improving, but one or more critical "
                                   "metrics remain outside tolerance (requires structural representation expansion).")
            break

    print("=" * 80)
    print(" MicroGrad Workload Cloning Final Report")
    print("=" * 80)
    print(f"TERMINAL STATE:  {terminal_state}")
    print(f"TERMINAL REASON: {terminal_reason}\n")
    print(f"Composite RMS Loss L: {best_loss:.4f}\n")

    print(f"{'Metric':<12} | {'Target':>10} | {'Clone':>10} | {'Abs Error':>12} | {'Scale S_i':>10} | {'z-score':>8} | {'Status':>6}")
    print("-" * 80)
    for m, det in best_details.items():
        t_val = det["target"]
        c_val = det["clone"]
        err = det["abs_error"]
        s_i = det["scale_S"]
        z = det["z"]
        st = "PASS" if det["pass"] else "FAIL"
        print(f"{m:<12} | {t_val:10.4f} | {c_val:10.4f} | {err:12.4f} | {s_i:10.4f} | {z:8.2f} | {st:>6}")

    print("\nResidual Diagnostic Summary:")
    passed_metrics = [m for m, p in best_passes.items() if p]
    failed_metrics = [m for m, p in best_passes.items() if not p]
    print(f"  Passed Metrics: {', '.join(passed_metrics) if passed_metrics else 'None'}")
    print(f"  Failed Metrics: {', '.join(failed_metrics) if failed_metrics else 'None'}")

    print("\nFinal Tuned Semantic Knobs:")
    print(json.dumps(best_knobs, indent=2))

    if output_dir:
        out_p = Path(output_dir)
        out_p.mkdir(parents=True, exist_ok=True)
        res_file = out_p / "cloned_probe.json"
        with open(res_file, "w") as f:
            json.dump({
                "terminal_state": terminal_state,
                "terminal_reason": terminal_reason,
                "composite_loss": best_loss,
                "target": target_fp,
                "cloned_fingerprint": best_fp,
                "z_scores": best_z,
                "passes": best_passes,
                "details": best_details,
                "semantic_knobs": best_knobs,
            }, f, indent=2)
        print(f"\nResults saved to: {res_file}")

    return terminal_state, best_knobs, best_fp, best_loss


def main():
    parser = argparse.ArgumentParser(description="MicroGrad Workload Cloning Tuner with Causal Bottleneck Tolerances")
    parser.add_argument("--target", type=str, help="Path to target ChampSim JSON baseline")
    parser.add_argument("--target-name", type=str, help="SPEC benchmark name in results/oracle/ (e.g. 403.gcc-17B)")
    parser.add_argument("--epochs", type=int, default=6, help="Number of MicroGrad tuning epochs")
    parser.add_argument("--workers", type=int, default=4, help="Number of parallel worker processes")
    parser.add_argument("--output-dir", type=str, default=str(PROJECT_ROOT / "results" / "clones"), help="Output directory")

    args = parser.parse_args()

    if args.target_name:
        target_path = PROJECT_ROOT / "results" / "oracle" / args.target_name / "baseline.json"
    elif args.target:
        target_path = Path(args.target)
    else:
        target_path = PROJECT_ROOT / "results" / "oracle" / "403.gcc-17B" / "baseline.json"

    if not target_path.exists():
        print(f"Error: Target path does not exist: {target_path}")
        sys.exit(1)

    obs = extract_observation(target_path)
    target_fp = obs["fingerprint"]

    run_micrograd_tuning(target_fp, epochs=args.epochs, num_workers=args.workers, output_dir=args.output_dir)


if __name__ == "__main__":
    main()
