"""Combined one-service deployment host for the TxODDS World Cup portfolio.

Mounts the real ProofGuard and FinalityGate FastAPI applications under a single
Uvicorn process, without modifying either product's internal runtime. This is a
cost-saving deployment option; it does not replace the standalone product
deployments or exports.
"""

from __future__ import annotations

__all__ = ["create_host_app"]

__version__ = "0.1.0"


def __getattr__(name: str):  # pragma: no cover - thin lazy import shim
    if name == "create_host_app":
        from .app import create_host_app

        return create_host_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
