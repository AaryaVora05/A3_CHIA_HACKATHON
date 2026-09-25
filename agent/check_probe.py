#!/usr/bin/env python3
"""Fail-fast local validation of the V1.1 probe stack (no Pin / ChampSim needed).

For each case (a SemanticProbe), in order, stopping at the first failure:
  SEMANTIC  semantic -> backend -> canonical semantic projection round trip
  BUILD     gcc -O2 -static -fno-pie -no-pie
  STATIC    tools/check_static.py (exact counts, no spills, cmov-only selection,
            ALU distance, chain structure, lbd operand)
  NATIVE    self-check: the loop executed exactly the replica semantics
  CYCLE     dependent mode: full-cycle coverage at the real working-set size
  TRACE     tools/check_trace.py under valgrind on a same-shape twin with
            working_set <= 1024 KB (counts, executed == replica, hot fraction and
            region, run-start fraction, uniformity, dependent coverage)
  FILTER    reuse > 0: non-hot stream == reuse-0 stream (T6)
With --full, also runs the static sweeps (admissible shapes, cross-config invariance).

usage: python check_probe.py [--full] [--case JSON]
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get("A3_ROOT", HERE.parent))
TEMPLATE = Path(os.environ.get("PROBE_TEMPLATE", ROOT / "probes" / "template" / "probe.c"))
TOOLS = Path(os.environ.get("A3_TOOLS", ROOT / "tools"))
sys.path.insert(0, str(HERE))
os.environ.setdefault("PROBE_TEMPLATE", str(TEMPLATE))
os.environ.setdefault("CHECK_STATIC", str(TOOLS / "check_static.py"))

from semantic import SemanticProbe, from_backend   # noqa: E402
import run_probe as rp                              # noqa: E402

CASES = [
    SemanticProbe(),
    SemanticProbe(branch_frequency=1, branch_pattern="alternating"),
    SemanticProbe(branch_frequency=2, branch_pattern="periodic", branch_pattern_param=8),
    SemanticProbe(branch_frequency=4, branch_pattern="random", branch_pattern_param=50, load_branch_dependency=True),
    SemanticProbe(randomness=0.5, memory_dependency="independent", independent_chains=4, working_set_kb=16384),
    SemanticProbe(randomness=0.5, memory_dependency="dependent", independent_chains=2, working_set_kb=65536),
    SemanticProbe(randomness=1.0, memory_dependency="dependent", independent_chains=8, working_set_kb=4096),
    SemanticProbe(reuse=0.5, randomness=0.25, working_set_kb=1024, stride_lines=4),
    SemanticProbe(reuse=0.9, memory_dependency="dependent", randomness=0.1, independent_chains=4),
    SemanticProbe(alu_ops=8, dependency_distance=4),
    SemanticProbe(alu_ops=64, dependency_distance=8, branch_frequency=8, branch_pattern="random",
                  branch_pattern_param=30, load_branch_dependency=True, memory_dependency="dependent",
                  independent_chains=8, randomness=0.3, reuse=0.2, working_set_kb=2048),
]


class Fail(Exception):
    pass


def step(tag, ok, detail=""):
    print(f"  [{tag}] {'PASS' if ok else 'FAIL'} {detail}".rstrip())
    if not ok:
        raise Fail(tag)


def build(backend, out):
    r = subprocess.run(rp.build_cmd(backend, out), capture_output=True, text=True)
    return r.returncode == 0, r.stderr[-400:]


def run_tool(args):
    r = subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True)
    return r.returncode == 0, r.stdout


def check_case(i, sem, tmp):
    print(f"case {i}: {json.dumps(sem.model_dump())}")
    backend = rp.validate(sem.to_backend())
    step("SEMANTIC", from_backend(sem.to_backend()).model_dump() == sem.model_dump(), "round trip")
    b = tmp / f"p{i}"
    ok, err = build(backend, b)
    step("BUILD", ok, err if not ok else "")
    ok, out = run_tool([TOOLS / "check_static.py", b])
    step("STATIC", ok, out.strip().splitlines()[0] if out.strip() else "")
    sc = subprocess.run([str(b), "--iters", "20000"], capture_output=True, text=True).stdout
    step("NATIVE", "result.selfcheck=PASS" in sc, "self-check vs replica")
    if backend["DEPENDENT"]:
        cc = subprocess.run([str(b), "--check-cycle"], capture_output=True, text=True).stdout
        line = [l for l in cc.splitlines() if l.startswith("cycle.")]
        step("CYCLE", "cycle.full_coverage=PASS" in cc, " ".join(line[1:5]))
    twin = dict(backend, WSS_KB=min(backend["WSS_KB"], 1024))
    while twin["WSS_KB"] * 16 // twin["STRIDE_LINES"] < 2 * twin["NCHAINS"] * 64 and twin["STRIDE_LINES"] > 1:
        twin["STRIDE_LINES"] //= 2
    tb = tmp / f"t{i}"
    ok, err = build(twin, tb)
    step("BUILD twin", ok, err if not ok else f"WSS_KB={twin['WSS_KB']} STRIDE_LINES={twin['STRIDE_LINES']}")
    args = [TOOLS / "check_trace.py", tb, "--iters", "3000"]
    if twin["DEPENDENT"] and twin["REUSE"] == 0:
        args.append("--coverage")
    ok, out = run_tool(args)
    for l in out.splitlines():
        if l.strip().startswith("TRACE T"):
            print("    " + l.strip())
    step("TRACE", ok)
    if twin["REUSE"] > 0:
        z = tmp / f"z{i}"
        ok, err = build(dict(twin, REUSE=0.0), z)
        step("BUILD reuse-0 twin", ok, err if not ok else "")
        ok, out = run_tool([TOOLS / "check_trace.py", "--filter", z, tb, "--iters", "3000"])
        step("FILTER", ok, [l for l in out.splitlines() if "T6" in l][0].strip() if "T6" in out else out[-300:])


def main():
    cases = CASES
    if "--case" in sys.argv:
        cases = [SemanticProbe(**json.loads(sys.argv[sys.argv.index("--case") + 1]))]
    with tempfile.TemporaryDirectory(prefix="a3_check_") as td:
        tmp = Path(td)
        try:
            for i, sem in enumerate(cases):
                check_case(i, sem, tmp)
            if "--full" in sys.argv:
                for mode in (["admissible"], ["invariance"]):
                    ok, out = run_tool([TOOLS / "sweep_static.py", *mode])
                    print(out.rstrip())
                    step("STATIC sweep " + mode[0], ok)
        except Fail as e:
            print(json.dumps({"status": "FAIL", "failed_step": str(e)}))
            return 1
    print(json.dumps({"status": "PASS", "cases": len(cases), "template": str(TEMPLATE),
                      "verified": ["SEMANTIC", "STATIC", "NATIVE", "CYCLE", "TRACE", "FILTER"],
                      "not_verified_here": ["CHAMPSIM: Pin -s skip, XMM register IDs through the tracer, "
                                            "lbd branch_taken identity, warmup adequacy"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
