"""
agent/prompts.py -- Prompt templates and schemas for the CHIA Autonomous Diagnosis Agent.

Supplies the LLM with:
  1. The C code generator template semantics and 12 tunable probe knobs.
  2. The Master trace ChampSim output and the Cloned workload knobs.
  3. Structured probing protocols to diagnose hardware bottlenecks on machine M0.
"""

from typing import Literal, Optional, Dict, Any
from pydantic import BaseModel, Field, model_validator, field_validator

SYSTEM_PROMPT = """You are an expert computer architecture research agent participating in the A3/CHIA autonomous bottleneck diagnosis framework.

MISSION:
Given an unknown target master workload treated as a black box (no source code or binary access), your goal is to discover the PRIMARY microarchitectural bottleneck that limits its performance on machine M0.
The ground-truth oracle is explicitly defined as whichever hardware relaxation produces the MAXIMUM percentage IPC speedup on M0:
1. 'branch'   : Frontend branch prediction latency and misprediction flushes (relaxed to hashed perceptron).
2. 'cache'    : Last-Level Cache (LLC) capacity limits / working set spillover (relaxed from 2MB to 32MB LLC).
3. 'dram_lat' : Main memory access latency / serialized pointer-chasing dependencies (relaxed to 2x faster DRAM access).
4. 'dram_bw'  : DRAM channel throughput & bandwidth / streaming row buffer contention (relaxed to 4x DRAM channels).

(Note: Out-of-Order execution resources (ROB) and Miss Status Holding Registers (MSHR) on M0 were empirically verified to never form primary bottlenecks in our operational corpus. Diagnosis is strictly confined to the four classes above.)

AVAILABLE TOOLS & CAPABILITIES:
1. Master Execution Output: Black-box execution statistics (IPC, Branch MPKI, L1D/L2/LLC misses, DRAM traffic, LLC miss latency).
2. Cloned Workload Surrogate: A synthetic baseline probe whose knobs were tuned via MicroGrad to match the master workload's microarchitectural fingerprint.
3. Semantic Probe Generator: A fast synthetic probe generator (`probe.c`) with 12 semantic knobs. You propose targeted diagnostic perturbations to observe differential response signatures.

INTERVENTION-RESPONSE MATRIX & PHYSICAL PRINCIPLES:
| Candidate Bottleneck | Primary Diagnostic Intervention | Key Knob Overrides | Expected Supporting Evidence |
| :--- | :--- | :--- | :--- |
| **BRANCH** | Branch neutralization | `branch_pattern: "constant"` | Large ΔIPC from removing branch uncertainty (`BR_MPKI -> 0`). Low/moderate memory traffic (`LLC_MPKI < 2.0`, `DRAM_RQPI < 0.005`). |
| **CACHE** | Reduce Working Set Size (WSS) below LLC capacity | `working_set_kb: 1024` (or `reuse: 0.8`) | Large ΔIPC accompanied by significant LLC MPKI reduction and DRAM traffic collapse. |
| **DRAM_LAT** | Increase MLP / remove dependency serialization | `memory_dependency: "independent"`, `independent_chains: 4` | Large ΔIPC from parallelizing memory access while memory traffic (LLC_MPKI, DRAM_RQPI) remains comparable. |
| **DRAM_BW** | Increase memory throughput / expose bandwidth demand | `independent_chains: 8` (or `stride_lines: 1`, `randomness: 0.0`) | Large ΔIPC response to bandwidth/throughput intervention under sustained high DRAM demand (`DRAM_RQPI > 0.020`). |

INTERVENTION-RESPONSE DECISION GUIDELINES:
1. **WSS-Based Cache Probing**: To test whether a workload is limited by LLC capacity, reduce `working_set_kb: 1024` (or test temporal `reuse: 0.8`). If the workload is capacity-bound, fitting the footprint into LLC will cause a large IPC speedup and a steep reduction in DRAM traffic.
2. **MLP / Dependency Probing for Latency**: To test serialized DRAM latency, set `memory_dependency: "independent"`, `independent_chains: 4`. If the primary limiter is serialized load latency (ROB head blocking), unlocking MLP provides a large IPC uplift while memory traffic remains high.
3. **Dual-Hypothesis Memory Probing Protocol**: For workloads with substantial memory traffic (`LLC_MPKI > 3.0` or `DRAM_RQPI > 0.010`), do NOT terminate at Turn 1 after testing only cache. You MUST probe BOTH the latency intervention (`dram_lat`: `memory_dependency: "independent"`, `independent_chains: 4` at full working set) AND the cache intervention (`cache`: `working_set_kb: 1024`) across Turns 1 and 2 to observe whether unlocking MLP without shrinking the footprint resolves the stall before concluding at Turn 3.
4. **Physical Traffic Constraints**: A memory subsystem can only bottleneck execution if significant memory traffic exists on the master workload (`LLC_MPKI > 2.0` or `DRAM_RQPI > 0.004`). If memory traffic is negligible, focus diagnostic interventions on `branch`.
5. **Agent-Driven Early Termination**:
   - For pure compute/branch workloads with negligible memory traffic, if branch neutralization yields large IPC speedup, output `decision: "DIAGNOSE"` immediately.
   - For memory-intensive workloads, run dual-probing across Turns 1 and 2, and render `decision: "DIAGNOSE"` at Turn 3.
"""


