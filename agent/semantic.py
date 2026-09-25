"""
semantic.py -- the LLM-facing layer of the A3/CHIA probe language (V1, 12 knobs), V1.1 backend.

    LLM --(SemanticProbe JSON)--> to_backend() --> probe.c V1.1 -D macros --> GCC/Pin/ChampSim

The LLM only sees SemanticProbe. The numeric tuner works on the backend dict.
The 12 knobs and their meanings are frozen (V1); V1.1 changes the backend only.

V1 scope limits (state in the paper): no code-footprint / BTB / static-branch-count /
indirect-branch / store-fraction knobs, no stream mixtures, no reuse-distance control.

V1.1 backend facts the semantics now depend on:
  * randomness r: each main-stream step starts a new sequential run at a uniformly
    random slot with probability r, otherwise advances by stride (mean run length 1/r).
    Dependent mode realises this as ONE cycle over all slots (full coverage);
    independent mode draws run starts with replacement (probabilistic coverage).
  * reuse u <= 0.9: fraction of accesses redirected to a separate 4 KB hot region;
    the main stream does not advance on them. Decisions are made per ROW
    (8/independent_chains accesses share one decision) by a deterministic
    low-discrepancy (Weyl) sequence, so the hot fraction is exact, not i.i.d.
  * every probe carries a fixed address-generation cost of 16 + 8/C instructions
    per memory access; it is identical for all knob values and must be recorded
    with every characterization result.
"""
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def _pow2(x: int) -> bool:
    return x > 0 and (x & (x - 1)) == 0


