# Active Microarchitectural Bottleneck Diagnosis through Agent-Generated Microbenchmarks

**Authors:** Aarya Vora, Daksh Sawke  
**Artifact Repository:** CHIA A^3 Workshop Hackathon
**Date:** 24 September 2026  

---

## Abstract

Identifying the dominant microarchitectural bottleneck of a workload usually requires hardware knowledge and targeted experiments beyond passive counter inspection. We present an active diagnosis fram[...]

We evaluate against ground truth defined by counterfactual machine relaxations on 50 SPEC CPU2017 traces from the DPC-3 ChampSim trace set. The framework achieves 72.0% accuracy over the full corpus, [...]

---

## 1. Introduction & Motivation

Understanding why a workload performs poorly on a given processor is an important part of microarchitectural analysis. Architects use hardware performance counters to observe events such as branch mis[...]

Several approaches help structure this diagnosis. Counter-based techniques, such as Top-Down Microarchitecture Analysis (TMA), organize hardware events into increasingly detailed bottleneck categories[...]

This motivates a different form of automated diagnosis. Instead of only interpreting a fixed set of measurements, a diagnostic system can actively collect new evidence. When several explanations remai[...]

---
## 2. Experimental Results

### 2.1 Primary Benchmark Results (All 50 Workloads)

The complete benchmark evaluation using `Gemini 2.5 Pro` across all 50 workloads is summarized below:

