"""Append-only, hash-linked settlement ledger for FinalityGate resolutions.

A single resolution proves one market can settle (see ``commitment.py``). A
*ledger* commits to a whole batch of resolutions two ways at once:

    1. **Hash chain** - each entry commits to the previous entry's hash, so the
       order and contents of a batch cannot be altered without detection.
    2. **Batch Merkle root** - one 32-byte root over every per-resolution
       commitment root, with an inclusion proof per market. This is the single
       value a rollup-style settlement contract would anchor for the whole
       batch; any market's settlement can then be proven against it.

Stateless and deterministic: a ledger is built from a list of decisions and can
be re-verified from its own contents. No storage, no network, no chain call.
"""

from __future__ import annotations

from typing import Any

from .commitment import MerkleTree, build_commitment
from .core import canonical_sha256

GENESIS_ENTRY = "0" * 64


def _entry_identity(decision: dict[str, Any]) -> dict[str, Any]:
    payload = decision.get("receipt_payload", decision)
    market = payload.get("market", {}) if isinstance(payload.get("market"), dict) else {}
    return {
        "market_id": market.get("market_id"),
        "fixture_id": market.get("fixture_id"),
        "state": payload.get("state"),
        "resolved_selection": payload.get("resolved_selection"),
    }


def build_ledger(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a hash-linked ledger + batch Merkle root over resolution decisions."""

    entries: list[dict[str, Any]] = []
    commitment_roots: list[tuple[str, Any]] = []
    prev = GENESIS_ENTRY

    for index, decision in enumerate(decisions):
        commitment = build_commitment(decision)
        root = commitment["root"]
        identity = _entry_identity(decision)
        entry_body = {
            "index": index,
            "prev_entry_hash": prev,
            "commitment_root": root,
            **identity,
        }
        entry_hash = canonical_sha256(entry_body)
        entries.append(
            {
                **entry_body,
                "entry_hash": entry_hash,
                "receipt_sha256": decision.get("receipt_sha256"),
            }
        )
        commitment_roots.append((str(index), root))
        prev = entry_hash

    batch_root = None
    batch_proofs: dict[str, Any] = {}
    if commitment_roots:
        tree = MerkleTree(commitment_roots)
        batch_root = tree.root
        batch_proofs = {str(index): tree.proof(index) for index in range(len(commitment_roots))}

    return {
        "schema": "finalitygate.settlement-ledger.v1",
        "algorithm": "sha256 hash chain + sha256-merkle batch root",
        "count": len(entries),
        "genesis": GENESIS_ENTRY,
        "head_entry_hash": prev,
        "batch_root": batch_root,
        "batch_root_bytes": 32 if batch_root else 0,
        "entries": entries,
        "batch_proofs": batch_proofs,
        "onchain_note": (
            "the batch_root is the single 32-byte value a rollup-style settlement "
            "contract would anchor for the whole batch; no on-chain call is executed"
        ),
    }


def verify_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    """Independently re-prove a ledger's hash chain and batch Merkle root."""

    errors: list[str] = []
    entries = ledger.get("entries", [])
    genesis = ledger.get("genesis", GENESIS_ENTRY)
    expected_prev = genesis

    roots: list[tuple[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entry[{index}] is not an object")
            break
        if entry.get("index") != index:
            errors.append(f"entry[{index}] index out of order")
        if entry.get("prev_entry_hash") != expected_prev:
            errors.append(f"entry[{index}] prev_entry_hash breaks the chain")
        body = {
            "index": entry.get("index"),
            "prev_entry_hash": entry.get("prev_entry_hash"),
            "commitment_root": entry.get("commitment_root"),
            "market_id": entry.get("market_id"),
            "fixture_id": entry.get("fixture_id"),
            "state": entry.get("state"),
            "resolved_selection": entry.get("resolved_selection"),
        }
        recomputed = canonical_sha256(body)
        if recomputed != entry.get("entry_hash"):
            errors.append(f"entry[{index}] entry_hash mismatch")
        expected_prev = entry.get("entry_hash") if isinstance(entry.get("entry_hash"), str) else expected_prev
        roots.append((str(index), entry.get("commitment_root")))

    if entries:
        if expected_prev != ledger.get("head_entry_hash"):
            errors.append("head_entry_hash does not match the final entry")
        recomputed_batch = MerkleTree(roots).root
        if recomputed_batch != ledger.get("batch_root"):
            errors.append("batch_root mismatch")

    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "checked": len(entries),
        "head_entry_hash": ledger.get("head_entry_hash"),
        "batch_root": ledger.get("batch_root"),
    }
