#!/usr/bin/env python3
"""STATIC sweeps for probe V1.1.

  sweep_static.py admissible [--gpr]   every code shape passes check_static
  sweep_static.py invariance           the loop's shape signature is identical
                                       across all data knobs, branch pattern, lbd
                                       and memory_dependency for fixed (C, D, A, B)
Builds go to a temp dir; 2 parallel compiles.
"""
import concurrent.futures as cf
import itertools
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.environ.get("PROBE_TEMPLATE", os.path.join(HERE, "..", "probes", "template", "probe.c"))
CHECK = os.path.join(HERE, "check_static.py")
CFLAGS = ["-O2", "-static", "-fno-pie", "-no-pie"]


def build(defs, out):
    cmd = ["gcc", *CFLAGS, *[f"-D{k}={v}" for k, v in defs.items()], SRC, "-o", out, "-lm"]
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def static(out, sig=False):
    r = subprocess.run([sys.executable, CHECK, out] + (["--signature-only"] if sig else []),
                       capture_output=True, text=True)
    return r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "ERROR " + r.stderr[-200:]


def job(args):
    defs, sig, tmp, n = args
    out = os.path.join(tmp, f"b{n}")
    if not build(defs, out):
        return defs, "BUILD-FAIL"
    return defs, static(out, sig)


def admissible(gpr):
    rows = []
    for dep, C, D, B in itertools.product((0, 1), (1, 2, 4, 8), range(1, 9), (0, 1, 8, 64)):
        rows.append(dict(DEPENDENT=dep, NCHAINS=C, DEP_DIST=D, ALU_OPS=2 * D, NBRANCH=B,
                         LOAD_BR_DEP=B % 2, BR_PATTERN=3, BR_TAKEN=0.5, RANDOMNESS=0.5, REUSE=0.5,
                         ALU_XMM=0 if gpr else 1))
    res = run(rows, sig=False)
    ok = {}
    for d, r in res:
        key = (d["DEPENDENT"], d["NCHAINS"], d["DEP_DIST"])
        ok[key] = ok.get(key, True) and r.endswith("PASS")
    fails = [(d, r) for d, r in res if not r.endswith("PASS")]
    print(f"admissible ({'GPR' if gpr else 'XMM'} ALU): {len(res) - len(fails)}/{len(res)} builds PASS")
    for dep in (0, 1):
        for C in (1, 2, 4, 8):
            good = [D for D in range(1, 9) if ok[(dep, C, D)]]
            print(f"  {'dependent  ' if dep else 'independent'} C={C}: passing D = {good}")
    return fails


def invariance():
    shapes = [(1, 1, 0, 0), (2, 4, 8, 3), (8, 8, 16, 8), (4, 2, 2, 1)]
    data = [dict(WSS_KB=w, STRIDE_LINES=s, RANDOMNESS=r, REUSE=u, SEED=sd)
            for w, s, r, u, sd in [(4, 1, 0, 0, 1), (64, 4, 0.5, 0.3, 2), (4096, 1, 1, 0.9, 3),
                                   (65536, 16, 0.01, 0.0, 4), (1024, 2, 0.25, 0.5, 5)]]
    branch = [dict(BR_PATTERN=0), dict(BR_PATTERN=1), dict(BR_PATTERN=2, BR_PERIOD=5),
              dict(BR_PATTERN=3, BR_TAKEN=0.3)]
    bad = 0
    for C, D, A, B in shapes:
        rows = []
        for dep, dk, bk, lbd in itertools.product((0, 1), data, branch, (0, 1)):
            rows.append(dict(NCHAINS=C, DEP_DIST=D, ALU_OPS=A, NBRANCH=B, DEPENDENT=dep, LOAD_BR_DEP=lbd, **dk, **bk))
        res = run(rows, sig=True)
        mn = {r.split()[0] for _, r in res}
        full = {lbd: {r.split()[1] for d, r in res if d["LOAD_BR_DEP"] == lbd} for lbd in (0, 1)}
        ok = len(mn) == 1 and all(len(v) == 1 for v in full.values())
        print(f"shape C={C} D={D} A={A} B={B}: {len(res)} variants (2 modes x 5 data x 4 patterns x 2 lbd): "
              f"mnemonic signatures={len(mn)}, operand-kind signatures lbd0={len(full[0])} lbd1={len(full[1])} "
              f"-> {'PASS' if ok else 'FAIL'}")
        bad += not ok
    print("INVARIANCE", "PASS" if not bad else "FAIL")
    return bad


def run(rows, sig):
    with tempfile.TemporaryDirectory() as tmp, cf.ThreadPoolExecutor(max_workers=os.cpu_count() or 2) as ex:
        return list(ex.map(job, [(d, sig, tmp, n) for n, d in enumerate(rows)]))


if __name__ == "__main__":
    if sys.argv[1] == "admissible":
        f = admissible("--gpr" in sys.argv)
        sys.exit(1 if (f and "--gpr" not in sys.argv) else 0)
    else:
        sys.exit(invariance())
