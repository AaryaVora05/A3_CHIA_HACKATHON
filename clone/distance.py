import math

METRICS = [
    "IPC",
    "BR_MPKI",
    "L1D_MPKI",
    "L2_MPKI",
    "LLC_MPKI",
    "DRAM_RQPI",
]

# Calibrated normalizer scales across the M0 operational domain
METRIC_SCALES = {
    "IPC": 1.50,
    "BR_MPKI": 5.00,
    "L1D_MPKI": 10.00,
    "L2_MPKI": 8.00,
    "LLC_MPKI": 6.00,
    "DRAM_RQPI": 0.015,
}

# Engineering Tolerances for Causal Bottleneck Workload Cloning
# S_i = max(A_i, R_i * |T_i|)
# z_i = |C_i - T_i| / S_i <= 1.0 is required for PASS.
TOLERANCE_SPECS = {
    "IPC": {"rel": 0.10, "abs": 0.05, "unit": "IPC"},
    "BR_MPKI": {"rel": 0.10, "abs": 1.00, "unit": "MPKI"},
    "L1D_MPKI": {"rel": 0.10, "abs": 1.00, "unit": "MPKI"},
    "L2_MPKI": {"rel": 0.10, "abs": 1.00, "unit": "MPKI"},
    "LLC_MPKI": {"rel": 0.10, "abs": 0.50, "unit": "MPKI"},
    "DRAM_RQPI": {"rel": 0.10, "abs": 0.0005, "unit": "req/instr"},
}


def compute_micrograd_loss(cand_fp, target_fp):
    """
    Canonical normalized log-loss across microarchitectural metrics.
    Used by MicroGrad cloner and gradient-descent tuners.
    """
    total_loss = 0.0
    comp_loss = {}
    for m in METRICS:
        if m == "IPC":
            xc, xt = cand_fp[m], target_fp[m]
        else:
            xc = math.log1p(cand_fp[m])
            xt = math.log1p(target_fp[m])
        scale = METRIC_SCALES[m]
        err = (xc - xt) / scale
        l_val = err * err
        comp_loss[m] = l_val
        total_loss += l_val
    return total_loss, comp_loss


def compute_normalized_errors(cand_fp, target_fp, weights=None):
    """
    Unified Causal Tolerance Metric:
      S_i = max(A_i, R_i * |T_i|)
      z_i = |C_i - T_i| / S_i
      L = sqrt( sum(w_i * z_i^2) / sum(w_i) )

    Returns:
      composite_L: Root-mean-square normalized error.
      z_scores: Dict of z_i per metric (z_i <= 1.0 means PASS).
      passes: Dict of bool pass/fail per metric.
      details: Detailed breakdown including C_i, T_i, |C_i - T_i|, S_i.
    """
    z_scores = {}
    passes = {}
    details = {}
    weighted_sq_sum = 0.0
    weight_sum = 0.0

    for metric, spec in TOLERANCE_SPECS.items():
        c_val = cand_fp[metric]
        t_val = target_fp[metric]

        r_tol = spec["rel"]
        a_tol = spec["abs"]
        s_i = max(a_tol, r_tol * abs(t_val))
        diff = abs(c_val - t_val)
        z_i = diff / s_i

        passed = bool(z_i <= 1.0)
        z_scores[metric] = z_i
        passes[metric] = passed

        w_i = (weights or {}).get(metric, 1.0)
        weighted_sq_sum += w_i * (z_i ** 2)
        weight_sum += w_i

        details[metric] = {
            "target": t_val,
            "clone": c_val,
            "abs_error": diff,
            "scale_S": s_i,
            "z": z_i,
            "pass": passed,
            "unit": spec["unit"],
        }

    composite_l = math.sqrt(weighted_sq_sum / max(weight_sum, 1e-6))
    return composite_l, z_scores, passes, details

