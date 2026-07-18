#!/usr/bin/env bash
set -euo pipefail

REPORT="${1:-outputs/local_validation_report.json}"
TIMEOUT_SECONDS="${2:-900}"

python scripts/local_gate.py --report "$REPORT" --timeout "$TIMEOUT_SECONDS"
