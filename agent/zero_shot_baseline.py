#!/usr/bin/env python3
"""
Direct Zero-Shot LLM Reasoning Baseline Agent.

This baseline directly feeds the target application's ChampSim architectural
fingerprint and secondary telemetry into an LLM (Gemini 2.5 Pro) in a single
zero-shot prompt without physical cloning, proxy kernels, or intervention probing.
Used as an ablation baseline to demonstrate the value of CHIA's active probing loop.
"""

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Literal, Dict, Any, List

from google import genai
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "agent"))

from observation import extract_observation


class ZeroShotDiagnosisResponse(BaseModel):
    primary_bottleneck: Literal["branch", "cache", "dram_lat", "dram_bw"] = Field(
        description="The diagnosed single primary hardware bottleneck class"
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Confidence score between 0.0 and 1.0"
    )
    reasoning: str = Field(
        description="Detailed microarchitectural reasoning explaining why this bottleneck was selected"
    )


ZERO_SHOT_SYSTEM_PROMPT = """You are an expert microarchitecture performance analyst.
Your task is to analyze the hardware performance counters of a workload simulated on a modern Out-of-Order x86-64 processor and diagnose its single PRIMARY performance bottleneck.

TARGET MICROARCHITECTURE (M0 Model):
- Core: 4-wide decode/issue/retire, 256-entry ROB, 72-entry Load Queue, 56-entry Store Queue
- L1 Data Cache: 32 KB, 8-way, 4 cycles hit latency
- L2 Data Cache: 512 KB, 8-way, 14 cycles hit latency
- LLC (L3 Cache): 2 MB, 16-way, 40 cycles hit latency
- DRAM: DDR4-3200, 1 channel, 1 rank, 8 banks per rank, tRP=14ns, tRCD=14ns, tCAS=14ns (LLC miss roundtrip ~150-250 cycles)

BOTTLENECK TAXONOMY:
1. `branch`: Frontend/pipeline flushes from branch mispredictions.
2. `cache`: Last-level cache capacity limitation (working set exceeds LLC, causing massive capacity misses to DRAM).
3. `dram_lat`: Serialized memory latency (dependent load chains, pointer chasing, low MLP blocking ROB retirement head).
4. `dram_bw`: Memory bandwidth saturation (high sustained DRAM request throughput, row buffer thrashing, memory controller backpressure).

You must analyze the provided telemetry fingerprint and output a JSON response conforming strictly to the requested schema.
"""


def load_ground_truth(workload: str) -> dict:
    """Load oracle ground-truth bottleneck classification."""
    for fname in ["oracle_analysis_6class.csv", "oracle_analysis_extra26.csv"]:
        oracle_csv = PROJECT_ROOT / "results" / "oracle" / fname
        if oracle_csv.exists():
            with open(oracle_csv, mode="r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row["workload"] == workload:
                        return row
    return {}


def run_zero_shot_diagnosis(workload: str, client: genai.Client) -> Dict[str, Any]:
    """Runs a direct zero-shot LLM bottleneck diagnosis for a workload."""
    master_json = PROJECT_ROOT / "results" / "oracle" / workload / "baseline.json"
    if not master_json.exists():
        return {"status": "ERROR", "error": f"Master baseline not found at {master_json}"}

    master_obs = extract_observation(master_json)
    master_fp = master_obs["fingerprint"]
    master_sec = master_obs["secondary"]
    master_r_hit = master_sec.get("DRAM_row_hit_rate", 0.0)
    master_llc_lat = master_sec.get("LLC_miss_latency", 0.0)
    master_dbus = master_sec.get("DRAM_dbus_congestion", 0.0)

    user_prompt = f"""TARGET WORKLOAD: {workload}

ARCHITECTURAL FINGERPRINT:
- Instructions Per Cycle (IPC): {master_fp['IPC']:.3f}
- Branch Mispredictions Per Kilo-Instruction (BR_MPKI): {master_fp['BR_MPKI']:.2f}
- L1D MPKI: {master_fp['L1D_MPKI']:.2f}
- L2 MPKI: {master_fp['L2_MPKI']:.2f}
- LLC (L3) MPKI: {master_fp['LLC_MPKI']:.2f}
- DRAM Requests Per Instruction (DRAM_RQPI): {master_fp['DRAM_RQPI']:.5f}

SECONDARY TELEMETRY:
- DRAM Row Buffer Hit Rate: {master_r_hit * 100:.1f}%
- Average LLC Miss Latency: {master_llc_lat:.1f} cycles
- DRAM DBUS Congestion Delay: {master_dbus:.2f} cycles

TASK:
Diagnose the single PRIMARY performance bottleneck class (`branch`, `cache`, `dram_lat`, or `dram_bw`).
Output valid JSON adhering to the schema.
"""

    t0 = time.time()
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-pro",
                contents=f"{ZERO_SHOT_SYSTEM_PROMPT}\n\n{user_prompt}",
                config={
                    "response_mime_type": "application/json",
                    "response_schema": ZeroShotDiagnosisResponse,
                    "temperature": 0.0,
                },
            )
            data = json.loads(response.text)
            elapsed = time.time() - t0
            
            oracle = load_ground_truth(workload)
            gt_class = oracle.get("best_class", "UNKNOWN").upper()
            gt_pct = oracle.get("best_pct", "-")
            predicted_class = data["primary_bottleneck"].upper()
            correct = (predicted_class == gt_class)

            return {
                "status": "OK",
                "workload": workload,
                "oracle_class": gt_class,
                "oracle_speedup": gt_pct,
                "predicted_class": predicted_class,
                "confidence": data["confidence"],
                "reasoning": data["reasoning"],
                "correct": correct,
                "latency_sec": elapsed,
            }
        except Exception as e:
            if attempt < 4:
                time.sleep(5 * (attempt + 1))
            else:
                return {"status": "ERROR", "error": str(e), "workload": workload}

    return {"status": "ERROR", "error": "Max retries exceeded", "workload": workload}


