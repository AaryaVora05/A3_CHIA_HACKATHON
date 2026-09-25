/*
 * probe.c V1.1 -- semantic-knob workload backend for Pin 3.22 -> ChampSim.
 * Implements docs/V1.1_generator_spec.md (approved). All knobs are compile-time
 * (-D, set by run_probe.py); every threshold/mask reaches the measured loop only
 * as an immediate inside inline asm, so the compiler cannot specialise the
 * code on knob values: the loop's instruction sequence is identical for every
 * value of WSS_KB, STRIDE_LINES, RANDOMNESS, REUSE, BR_PATTERN, LOAD_BR_DEP,
 * and (mnemonics) DEPENDENT.
 *
 * Build:  gcc -O2 -static -fno-pie -no-pie -D... probe.c -o probe
 * Run:    ./probe                      (loops "forever"; the tracer stops it)
 *         ./probe --iters N            (finite run + self-check)
 *         ./probe --dry-run            (initialisation only: used to measure the Pin skip)
 *         ./probe --print-config | --dump-addrs F N | --dump-branches F N | --check-cycle
 *
 * Backend macros (semantic.py computes them):
 *   NBRANCH 0..64      BR_PATTERN 0 const,1 alt,2 periodic,3 random
 *   BR_PERIOD 3..4096 (periodic)          BR_TAKEN (0,1) (random)
 *   LOAD_BR_DEP 0/1    WSS_KB pow2 4..65536   STRIDE_LINES pow2
 *   RANDOMNESS [0,1]   REUSE [0,0.9]      DEPENDENT 0/1   NCHAINS 1,2,4,8
 *   ALU_OPS 0..128 (multiple of DEP_DIST) DEP_DIST 1..8   ALU_XMM 1 (default) / 0
 *   SEED
 */
#define _GNU_SOURCE
#include <inttypes.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#if defined(__PIE__) || defined(__pie__)
#error "build with -fno-pie -no-pie (absolute addressing of static arrays)"
#endif
#if !defined(__x86_64__)
#error "x86-64 only"
#endif

/* ---------------- knobs (defaults = neutral probe) ---------------- */
#ifndef NBRANCH
#define NBRANCH 0
#endif
#ifndef BR_PATTERN
#define BR_PATTERN 0
#endif
#ifndef BR_PERIOD
#define BR_PERIOD 4
#endif
#ifndef BR_TAKEN
#define BR_TAKEN 0.5
#endif
#ifndef LOAD_BR_DEP
#define LOAD_BR_DEP 0
#endif
#ifndef WSS_KB
#define WSS_KB 1024
#endif
#ifndef STRIDE_LINES
#define STRIDE_LINES 1
#endif
#ifndef RANDOMNESS
#define RANDOMNESS 0.0
#endif
#ifndef REUSE
#define REUSE 0.0
#endif
#ifndef DEPENDENT
#define DEPENDENT 0
#endif
#ifndef NCHAINS
#define NCHAINS 1
#endif
#ifndef ALU_OPS
#define ALU_OPS 0
#endif
#ifndef DEP_DIST
#define DEP_DIST 1
#endif
#ifndef ALU_XMM
#define ALU_XMM 1
#endif
#ifndef SEED
#define SEED 1
#endif

/* ---------------- structural validation (compile time) ---------------- */
#if !(NCHAINS == 1 || NCHAINS == 2 || NCHAINS == 4 || NCHAINS == 8)
#error "NCHAINS must be 1, 2, 4 or 8"
#endif
#if NBRANCH < 0 || NBRANCH > 64
#error "NBRANCH must be 0..64 (semantic max is 8)"
#endif
#if BR_PATTERN < 0 || BR_PATTERN > 3
#error "BR_PATTERN must be 0..3"
#endif
#if BR_PATTERN == 2 && (BR_PERIOD < 3 || BR_PERIOD > 4096)
#error "BR_PERIOD must be 3..4096 (period 2 is BR_PATTERN=1, alternating)"
#endif
#if DEP_DIST < 1 || DEP_DIST > 8
#error "DEP_DIST must be 1..8"
#endif
#if ALU_OPS < 0 || ALU_OPS > 128 || (ALU_OPS % DEP_DIST) != 0
#error "ALU_OPS must be 0..128 and a multiple of DEP_DIST"
#endif
#if (WSS_KB & (WSS_KB - 1)) || WSS_KB < 4 || WSS_KB > 65536
#error "WSS_KB must be a power of two in 4..65536"
#endif
#if (STRIDE_LINES & (STRIDE_LINES - 1)) || STRIDE_LINES < 1
#error "STRIDE_LINES must be a power of two"
#endif
#if (WSS_KB * 16 / STRIDE_LINES) < 2 * NCHAINS
#error "need at least 2*NCHAINS slots (WSS_KB*16/STRIDE_LINES)"
#endif
#if LOAD_BR_DEP != 0 && LOAD_BR_DEP != 1
#error "LOAD_BR_DEP must be 0 or 1"
#endif
#if DEPENDENT != 0 && DEPENDENT != 1
#error "DEPENDENT must be 0 or 1"
#endif

