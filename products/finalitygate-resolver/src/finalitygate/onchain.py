"""Solana settlement-anchor framing for a FinalityGate commitment.

Maps a 32-byte resolution / batch Merkle root to the *shape* of the on-chain
anchor a Solana settlement program would use — WITHOUT executing anything
on-chain and WITHOUT claiming a real program-derived address. ``findProgramAddress``
requires the runtime and the program's real ID; we therefore expose the seed
inputs and an explicitly-labeled illustrative digest, never a fake PDA. This
keeps the honest proof boundary the submission commits to.
"""

from __future__ import annotations

import hashlib
from typing import Any

DEFAULT_SEED_LABEL = "daily_scores_roots"


def _is_canonical_root(root_hex: Any) -> bool:
    return (
        isinstance(root_hex, str)
        and len(root_hex) == 64
        and all(ch in "0123456789abcdefABCDEF" for ch in root_hex)
    )


def commitment_anchor(
    root_hex: str | None,
    *,
    seed_label: str = DEFAULT_SEED_LABEL,
    program_id: str | None = None,
) -> dict[str, Any]:
    """Describe how a 32-byte commitment root maps to an on-chain settlement anchor.

    Deterministic, offline, and explicitly non-executing. The
    ``illustrative_anchor_digest`` is a SHA-256 over the seeds for demonstration
    only — NOT a real Solana PDA.
    """

    valid = _is_canonical_root(root_hex)
    normalized = root_hex.lower() if valid else None
    illustrative = (
        hashlib.sha256(f"{seed_label}|{normalized}".encode("utf-8")).hexdigest() if valid else None
    )
    return {
        "schema": "finalitygate.onchain-anchor-framing.v1",
        "root": normalized,
        "root_is_canonical_32_bytes": valid,
        "seed_label": seed_label,
        "pda_seed_inputs": [seed_label, normalized],
        "program_id": program_id,
        "illustrative_anchor_digest": illustrative,
        "onchain_call_executed": False,
        "network": None,
        "note": (
            "A settlement program would store this 32-byte root under a PDA derived from "
            "these seeds and compare it during a validateStat-style check. "
            "illustrative_anchor_digest is a deterministic SHA-256 over the seeds for "
            "demonstration only; it is NOT a real Solana program-derived address "
            "(findProgramAddress requires the on-chain runtime and the program's real ID)."
        ),
    }
