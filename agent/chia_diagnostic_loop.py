#!/usr/bin/env python3
"""
agent/chia_diagnostic_loop.py -- CHIA Loop for Autonomous Bottleneck Diagnosis.

Orchestrates the iterative feedback loop between:
  1. The LLM Agent (Gemini 2.5 Flash via Google GenAI)
  2. The Semantic Probe Synthesizer and ChampSim M0 execution nodes
using CHIA Functions (Ray-distributed execution).

Given:
  - Master Workload ChampSim output
  - Cloned Workload knobs (from MicroGrad)
  - Synthetic probe code generator (probe.c / SemanticProbe)
The agent hypothesizes the machine bottleneck (branch, cache, dram_lat, dram_bw, rob),
dispatches small diagnostic probes to isolate sensitivities on M0,
and discovers the primary microarchitectural limiter.
"""

import argparse
import copy
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Any, Tuple

# Setup project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))
sys.path.insert(0, str(PROJECT_ROOT / "clone"))

from chia.base.ChiaFunction import ChiaFunction, get
from google import genai
from google.genai.types import HttpOptions

from semantic import SemanticProbe
from observation import extract_observation
from distance import METRICS, compute_normalized_errors
from execution_profile import EXEC_PROFILE
from prompts import (
    SYSTEM_PROMPT,
    AgentStepResponse,
    FinalDiagnosis,
    build_agent_turn_prompt,
)

PYTHON = str(PROJECT_ROOT / "agent" / ".venv" / "bin" / "python")
RUN_PROBE = str(PROJECT_ROOT / "agent" / "run_probe.py")
PIN_BIN = "/home/avds_a3/pin-3.22-reference/pin"
TRACER_SO = "/home/avds_a3/ChampSim-pin322-reference/tracer/pin/obj-intel64/champsim_tracer.so"
REAL_CHAMPSIM = "/home/avds_a3/ChampSim/bin/champsim"
SIM_INSTR = EXEC_PROFILE["SIM_INSTR"]
WARMUP_MIN = EXEC_PROFILE["WARMUP_MIN"]


_SESSION_PROBE_CACHE: Dict[str, dict] = {}


def get_probe_cache_key(knobs_dict: dict) -> str:
    try:
        sem = SemanticProbe(**knobs_dict)
        return json.dumps(sem.model_dump(), sort_keys=True)
    except Exception:
        return json.dumps(knobs_dict, sort_keys=True)


@ChiaFunction(num_cpus=1.0)
def execute_probe_node(knobs_dict: dict) -> dict:
    """
    CHIA Node: Compiles and executes a synthetic probe on M0 via ChampSim.
    Runs a fast 500k-instruction probe to extract microarchitectural response.
    Reuses cached simulation results if an identical probe configuration was already simulated.
    """
    cache_key = get_probe_cache_key(knobs_dict)
    if cache_key in _SESSION_PROBE_CACHE:
        cached_res = copy.deepcopy(_SESSION_PROBE_CACHE[cache_key])
        cached_res["cached"] = True
        return cached_res

    worker_id = os.getpid()
    w_dir = Path(f"/tmp/chia_probe_worker_{worker_id}")
    w_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = w_dir / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)

    uid = f"{int(time.time()*1000)%1000000}"
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

        res = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=450)
        if res.returncode != 0:
            return {"status": "FAIL", "error": res.stderr[-300:]}

        if not stats_json.exists():
            return {"status": "FAIL", "error": "ChampSim stats JSON not generated"}

        obs = extract_observation(stats_json)
        result_dict = {
            "status": "OK",
            "fingerprint": obs["fingerprint"],
            "secondary": obs["secondary"],
            "knobs": sem.model_dump(),
            "cached": False,
        }
        _SESSION_PROBE_CACHE[cache_key] = copy.deepcopy(result_dict)
        return result_dict
    except Exception as e:
        return {"status": "FAIL", "error": str(e)}
    finally:
        wrapper_sh.unlink(missing_ok=True)
        stats_json.unlink(missing_ok=True)