/* ---------------- derived constants ---------------- */
#define ROWS        (8 / NCHAINS)                      /* rows per iteration */
#define LINE        64ull
#define SPAN        ((uint64_t)WSS_KB * 1024ull)       /* main span bytes */
#define SLOTS       (SPAN / (LINE * STRIDE_LINES))     /* N */
#define NB          (__builtin_ctzll(SLOTS))            /* log2 N */
#define SL          (__builtin_ctzll((uint64_t)STRIDE_LINES))
#define STRIDEB     (LINE * STRIDE_LINES)
#define SPANMASK    (SPAN - 1)
#define MAX_SPAN    (64ull << 20)
#define HOT_BYTES   4096ull
/* thresholds: an event happens iff u32 < T.  1.0 maps to 2^32-1 (misses one
 * value in 2^32); 0 maps to 0 (never). */
#define THR(x)      ((uint32_t)((x) >= 1.0 ? 4294967295.0 : (double)(x) * 4294967296.0 + 0.5))
#define TR          THR(RANDOMNESS)
#define TU          THR(REUSE)
/* generator constants */
#define LA          0x4C957F2Du    /* 64-bit LCG multiplier (imm32); low 32 bits form a full-period LCG */
#define LC          0x55FE3E61u    /* odd increment */
#define MB          0x2545F491u    /* target hash multiplier */
#define KW          0x9E3779B9u    /* Weyl constant for row-level reuse decisions */
#define SEEDOFF     ((uint32_t)(((uint64_t)(SEED) * 0x9E3779B97F4A7C15ull) >> 40))  /* < 2^24 */
/* ALU op immediate for GPR fallback */
#define ALU_IMM     0x5A5A5A5A

/* branch generator (identical instruction shape for all patterns):
 *   per branch:    taken <=> (x + (lbd ? load>>63 : 0)) < T ;  x = AB*x + CB
 *   per iteration: x = AI*x + CI                        (only if NBRANCH > 0) */
#define LCG_A 1664525u
#define LCG_C 1013904223u
#if BR_PATTERN == 3
#define BR_AB LCG_A
#define BR_CB LCG_C
#define BR_CI 0u
#define BR_T  THR(BR_TAKEN)
#else
#define BR_AB 1u
#define BR_CB 0u
#if BR_PATTERN == 0
#define BR_CI 0u
#define BR_T  0u
#elif BR_PATTERN == 1
#define BR_CI 0x80000000u
#define BR_T  0x80000000u
#else
#define BR_CI ((uint32_t)((0x100000000ull + (BR_PERIOD) - 1) / (BR_PERIOD)))
#define BR_T  BR_CI
#endif
#endif
#define BR_AI 1u

#define STR_(x) #x
#define STR(x) STR_(x)

static const char *const PAT_NAMES[] = {"constant", "alternating", "periodic", "random"};

/* ---------------- storage: span, gap, hot region (disjoint) ---------------- */
static uint8_t g_span[MAX_SPAN] __attribute__((aligned(4096)));
static uint8_t g_gap[1 << 16] __attribute__((aligned(4096), used));
static uint8_t g_hot[HOT_BYTES] __attribute__((aligned(4096)));

static void die(const char *m) { fprintf(stderr, "probe: error: %s\n", m); exit(2); }

/* runtime validation of the floating knobs (cannot be #if-checked) */
static void validate_float_knobs(void) {
    if (!(RANDOMNESS >= 0.0 && RANDOMNESS <= 1.0)) die("RANDOMNESS must be in [0,1]");
    if (!(REUSE >= 0.0 && REUSE <= 0.9)) die("REUSE must be in [0,0.9]");
#if BR_PATTERN == 3
    if (!(BR_TAKEN > 0.0 && BR_TAKEN < 1.0)) die("BR_TAKEN must be in (0,1) for random");
#endif
}