class NextProbeSpec(BaseModel):
    knob_overrides: Dict[str, Any] = Field(
        description="Dictionary of specific semantic knob overrides to apply (e.g., {'branch_pattern': 'constant'} for branch, {'working_set_kb': 1024} for cache, {'memory_dependency': 'independent', 'independent_chains': 4} for dram_lat, {'stride_lines': 1, 'randomness': 0.0} for dram_bw). MUST NOT BE EMPTY."
    )
    hypothesis_tested: Literal["branch", "cache", "dram_lat", "dram_bw"] = Field(
        description="Which bottleneck hypothesis this probe is designed to test"
    )
    expected_outcome: str = Field(
        description="Expected behavior of IPC and cache/branch metrics under this hypothesis"
    )

    @model_validator(mode="after")
    def populate_default_intervention_if_empty(self):
        if not self.knob_overrides:
            defaults = {
                "branch": {"branch_pattern": "constant"},
                "cache": {"working_set_kb": 1024},
                "dram_lat": {"memory_dependency": "independent", "independent_chains": 4},
                "dram_bw": {"stride_lines": 1, "randomness": 0.0},
            }
            self.knob_overrides = defaults.get(self.hypothesis_tested, {"working_set_kb": 1024})
        return self


class FinalDiagnosis(BaseModel):
    primary_bottleneck: Literal["branch", "cache", "dram_lat", "dram_bw"]
    confidence: float = Field(ge=0.0, le=1.0)
    justification: str


class AgentStepResponse(BaseModel):
    hypotheses: Dict[str, float] = Field(
        description="Current belief distribution over the 4 physical bottlenecks ('branch', 'cache', 'dram_lat', 'dram_bw') summing to 1.0"
    )
    reasoning: str = Field(
        description="Detailed technical reasoning connecting observed probe responses to architectural hypotheses"
    )
    decision: Literal["PROBE", "DIAGNOSE"] = Field(
        description="Whether to run another diagnostic probe or render the final diagnosis"
    )
    next_probe: Optional[NextProbeSpec] = Field(
        default=None,
        description="Specification for the next probe to run (required if decision == 'PROBE')"
    )
    final_diagnosis: Optional[FinalDiagnosis] = Field(
        default=None,
        description="Final identified bottleneck (required if decision == 'DIAGNOSE')"
    )

    @field_validator("hypotheses")
    @classmethod
    def validate_hypotheses_distribution(cls, v: Dict[str, float]) -> Dict[str, float]:
        required_keys = {"branch", "cache", "dram_lat", "dram_bw"}
        if not v or not isinstance(v, dict):
            return {k: 0.25 for k in required_keys}
        # Fill missing keys with 0.0
        complete_v = {k: float(v.get(k, 0.0)) for k in required_keys}
        total = sum(complete_v.values())
        if total <= 0:
            return {k: 0.25 for k in required_keys}
        return {k: float(prob / total) for k, prob in complete_v.items()}

    @model_validator(mode="after")
    def validate_decision_consistency(self):
        if self.decision == "PROBE" and self.next_probe is None:
            raise ValueError("next_probe is required when decision is 'PROBE'")
        if self.decision == "DIAGNOSE" and self.final_diagnosis is None:
            raise ValueError("final_diagnosis is required when decision is 'DIAGNOSE'")
        return self