| # | Workload | Ground Truth Oracle | Oracle Speedup | Surrogate FOM | Quality Tier | Gate Status | CHIA Diagnosis | Conf. | Match Status | Diagnostic Time |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| 1 | `403.gcc-17B` | **BRANCH** | +27.0% | **78.6%** | HIGH | **PASS** | **BRANCH** | 90% | **MATCH** | 98.5s |
| 2 | `410.bwaves-945B` | **BRANCH** | +9.3% | **95.5%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 36.9s |
| 3 | `429.mcf-184B` | **CACHE** | +91.4% | **58.8%** | MODERATE | **PASS** | **CACHE** | 95% | **MATCH** | 121.1s |
| 4 | `429.mcf-22B` | **CACHE** | +125.0% | **82.8%** | HIGH | **PASS** | **CACHE** | 95% | **MATCH** | 98.1s |
| 5 | `429.mcf-51B` | **CACHE** | +148.7% | **61.9%** | MODERATE | **PASS** | **CACHE** | 95% | **MATCH** | 121.7s |
| 6 | `433.milc-127B` | **DRAM_LAT** | +52.7% | **63.7%** | MODERATE | **PASS** | **CACHE** | 90% | **Mismatch** | 99.8s |
| 7 | `433.milc-274B` | **DRAM_LAT** | +65.1% | **11.5%** | COARSE | **REJECT** | **CACHE** | 95% | **Mismatch** | 119.6s |
| 8 | `433.milc-337B` | **DRAM_LAT** | +67.0% | **38.3%** | MODERATE | **PASS** | **CACHE** | 95% | **Mismatch** | 99.0s |
| 9 | `437.leslie3d-134B` | **DRAM_LAT** | +37.2% | **9.2%** | COARSE | **REJECT** | **CACHE** | 90% | **Mismatch** | 118.5s |
| 10 | `437.leslie3d-232B` | **CACHE** | +41.8% | **0.0%** | COARSE | **REJECT** | **CACHE** | 95% | **MATCH** | 111.3s |
| 11 | `437.leslie3d-273B` | **CACHE** | +57.4% | **88.0%** | HIGH | **PASS** | **CACHE** | 85% | **MATCH** | 105.5s |
| 12 | `445.gobmk-17B` | **BRANCH** | +62.8% | **71.0%** | MODERATE | **PASS** | **BRANCH** | 95% | **MATCH** | 39.2s |
| 13 | `445.gobmk-30B` | **BRANCH** | +25.6% | **69.9%** | MODERATE | **PASS** | **BRANCH** | 95% | **MATCH** | 107.8s |
| 14 | `450.soplex-247B` | **CACHE** | +277.8% | **79.1%** | HIGH | **PASS** | **CACHE** | 90% | **MATCH** | 91.5s |
| 15 | `450.soplex-92B` | **CACHE** | +225.4% | **65.8%** | MODERATE | **PASS** | **CACHE** | 98% | **MATCH** | 101.5s |
| 16 | `458.sjeng-1088B` | **BRANCH** | +52.9% | **63.3%** | MODERATE | **PASS** | **BRANCH** | 97% | **MATCH** | 38.0s |
| 17 | `458.sjeng-31B` | **BRANCH** | +42.4% | **64.6%** | MODERATE | **PASS** | **BRANCH** | 95% | **MATCH** | 39.0s |
| 18 | `462.libquantum-1343B` | **CACHE** | +80.5% | **76.4%** | HIGH | **PASS** | **CACHE** | 90% | **MATCH** | 85.7s |
| 19 | `470.lbm-1274B` | **DRAM_LAT** | +59.0% | **0.0%** | COARSE | **REJECT** | **CACHE** | 95% | **Mismatch** | 104.5s |
| 20 | `471.omnetpp-188B` | **CACHE** | +149.7% | **81.0%** | HIGH | **PASS** | **CACHE** | 95% | **MATCH** | 89.0s |
| 21 | `473.astar-153B` | **BRANCH** | +55.0% | **79.7%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 54.5s |
| 22 | `473.astar-42B` | **BRANCH** | +32.7% | **0.0%** | COARSE | **REJECT** | **CACHE** | 99% | **Mismatch** | 90.4s |
| 23 | `482.sphinx3-1395B` | **CACHE** | +232.1% | **60.8%** | MODERATE | **PASS** | **CACHE** | 95% | **MATCH** | 99.6s |
| 24 | `482.sphinx3-417B` | **CACHE** | +249.8% | **18.5%** | COARSE | **REJECT** | **CACHE** | 98% | **MATCH** | 97.2s |
| 25 | `483.xalancbmk-716B` | **BRANCH** | +14.8% | **88.2%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 54.9s |
| 26 | `600.perlbench_s-210B` | **BRANCH** | +4.3% | **79.0%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 42.3s |
| 27 | `602.gcc_s-734B` | **CACHE** | +158.2% | **4.8%** | COARSE | **REJECT** | **CACHE** | 95% | **MATCH** | 89.9s |
| 28 | `603.bwaves_s-3699B` | **BRANCH** | +40.2% | **93.1%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 52.3s |
| 29 | `605.mcf_s-472B` | **CACHE** | +130.5% | **82.6%** | HIGH | **PASS** | **CACHE** | 95% | **MATCH** | 112.0s |
| 30 | `605.mcf_s-484B` | **CACHE** | +79.8% | **88.4%** | HIGH | **PASS** | **CACHE** | 98% | **MATCH** | 89.3s |
| 31 | `605.mcf_s-665B` | **DRAM_LAT** | +27.1% | **69.9%** | MODERATE | **PASS** | **CACHE** | 95% | **Mismatch** | 93.9s |
| 32 | `605.mcf_s-782B` | **DRAM_LAT** | +44.5% | **53.1%** | MODERATE | **PASS** | **CACHE** | 95% | **Mismatch** | 122.5s |
| 33 | `605.mcf_s-994B` | **CACHE** | +85.5% | **72.8%** | MODERATE | **PASS** | **CACHE** | 97% | **MATCH** | 125.0s |
| 34 | `619.lbm_s-2676B` | **DRAM_BW** | +61.9% | **0.0%** | COARSE | **REJECT** | **CACHE** | 95% | **Mismatch** | 108.6s |
| 35 | `619.lbm_s-2677B` | **DRAM_BW** | +90.5% | **0.0%** | COARSE | **REJECT** | **CACHE** | 89% | **Mismatch** | 133.8s |
| 36 | `619.lbm_s-3766B` | **DRAM_BW** | +79.8% | **0.0%** | COARSE | **REJECT** | **CACHE** | 90% | **Mismatch** | 117.5s |
| 37 | `619.lbm_s-4268B` | **DRAM_BW** | +90.6% | **0.0%** | COARSE | **REJECT** | **DRAM_LAT** | 90% | **Mismatch** | 93.5s |
| 38 | `620.omnetpp_s-141B` | **CACHE** | +91.5% | **30.0%** | COARSE | **REJECT** | **DRAM_LAT** | 60% | **Mismatch** | 147.3s |
| 39 | `620.omnetpp_s-874B` | **CACHE** | +91.7% | **39.4%** | MODERATE | **PASS** | **CACHE** | 95% | **MATCH** | 97.9s |
| 40 | `623.xalancbmk_s-700B` | **BRANCH** | +9.3% | **42.2%** | MODERATE | **PASS** | **BRANCH** | 85% | **MATCH** | 93.4s |
| 41 | `631.deepsjeng_s-928B` | **BRANCH** | +19.6% | **77.1%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 53.1s |
| 42 | `641.leela_s-149B` | **BRANCH** | +23.0% | **75.2%** | HIGH | **PASS** | **BRANCH** | 99% | **MATCH** | 37.0s |
| 43 | `641.leela_s-334B` | **BRANCH** | +28.8% | **71.6%** | MODERATE | **PASS** | **BRANCH** | 99% | **MATCH** | 40.4s |
| 44 | `641.leela_s-800B` | **BRANCH** | +20.5% | **72.7%** | MODERATE | **PASS** | **BRANCH** | 99% | **MATCH** | 38.8s |
| 45 | `648.exchange2_s-1699B` | **BRANCH** | +56.5% | **86.0%** | HIGH | **PASS** | **BRANCH** | 98% | **MATCH** | 36.8s |
| 46 | `648.exchange2_s-353B` | **BRANCH** | +55.6% | **86.0%** | HIGH | **PASS** | **BRANCH** | 99% | **MATCH** | 38.1s |
| 47 | `648.exchange2_s-72B` | **BRANCH** | +61.3% | **84.9%** | HIGH | **PASS** | **BRANCH** | 99% | **MATCH** | 39.8s |
| 48 | `649.fotonik3d_s-1176B` | **DRAM_LAT** | +61.5% | **0.0%** | COARSE | **REJECT** | **CACHE** | 85% | **Mismatch** | 104.4s |
| 49 | `649.fotonik3d_s-1B` | **BRANCH** | +28.5% | **83.5%** | HIGH | **PASS** | **BRANCH** | 95% | **MATCH** | 38.6s |
| 50 | `654.roms_s-842B` | **BRANCH** | +55.0% | **80.0%** | HIGH | **PASS** | **BRANCH** | 99% | **MATCH** | 37.5s |