/* ---------------- init-time PRNG (splitmix64 + xoshiro256**) ---------------- */
typedef struct { uint64_t s[4]; } rng_t;
static uint64_t splitmix64(uint64_t *st) {
    uint64_t z = (*st += 0x9E3779B97F4A7C15ull);
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ull;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBull;
    return z ^ (z >> 31);
}
static void rng_init(rng_t *r, uint64_t stream) {
    uint64_t st = (uint64_t)(SEED) ^ (stream * 0xD1B54A32D192ED03ull);
    for (int i = 0; i < 4; i++) r->s[i] = splitmix64(&st);
}
static inline uint64_t rotl(uint64_t x, int k) { return (x << k) | (x >> (64 - k)); }
static uint64_t rng_next(rng_t *r) {
    uint64_t *s = r->s, res = rotl(s[1] * 5, 7) * 9, t = s[1] << 17;
    s[2] ^= s[0]; s[3] ^= s[1]; s[1] ^= s[2]; s[0] ^= s[3]; s[2] ^= t; s[3] = rotl(s[3], 45);
    return res;
}
__attribute__((unused)) static double rng_unit(rng_t *r) { return (double)(rng_next(r) >> 11) * 0x1.0p-53; }
__attribute__((unused)) static uint64_t rng_below(rng_t *r, uint64_t n) {
    uint64_t lim = UINT64_MAX - UINT64_MAX % n, x;
    do x = rng_next(r); while (x >= lim);
    return x % n;
}
enum { STREAM_CYCLE = 1, STREAM_ADDR = 2, STREAM_BRANCH = 3 };

/* ========================================================================
 * MEASURED KERNEL
 * One iteration = ROWS rows; a row = row header + NCHAINS accesses, followed
 * by its share of the branches and ALU ops; then the per-iteration branch
 * state step (if NBRANCH>0) and the loop latch.
 * Static instructions / iteration = 72/C + 128 + 7B + 2[B>0] + A + 2.
 * ====================================================================== */

/* Row header (9 instr): Weyl reuse decision over the iteration counter.
 *   key = ROWS*i + row + SEEDOFF (mod 2^32); w = key*KW (mod 2^32)
 *   hot <=> w < TU ;  h = hot ? g_hot + 64*(w>>26) : 0                     */
#define ROW_HDR(R) do {                                                          \
        uint64_t m_;                                                             \
        __asm__ volatile("lea %c[d](,%[i],%c[sc]), %k[h]\n\t"                      \
                         "imul $" STR(KW) ", %k[h], %k[h]\n\t"                    \
                         "cmp %[tu], %k[h]\n\t"                                   \
                         "mov $0, %[m]\n\t"   /* m reads no stale register */    \
                         "sbb $0, %[m]\n\t"   /* m = hot ? -1 : 0 */              \
                         "shr $26, %k[h]\n\t"                                     \
                         "shl $6, %k[h]\n\t"                                      \
                         "add %[hb], %[h]\n\t"                                    \
                         "and %[m], %[h]"                                         \
                         : [h] "=&r"(h), [m] "=&r"(m_)                            \
                         : [i] "r"(i), [d] "i"((R) + SEEDOFF), [sc] "i"(ROWS),    \
                           [tu] "i"((int32_t)TU), [hb] "i"(g_hot)                 \
                         : "cc");                                                 \
    } while (0)

/* Generator (12 instr), identical in both modes. `o` is the chain offset
 * (independent) or a persistent private scratch (dependent; results unused). */
#define GEN(o)                                                                   \
    "imul $" STR(LA) ", %[s], %[t]\n\t"        /* t = s*LA + LC   (candidate)   */ \
    "add $" STR(LC) ", %[t]\n\t"                                                  \
    "imul $" STR(MB) ", %[t], %[q]\n\t"        /* q = random slot offset        */ \
    "shr %[shn], %[q]\n\t"                                                        \
    "shl %[shs], %[q]\n\t"                                                        \
    "lea %c[sb](%[" o "]), %[p]\n\t"           /* p = sequential next offset    */ \
    "and %[msk], %[p]\n\t"                                                        \
    "cmp %[tr], %k[t]\n\t"                     /* run start <=> low32(t) < TR   */ \
    "cmovb %[q], %[p]\n\t"                                                        \
    "test %[h], %[h]\n\t"                      /* ZF=1 <=> row not hot          */ \
    "cmove %[p], %[" o "]\n\t"                 /* advance stream only if not hot */ \
    "cmove %[t], %[s]\n\t"                     /* advance RNG only if not hot   */
#define GEN_IMM                                                                  \
    [shn] "i"(64 - NB), [shs] "i"(6 + SL), [sb] "i"(STRIDEB),                    \
    [msk] "i"((int32_t)SPANMASK), [tr] "i"((int32_t)TR)

#if DEPENDENT
/* tail: address = hot ? hot line : v_k ; load ; v_k = hot ? v_k : loaded */
#define ACCESS(chain)                                                            \
    __asm__ volatile(GEN("p")                                                    \
                     "lea (%[v]), %[q]\n\t"                                       \
                     "cmovne %[h], %[q]\n\t"                                      \
                     "mov (%[q]), %[q]\n\t"                                       \
                     "cmove %[q], %[v]"                                           \
                     : [s] "+r"(s), [p] "+r"(dscr), [v] "+r"(chain),              \
                       [t] "=&r"(t_), [q] "=&r"(x)                                \
                     : [h] "r"(h), GEN_IMM : "cc", "memory")
