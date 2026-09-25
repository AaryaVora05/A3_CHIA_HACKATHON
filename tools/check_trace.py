#!/usr/bin/env python3
"""TRACE checks for a probe V1.1 binary, from the executed instruction stream.

Runs the binary under valgrind lackey (--trace-mem=yes, --vex-iropt-level=0 so
that loads with unused results are not dropped), keeps the measured loop only,
and checks:
  T1 per iteration: executed instructions = static count, exactly 8 loads,
     0 stores, B+1 conditional branches
  T2 executed data-load addresses == the binary's replica dump (every access)
  T3 hot accesses: fraction matches REUSE (row-granular, Weyl) and all hot
     addresses lie in the 4 KB hot region; main accesses lie in the span;
     the two regions are disjoint
  T4 independent: run-start fraction of the main stream ~ RANDOMNESS
     (binomial 5-sigma), random-start slots uniform over the span (chi-square)
  T5 dependent (--coverage): one pass of every chain visits all N slots
     exactly once (requires REUSE=0; iterations chosen automatically)
  T6 (--filter A B): the non-hot stream of binary B (REUSE=u) equals the
     stream of binary A (REUSE=0), same other knobs and seed
usage:
  check_trace.py BIN [--iters N] [--coverage]
  check_trace.py --filter BIN_REUSE0 BIN_REUSEU [--iters N]
Keep WSS_KB <= 4096: initialisation is traced too.
"""
import math
import os
import re
import subprocess
import sys
import tempfile


def cfg(binary):
    out = subprocess.run([binary, "--print-config"], capture_output=True, text=True, check=True).stdout
    c = {}
    for line in out.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            c[k.strip()] = v.split()[0] if v.split() else ""
    return c


def symbols(binary):
    out = subprocess.run(["nm", binary], capture_output=True, text=True, check=True).stdout
    sym = {}
    for line in out.splitlines():
        p = line.split()
        if len(p) == 3:
            sym[p[2]] = int(p[0], 16)
    return sym


def loop_range(binary):
    dis = subprocess.run(["objdump", "-d", "--no-show-raw-insn", binary], capture_output=True, text=True, check=True).stdout
    body = re.search(r"<run_probe>:\n(.*?)\n\n", dis, re.S).group(1)
    ins = [(int(m.group(1), 16), m.group(2), m.group(3)) for m in
           (re.match(r"\s*([0-9a-f]+):\s+(\S+)\s*(.*)", l) for l in body.splitlines()) if m]
    for a, op, args in ins:
        if op == "jne" and int(args.split()[0], 16) < a:
            head = int(args.split()[0], 16)
            jcc = {x for x, o, _ in ins if head <= x <= a and o.startswith("j") and o != "jmp"}
            return head, a, jcc
    raise SystemExit("no loop")


