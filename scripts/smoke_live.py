#!/usr/bin/env python3
"""Post-redeploy smoke test for the live TxODDS World Cup portfolio host.

Checks every judge-facing page and API on the deployed one-service host. Run it
right after a Render redeploy to confirm nothing 404s.

    python scripts/smoke_live.py
    python scripts/smoke_live.py https://your-host.onrender.com

Exit code 0 = all good, 1 = at least one endpoint failed. Uses only stdlib.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from urllib.error import HTTPError, URLError

DEFAULT_BASE = "https://txodds-portfolio-host.onrender.com"

# (method, path, expected_status, optional json-substring that must appear)
CHECKS: list[tuple[str, str, int, str | None]] = [
    ("GET", "/", 200, None),
    ("GET", "/health", 200, '"status"'),
    # ProofGuard (Trading Agents)
    ("GET", "/proofguard/", 200, None),
    ("GET", "/proofguard/playground", 200, None),
    ("GET", "/proofguard/api/health", 200, '"PASS"'),
    ("GET", "/proofguard/api/snapshot", 200, None),
    ("GET", "/proofguard/api/receipts/verify", 200, '"status"'),
    ("GET", "/proofguard/api/receipts/tamper-demo", 200, '"authentic"'),
    ("GET", "/proofguard/api/model/preview", 200, None),
    # FinalityGate (Markets)
    ("GET", "/finalitygate/", 200, None),
    ("GET", "/finalitygate/resolver", 200, None),
    ("GET", "/finalitygate/explorer", 200, None),
    ("GET", "/finalitygate/api/health", 200, '"PASS"'),
    ("GET", "/finalitygate/api/demo", 200, None),
    ("GET", "/finalitygate/api/ledger", 200, '"batch_root"'),
    ("GET", "/finalitygate/api/commitments/demo", 200, '"commitments"'),
    ("GET", "/finalitygate/api/onchain-anchor", 200, '"onchain'),
]


def fetch(base: str, method: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read(4096).decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, ""
    except URLError as exc:
        return 0, f"URLError: {exc.reason}"


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    print(f"Smoke-testing {base}\n" + "-" * 60)
    failures = 0
    for method, path, want, needle in CHECKS:
        status, body = fetch(base, method, path)
        ok = status == want and (needle is None or needle in body)
        if not ok:
            failures += 1
        mark = "OK  " if ok else "FAIL"
        extra = "" if (needle is None or needle in body or not ok) else ""
        note = "" if ok else f"  (got {status}, wanted {want}{'' if needle is None else f', needle {needle!r} missing' if status == want else ''})"
        print(f"[{mark}] {status:>3}  {method} {path}{note}")
    print("-" * 60)
    if failures:
        print(f"{failures} endpoint(s) FAILED — the deploy may be stale or mid-build.")
        return 1
    print("All endpoints healthy. Every judge quick-verify link resolves.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
