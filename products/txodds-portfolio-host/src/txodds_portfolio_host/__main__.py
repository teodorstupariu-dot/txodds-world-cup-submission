"""Run the combined portfolio host with uvicorn.

Usage:
    python -m txodds_portfolio_host

Environment: HOST (default 0.0.0.0), PORT (default 8080), plus the PROOFGUARD_*
and TXLINE_* variables consumed by the mounted ProofGuard runtime.
"""

from __future__ import annotations

from .app import run

if __name__ == "__main__":
    run()
