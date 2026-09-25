import re, struct, subprocess, sys, tempfile, os
REC = struct.Struct("<QBB2B4B2Q4Q")
binary, out, iters, corrupt = sys.argv[1], sys.argv[2], int(sys.argv[3]), len(sys.argv) > 4
dis = subprocess.run(["objdump","-d","--no-show-raw-insn",binary],capture_output=True,text=True).stdout
body = re.search(r"<run_probe>:\n(.*?)\n\n", dis, re.S).group(1)
ins = [(int(m.group(1),16), m.group(2), m.group(3)) for m in (re.match(r"\s*([0-9a-f]+):\s+(\S+)\s*(.*)", l) for l in body.splitlines()) if m]
for a,op,args in ins:
    if op=="jne" and int(args.split()[0],16)<a: head,latch=int(args.split()[0],16),a
loop=[x for x in ins if head<=x[0]<=latch]; pro=[x for x in ins if x[0]<head]
ids={}
def rid(n):
    n=n.lstrip('%'); n=re.sub(r"^e(..)$",r"r\1",n); n=re.sub(r"(r\d+)[dwb]$",r"\1",n)
    return ids.setdefault(n, len(ids)+3)
td=tempfile.mkdtemp()
subprocess.run([binary,"--iters",str(iters),"--dump-addrs",f"{td}/a",str(iters*8),"--dump-branches",f"{td}/b",str(10**9)],check=True)
addrs=[int(l.split()[2],16) for l in open(f"{td}/a")]; br=open(f"{td}/b").read().strip()
ai=bi=0; recs=[]
def emit(ip, isbr=0, tk=0, dst=(), src=(), smem=()):
    d=list(dst)[:2]+[0]*(2-len(dst[:2])); s=list(src)[:4]+[0]*(4-len(src[:4])); m=list(smem)[:4]+[0]*(4-len(smem[:4]))
    recs.append(REC.pack(ip,isbr,tk,*d,*s,0,0,*m))
for a,op,args in pro[-5:]: emit(a)
for it in range(iters):
    pc=0
    for a,op,args in loop:
        regs=re.findall(r"%\w+",args)
        if op=="pxor":
            x=rid(regs[1]); k=rid(regs[0])
            if corrupt and pc==0 and it==3: x=rid("xmm15")
            emit(a,dst=[x],src=[x,k]); pc+=1
        elif op=="mov" and "(" in args and not args.startswith("0x") :
            emit(a,dst=[rid(regs[-1])],src=[rid(regs[0])],smem=[addrs[ai]]); ai+=1
        elif op=="jb":
            emit(a,1,int(br[bi]),dst=[26],src=[26,25]); bi+=1
        elif op=="jne":
            emit(a,1,1 if it<iters-1 else 0,dst=[26],src=[26,25])
        else:
            emit(a,dst=[rid(regs[-1])] if regs else [],src=[rid(r) for r in regs])
open(out,"wb").write(b"".join(recs))
