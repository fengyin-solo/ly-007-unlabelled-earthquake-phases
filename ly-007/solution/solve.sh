#!/bin/bash
set -euo pipefail

SOLUTION_DIR="$(cd "$(dirname "$0")" && pwd)"
cp "$SOLUTION_DIR/reference.py" /app/phase_association.py
chmod 0755 /app/phase_association.py

cd /app
python3 phase_association.py data/catalogue.json data/result.json data/ledger.jsonl
python3 phase_association.py data/catalogue.json data/result.json data/ledger.jsonl
