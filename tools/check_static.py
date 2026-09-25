#!/usr/bin/env python3
"""STATIC checks for a probe V1.1 binary (from its disassembly).

Parses run_probe's measured loop against the spec layout, block by block:
  row header (8) | NCHAINS x access (16) | branches of the row (7 each) | ALU ops of the row
  ... repeated ROWS times | branch-state step (2, if B>0) | loop counter + jne
and fails on any deviation. Checks:
  S1 exact instruction count = 72/C + 128 + 7B + 2[B>0] + A + 2
  S2 no %rsp references (no spills), no stores, exactly 8 memory loads
  S3 exactly B+1 conditional branches: B `jb` whose target is the next
     instruction, plus the latch; no other jumps
  S4 address selection is cmov-only (8 cmovb, 24 cmove, 8 cmovne per iteration)
  S5 ALU op q writes the register of op q-D (cyclically), D distinct registers
  S6 dependent: load address = lea(v_k), v_k updated only by cmove from the load,
     C distinct chain registers used consistently; independent: address = lea(span+off_k)
  S7 lbd=1: every branch source is `mov` of the preceding row's last load; lbd=0: `mov $0x0`
  S8 data flow: no instruction reads a load-derived register (or flags) except on the
     intended paths -- dependent chain (lea/cmovne/load/cmove), the equaliser cmove, and
     (lbd=1 only) the branch source/compare/jb. Catches allocator-induced hidden load
     dependences such as `sbb %r,%r` on a register that still holds a loaded value.
Prints a shape signature (mnemonic + operand-kind sequence) for cross-config invariance.
usage: check_static.py BINARY [--show] [--signature-only]
"""
import hashlib
import re
import subprocess
import sys

R32 = {"eax": "rax", "ebx": "rbx", "ecx": "rcx", "edx": "rdx", "esi": "rsi", "edi": "rdi", "ebp": "rbp", "esp": "rsp"}


def norm(r):
    r = r.strip().lstrip("%")
    if r in R32:
        return R32[r]
    m = re.fullmatch(r"(r\d+)[dwb]?", r)
    return m.group(1) if m else r


def config(binary):
    out = subprocess.run([binary, "--print-config"], capture_output=True, text=True, check=True).stdout
    cfg = {}
    for line in out.splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.split()[0] if v.split() else ""
    return cfg


def loop(binary):
    dis = subprocess.run(["objdump", "-d", "--no-show-raw-insn", binary], capture_output=True, text=True, check=True).stdout
    body = re.search(r"<run_probe>:\n(.*?)\n\n", dis, re.S).group(1)
    ins = []
    for line in body.splitlines():
        m = re.match(r"\s*([0-9a-f]+):\s+(\S+)\s*(.*)", line)
        if m:
            ins.append((int(m.group(1), 16), m.group(2), m.group(3).split("<")[0].strip()))
    for idx, (a, op, args) in enumerate(ins):
        if op == "jne" and int(args.split()[0], 16) < a:
            s = next(k for k, x in enumerate(ins) if x[0] == int(args.split()[0], 16))
            return ins[s:idx + 1]
    raise SystemExit("loop latch not found")


def ops(args):
    """split operands at top-level commas"""
    out, depth, cur = [], 0, ""
    for ch in args:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur); cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return [o.strip() for o in out]


def kind(o):
    if o.startswith("$"):
        return "i"
    if "(" in o:
        return "m"
    if o.startswith("%xmm"):
        return "x"
    if o.startswith("%"):
        return "r"
    return "a"   # address / label


HDR = ["lea", "imul", "cmp", "mov", "sbb", "shr", "shl", "add", "and"]
ACC = ["imul", "add", "imul", "shr", "shl", "lea", "and", "cmp", "cmovb", "test", "cmove", "cmove",
       "lea", "cmovne", "mov", "cmove"]
BR = ["mov", "shr", "add", "cmp", "jb", "imul", "add"]


def rw(op, args):
    """(reads, writes) register sets; 'flags' is a pseudo-register."""
    o = ops(args)
    regs = lambda x: {norm(r) for r in re.findall(r"%(\w+)", x)}
    reads, writes = set(), set()
    if op.startswith("j"):
        return ({"flags"} if op not in ("jmp",) else set()), set()
    dst = o[-1] if o else ""
    dst_is_reg = dst.startswith("%")
    if op in ("mov", "movl", "lea", "movzx", "movq") or (op == "imul" and len(o) == 3):
        for x in o[:-1]:
            reads |= regs(x)
        if not dst_is_reg:
            reads |= regs(dst)
    else:
        for x in o:
            reads |= regs(x)
    if dst_is_reg and op not in ("cmp", "test"):
        writes |= regs(dst)
    if op.startswith("cmov") or op in ("sbb", "adc"):
        reads.add("flags")
    if op not in ("mov", "movl", "lea", "movzx", "movq", "pxor") and not op.startswith("cmov"):
        writes.add("flags")
    return reads, writes