#else
/* tail: address = hot ? hot line : span + off ; load ; equaliser cmov */
#define ACCESS(chain)                                                            \
    __asm__ volatile(GEN("o")                                                    \
                     "lea %c[base](%[o]), %[q]\n\t"                               \
                     "cmovne %[h], %[q]\n\t"                                      \
                     "mov (%[q]), %[q]\n\t"                                       \
                     "cmove %[q], %[q]"                                           \
                     : [s] "+r"(s), [o] "+r"(chain),                              \
                       [t] "=&r"(t_), [q] "=&r"(x), [p] "=&r"(p_)                 \
                     : [h] "r"(h), [base] "i"(g_span), GEN_IMM : "cc", "memory")
#endif

/* Branch (7 instr): src(3) cmp jb(->next instr) imul add.
 * lbd=1 folds (loaded>>63), a runtime zero, into the compared value. */
#if LOAD_BR_DEP
#define BSRC "mov %[x], %[bt]\n\tshr $63, %[bt]\n\tadd %[xb], %[bt]\n\t"
#else
#define BSRC "mov $0, %k[bt]\n\tshr $63, %[bt]\n\tadd %[xb], %[bt]\n\t"
#endif
#define BRANCH()                                                                 \
    __asm__ volatile(BSRC "cmp %[T], %k[bt]\n\tjb 1f\n1:\n\t"                     \
                     "imul %[A], %k[xb], %k[xb]\n\tadd %[C], %k[xb]"              \
                     : [xb] "+r"(xb), [bt] "=&r"(bt_)                             \
                     : [x] "r"(x), [T] "i"((int32_t)BR_T), [A] "i"((int32_t)BR_AB), \
                       [C] "i"((int32_t)BR_CB) : "cc")
#define BRANCH_ITER_STEP()                                                       \
    __asm__ volatile("imul %[A], %k[xb], %k[xb]\n\tadd %[C], %k[xb]"             \
                     : [xb] "+r"(xb) : [A] "i"((int32_t)BR_AI), [C] "i"((int32_t)BR_CI) : "cc")

/* ALU op: chain register ^= constant. Op q uses chain q mod D => op q depends on op q-D. */
#if ALU_XMM
typedef long long v2di __attribute__((vector_size(16)));
#define ALU(reg) __asm__ volatile("pxor %[k], %[a]" : [a] "+x"(reg) : [k] "x"(kx))
#else
#define ALU(reg) __asm__ volatile("xor $" STR(ALU_IMM) ", %q[a]" : [a] "+r"(reg) :: "cc")
#endif
#define ALU_K(k) do { switch (k) {                                               \
        case 0: ALU(a0); break; case 1: ALU(a1); break; case 2: ALU(a2); break;  \
        case 3: ALU(a3); break; case 4: ALU(a4); break; case 5: ALU(a5); break;  \
        case 6: ALU(a6); break; case 7: ALU(a7); break; } } while (0)
#define ACCESS_K(k) do { switch (k) {                                           \
        case 0: ACCESS(c0); break; case 1: ACCESS(c1); break;                    \
        case 2: ACCESS(c2); break; case 3: ACCESS(c3); break;                    \
        case 4: ACCESS(c4); break; case 5: ACCESS(c5); break;                    \
        case 6: ACCESS(c6); break; case 7: ACCESS(c7); break; } } while (0)

#define ROW(R) {                                                                 \
        uint64_t h, x;                                                           \
        ROW_HDR(R);                                                              \
        _Pragma("GCC unroll 8") for (int k = 0; k < NCHAINS; k++) {              \
            uint64_t t_, p_; (void)p_; ACCESS_K(k);                         \
        }                                                                        \
        _Pragma("GCC unroll 64")                                                 \
        for (int j = (R) * NBRANCH / ROWS; j < ((R) + 1) * NBRANCH / ROWS; j++) { \
            uint64_t bt_; BRANCH();                                              \
        }                                                                        \
        _Pragma("GCC unroll 128")                                                \
        for (int q = (R) * ALU_OPS / ROWS; q < ((R) + 1) * ALU_OPS / ROWS; q++)  \
            ALU_K(q % DEP_DIST);                                                 \
        __asm__ volatile("" :: "r"(x));                                          \
    }

typedef struct {
    uint64_t s;          /* address RNG */
    uint64_t chain[8];   /* independent: span offsets; dependent: absolute line addresses */
    uint64_t dscr;       /* dependent generator scratch */
    uint64_t xb;         /* branch state (32-bit value) */
    uint64_t alu[8];     /* ALU chain values (low 64 bits) */
} probe_state_t;

__attribute__((noinline, noipa, used)) void probe_roi_begin(void) { __asm__ volatile(""); }
__attribute__((noinline, noipa, used)) void probe_roi_end(void) { __asm__ volatile(""); }

