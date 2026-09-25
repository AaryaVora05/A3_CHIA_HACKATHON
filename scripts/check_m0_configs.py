#!/usr/bin/env python3

import json
from pathlib import Path

ROOT = Path.home() / "a3-hackathon" / "configs" / "M0"


def load(name):
    with (ROOT / name).open() as f:
        return json.load(f)


def flatten(obj, prefix=""):
    out = {}

    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else k
            out.update(flatten(v, key))

    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}.{i}"
            out.update(flatten(v, key))

    else:
        out[prefix] = obj

    return out


base = flatten(load("baseline.json"))

for name in ["branch.json", "cache.json", "dram4.json"]:
    candidate = flatten(load(name))

    print(f"\n===== {name} =====")

    keys = sorted(set(base) | set(candidate))

    for k in keys:
        if base.get(k) != candidate.get(k):
            print(f"{k}: {base.get(k)!r} -> {candidate.get(k)!r}")
