#!/usr/bin/env python3
"""
run_probe.py -- execute ONE probe (V1.1 backend) on M0 and return parsed stats.

    knobs (JSON) -> GCC -> STATIC preflight -> Pin 3.22 tracer (-s skips init) -> ChampSim -> stats JSON

Usage (backend knobs = probe.c V1.1 macros):
    python run_probe.py '{"DEPENDENT":1,"RANDOMNESS":1.0,"WSS_KB":65536,"NCHAINS":4}'
    python run_probe.py '{"DEPENDENT":1,"WSS_KB":65536}' --dry-run
Usage (semantic knobs, see semantic.py):
    python run_probe.py '{"memory_dependency":"dependent","randomness":1.0,"working_set_kb":65536}' --semantic

Required environment:
    PIN_BIN      Pin 3.22 `pin` launcher
    TRACER_SO    ChampSim champsim_tracer.so
Optional:
    CHAMPSIM_BIN (default ~/ChampSim/bin/champsim), PROBE_TEMPLATE, A3_ROOT, CHECK_STATIC,
    SIM_INSTR (10M), WARMUP_PASSES (1.0), WARMUP_MIN (2M), TRACE_RECORD_BYTES (64), TMPDIR

Changes vs V1 pipeline:
  * backend macros follow probe.c V1.1 (floats for RANDOMNESS / REUSE / BR_TAKEN)
  * -O2 -static -fno-pie -no-pie; every binary must pass tools/check_static.py and a
    native self-check before it is traced (fail fast)
  * initialisation is SKIPPED with the tracer's -s option; its length is measured by
    tracing `probe --dry-run` into a FIFO and counting records (no multi-GB file)
  * the probe is run with a finite --iters covering skip+trace, so it terminates even if
    the tracer does not stop the application
  * ChampSim warmup = max(WARMUP_MIN, WARMUP_PASSES x instructions per span pass) --
    a POLICY whose adequacy is NOT yet verified; it is recorded with every result
  * generator overhead (static instr/iter, instr/access, load fraction) is recorded

VERIFY ON THE VM before trusting results (tools/check_champsim_trace.py automates 1-3):
  1. -s semantics: the first traced IPs are inside run_probe's loop
  2. XMM ALU: pxor register IDs survive the tracer and keep the op i -> op i-D chains
  3. lbd: branch_taken sequences identical for LOAD_BR_DEP=0/1
  4. ChampSim CLI flags and parse_stats() regexes match your ChampSim build
"""
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(os.environ.get("A3_ROOT", Path.home() / "a3-hackathon"))
TEMPLATE = Path(os.environ.get("PROBE_TEMPLATE", ROOT / "probes" / "template" / "probe.c"))
CHECK_STATIC = Path(os.environ.get("CHECK_STATIC", ROOT / "tools" / "check_static.py"))
CACHE = ROOT / "cache"
PIN = os.environ.get("PIN_BIN")
TRACER = os.environ.get("TRACER_SO")
CHAMPSIM = os.environ.get("CHAMPSIM_BIN", str(Path.home() / "ChampSim" / "bin" / "champsim"))
RECORD_BYTES = int(os.environ.get("TRACE_RECORD_BYTES", "64"))
SIM_INSTR = int(os.environ.get("SIM_INSTR", 10_000_000))
WARMUP_PASSES = float(os.environ.get("WARMUP_PASSES", 1.0))
WARMUP_MIN = int(os.environ.get("WARMUP_MIN", 2_000_000))
TIMEOUT_S = int(os.environ.get("PROBE_TIMEOUT", 3600))
CC = os.environ.get("CC_BIN", "gcc")
CFLAGS = ["-O2", "-static", "-fno-pie", "-no-pie"]