__attribute__((noinline, noipa)) static void run_probe(long iters, probe_state_t *st) {
    probe_roi_begin();
    /* Loop-carried GPRs are pinned: r15 = address RNG, r14 = branch state,
     * r13 = dependent generator scratch, chains -> r12 r11 r10 r9 r8 rdi rsi rbx.
     * rax rcx rdx rbp (+ r13 in independent mode) remain for the loop counter
     * and per-access temporaries. Only chains < NCHAINS are ever live. */
    register uint64_t s __asm__("r15") = st->s;
    register uint64_t xb __asm__("r14") = st->xb;
    register uint64_t dscr __asm__("r13") = st->dscr;
    register uint64_t c0 __asm__("r12"), c1 __asm__("r11"), c2 __asm__("r10"), c3 __asm__("r9");
    register uint64_t c4 __asm__("r8"), c5 __asm__("rdi"), c6 __asm__("rsi"), c7 __asm__("rbx");
    c0 = st->chain[0];
    if (NCHAINS > 1) c1 = st->chain[1];
    if (NCHAINS > 2) { c2 = st->chain[2]; c3 = st->chain[3]; }
    if (NCHAINS > 4) { c4 = st->chain[4]; c5 = st->chain[5]; c6 = st->chain[6]; c7 = st->chain[7]; }
#if ALU_XMM
    /* XMM chains are pinned (GNU local register variables, honoured at asm
     * operands): without pinning GCC may rename a chain mid-loop and insert
     * movdqa copies, which check_static.py rejects. */
    register v2di kx __asm__("xmm0") = (v2di){ALU_IMM, 0};
    register v2di a0 __asm__("xmm1") = (v2di){(long long)st->alu[0]};
    register v2di a1 __asm__("xmm2") = (v2di){(long long)st->alu[1]};
    register v2di a2 __asm__("xmm3") = (v2di){(long long)st->alu[2]};
    register v2di a3 __asm__("xmm4") = (v2di){(long long)st->alu[3]};
    register v2di a4 __asm__("xmm5") = (v2di){(long long)st->alu[4]};
    register v2di a5 __asm__("xmm6") = (v2di){(long long)st->alu[5]};
    register v2di a6 __asm__("xmm7") = (v2di){(long long)st->alu[6]};
    register v2di a7 __asm__("xmm8") = (v2di){(long long)st->alu[7]};
#else
    uint64_t a0 = st->alu[0], a1 = st->alu[1], a2 = st->alu[2], a3 = st->alu[3];
    uint64_t a4 = st->alu[4], a5 = st->alu[5], a6 = st->alu[6], a7 = st->alu[7];
#endif
    long i = -iters;
    do {
        ROW(0)
#if ROWS >= 2
        ROW(1)
#endif
#if ROWS >= 4
        ROW(2) ROW(3)
#endif
#if ROWS >= 8
        ROW(4) ROW(5) ROW(6) ROW(7)
#endif
#if NBRANCH > 0
        BRANCH_ITER_STEP();
#endif
        i++;
    } while (i != 0);
    st->s = s; st->xb = xb;
    if (DEPENDENT) st->dscr = dscr;
    st->chain[0] = c0;
    if (NCHAINS > 1) st->chain[1] = c1;
    if (NCHAINS > 2) { st->chain[2] = c2; st->chain[3] = c3; }
    if (NCHAINS > 4) { st->chain[4] = c4; st->chain[5] = c5; st->chain[6] = c6; st->chain[7] = c7; }
#if ALU_XMM
    st->alu[0] = (uint64_t)a0[0];
    if (DEP_DIST > 1) st->alu[1] = (uint64_t)a1[0];
    if (DEP_DIST > 2) st->alu[2] = (uint64_t)a2[0];
    if (DEP_DIST > 3) st->alu[3] = (uint64_t)a3[0];
    if (DEP_DIST > 4) st->alu[4] = (uint64_t)a4[0];
    if (DEP_DIST > 5) st->alu[5] = (uint64_t)a5[0];
    if (DEP_DIST > 6) st->alu[6] = (uint64_t)a6[0];
    if (DEP_DIST > 7) st->alu[7] = (uint64_t)a7[0];
#else
    st->alu[0] = a0; st->alu[1] = a1; st->alu[2] = a2; st->alu[3] = a3;
    st->alu[4] = a4; st->alu[5] = a5; st->alu[6] = a6; st->alu[7] = a7;
#endif
    probe_roi_end();
}

/* ========================================================================
 * INITIALISATION (outside the measured loop; skipped by Pin -s)
 * ====================================================================== */
static uint64_t g_cycle_runs __attribute__((unused));          /* number of runs in the dependent cycle */

