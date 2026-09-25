#!/bin/bash
set -e

WORKLOADS=$(cat configs/characterization/cases.txt)
TOTAL=$(echo "$WORKLOADS" | wc -w)
COUNT=1

for W in $WORKLOADS; do
    echo "[$COUNT/$TOTAL] Cloning $W..."
    agent/.venv/bin/python clone/micrograd_cloner.py --workload "$W" --epochs 2 --workers 4 > /dev/null
    COUNT=$((COUNT+1))
done

agent/.venv/bin/python -c "
import json
import glob
import numpy as np
files = glob.glob('results/clones/*_clone.json')
fidelities = []
for f in files:
    with open(f) as fp:
        d = json.load(fp)
        fidelities.append(d.get('average_fidelity', 0.0))
print(f'\n--- NEW FIDELITY ACROSS 26 WORKLOADS ---')
print(f'Mean fidelity: {np.mean(fidelities):.1f}%')
print(f'Median fidelity: {np.median(fidelities):.1f}%')
"
