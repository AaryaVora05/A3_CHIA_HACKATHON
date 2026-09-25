"""
clone/execution_profile.py -- Single source of truth for simulation execution settings.

Every module (cloner, tuner, CHIA loop, sweeps) imports this immutable configuration
to guarantee identical measurement windows across baseline characterization and diagnostic probing.
"""

EXEC_PROFILE = {
    "SIM_INSTR": "1000000",
    "WARMUP_MIN": "500000",
}