__attribute__((unused)) static uint64_t slot_addr(uint64_t slot) { return (uint64_t)(uintptr_t)(g_span + slot * STRIDEB); }

/* Dependent mode: one cycle over all N slots. Walk slots in address order,
 * start a new run at each slot with probability RANDOMNESS, shuffle the runs,
 * concatenate, close the loop. Each slot line's first qword = successor address. */
#if DEPENDENT
static void build_cycle(probe_state_t *st) {
    const uint64_t N = SLOTS;
    uint32_t *start = malloc(N * sizeof *start), *len = malloc(N * sizeof *len);
    if (!start || !len) die("out of memory");
    rng_t r; rng_init(&r, STREAM_CYCLE);
    uint64_t nr = 0;
    start[0] = 0; len[0] = 1;
    for (uint64_t j = 1; j < N; j++) {
        if (rng_unit(&r) < (double)RANDOMNESS) { nr++; start[nr] = (uint32_t)j; len[nr] = 1; }
        else len[nr]++;
    }
    nr++;
    for (uint64_t k = nr - 1; k > 0; k--) {             /* shuffle runs */
        uint64_t j = rng_below(&r, k + 1);
        uint32_t a = start[k], b = len[k]; start[k] = start[j]; len[k] = len[j]; start[j] = a; len[j] = b;
    }
    uint32_t *order = malloc(N * sizeof *order);
    if (!order) die("out of memory");
    uint64_t n = 0;
    for (uint64_t k = 0; k < nr; k++)
        for (uint32_t m = 0; m < len[k]; m++) order[n++] = start[k] + m;
    for (uint64_t k = 0; k < N; k++)
        *(volatile uint64_t *)(g_span + (uint64_t)order[k] * STRIDEB) = slot_addr(order[(k + 1) % N]);
    for (int c = 0; c < 8; c++) st->chain[c] = c < NCHAINS ? slot_addr(order[(uint64_t)c * (N / NCHAINS)]) : 0;
    g_cycle_runs = nr;
    free(start); free(len); free(order);
}
#endif

static void init_probe(probe_state_t *st) {
    memset(st, 0, sizeof *st);
    for (uint64_t k = 0; k < HOT_BYTES / LINE; k++) *(volatile uint64_t *)(g_hot + k * LINE) = 0;
#if DEPENDENT
    build_cycle(st);
#else
    for (uint64_t k = 0; k < SLOTS; k++) *(volatile uint64_t *)(g_span + k * STRIDEB) = 0;
    for (int c = 0; c < 8; c++) st->chain[c] = c < NCHAINS ? (uint64_t)c * (SPAN / NCHAINS) : 0;
#endif
    rng_t r; rng_init(&r, STREAM_ADDR);
    st->s = rng_next(&r);
#if BR_PATTERN == 3
    rng_t b; rng_init(&b, STREAM_BRANCH);
    st->xb = (uint32_t)rng_next(&b);
#endif
    for (int k = 0; k < 8; k++) st->alu[k] = (uint64_t)(k + 1);
}

/* ========================================================================
 * REPLICA: the kernel's exact semantics in C. Used for dumps and the
 * self-check (which proves the asm executed exactly this).
 * ====================================================================== */
static int lt32(uint32_t a, uint32_t b) { return a < b; }
typedef struct { FILE *fa; uint64_t na, ca; FILE *fb; uint64_t nb, cb; } dump_t;

static void replica(long iters, probe_state_t *st, dump_t *dp) {
    uint64_t s = st->s, dscr = st->dscr;
    uint32_t xb = (uint32_t)st->xb;
    uint64_t alu_ops_per_chain = 0;
    for (long i = -iters; i != 0; i++) {
        for (int R = 0; R < ROWS; R++) {
            uint32_t key = (uint32_t)((uint64_t)i * ROWS + (uint64_t)R + SEEDOFF);
            uint32_t w = key * KW;
            uint64_t h = (w < TU) ? (uint64_t)(uintptr_t)g_hot + ((uint64_t)(w >> 26) << 6) : 0;
            uint64_t x = 0;
            for (int k = 0; k < NCHAINS; k++) {
                uint64_t t = s * (uint64_t)LA + (uint64_t)LC;
                uint64_t q = ((t * (uint64_t)MB) >> (64 - NB)) << (6 + SL);
#if DEPENDENT
                uint64_t p = (dscr + STRIDEB) & SPANMASK;
                if ((uint32_t)t < TR) p = q;
                dscr = p;                                 /* o and p are the same register */
                if (!h) s = t;
                uint64_t addr = h ? h : st->chain[k];
                uint64_t v = *(volatile uint64_t *)(uintptr_t)addr;
                if (!h) st->chain[k] = v;
#else
                uint64_t p = (st->chain[k] + STRIDEB) & SPANMASK;
                if ((uint32_t)t < TR) p = q;
                if (!h) { st->chain[k] = p; s = t; }
                uint64_t addr = h ? h : (uint64_t)(uintptr_t)g_span + st->chain[k];
                uint64_t v = *(volatile uint64_t *)(uintptr_t)addr;
#endif
                x = v;
                if (dp && dp->fa && dp->ca < dp->na) {
                    fprintf(dp->fa, "%d %d 0x%" PRIx64 "\n", h != 0, k, addr); dp->ca++;
                }
            }
            for (int j = R * NBRANCH / ROWS; j < (R + 1) * NBRANCH / ROWS; j++) {
                uint64_t bt = (LOAD_BR_DEP ? (x >> 63) : 0) + xb;
                int taken = lt32((uint32_t)bt, (uint32_t)BR_T);
                if (dp && dp->fb && dp->cb < dp->nb) { fputc(taken ? '1' : '0', dp->fb); dp->cb++; }
                xb = (uint32_t)BR_AB * xb + (uint32_t)BR_CB;
            }
        }
        if (NBRANCH > 0) xb = (uint32_t)BR_AI * xb + (uint32_t)BR_CI;
        alu_ops_per_chain += ALU_OPS / DEP_DIST;
    }
    st->s = s; st->dscr = dscr; st->xb = xb;
    for (int k = 0; k < DEP_DIST; k++) if (alu_ops_per_chain & 1) st->alu[k] ^= (uint64_t)ALU_IMM;
}