def main():
    parser = argparse.ArgumentParser(description="Direct Zero-Shot LLM Reasoning Baseline")
    parser.add_argument("--workloads", nargs="+", help="Specific workloads to evaluate")
    parser.add_argument("--all", action="store_true", help="Run across all verified master workloads")
    parser.add_argument("--output", type=str, default="results/zero_shot_baseline_summary.json",
                        help="Path to output summary JSON")
    args = parser.parse_args()

    project_id = os.environ.get("VERTEX_PROJECT_ID", "chia-508019")
    client = genai.Client(vertexai=True, project=project_id, location="us-central1")

    if args.workloads:
        workloads = args.workloads
    else:
        # Discover verified workloads
        workloads = []
        for fname in ["results/oracle/oracle_analysis_6class.csv", "results/oracle/oracle_analysis_extra26.csv"]:
            p = PROJECT_ROOT / fname
            if p.exists():
                with open(p) as f:
                    for row in csv.DictReader(f):
                        w = row["workload"]
                        base_json = PROJECT_ROOT / "results" / "oracle" / w / "baseline.json"
                        if base_json.exists() and w not in workloads:
                            workloads.append(w)

    out_path = PROJECT_ROOT / args.output
    existing_map = {}
    if out_path.exists():
        try:
            with open(out_path) as f:
                prev_data = json.load(f)
                for item in prev_data:
                    if item.get("status") == "OK" and item.get("workload") in workloads:
                        existing_map[item["workload"]] = item
        except Exception:
            pass

    to_run = [w for w in workloads if w not in existing_map]

    print(f"\n==================================================================================")
    print(f" DIRECT ZERO-SHOT LLM REASONING BASELINE (Total: {len(workloads)}, To Run: {len(to_run)})")
    print(f"==================================================================================\n")

    header = f"{'Workload':<22} | {'Oracle Truth':<12} | {'Speedup':<8} | {'Zero-Shot Pred':<15} | {'Conf':<6} | {'Match?':<8} | {'Time':<6}"
    print(header)
    print("-" * 92)

    from concurrent.futures import ThreadPoolExecutor, as_completed

    if to_run:
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_w = {executor.submit(run_zero_shot_diagnosis, w, client): w for w in to_run}
            for future in as_completed(future_to_w):
                res = future.result()
                if res.get("status") == "OK":
                    existing_map[res["workload"]] = res
                    m_str = "MATCH" if res["correct"] else "MISMATCH"
                    print(f"{res['workload']:<22} | {res['oracle_class']:<12} | {res['oracle_speedup']:<8} | {res['predicted_class']:<15} | {res['confidence']*100:4.1f}% | {m_str:<8} | {res['latency_sec']:5.1f}s")
                else:
                    w = future_to_w[future]
                    print(f"{w:<22} | ERROR: {res.get('error')}")

    results = sorted(list(existing_map.values()), key=lambda x: x["workload"])
    print("-" * 92)
    total = len(results)
    total_correct = sum(1 for r in results if r["correct"])
    acc = (total_correct / max(total, 1)) * 100.0
    print(f"\nZero-Shot LLM Reasoning Final Accuracy: {total_correct}/{total} ({acc:.1f}%)")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"Saved zero-shot baseline summary to {out_path}\n")


if __name__ == "__main__":
    main()