def dataflow(body, roles, dep, lbd, err):
    allowed = {"tail_cmov"}
    if dep:
        allowed |= {"tail_lea", "tail_cmovne", "load"}
    if lbd:
        allowed |= {"br_src", "br_shr", "br_add", "br_cmp", "br_jb"}
    tainted = set()
    bad = []
    for _ in range(2):                       # two passes: catches loop-carried taint
        for (_, op, a), role in zip(body, roles):
            r, w = rw(op, a)
            t = r & tainted
            if t and role not in allowed:
                bad.append(f"{role}: {op} {a} reads load-derived {sorted(t)}")
            if role == "load" or (t and role in allowed):
                tainted |= w
            else:
                tainted -= w
    for b in dict.fromkeys(bad):
        err.append("S8 hidden load dependence: " + b)


def main():
    binary = sys.argv[1]
    show = "--show" in sys.argv
    c = config(binary)
    B, A, D, C = int(c["knob.branch_frequency"]), int(c["knob.alu_ops"]), int(c["knob.dependency_distance"]), int(c["knob.independent_chains"])
    dep = c["knob.memory_dependency"] == "dependent"
    lbd = c["knob.load_branch_dependency"] == "1"
    xmm = c["backend.alu_regs"] == "xmm"
    ROWS = 8 // C
    expect_n = int(c["derived.static_instr_per_iter"])
    L = loop(binary)
    err = []
    sig = hashlib.sha1(" ".join(op + ":" + "".join(kind(o) for o in ops(a)) for _, op, a in L).encode()).hexdigest()[:16]
    msig = hashlib.sha1(" ".join(op for _, op, _ in L).encode()).hexdigest()[:16]
    if "--signature-only" in sys.argv:
        print(f"{msig} {sig}"); return 0

    # S1
    if len(L) != expect_n:
        err.append(f"S1 instruction count {len(L)} != {expect_n}")
    # S2
    loads = [(op, a) for _, op, a in L if op != "lea" and "(" in a]
    for op, a in loads:
        if "%rsp" in a:
            err.append(f"S2 stack reference (spill): {op} {a}")
    stores = [(op, a) for op, a in loads if "(" in ops(a)[-1]]
    if stores:
        err.append(f"S2 stores in loop: {stores[:3]}")
    if len(loads) != 8 or any(op != "mov" for op, _ in loads):
        err.append(f"S2 memory ops: {len(loads)} (expected 8 `mov` loads)")
    # S3
    jumps = [(i, op, a) for i, (_, op, a) in enumerate(L) if op.startswith("j")]
    jb = [j for j in jumps if j[1] == "jb"]
    if len(jb) != B or len(jumps) != B + 1 or L[-1][1] != "jne":
        err.append(f"S3 jumps: {len(jb)} jb (expected {B}), {len(jumps)} total (expected {B + 1})")
    for i, op, a in jb:
        if int(a.split()[0], 16) != L[i + 1][0]:
            err.append("S3 jb target is not the fall-through instruction")
    # S4
    cnt = lambda m: sum(1 for _, op, _ in L if op == m)
    if (cnt("cmovb"), cnt("cmove"), cnt("cmovne")) != (8, 24, 8):
        err.append(f"S4 cmov counts {cnt('cmovb')}/{cnt('cmove')}/{cnt('cmovne')} != 8/24/8")

    # block parse (loop counter increment may be scheduled anywhere after the last header)
    ireg = None
    body = list(L)
    hdr_lea = body[0]
    m = re.match(r"(?:0x[0-9a-f]+)?\(,(%\w+),\d\)", ops(hdr_lea[2])[0])
    if not m:
        err.append("layout: loop does not start with a row header")
    else:
        ireg = norm(m.group(1))
    inc_idx = [k for k, (_, op, a) in enumerate(body)
               if ireg and ((op == "add" and ops(a) == ["$0x1", "%" + ireg]) or (op == "inc" and norm(a) == ireg)
                            or (op == "sub" and ops(a) == ["$0xffffffffffffffff", "%" + ireg]))]
    if len(inc_idx) != 1:
        err.append(f"layout: loop-counter increment not found exactly once ({len(inc_idx)})")
    else:
        body.pop(inc_idx[0])
    pos = 0
    alu_regs, chain_regs, last_load = [], {}, None
    roles = [None] * len(body)

    def take(pattern, what, rl=None):
        nonlocal pos
        blk = body[pos:pos + len(pattern)]
        got = [op for _, op, _ in blk]
        if got != pattern:
            err.append(f"layout: {what} at {pos}: got {got} expected {pattern}")
            raise StopIteration
        for j in range(len(pattern)):
            roles[pos + j] = (rl or [what] * len(pattern))[j]
        pos += len(pattern)
        return blk

    try:
        for R in range(ROWS):
            take(HDR, f"row {R} header")
            for k in range(C):
                blk = take(ACC, f"row {R} access {k}", ["gen"] * 12 + ["tail_lea", "tail_cmovne", "load", "tail_cmov"])
                lea2, cmovne, ld, cmv = blk[12], blk[13], blk[14], blk[15]
                q = norm(ops(ld[2])[1])
                if norm(re.search(r"\((%\w+)\)", ops(ld[2])[0]).group(1)) != q or norm(ops(lea2[2])[1]) != q or norm(ops(cmovne[2])[1]) != q:
                    err.append(f"S6 row {R} access {k}: load/address register mismatch")
                src = ops(lea2[2])[0]
                base = norm(re.search(r"\((%\w+)\)", src).group(1))
                if dep:
                    if not re.fullmatch(r"(0x0)?\(%\w+\)", src):
                        err.append(f"S6 dependent address is not lea (v_k): {lea2[2]}")
                    if norm(ops(cmv[2])[0]) != q or norm(ops(cmv[2])[1]) != base:
                        err.append(f"S6 chain register not updated from the load: {cmv[2]}")
                else:
                    if not re.fullmatch(r"0x[0-9a-f]+\(%\w+\)", src):
                        err.append(f"S6 independent address is not lea span(off_k): {lea2[2]}")
                    if norm(ops(blk[10][2])[1]) != base:
                        err.append("S6 offset register used for address differs from the advanced offset")
                chain_regs.setdefault(k, set()).add(base)
                last_load = q
            b0, b1 = R * B // ROWS, (R + 1) * B // ROWS
            for j in range(b0, b1):
                blk = take(BR, f"row {R} branch {j}", ["br_src", "br_shr", "br_add", "br_cmp", "br_jb", "br_state", "br_state"])
                src = ops(blk[0][2])[0]
                if lbd and norm(src) != last_load:
                    err.append(f"S7 lbd=1 branch source {src} is not the last load register {last_load}")
                if not lbd and src != "$0x0":
                    err.append(f"S7 lbd=0 branch source is {src}, expected $0x0")
            a0, a1 = R * A // ROWS, (R + 1) * A // ROWS
            for q_ in range(a0, a1):
                _, op, a = body[pos]
                if (xmm and op != "pxor") or (not xmm and not (op == "xor" and a.startswith("$0x5a5a5a5a"))):
                    err.append(f"S5 expected ALU op at {pos}, got {op} {a}"); raise StopIteration
                alu_regs.append(ops(a)[-1]); roles[pos] = "alu"; pos += 1
        if B:
            take(["imul", "add"], "branch-state step")
        take(["jne"], "latch")
        if pos != len(body):
            err.append("layout: trailing instructions")
        dataflow(body, roles, dep, lbd, err)
    except StopIteration:
        pass
    # S5
    if A and len(alu_regs) == A:
        dists = set()
        for q_ in range(A):
            for back in range(1, A + 1):
                if alu_regs[(q_ - back) % A] == alu_regs[q_]:
                    dists.add(back); break
        if dists != {D} or len(set(alu_regs)) != D:
            err.append(f"S5 ALU distances {sorted(dists)} with {len(set(alu_regs))} registers; expected D={D}")
    # S6 chain registers
    if dep and len(chain_regs) == C:
        regs = [next(iter(v)) for v in chain_regs.values()]
        if any(len(v) != 1 for v in chain_regs.values()) or len(set(regs)) != C:
            err.append(f"S6 dependent chains use registers {chain_regs}; expected {C} distinct, fixed per chain")
    if show:
        for _, op, a in L:
            print(f"   {op:7s} {a}")
    print(f"STATIC binary={binary.split('/')[-1]} instr={len(L)}/{expect_n} loads={len(loads)} "
          f"jcc={len(jumps)} cmov={cnt('cmovb')}/{cnt('cmove')}/{cnt('cmovne')} "
          f"alu={len(alu_regs)} regs={len(set(alu_regs))} signature={sig}")
    for e in err:
        print("  FAIL", e)
    print("STATIC", "PASS" if not err else "FAIL")
    return 0 if not err else 1


if __name__ == "__main__":
    sys.exit(main())