/* ========================================================================
 * Reporting
 * ====================================================================== */
#define STATIC_INSTR_PER_ITER (72 / NCHAINS + 128 + 7 * NBRANCH + (NBRANCH > 0 ? 2 : 0) + ALU_OPS + 2)

static void print_config(void) {
    printf("# probe V1.1 resolved configuration\n");
    printf("knob.branch_frequency=%d\n", NBRANCH);
    printf("knob.branch_pattern=%s%s\n", PAT_NAMES[BR_PATTERN], NBRANCH ? "" : " (inactive)");
    if (BR_PATTERN == 2) printf("knob.branch_pattern_param=%d\n", BR_PERIOD);
    else if (BR_PATTERN == 3) printf("knob.branch_pattern_param=%.6g\n", (double)BR_TAKEN);
    else printf("knob.branch_pattern_param=n/a\n");
    printf("knob.load_branch_dependency=%d%s\n", LOAD_BR_DEP, NBRANCH ? "" : " (inactive)");
    printf("knob.working_set_kb=%d\nknob.stride_lines=%d\n", WSS_KB, STRIDE_LINES);
    printf("knob.randomness=%.6g\nknob.reuse=%.6g\n", (double)RANDOMNESS, (double)REUSE);
    printf("knob.memory_dependency=%s\nknob.independent_chains=%d\n", DEPENDENT ? "dependent" : "independent", NCHAINS);
    printf("knob.alu_ops=%d\nknob.dependency_distance=%d%s\n", ALU_OPS, DEP_DIST, ALU_OPS ? "" : " (inactive)");
    printf("backend.alu_regs=%s\nbackend.seed=%d\n", ALU_XMM ? "xmm" : "gpr", SEED);
    printf("derived.slots=%" PRIu64 "\nderived.rows_per_iter=%d\n", (uint64_t)SLOTS, ROWS);
    printf("derived.thresholds TR=%u TU=%u BR_T=%u\n", (unsigned)TR, (unsigned)TU, (unsigned)BR_T);
    printf("derived.data_loads_per_iter=8\n");
    printf("derived.cond_branches_per_iter=%d\n", NBRANCH + 1);
    printf("derived.static_instr_per_iter=%d\n", STATIC_INSTR_PER_ITER);
    printf("overhead.generator_instr_per_access=%.3g\n", 16.0 + 9.0 / NCHAINS);
    printf("overhead.addrgen_instr_per_iter=%d  (address generation + selection, excluding the 8 loads)\n", 72 / NCHAINS + 120);
    printf("overhead.load_fraction_of_instr=%.4f\n", 8.0 / STATIC_INSTR_PER_ITER);
    double main_frac = 1.0 - (double)TU / 4294967296.0;
    double it_pass = (double)SLOTS / 8.0 / main_frac;   /* N main-stream accesses */
    printf("derived.iters_per_span_pass=%.0f  (N main-stream accesses; %s)\n", it_pass,
           DEPENDENT ? "dependent: exactly one full cycle" : "independent: coverage is probabilistic");
    printf("derived.instr_per_span_pass=%.0f\n", it_pass * STATIC_INSTR_PER_ITER);
    if (!DEPENDENT && RANDOMNESS > 0)
        printf("derived.est_full_coverage_accesses=%.0f  (coupon-collector estimate at r=1; NOT a warmup guarantee)\n",
               (double)SLOTS * log((double)SLOTS));
    if (BR_PATTERN == 2) {
        uint64_t P = BR_PERIOD, Cv = BR_CI, dl = Cv * P - 0x100000000ull;
        if (dl) printf("derived.periodic_exact_iterations=%" PRIu64 "\n", ((Cv + dl - 1) / dl) * P);
        else printf("derived.periodic_exact_iterations=inf\n");
    }
}