def build_agent_turn_prompt(
    workload: str,
    master_fingerprint: dict,
    cloned_knobs: dict,
    cloned_fingerprint: dict,
    history: list,
    turn: int,
    max_turns: int,
    clone_quality: Optional[dict] = None,
) -> str:
    """Build the prompt for the current diagnosis turn with anonymized workload ID."""

    history_str = ""
    if not history:
        history_str = "No diagnostic probes run yet. You are at Turn 1.\n"
    else:
        history_str = "HISTORY OF DIAGNOSTIC PROBES RUN SO FAR:\n"
        for i, h in enumerate(history, 1):
            probe_info = h["probe"]
            obs = h["result"]["fingerprint"]
            sec = h["result"].get("secondary", {})
            r_hit = sec.get("DRAM_row_hit_rate", None)
            r_hit_str = f" | Row_Hit: {r_hit*100:.1f}%" if r_hit is not None else ""
            delta_ipc = (obs["IPC"] - cloned_fingerprint["IPC"]) / max(cloned_fingerprint["IPC"], 0.001) * 100.0
            history_str += f"""--- Probe Experiment #{i} ---
Targeted Hypothesis: {probe_info.get('hypothesis_tested')}
Knob Overrides Applied: {probe_info.get('knob_overrides')}
Rationale: {probe_info.get('expected_outcome')}
Observed Result on M0:
  IPC: {obs['IPC']:.3f} (Delta vs Baseline: {delta_ipc:+.1f}%)
  BR_MPKI: {obs['BR_MPKI']:.2f} | L1D_MPKI: {obs['L1D_MPKI']:.2f} | L2_MPKI: {obs['L2_MPKI']:.2f} | LLC_MPKI: {obs['LLC_MPKI']:.2f} | DRAM_RQPI: {obs['DRAM_RQPI']:.5f}{r_hit_str}
"""

    quality_str = ""
    if clone_quality:
        quality_str = f"\nClone Surrogate Quality (Tolerance Z-Scores):\n{clone_quality}\n"

    prompt = f"""=== TARGET WORKLOAD PROFILING & BOTTLENECK INVESTIGATION (Turn {turn}/{max_turns}) ===

1. MASTER WORKLOAD EXECUTION OUTPUT ON M0:
{master_fingerprint}

2. CLONED WORKLOAD BASELINE ON M0:
Baseline Cloned Knobs:
{cloned_knobs}

Baseline Cloned Fingerprint:
{cloned_fingerprint}
{quality_str}

3. PROBE CODE GENERATOR AVAILABLE KNOBS:
- branch_frequency: int in [0..8] (number of conditional branches per iteration)
- branch_pattern: 'constant' | 'alternating' | 'periodic' | 'random'
- branch_pattern_param: int (period for periodic, taken % for random)
- load_branch_dependency: bool (if True, branch outcome depends on load data)
- working_set_kb: int, power of 2 in [4..65536] (spans L1D, L2, LLC, or DRAM)
- stride_lines: int, power of 2 in [1..1024]
- randomness: float in [0.0..1.0] (fraction of accesses using hash permutation)
- reuse: float in [0.0..0.9] (fraction of accesses redirected to 4KB hot region)
- memory_dependency: 'independent' | 'dependent' (dependent = pointer chasing)
- independent_chains: 1 | 2 | 4 | 8 (number of parallel memory streams)
- alu_ops: int in [0..64]
- dependency_distance: int in [1..8]

4. INVESTIGATION PROGRESS:
{history_str}

TASK:
Analyze the baseline and probe history. Update your hypothesis probabilities across:
  ['branch', 'cache', 'dram_lat', 'dram_bw'].
If Turn >= {max_turns}, you MUST choose decision="DIAGNOSE". Otherwise, choose whether to run a diagnostic probe or deliver your final diagnosis.
Return strictly valid JSON conforming to the AgentStepResponse schema.
"""
    return prompt
