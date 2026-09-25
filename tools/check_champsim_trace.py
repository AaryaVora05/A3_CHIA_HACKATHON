#!/usr/bin/env python3
"""CHAMPSIM-level checks on a real .champsimtrace produced by Pin 3.22 + champsim_tracer.
Run this ON THE VM; it cannot be run without Pin.

Record format (ChampSim tracer, 64 bytes):
  u64 ip; u8 is_branch; u8 branch_taken; u8 dst_reg[2]; u8 src_reg[4]; u64 dst_mem[2]; u64 src_mem[4]
(.xz / .gz traces are read transparently.)

  C1 skip       the trace starts inside run_probe and reaches the loop within 1 iteration
  C2 counts     every complete iteration has exactly static_instr_per_iter records,
                8 records with a source memory operand, none with a destination memory operand,
                and B+1 records flagged is_branch
  C3 xmm        (ALU_XMM builds, A>0) every pxor record reads and writes the same register ID,
                op q's register == op q-D's, D distinct IDs, and no other loop instruction
                writes those IDs (register IDs survive the tracer without aliasing)
  C4 lbd        (--compare TRACE_OTHER) the branch_taken sequences of two traces (e.g. the
                LOAD_BR_DEP=0 and =1 builds of the same config) are identical
  C5 addresses  every load address lies in the span or the hot region

usage: check_champsim_trace.py BINARY TRACE [--compare BINARY2 TRACE2] [--max-records N]
"""
import gzip
import lzma
import re
import struct
import subprocess
import sys

REC = struct.Struct("<QBB2B4B2Q4Q")


def records(path, limit):
    op = lzma.open if path.endswith(".xz") else gzip.open if path.endswith(".gz") else open
    with op(path, "rb") as f:
        n = 0
        while n < limit:
            b = f.read(REC.size)
            if len(b) < REC.size:
                return
            v = REC.unpack(b)
            yield dict(ip=v[0], br=v[1], taken=v[2], dst=[r for r in v[3:5] if r], src=[r for r in v[5:9] if r],
                       dmem=[m for m in v[9:11] if m], smem=[m for m in v[11:15] if m])
            n += 1


def info(binary):
    cfg = {}
    for line in subprocess.run([binary, "--print-config"], capture_output=True, text=True, check=True).stdout.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.split()[0] if v.split() else ""
    dis = subprocess.run(["objdump", "-d", "--no-show-raw-insn", binary], capture_output=True, text=True, check=True).stdout
    body = re.search(r"<run_probe>:\n(.*?)\n\n", dis, re.S).group(1)
    ins = [(int(m.group(1), 16), m.group(2), m.group(3)) for m in
           (re.match(r"\s*([0-9a-f]+):\s+(\S+)\s*(.*)", l) for l in body.splitlines()) if m]
    head = latch = None
    for a, op, args in ins:
        if op == "jne" and int(args.split()[0], 16) < a:
            head, latch = int(args.split()[0], 16), a
    pxor = [a for a, op, _ in ins if op == "pxor" and head <= a <= latch]
    sym = {}
    for line in subprocess.run(["nm", binary], capture_output=True, text=True, check=True).stdout.splitlines():
        p = line.split()
        if len(p) == 3:
            sym[p[2]] = int(p[0], 16)
    return cfg, (ins[0][0], ins[-1][0]), head, latch, pxor, sym


RES = []


def check(name, ok, detail):
    RES.append(ok)
    print(f"  CHAMPSIM {name}: {'PASS' if ok else 'FAIL'}  {detail}")


def analyse(binary, trace, limit):
    cfg, (f0, f1), head, latch, pxor, sym = info(binary)
    static = int(cfg["derived.static_instr_per_iter"])
    B, A, D = int(cfg["knob.branch_frequency"]), int(cfg["knob.alu_ops"]), int(cfg["knob.dependency_distance"])
    span0, span1 = sym["g_span"], sym["g_span"] + int(cfg["knob.working_set_kb"]) * 1024
    hot0 = sym["g_hot"]
    recs = list(records(trace, limit))
    print(f"binary={binary} trace={trace} records={len(recs)}")
    first = recs[0]["ip"] if recs else 0
    reach = next((k for k, r in enumerate(recs) if r["ip"] == head), None)
    check("C1 skip", recs and f0 <= first <= f1 and reach is not None and reach <= static,
          f"first ip={first:#x} (run_probe {f0:#x}-{f1:#x}), loop head reached after {reach} records")
    # complete iterations: from the record after a latch to the next latch
    lat = [k for k, r in enumerate(recs) if r["ip"] == latch]
    bad = []
    taken = []
    xmm_ok, xmm_detail = True, "n/a"
    pxor_seq = []
    loop_dst = set()
    outside = 0
    for a, b in zip(lat, lat[1:]):
        it = recs[a + 1:b + 1]
        n_ld = sum(1 for r in it if r["smem"])
        n_st = sum(1 for r in it if r["dmem"])
        n_br = sum(1 for r in it if r["br"])
        if len(it) != static or n_ld != 8 or n_st != 0 or n_br != B + 1:
            bad.append((len(it), n_ld, n_st, n_br))
        for r in it:
            for m in r["smem"]:
                if not (span0 <= m < span1 or hot0 <= m < hot0 + 4096):
                    outside += 1
            if r["ip"] in pxor:
                pxor_seq.append(r)
            else:
                loop_dst.update(r["dst"])
            if r["br"] and r["ip"] != latch:
                taken.append(r["taken"])
    check("C2 counts", lat and not bad,
          f"{max(len(lat) - 1, 0)} complete iterations; mismatches={bad[:3]} (expect {static} records, 8 loads, 0 stores, {B + 1} branches)")
    check("C5 addresses", outside == 0, f"{outside} load addresses outside span/hot")
    if cfg.get("backend.alu_regs") == "xmm" and A:
        ids = []
        for r in pxor_seq:
            regs = [x for x in r["dst"] if x in r["src"]]
            ids.append(regs[0] if len(regs) == 1 and len(r["dst"]) == 1 else None)
        per_iter = [ids[k:k + A] for k in range(0, len(ids) - A + 1, A)]
        ok = bool(per_iter) and all(None not in p for p in per_iter)
        dist = set()
        if ok:
            p = per_iter[0]
            for q in range(A):
                for back in range(1, A + 1):
                    if p[(q - back) % A] == p[q]:
                        dist.add(back); break
            ok = dist == {D} and len(set(p)) == D and all(x == p for x in per_iter) and not (set(p) & loop_dst)
        xmm_ok = ok
        xmm_detail = (f"pxor register IDs per iteration={per_iter[0] if per_iter else None}, distances={sorted(dist)}, "
                      f"collide with other loop destinations={sorted(set(per_iter[0]) & loop_dst) if per_iter and None not in per_iter[0] else 'n/a'}")
        check("C3 xmm", xmm_ok, xmm_detail)
    return taken


if __name__ == "__main__":
    a = sys.argv[1:]
    limit = int(a[a.index("--max-records") + 1]) if "--max-records" in a else 2_000_000
    t1 = analyse(a[0], a[1], limit)
    if "--compare" in a:
        k = a.index("--compare")
        t2 = analyse(a[k + 1], a[k + 2], limit)
        n = min(len(t1), len(t2))
        check("C4 lbd outcome identity", n > 0 and t1[:n] == t2[:n], f"first {n} pattern-branch outcomes identical: {t1[:n] == t2[:n]}")
    print("CHAMPSIM", "PASS" if all(RES) else "FAIL")
    sys.exit(0 if all(RES) else 1)