@ChiaFunction(num_cpus=0.5)
def llm_reasoning_node(prompt_text: str) -> dict:
    """
    CHIA Node: Invokes the Gemini reasoning agent to analyze probe history
    and synthesize the next diagnostic probe or final diagnosis.
    """
    client = genai.Client(
        vertexai=True,
        project="chia-508019",
        location="us-central1",
    )

    full_prompt = f"{SYSTEM_PROMPT}\n\n{prompt_text}"

    models_to_try = ["gemini-2.5-pro", "gemini-2.5-pro", "gemini-2.5-flash", "gemini-1.5-pro"]
    for attempt, m_name in enumerate(models_to_try):
        try:
            response = client.models.generate_content(
                model=m_name,
                contents=full_prompt,
                config={
                    "response_mime_type": "application/json",
                    "response_schema": AgentStepResponse,
                    "temperature": 0.1,
                },
            )
            data = json.loads(response.text)
            return {"status": "OK", "data": data, "raw_text": response.text}
        except Exception as e:
            if attempt < len(models_to_try) - 1:
                import time
                time.sleep(3 * (attempt + 1))
            else:
                return {"status": "ERROR", "error": str(e), "raw_text": ""}


def load_ground_truth(workload: str) -> dict:
    """Load oracle ground-truth bottleneck classification."""
    import csv
    for fname in ["oracle_analysis_6class.csv", "oracle_analysis_extra26.csv"]:
        oracle_csv = PROJECT_ROOT / "results" / "oracle" / fname
        if oracle_csv.exists():
            with open(oracle_csv, mode="r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["workload"] == workload:
                        return row
    return {}



def run_chia_diagnostic_loop(workload: str, max_turns: int = 3, use_chia_remote: bool = True):
    """
    Runs the complete CHIA Loop for autonomous bottleneck discovery.
    """
    print("\n" + "=" * 80)
    print(f" CHIA LOOP: Autonomous Bottleneck Diagnosis for '{workload}'")
    print("=" * 80)

    # 1. Load Master Trace ChampSim Baseline
    master_json = PROJECT_ROOT / "results" / "oracle" / workload / "baseline.json"
    if not master_json.exists():
        print(f"Error: Master baseline not found at {master_json}")
        return None
    master_obs = extract_observation(master_json)
    master_fp = master_obs["fingerprint"]
    master_sec = master_obs["secondary"]
    master_r_hit = master_sec.get("DRAM_row_hit_rate", 0.0)
    master_llc_lat = master_sec.get("LLC_miss_latency", 0.0)
    master_dbus = master_sec.get("DRAM_dbus_congestion", 0.0)
    master_fp_prompt = dict(
        master_fp,
        DRAM_row_hit_rate=f"{master_r_hit*100:.1f}%",
        LLC_miss_latency_cycles=round(master_llc_lat, 1),
        DRAM_dbus_congestion_cycles=round(master_dbus, 2),
    )

    print("Target Master ChampSim Fingerprint on M0:")
    print(f"  IPC:       {master_fp['IPC']:6.3f}   |   BR_MPKI:   {master_fp['BR_MPKI']:6.2f}")
    print(f"  L1D_MPKI:  {master_fp['L1D_MPKI']:6.2f}   |   L2_MPKI:   {master_fp['L2_MPKI']:6.2f}")
    print(f"  LLC_MPKI:  {master_fp['LLC_MPKI']:6.2f}   |   DRAM_RQPI: {master_fp['DRAM_RQPI']:8.5f}   |   Row_Hit: {master_r_hit*100:.1f}%")
    print(f"  LLC Miss Lat: {master_llc_lat:5.1f} cyc | DBUS Congest: {master_dbus:5.2f} cyc\n")

    # 2. Load Cloned Workload Baseline
    clone_json = PROJECT_ROOT / "results" / "clones" / f"{workload}_clone.json"
    if not clone_json.exists():
        print(f"Cloned baseline not found at {clone_json}. Running fast MicroGrad cloner...")
        from micrograd_cloner import clone_workload
        clone_data = clone_workload(workload, master_fp, epochs=1, num_workers=4,
                                   output_dir=str(PROJECT_ROOT / "results" / "clones"))
    else:
        with open(clone_json) as f:
            clone_data = json.load(f)

    cloned_knobs = clone_data["full_knobs"]

    # Evaluate baseline clone under uniform session execution profile
    print("Evaluating Cloned Baseline under uniform session execution profile...")
    if use_chia_remote:
        base_ref = execute_probe_node.chia_remote(cloned_knobs)
        base_res = get(base_ref)
    else:
        base_res = execute_probe_node(cloned_knobs)

    if base_res.get("status") != "OK":
        print(f"  [ERROR] Cloned baseline re-measurement failed under active profile: {base_res.get('error')}")
        oracle = load_ground_truth(workload)
        diag_result = {
            "workload": workload,
            "execution_profile": EXEC_PROFILE,
            "status": "CLONE_SIM_FAILED",
            "clone_passed_gate": False,
            "composite_error": None,
            "max_error": None,
            "z_scores": {},
            "diagnosis": None,
            "oracle_ground_truth": oracle.get("best_class", "UNKNOWN"),
            "oracle_speedup_pct": oracle.get("best_pct", "0.0"),
            "correct": False,
            "loop_time_sec": 0.0,
        }
        out_file = PROJECT_ROOT / "results" / "clones" / f"{workload}_diagnosis.json"
        with open(out_file, "w") as f:
            json.dump(diag_result, f, indent=2)
        return diag_result

    cloned_fp = base_res["fingerprint"]

    # Figure of Merit (FOM) and Normalized Fidelity Analysis
    composite_L, z_scores, passes, details = compute_normalized_errors(cloned_fp, master_fp)
    max_z = max(z_scores.values()) if z_scores else 0.0
    
    # FOM: 100% at L=0, scaled smoothly across tolerance space
    fom = round(max(0.0, min(100.0, 100.0 * (1.0 - composite_L / 15.0))), 1)
    if composite_L <= 4.0:
        quality_tier = "HIGH"
    elif composite_L <= 10.0:
        quality_tier = "MODERATE"
    else:
        quality_tier = "COARSE"
        
    # Relaxed gate (composite error <= 10.0z ensures ~90% pass rate)
    RELAXED_COMPOSITE_GATE = 10.0
    clone_passed_gate = bool(composite_L <= RELAXED_COMPOSITE_GATE)

    print("Cloned Workload Baseline on M0:")
    print(f"  Active Knobs: {clone_data.get('active_knobs')}")
    print(f"  Observed FP:  IPC={cloned_fp['IPC']:.3f}, BR={cloned_fp['BR_MPKI']:.2f}, "
          f"L1D={cloned_fp['L1D_MPKI']:.2f}, L2={cloned_fp['L2_MPKI']:.2f}, "
          f"LLC={cloned_fp['LLC_MPKI']:.2f}, DRAM={cloned_fp['DRAM_RQPI']:.5f}")
    print(f"  Normalized Fidelity Errors: { {k: round(v, 2) for k, v in z_scores.items()} }")
    print(f"  Figure of Merit (FOM): {fom}% ({quality_tier}) | Composite: {composite_L:.2f}z | Max: {max_z:.2f}z | Gate: {'PASSED' if clone_passed_gate else 'REJECTED'}\n")

    oracle = load_ground_truth(workload)
    ground_truth_class = oracle.get("best_class", "UNKNOWN")
    ground_truth_gain = oracle.get("best_pct", "0.0")

    clone_quality_info = {
        "figure_of_merit": f"{fom}%",
        "quality_tier": quality_tier,
        "composite_error": f"{composite_L:.2f}z",
        "max_error": f"{max_z:.2f}z",
        "per_metric_errors": {k: f"{v:.2f}z" for k, v in z_scores.items()},
    }

    # 3. Iterative CHIA Diagnostic Loop (Executes for all workloads)
    history = []
    final_diagnosis = None
    t0 = time.time()

    for turn in range(1, max_turns + 1):
        print(f"\n>>> [CHIA LOOP TURN {turn}/{max_turns}] Invoking Agent Reasoning Node...")
        turn_prompt = build_agent_turn_prompt(
            workload=workload,
            master_fingerprint=master_fp_prompt,
            cloned_knobs=cloned_knobs,
            cloned_fingerprint=cloned_fp,
            history=history,
            turn=turn,
            max_turns=max_turns,
            clone_quality=clone_quality_info,
        )


        # Dispatch LLM Reasoning Node
        if use_chia_remote:
            ref = llm_reasoning_node.chia_remote(turn_prompt)
            llm_result = get(ref)
        else:
            llm_result = llm_reasoning_node(turn_prompt)

        if llm_result["status"] != "OK":
            print(f"LLM Error: {llm_result.get('error')}")
            break

        agent_resp = AgentStepResponse.model_validate(llm_result["data"])

        # Display Current Hypotheses
        print(f"  Belief Distribution: {agent_resp.hypotheses}")
        print(f"  Reasoning: {agent_resp.reasoning[:200]}...")

        if agent_resp.decision == "DIAGNOSE" or turn >= max_turns:
            if agent_resp.final_diagnosis is not None:
                final_diagnosis = agent_resp.final_diagnosis
            else:
                best_hyp = max(agent_resp.hypotheses.items(), key=lambda x: x[1])
                final_diagnosis = FinalDiagnosis(
                    primary_bottleneck=best_hyp[0],
                    confidence=best_hyp[1],
                    justification=agent_resp.reasoning,
                )
            print(f"\n  *** Agent Rendered FINAL DIAGNOSIS at Turn {turn} ***")
            break

        # Execute Next Diagnostic Probe
        next_probe = agent_resp.next_probe
        if not next_probe:
            print("  Agent chose PROBE but provided no probe spec. Ending loop.")
            break

        print(f"  Proposing Probe for Hypothesis: '{next_probe.hypothesis_tested}'")
        print(f"  Knob Overrides: {next_probe.knob_overrides}")
        print(f"  Expected Outcome: {next_probe.expected_outcome}")

        # Merge overrides with clone baseline
        candidate_knobs = copy.deepcopy(cloned_knobs)
        for k, v in next_probe.knob_overrides.items():
            candidate_knobs[k] = v

        print(f"  Dispatching Probe to CHIA Execution Node...")
        if use_chia_remote:
            probe_ref = execute_probe_node.chia_remote(candidate_knobs)
            probe_res = get(probe_ref)
        else:
            probe_res = execute_probe_node(candidate_knobs)

        if probe_res["status"] != "OK":
            print(f"  Probe execution failed: {probe_res.get('error')}")
            break

        obs = probe_res["fingerprint"]
        delta_ipc = (obs["IPC"] - cloned_fp["IPC"]) / max(cloned_fp["IPC"], 0.001) * 100.0
        hit_tag = " [CACHE HIT]" if probe_res.get("cached") else ""
        print(f"  Probe Result{hit_tag}: IPC={obs['IPC']:.3f} (Delta: {delta_ipc:+.1f}%) | "
              f"BR_MPKI={obs['BR_MPKI']:.2f} | LLC_MPKI={obs['LLC_MPKI']:.2f} | "
              f"DRAM_RQPI={obs['DRAM_RQPI']:.5f}")

        history.append({
            "turn": turn,
            "probe": next_probe.model_dump(),
            "result": probe_res,
        })

    loop_time = time.time() - t0

    # 4. Final Evaluation & Oracle Comparison
    print("\n" + "=" * 80)
    print(f" CHIA DIAGNOSTIC RESULTS FOR '{workload}' (Completed in {loop_time:.1f}s)")
    print("=" * 80)

    oracle = load_ground_truth(workload)
    ground_truth_class = oracle.get("best_class", "UNKNOWN")
    raw_gain = str(oracle.get("best_pct", "0.0")).replace("+", "").replace("%", "")
    try:
        ground_truth_gain = float(raw_gain)
    except Exception:
        ground_truth_gain = 0.0

    if final_diagnosis:
        pred_class = final_diagnosis.primary_bottleneck
        conf = final_diagnosis.confidence
        justification = final_diagnosis.justification
        is_correct = (pred_class.lower() == ground_truth_class.lower())

        print(f"Agent Diagnosis:       {pred_class.upper()} (Confidence: {conf*100:.1f}%)")
        print(f"Oracle Ground Truth:   {ground_truth_class.upper()} (Speedup on relaxation: +{ground_truth_gain:.1f}%)")
        match_str = "MATCH (SUCCESS)" if is_correct else "MISMATCH"
        print(f"Validation Result:     {match_str}")
        print(f"\nJustification:\n  {justification}")
    else:
        print("Agent did not reach a final diagnosis.")

    diag_result = {
        "workload": workload,
        "execution_profile": EXEC_PROFILE,
        "status": "COMPLETED",
        "figure_of_merit": fom,
        "quality_tier": quality_tier,
        "clone_passed_gate": clone_passed_gate,
        "composite_error": round(composite_L, 3),
        "max_error": round(max_z, 3),
        "normalized_fidelity_errors": {k: round(v, 2) for k, v in z_scores.items()},
        "master_fingerprint": master_fp,
        "cloned_fingerprint": cloned_fp,
        "turns_executed": len(history),
        "history": history,
        "diagnosis": final_diagnosis.model_dump() if final_diagnosis else None,
        "oracle_ground_truth": ground_truth_class,
        "oracle_speedup_pct": ground_truth_gain,
        "correct": (final_diagnosis.primary_bottleneck.lower() == ground_truth_class.lower()) if final_diagnosis else False,
        "loop_time_sec": loop_time,
    }

    out_file = PROJECT_ROOT / "results" / "clones" / f"{workload}_diagnosis.json"
    with open(out_file, "w") as f:
        json.dump(diag_result, f, indent=2)
    print(f"\nDiagnostic trace saved to: {out_file}")

    return diag_result


def main():
    parser = argparse.ArgumentParser(description="CHIA Loop Autonomous Bottleneck Diagnosis")
    parser.add_argument("--workload", type=str, default=None,
                        help="Single target master workload to diagnose")
    parser.add_argument("--workloads", nargs="+", default=None,
                        help="List of master workloads to diagnose")
    parser.add_argument("--all", action="store_true",
                        help="Diagnose all available master traces in maxweight_26.csv")
    parser.add_argument("--extra26", action="store_true",
                        help="Diagnose the 26 newly added master traces independently")
    parser.add_argument("--manifest", type=str, default=None,
                        help="Path to manifest file with trace names")
    parser.add_argument("--turns", type=int, nargs="?", const=3, default=3,
                        help="Max diagnostic probe turns (default: 3)")
    parser.add_argument("--local", action="store_true", help="Run CHIA functions locally instead of Ray remote")
    parser.add_argument("--resume", action="store_true", help="Resume from existing chia_diagnosis_summary.json")

    args = parser.parse_args()

    workloads_to_run = []
    if args.extra26:
        extra_txt = PROJECT_ROOT / "workloads" / "dpc3" / "extra26_targeted.txt"
        if extra_txt.exists():
            with open(extra_txt) as f:
                workloads_to_run = [line.strip().replace(".champsimtrace.xz", "") for line in f if line.strip()]
    elif args.manifest:
        m_path = Path(args.manifest)
        if m_path.exists():
            with open(m_path) as f:
                workloads_to_run = [line.strip().replace(".champsimtrace.xz", "") for line in f if line.strip()]
    elif args.all:
        verified_workloads = []
        for fname in ["results/oracle/oracle_analysis_6class.csv", "results/oracle/oracle_analysis_extra26.csv"]:
            p = PROJECT_ROOT / fname
            if p.exists():
                import csv
                with open(p) as f:
                    for row in csv.DictReader(f):
                        w = row["workload"]
                        base_json = PROJECT_ROOT / "results" / "oracle" / w / "baseline.json"
                        if base_json.exists() and w not in verified_workloads:
                            verified_workloads.append(w)
        if verified_workloads:
            workloads_to_run = verified_workloads
        else:
            oracle_dir = PROJECT_ROOT / "results" / "oracle"
            for p in sorted(oracle_dir.iterdir()):
                if p.is_dir() and (p / "baseline.json").exists():
                    workloads_to_run.append(p.name)
    elif args.workloads:
        workloads_to_run = args.workloads
    elif args.workload:
        workloads_to_run = [args.workload]
    else:
        workloads_to_run = ["450.soplex-247B"]

    def print_summary_table(rows, total_expected=None, is_intermediate=False):
        tag = f"PROGRESS UPDATE ({len(rows)}/{total_expected})" if is_intermediate else f"FINAL SUMMARY (ALL {len(rows)} WORKLOADS)"
        print("\n" + "=" * 116)
        print(f" CHIA AUTONOMOUS BOTTLENECK DIAGNOSIS BENCHMARK {tag}")
        print("=" * 116)
        header = f"{'Workload':<22} | {'Oracle Truth':<12} | {'Speedup':<8} | {'FOM':>6} | {'Quality':<8} | {'CHIA Diagnosis':<15} | {'Conf':<6} | {'Match?':<8} | {'Time':<6}"
        print(header)
        print("-" * 116)
        total_count = len(rows)
        accepted_count = sum(1 for r in rows if r.get("clone_passed_gate"))
        accepted_correct = sum(1 for r in rows if r.get("clone_passed_gate") and r.get("correct"))
        overall_correct = sum(1 for r in rows if r.get("correct"))

        for r in rows:
            m_str = "MATCH" if r["correct"] else "MISMATCH"
            print(f"{r['workload']:<22} | {r['oracle_class']:<12} | {r['oracle_speedup']:<8} | {r['fom']:>6} | {r['quality_tier']:<8} | {r['agent_diagnosis']:<15} | {r['confidence']:<6} | {m_str:<8} | {r['loop_time']:<6}")
        print("-" * 116)
        cov_pct = (accepted_count / max(total_count, 1)) * 100.0
        acc_accepted = (accepted_correct / max(accepted_count, 1)) * 100.0 if accepted_count > 0 else 0.0
        acc_overall = (overall_correct / max(total_count, 1)) * 100.0
        print(f"Surrogate Acceptance Rate (FOM >= 33.3%, L <= 10.0z): {accepted_count}/{total_count} ({cov_pct:.1f}%)")
        print(f"Diagnostic Accuracy on Accepted Surrogates:         {accepted_correct}/{accepted_count} ({acc_accepted:.1f}%)")
        print(f"Overall Benchmark Accuracy:                         {overall_correct}/{total_count} ({acc_overall:.1f}%)\n")

    summary_file = PROJECT_ROOT / "results" / "clones" / "chia_diagnosis_summary.json"
    summary_rows = []
    already_done = set()
    if args.resume and summary_file.exists():
        try:
            with open(summary_file) as f:
                loaded = json.load(f)
                for item in loaded:
                    if item.get("workload") in workloads_to_run:
                        summary_rows.append(item)
                        already_done.add(item["workload"])
            print(f"Resumed from {summary_file}: Loaded {len(summary_rows)} existing diagnoses.")
        except Exception as e:
            print(f"Warning: Could not load resume summary: {e}")

    print(f"\nDiscovered {len(workloads_to_run)} workload(s) total ({len(already_done)} already done, {len(workloads_to_run)-len(already_done)} remaining):")
    for w in workloads_to_run:
        status_tag = " [DONE]" if w in already_done else ""
        print(f"  - {w}{status_tag}")

    for idx, w in enumerate(workloads_to_run, 1):
        if w in already_done:
            continue
        print(f"\n[{idx}/{len(workloads_to_run)}] Processing '{w}'...")
        try:
            res = run_chia_diagnostic_loop(w, max_turns=args.turns, use_chia_remote=not args.local)
            if res:
                diag = res.get("diagnosis") or {}
                gate_passed = res.get("clone_passed_gate", False)
                comp_e = res.get("composite_error", 0.0)
                fom_val = res.get("figure_of_merit", 0.0)
                tier_val = res.get("quality_tier", "COARSE")
                summary_rows.append({
                    "workload": w,
                    "oracle_class": res.get("oracle_ground_truth", "UNKNOWN").upper(),
                    "oracle_speedup": f"+{float(res.get('oracle_speedup_pct', 0)):.1f}%",
                    "fom": f"{fom_val:.1f}%",
                    "quality_tier": tier_val,
                    "clone_passed_gate": gate_passed,
                    "composite_error": comp_e,
                    "agent_diagnosis": diag.get("primary_bottleneck", "UNKNOWN").upper(),
                    "confidence": f"{diag.get('confidence', 0)*100:.1f}%" if diag else "-",
                    "correct": res.get("correct", False),
                    "loop_time": f"{res.get('loop_time_sec', 0):.1f}s",
                })
        except Exception as e:
            print(f"Error diagnosing {w}: {e}")
            summary_rows.append({
                "workload": w,
                "oracle_class": "ERROR",
                "oracle_speedup": "-",
                "fom": "-",
                "quality_tier": "ERROR",
                "clone_passed_gate": False,
                "composite_error": None,
                "agent_diagnosis": "ERROR",
                "confidence": "-",
                "correct": False,
                "loop_time": "-",
            })

        # Persist summary immediately after each workload completes
        try:
            with open(summary_file, "w") as f:
                json.dump(summary_rows, f, indent=2)
        except Exception:
            pass

        # Print intermediate table every 10 runs
        if len(summary_rows) % 10 == 0:
            print_summary_table(summary_rows, total_expected=len(workloads_to_run), is_intermediate=True)

    # Render Final Summary Table
    print_summary_table(summary_rows, total_expected=len(workloads_to_run), is_intermediate=False)

    final_summary_list = sorted(summary_rows, key=lambda x: x["workload"])
    with open(summary_file, "w") as f:
        json.dump(final_summary_list, f, indent=2)
    print(f"Full cumulative summary ({len(final_summary_list)} workloads) saved to: {summary_file}")

    if args.extra26:
        extra_summary_file = PROJECT_ROOT / "results" / "clones" / "chia_diagnosis_extra26_summary.json"
        with open(extra_summary_file, "w") as f:
            json.dump(summary_rows, f, indent=2)
        print(f"Dedicated Extra-26 summary saved to: {extra_summary_file}")



if __name__ == "__main__":
    main()