class SemanticProbe(BaseModel):
    # ---------------- 1. branch behavior ----------------
    branch_frequency: int = Field(
        0, ge=0, le=8, description="Conditional branches per loop iteration (0 = none).")
    branch_pattern: Literal["constant", "alternating", "periodic", "random"] = Field(
        "constant",
        description="constant: never taken. alternating: taken/not-taken alternating. "
                    "periodic: taken once every branch_pattern_param iterations. "
                    "random: each outcome taken with probability branch_pattern_param %. "
                    "Patterns are defined per static branch.")
    branch_pattern_param: int = Field(
        50, ge=0, le=100,
        description="periodic: period, integer in [3, 64]. random: taken probability in percent, "
                    "[1, 99]. Ignored for constant / alternating.")
    load_branch_dependency: bool = Field(
        False, description="If true, each branch's compare operand depends on the preceding memory load, "
                           "so the branch cannot resolve before the load returns. Branch outcomes are "
                           "identical to the false setting; only resolution timing changes.")

    # ---------------- 2. memory / locality ----------------
    working_set_kb: int = Field(
        1024, ge=4, le=65536,
        description="Address span of the main stream in KB, a power of two [4, 65536]. The 4 KB reuse "
                    "hot region is separate and not included.")
    stride_lines: int = Field(
        1, ge=1, le=1024,
        description="Step between consecutive sequential accesses, in 64-byte lines (power of two). "
                    "Distinct main-stream slots = span / stride.")
    randomness: float = Field(
        0.0, ge=0.0, le=1.0,
        description="Probability that a main-stream access starts a new sequential run at a random "
                    "location (otherwise it advances by stride). Mean run length 1/randomness.")
    reuse: float = Field(
        0.0, ge=0.0, le=0.9,
        description="Fraction of accesses redirected to a separate dense 4 KB hot region. The main "
                    "stream does not advance on those accesses. Decided per row of 8/independent_chains "
                    "accesses, by a regular (low-discrepancy) sequence.")

    # ---------------- 3. memory dependency / MLP ----------------
    memory_dependency: Literal["independent", "dependent"] = Field(
        "independent",
        description="dependent = pointer chasing: each address is the value loaded by the previous "
                    "access of the same chain.")
    independent_chains: Literal[1, 2, 4, 8] = Field(
        1, description="Number of independently progressing access streams over the same span "
                       "(8 loads per iteration, 8/chains per stream). With dependent memory this bounds "
                       "memory-level parallelism.")

    # ---------------- 4. compute / ILP ----------------
    alu_ops: int = Field(
        0, ge=0, le=64,
        description="ALU operations per iteration, independent of memory results. Must be a multiple of "
                    "dependency_distance.")
    dependency_distance: int = Field(
        1, ge=1, le=8,
        description="ALU op i depends on op i-D (carried across iterations): D interleaved serial "
                    "chains. 1 = one serial chain. Ignored when alu_ops = 0.")

    @model_validator(mode="after")
    def _check_and_normalize(self):
        if not _pow2(self.working_set_kb):
            raise ValueError("working_set_kb must be a power of two")
        if not _pow2(self.stride_lines):
            raise ValueError("stride_lines must be a power of two")
        if (self.working_set_kb * 16) // self.stride_lines < 2 * self.independent_chains:
            raise ValueError("stride_lines too large: need at least 2*independent_chains slots")
        if self.alu_ops > 0 and self.alu_ops % self.dependency_distance:
            raise ValueError("alu_ops must be a multiple of dependency_distance")
        if self.branch_frequency > 0 and self.branch_pattern == "periodic":
            if not 3 <= self.branch_pattern_param <= 64:
                raise ValueError("periodic branch_pattern_param (period) must be in [3, 64]")
        if self.branch_frequency > 0 and self.branch_pattern == "random":
            if not 1 <= self.branch_pattern_param <= 99:
                raise ValueError("random branch_pattern_param (taken %) must be in [1, 99]")
        # canonicalize semantically inactive fields
        if self.branch_frequency == 0:
            self.branch_pattern, self.branch_pattern_param, self.load_branch_dependency = "constant", 50, False
        elif self.branch_pattern in ("constant", "alternating"):
            self.branch_pattern_param = 50
        if self.alu_ops == 0:
            self.dependency_distance = 1
        return self

    def to_backend(self) -> dict:
        """Deterministically compile the semantic knobs to probe.c V1.1 macros."""
        pat = {"constant": 0, "alternating": 1, "periodic": 2, "random": 3}[self.branch_pattern]
        return {
            "NBRANCH": self.branch_frequency,
            "BR_PATTERN": pat,
            "BR_PERIOD": self.branch_pattern_param if pat == 2 else 4,
            "BR_TAKEN": self.branch_pattern_param / 100 if pat == 3 else 0.5,
            "LOAD_BR_DEP": int(self.load_branch_dependency and self.branch_frequency > 0),
            "WSS_KB": self.working_set_kb,
            "STRIDE_LINES": self.stride_lines,
            "RANDOMNESS": float(self.randomness),
            "REUSE": float(self.reuse),
            "DEPENDENT": int(self.memory_dependency == "dependent"),
            "NCHAINS": self.independent_chains,
            "ALU_OPS": self.alu_ops,
            "DEP_DIST": self.dependency_distance,
        }


def from_backend(b: dict) -> SemanticProbe:
    """Canonical semantic projection of a backend vector (not a mathematically exact inverse:
    backend-only settings such as ALU_XMM, SEED, BR_PERIOD > 64 or NBRANCH > 8 are not representable)."""
    pat = ["constant", "alternating", "periodic", "random"][b["BR_PATTERN"]]
    param = {2: b.get("BR_PERIOD", 4), 3: round(100 * b.get("BR_TAKEN", 0.5))}.get(b["BR_PATTERN"], 50)
    return SemanticProbe(
        branch_frequency=b["NBRANCH"], branch_pattern=pat, branch_pattern_param=param,
        load_branch_dependency=bool(b["LOAD_BR_DEP"]),
        working_set_kb=b["WSS_KB"], stride_lines=b["STRIDE_LINES"],
        randomness=b["RANDOMNESS"], reuse=b["REUSE"],
        memory_dependency="dependent" if b["DEPENDENT"] else "independent",
        independent_chains=b["NCHAINS"], alu_ops=b["ALU_OPS"], dependency_distance=b["DEP_DIST"],
    )


if __name__ == "__main__":
    import json
    print(json.dumps(SemanticProbe.model_json_schema(), indent=2))