---

## 3. Repository Structure & Artifact Reproduction

```text
A3_CHIA_HACKATHON/
├── .gitignore
├── README.md
├── requirements.txt
├── agent/
│   ├── __init__.py
│   ├── check_probe.py
│   ├── chia_diagnostic_loop.py
│   ├── prompts.py
│   ├── run_probe.py
│   ├── semantic.py
│   └── zero_shot_baseline.py
├── clone/
│   ├── distance.py
│   ├── execution_profile.py
│   ├── micrograd_cloner.py
│   ├── observation.py
│   ├── run_all_clones_4epoch.py
│   └── tuner.py
├── configs/
│   ├── baseline.json
│   ├── M0/
│   │   ├── baseline.json
│   │   ├── branch.json
│   │   ├── cache.json
│   │   ├── dram4.json
│   │   ├── dram8.json
│   │   ├── latency.json
│   │   ├── mshr.json
│   │   ├── rob.json
│   │   ├── source_champsim_config.json
│   │   ├── champsim_commit.txt
│   │   └── bin/
│   │       ├── m0_baseline
│   │       ├── m0_branch
│   │       ├── m0_cache
│   │       ├── m0_dram4
│   │       ├── m0_dram8
│   │       ├── m0_latency
│   │       ├── m0_mshr
│   │       ├── m0_rob
│   │       └── ...
│   └── ...
├── probes/
│   └── template/
│       ├── probe.c
│       └── probe.c.bak
├── results/
│   ├── clones/
│   │   ├── chia_diagnosis_summary.json
│   │   ├── *_clone.json
│   │   └── *_diagnosis.json
│   └── oracle/
│       ├── <workload>/
│       │   ├── baseline.json
│       │   ├── branch.json
│       │   ├── cache.json
│       │   ├── dram4.json
│       │   ├── latency.json
│       │   ├── mshr.json
│       │   ├── rob.json
│       │   └── ...
│       └── oracle_analysis_6class.csv
└── ...
```

### Reproducing the 50-Workload Benchmark

```bash
# 1. Activate the environment
source agent/.venv/bin/activate

# 2. Run the full 50-workload CHIA autonomous diagnostic benchmark
python agent/chia_diagnostic_loop.py --all --turns 3

# 3. View the JSON summary and per-trace multi-turn reasoning transcripts
cat results/clones/chia_diagnosis_summary.json
```

---

## 4. Conclusion

This work demonstrates that coupling foundational LLMs  with differentiable semantic proxy cloning and active cycle-accurate probing solves the microarchitectural causal grounding gap. By replacing st[...]