def trace(binary, iters):
    """returns (iterations, instrs, loads[(addr, size)], stores, jcc executions)"""
    head, latch, jcc = loop_range(binary)
    r, w = os.pipe()
    p = subprocess.Popen(["valgrind", "--tool=lackey", "--trace-mem=yes", "--vex-iropt-level=0",
                          f"--log-fd={w}", binary, "--iters", str(iters)],
                         pass_fds=(w,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.close(w)
    n_it = n_ins = n_st = n_j = 0
    loads = []
    inloop = False
    with os.fdopen(r) as f:
        for line in f:
            if line[0] == "I":
                a = int(line[3:line.index(",")], 16)
                inloop = head <= a <= latch
                if inloop:
                    n_ins += 1
                    if a in jcc:
                        n_j += 1
                    if a == latch:
                        n_it += 1
            elif inloop and line[1] == "L":
                a, sz = line[3:].split(",")
                loads.append((int(a, 16), int(sz)))
            elif inloop and line[1] in "SM":
                n_st += 1
    p.wait()
    return n_it, n_ins, loads, n_st, n_j


def replica_addrs(binary, iters):
    with tempfile.TemporaryDirectory() as d:
        f = os.path.join(d, "a")
        subprocess.run([binary, "--iters", str(iters), "--dump-addrs", f, str(iters * 8)], check=True,
                       capture_output=True)
        return [(int(l.split()[0]), int(l.split()[1]), int(l.split()[2], 16)) for l in open(f)]


RES = []


def check(name, ok, detail):
    RES.append(ok)
    print(f"  TRACE {name}: {'PASS' if ok else 'FAIL'}  {detail}")


def single(binary, iters, coverage, stat_iters):
    c, sym = cfg(binary), symbols(binary)
    B, C = int(c["knob.branch_frequency"]), int(c["knob.independent_chains"])
    dep = c["knob.memory_dependency"] == "dependent"
    r, u = float(c["knob.randomness"]), float(c["knob.reuse"])
    N = int(c["derived.slots"])
    stride = int(c["knob.stride_lines"]) * 64
    span0, span1 = sym["g_span"], sym["g_span"] + int(c["knob.working_set_kb"]) * 1024
    hot0, hot1 = sym["g_hot"], sym["g_hot"] + 4096
    if coverage:
        if not dep or u != 0:
            raise SystemExit("--coverage needs DEPENDENT=1 and REUSE=0")
        iters = (N // C) // (8 // C)
    print(f"binary={os.path.basename(binary)} iters={iters}")
    it, ins, loads, st, nj = trace(binary, iters)
    static = int(c["derived.static_instr_per_iter"])
    check("T1 counts", it == iters and ins == static * iters and len(loads) == 8 * iters and st == 0 and nj == (B + 1) * iters,
          f"iters={it} instr/iter={ins / max(it, 1):.2f} (static {static}) loads/iter={len(loads) / max(it, 1):.2f} "
          f"stores={st} jcc/iter={nj / max(it, 1):.2f} (expect {B + 1})")
    rep = replica_addrs(binary, iters)
    ex = [a for a, _ in loads]
    same = sum(1 for (h, k, a), e in zip(rep, ex) if a == e)
    check("T2 executed == replica", same == len(rep) == len(ex), f"{same}/{len(rep)} addresses match")
    hot = [a for a in ex if hot0 <= a < hot1]
    main = [a for a in ex if span0 <= a < span1]
    frac = len(hot) / len(ex)
    tol = 1.0 / (iters * (8 // C)) + 0.005
    disjoint = hot1 <= span0 or span1 <= hot0
    check("T3 regions/hot fraction", len(hot) + len(main) == len(ex) and disjoint and abs(frac - u) <= tol,
          f"hot={frac:.4f} (REUSE {u}, tol {tol:.4f}); outside both regions={len(ex) - len(hot) - len(main)}; "
          f"hot=[{hot0:#x},{hot1:#x}) span=[{span0:#x},{span1:#x})")
    if not dep and r > 0:
        # Statistics on a larger sample from the replica stream (T2 proved replica == executed).
        big = replica_addrs(binary, stat_iters)
        chains = [[] for _ in range(C)]
        for (h, k, a) in big:
            if not h:
                chains[k].append(a)
        steps = jumps = 0
        tgt = []
        for s_ in chains:
            for x, y in zip(s_, s_[1:]):
                steps += 1
                seq = span0 + ((x - span0 + stride) % (span1 - span0))
                if y != seq:
                    jumps += 1
                    tgt.append((y - span0) // stride)
        # a random start lands on the sequential successor with prob 1/N: expected observed jump rate r*(1-1/N)
        exp = r * (1 - 1 / N)
        sd = math.sqrt(max(exp * (1 - exp), 1e-12) / steps)
        check("T4a run-start fraction", abs(jumps / steps - exp) <= 5 * sd + 1e-9,
              f"{jumps / steps:.5f} vs r={r} (5 sigma = {5 * sd:.5f}, {steps} main steps, replica sample)")
        bins = min(N, 256)
        cnt = [0] * bins
        for t in tgt:
            cnt[t * bins // N] += 1
        e = len(tgt) / bins
        chi = sum((x - e) ** 2 / e for x in cnt) if e > 0 else 0
        df = bins - 1
        crit = df * (1 - 2 / (9 * df) + 3.09 * math.sqrt(2 / (9 * df))) ** 3   # p = 0.001 (Wilson-Hilferty)
        check("T4b random-start uniformity", e >= 20 and chi < crit,
              f"chi2={chi:.1f} df={df} (p=0.001 critical {crit:.1f}), {len(tgt)} random starts, {e:.0f}/bin")
    if coverage:
        slots = [(a - span0) // stride for a in main]
        check("T5 dependent full coverage", len(slots) == N and len(set(slots)) == N,
              f"{len(slots)} main accesses, {len(set(slots))} distinct slots of N={N}")


def filt(a_bin, b_bin, iters):
    ca, cb = cfg(a_bin), cfg(b_bin)
    assert float(ca["knob.reuse"]) == 0, "first binary must have REUSE=0"
    sb = symbols(b_bin)
    hot0 = sb["g_hot"]
    _, _, la, _, _ = trace(a_bin, iters)
    _, _, lb, _, _ = trace(b_bin, iters)
    ea = [a for a, _ in la]
    eb = [a for a, _ in lb if not (hot0 <= a < hot0 + 4096)]
    n = min(len(ea), len(eb))
    same = sum(1 for x, y in zip(ea[:n], eb[:n]) if x == y)
    print(f"binaries: {os.path.basename(a_bin)} (REUSE=0) vs {os.path.basename(b_bin)} (REUSE={cb['knob.reuse']})")
    check("T6 reuse filtering", n > 0 and same == n,
          f"first {n} non-hot accesses of REUSE={cb['knob.reuse']} == REUSE=0 stream: {same}/{n}")


if __name__ == "__main__":
    a = sys.argv[1:]
    iters = int(a[a.index("--iters") + 1]) if "--iters" in a else 4000
    if a[0] == "--filter":
        filt(a[1], a[2], iters)
    else:
        single(a[0], iters, "--coverage" in a, int(a[a.index("--stat-iters") + 1]) if "--stat-iters" in a else 100000)
    print("TRACE", "PASS" if all(RES) else "FAIL")
    sys.exit(0 if all(RES) else 1)