# name: (default, lo, hi, kind)   kind: cont / int / log2 = tunable, cat = fixed by the LLM,
#                                 aux = backend-only, seed = never tuned
KNOB_SPEC = {
    "NBRANCH":      (0,    0,    64,    "int"),    # semantic max 8; up to 64 for backend stress tests
    "BR_PATTERN":   (0,    0,    3,     "cat"),    # 0 constant, 1 alternating, 2 periodic, 3 random
    "BR_PERIOD":    (4,    3,    4096,  "int"),
    "BR_TAKEN":     (0.5,  0.0,  1.0,   "cont"),   # open interval (0,1) when random
    "LOAD_BR_DEP":  (0,    0,    1,     "cat"),
    "WSS_KB":       (1024, 4,    65536, "log2"),
    "STRIDE_LINES": (1,    1,    1024,  "log2"),
    "RANDOMNESS":   (0.0,  0.0,  1.0,   "cont"),
    "REUSE":        (0.0,  0.0,  0.9,   "cont"),
    "DEPENDENT":    (0,    0,    1,     "cat"),
    "NCHAINS":      (1,    1,    8,     "log2"),
    "ALU_OPS":      (0,    0,    128,   "int"),
    "DEP_DIST":     (1,    1,    8,     "int"),
    "ALU_XMM":      (1,    0,    1,     "aux"),    # 0 = GPR fallback if the XMM tracer check fails
    "SEED":         (1,    1,    10**6, "seed"),
}
FLOATS = {"BR_TAKEN", "RANDOMNESS", "REUSE"}
TUNABLE = [k for k, (_, _, _, kind) in KNOB_SPEC.items() if kind in ("cont", "int", "log2")]


def _pow2(x):
    return x > 0 and (x & (x - 1)) == 0


def canonicalize(k: dict) -> dict:
    k = dict(k)
    if k["NBRANCH"] == 0:
        k.update(BR_PATTERN=0, BR_PERIOD=4, BR_TAKEN=0.5, LOAD_BR_DEP=0)
    elif k["BR_PATTERN"] in (0, 1):
        k.update(BR_PERIOD=4, BR_TAKEN=0.5)
    elif k["BR_PATTERN"] == 2:
        k["BR_TAKEN"] = 0.5
    else:
        k["BR_PERIOD"] = 4
    if k["ALU_OPS"] == 0:
        k["DEP_DIST"] = 1
    return k


def active_knobs(k: dict) -> list:
    k = canonicalize(k)
    out = []
    for n in TUNABLE:
        if n == "BR_PERIOD" and (k["NBRANCH"] == 0 or k["BR_PATTERN"] != 2):
            continue
        if n == "BR_TAKEN" and (k["NBRANCH"] == 0 or k["BR_PATTERN"] != 3):
            continue
        if n == "DEP_DIST" and k["ALU_OPS"] == 0:
            continue
        if n == "STRIDE_LINES" and k["RANDOMNESS"] >= 1.0:
            continue            # every access starts a new run: stride has no effect
        out.append(n)
    return out


def validate(knobs: dict) -> dict:
    unknown = set(knobs) - set(KNOB_SPEC)
    if unknown:
        raise ValueError(f"unknown knobs: {sorted(unknown)}")
    full = {}
    for name, (default, lo, hi, kind) in KNOB_SPEC.items():
        v = knobs.get(name, default)
        v = float(v) if name in FLOATS else int(v)
        if not lo <= v <= hi:
            raise ValueError(f"{name}={v} outside [{lo}, {hi}]")
        if kind == "log2" and not _pow2(v):
            raise ValueError(f"{name}={v} must be a power of two")
        full[name] = v
    if (full["WSS_KB"] * 16) // full["STRIDE_LINES"] < 2 * full["NCHAINS"]:
        raise ValueError("need at least 2*NCHAINS slots (WSS_KB*16/STRIDE_LINES)")
    if full["ALU_OPS"] % full["DEP_DIST"]:
        raise ValueError("ALU_OPS must be a multiple of DEP_DIST")
    if full["NBRANCH"] and full["BR_PATTERN"] == 3 and not 0 < full["BR_TAKEN"] < 1:
        raise ValueError("BR_TAKEN must be in (0,1) for the random pattern")
    return canonicalize(full)


def _mtime(p):
    try:
        return int(Path(p).stat().st_mtime)
    except (OSError, TypeError):
        return 0