static void check_cycle(const probe_state_t *st) {
#if DEPENDENT
    const uint64_t N = SLOTS, base = (uint64_t)(uintptr_t)g_span;
    uint8_t *seen = calloc(N, 1);
    if (!seen) die("out of memory");
    uint64_t a = st->chain[0], steps = 0, jumps = 0, distinct = 0, dup = 0;
    do {
        uint64_t slot = (a - base) / STRIDEB;
        if (slot >= N || (a - base) % STRIDEB) { printf("cycle.error=address outside span\n"); break; }
        if (seen[slot]) dup++; else { seen[slot] = 1; distinct++; }
        uint64_t nx = *(uint64_t *)(uintptr_t)a;
        if (nx != a + STRIDEB) jumps++;
        a = nx; steps++;
    } while (a != st->chain[0] && steps <= N);
    printf("cycle.slots=%" PRIu64 "\ncycle.length=%" PRIu64 "\ncycle.distinct=%" PRIu64 "\ncycle.duplicates=%" PRIu64 "\n",
           N, steps, distinct, dup);
    printf("cycle.run_start_fraction=%.6f  (target %.6g)\ncycle.mean_run_length=%.4f\n",
           (double)jumps / (double)steps, (double)RANDOMNESS, jumps ? (double)steps / (double)jumps : (double)steps);
    printf("cycle.full_coverage=%s\n", (steps == N && distinct == N && dup == 0) ? "PASS" : "FAIL");
    free(seen);
#else
    (void)st;
    printf("cycle.full_coverage=n/a (independent mode: coverage is probabilistic)\n");
#endif
}

int main(int argc, char **argv) {
    long iters = 0; int dry = 0, pc = 0, cc = 0, have_iters = 0;
    dump_t dp = {0};
    for (int k = 1; k < argc; k++) {
        if (!strcmp(argv[k], "--iters") && k + 1 < argc) { iters = atol(argv[++k]); have_iters = 1; }
        else if (!strcmp(argv[k], "--dry-run")) dry = 1;
        else if (!strcmp(argv[k], "--print-config")) pc = 1;
        else if (!strcmp(argv[k], "--check-cycle")) cc = 1;
        else if (!strcmp(argv[k], "--dump-addrs") && k + 2 < argc) {
            dp.fa = fopen(argv[k + 1], "w"); dp.na = strtoull(argv[k + 2], 0, 0); k += 2;
            if (!dp.fa) die("cannot open dump file");
        } else if (!strcmp(argv[k], "--dump-branches") && k + 2 < argc) {
            dp.fb = fopen(argv[k + 1], "w"); dp.nb = strtoull(argv[k + 2], 0, 0); k += 2;
            if (!dp.fb) die("cannot open dump file");
        } else { fprintf(stderr, "probe: unknown argument %s\n", argv[k]); return 2; }
    }
    validate_float_knobs();
    if (pc) { print_config(); return 0; }
    if (have_iters && iters < 1) die("--iters must be >= 1");

    probe_state_t st;
    init_probe(&st);                         /* --- initialisation (Pin skips this) --- */
    if (dry) return 0;
    if (cc) { check_cycle(&st); return 0; }
    if (dp.fa || dp.fb) {
        long n = have_iters ? iters : 1000000;
        probe_state_t r = st; replica(n, &r, &dp);
        if (dp.fa) fclose(dp.fa);
        if (dp.fb) { fputc('\n', dp.fb); fclose(dp.fb); }
        return 0;
    }
    if (!have_iters) iters = 1L << 62;       /* trace mode: the tracer ends the run */

    probe_state_t ref = st;
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    run_probe(iters, &st);                   /* --- measured region --- */
    clock_gettime(CLOCK_MONOTONIC, &t1);

    double ns = ((double)(t1.tv_sec - t0.tv_sec) * 1e9 + (double)(t1.tv_nsec - t0.tv_nsec)) / (double)iters;
    printf("result.native_ns_per_iter=%.3f\nresult.native_ns_per_access=%.3f\n", ns, ns / 8.0);
    replica(iters, &ref, NULL);
    int ok = ref.s == st.s && ref.xb == st.xb && ref.dscr == st.dscr &&
             !memcmp(ref.chain, st.chain, sizeof st.chain) && !memcmp(ref.alu, st.alu, sizeof st.alu);
    printf("result.selfcheck=%s\n", ok ? "PASS" : "FAIL");
    return ok ? 0 : 1;
}