def _version(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return (p.stdout + p.stderr).strip()[:2000]
    except Exception:
        return "unavailable"


def cache_key(knobs: dict) -> str:
    payload = json.dumps({
        "knobs": canonicalize(knobs), "sim": SIM_INSTR, "warm_passes": WARMUP_PASSES, "warm_min": WARMUP_MIN,
        "champsim": CHAMPSIM, "champsim_mtime": _mtime(CHAMPSIM),
        "template": str(TEMPLATE), "template_mtime": _mtime(TEMPLATE),
        "compiler": CC, "compiler_version": _version([CC, "--version"]), "cflags": CFLAGS,
        "pin": PIN, "pin_mtime": _mtime(PIN), "tracer": TRACER, "tracer_mtime": _mtime(TRACER),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _fmt(k, v):
    return repr(float(v)) if k in FLOATS else str(int(v))


def build_cmd(knobs: dict, out_bin: Path) -> list:
    defs = [f"-D{k}={_fmt(k, v)}" for k, v in sorted(knobs.items())]
    return [CC, *CFLAGS, *defs, str(TEMPLATE), "-o", str(out_bin), "-lm"]


def probe_info(binary: Path) -> dict:
    out = subprocess.run([str(binary), "--print-config"], capture_output=True, text=True, check=True).stdout
    info = {}
    for line in out.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            v = v.split()[0] if v.split() else ""
            try:
                info[k.strip()] = float(v) if "." in v else int(v)
            except ValueError:
                info[k.strip()] = v
    return info


def preflight(binary: Path) -> dict:
    """STATIC check + native self-check. Raises on failure (fail fast, before any tracing)."""
    st = subprocess.run([sys.executable, str(CHECK_STATIC), str(binary)], capture_output=True, text=True)
    if st.returncode != 0:
        raise RuntimeError("STATIC check failed:\n" + st.stdout[-2000:])
    sc = subprocess.run([str(binary), "--iters", "2000"], capture_output=True, text=True)
    if "result.selfcheck=PASS" not in sc.stdout:
        raise RuntimeError("native self-check failed:\n" + sc.stdout[-1000:])
    return {"STATIC": "PASS", "NATIVE_SELFCHECK": "PASS", "static_report": st.stdout.strip().splitlines()[0]}


def trace_cmd(binary: Path, out, skip: int, n_instr: int, iters: int) -> list:
    return [PIN, "-t", TRACER, "-o", str(out), "-s", str(skip), "-t", str(n_instr), "--",
            str(binary), "--iters", str(iters)]


def sim_cmd(trace: Path, warmup: int, sim: int) -> list:
    return [CHAMPSIM, "--warmup-instructions", str(warmup), "--simulation-instructions", str(sim), str(trace)]


def _run(cmd, dry, **kw):
    if dry:
        print(" ".join(map(str, cmd)))
        return None
    return subprocess.run(cmd, check=True, timeout=TIMEOUT_S, **kw)


def init_length(knobs: dict, binary: Path, tmp: Path, dry=False) -> int:
    """Instructions executed by `probe --dry-run` (process start + initialisation + exit).
    Used as the tracer's -s skip: it overshoots the loop entry by the exit path only
    (a few thousand instructions), so tracing starts inside the steady-state loop.
    Counted by streaming the dry-run trace through a FIFO (no large file on disk)."""
    key = hashlib.sha256(json.dumps(canonicalize(knobs), sort_keys=True).encode()).hexdigest()[:16]
    store = CACHE / "init_len.json"
    table = json.loads(store.read_text()) if store.exists() else {}
    if key in table:
        return table[key]
    fifo = tmp / "init.fifo"
    cmd = [PIN, "-t", TRACER, "-o", str(fifo), "-s", "0", "-t", str(10**12), "--", str(binary), "--dry-run"]
    if dry:
        print("mkfifo", fifo, "&&", " ".join(cmd), "(count bytes / record size)")
        return 0
    os.mkfifo(fifo)
    total = [0]

    def reader():
        with open(fifo, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                total[0] += len(b)
    t = threading.Thread(target=reader)
    t.start()
    subprocess.run(cmd, check=True, timeout=TIMEOUT_S, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t.join()
    n = total[0] // RECORD_BYTES
    table[key] = n
    store.write_text(json.dumps(table))
    return n


def _last(pattern, text):
    m = re.findall(pattern, text)
    return m[-1] if m else None


def parse_stats(out: str) -> dict:
    """Best-effort parser for ChampSim text output (LAST match of each pattern). Adapt to your build."""
    stats = {}
    m = _last(r"CPU 0 cumulative IPC:\s*([\d.]+)\s+instructions:\s*(\d+)\s+cycles:\s*(\d+)", out)
    if not m:
        raise ValueError("could not parse IPC; adapt parse_stats() to your ChampSim output")
    stats["ipc"], stats["instructions"], stats["cycles"] = float(m[0]), int(m[1]), int(m[2])
    b = _last(r"Branch Prediction Accuracy:\s*([\d.]+)%\s+MPKI:\s*([\d.]+)", out)
    if b:
        stats["branch_accuracy_pct"], stats["branch_mpki"] = float(b[0]), float(b[1])
    instr = stats["instructions"]
    for level in ("L1D", "L1I", "L2C", "LLC"):
        c = _last(rf"{level}\s+TOTAL\s+ACCESS:\s*(\d+)\s+HIT:\s*(\d+)\s+MISS:\s*(\d+)", out)
        if c and instr:
            stats[f"{level}_mpki"] = 1000.0 * int(c[2]) / instr
            stats[f"{level}_miss_rate"] = int(c[2]) / max(int(c[0]), 1)
    return stats


def run_probe(knobs: dict, dry=False, keep=False) -> dict:
    knobs = validate(knobs)
    CACHE.mkdir(parents=True, exist_ok=True)
    key = cache_key(knobs)
    hit = CACHE / f"{key}.json"
    if hit.exists() and not dry:
        return json.loads(hit.read_text())
    if not dry and (not PIN or not TRACER):
        raise RuntimeError("set PIN_BIN and TRACER_SO (see the docstring)")
    tmp = Path(tempfile.mkdtemp(prefix="probe_", dir=os.environ.get("TMPDIR")))
    try:
        binary, tr = tmp / "probe", tmp / "probe.champsimtrace"
        subprocess.run(build_cmd(knobs, binary), check=True, capture_output=True, text=True)
        verification = preflight(binary)                       # fail fast
        info = probe_info(binary)
        per_iter = int(info["derived.static_instr_per_iter"])
        warmup = max(WARMUP_MIN, int(WARMUP_PASSES * float(info["derived.instr_per_span_pass"])))
        skip = init_length(knobs, binary, tmp, dry)
        n_instr = warmup + SIM_INSTR
        iters = math.ceil(n_instr / per_iter) + 10_000            # finite run: ends even if the tracer does not exit it
        _run(trace_cmd(binary, tr, skip, n_instr, iters), dry, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        res = _run(sim_cmd(tr, warmup, SIM_INSTR), dry, capture_output=True, text=True)
        if dry:
            return {}
        verification.update({"TRACE": "run check_probe.py (lackey) for this shape",
                             "CHAMPSIM_skip": "NOT-YET-VERIFIED (tools/check_champsim_trace.py)",
                             "CHAMPSIM_warmup_adequacy": "NOT-YET-VERIFIED"})
        result = {
            "key": key, "knobs": knobs, "active_knobs": active_knobs(knobs),
            "skip": skip, "warmup": warmup, "sim_instr": SIM_INSTR, "iters": iters,
            "warmup_policy": f"max({WARMUP_MIN}, {WARMUP_PASSES} x instr_per_span_pass)",
            "overhead": {
                "static_instr_per_iter": per_iter,
                "generator_instr_per_access": info["overhead.generator_instr_per_access"],
                "addrgen_instr_per_iter": info["overhead.addrgen_instr_per_iter"],
                "load_fraction_of_instr": info["overhead.load_fraction_of_instr"],
            },
            "verification": verification,
            "stats": parse_stats(res.stdout),
        }
        hit.write_text(json.dumps(result, indent=2))
        return result
    finally:
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)


def run_semantic(sem, **kw) -> dict:
    from semantic import SemanticProbe
    if not isinstance(sem, SemanticProbe):
        sem = SemanticProbe(**sem)
    out = run_probe(sem.to_backend(), **kw)
    if out:
        out["semantic"] = sem.model_dump()
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    knobs_in = json.loads(args[0]) if args else {}
    kw = dict(dry="--dry-run" in sys.argv, keep="--keep" in sys.argv)
    out = run_semantic(knobs_in, **kw) if "--semantic" in sys.argv else run_probe(knobs_in, **kw)
    if out:
        print(json.dumps(out, indent=2))
