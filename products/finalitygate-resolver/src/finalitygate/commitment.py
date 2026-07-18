"""Cryptographic settlement commitment for a FinalityGate resolution.

FinalityGate resolves *whether* an outcome market can settle. This module turns
that resolution into a verifiable **on-chain-style commitment**: a genuine
32-byte SHA-256 Merkle root over the individual resolution facts, plus an
inclusion proof for every fact. A settlement contract (e.g. a Solana
``validateStat``-style program) would store or compare exactly this 32-byte
root; here we compute and verify it deterministically off-chain — no chain call
is executed.

Design:
    * leaves are domain-separated (``0x00`` prefix), internal nodes (``0x01``)
      — prevents second-preimage/leaf-vs-node confusion;
    * odd levels duplicate the last node (Bitcoin-style);
    * an inclusion proof lets anyone re-derive the root from a single fact, so a
      judge can prove "the declared result HOME is committed under root X".

Pure standard library, deterministic, offline.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

_LEAF_PREFIX = b"\x00"
_NODE_PREFIX = b"\x01"


def _canonical_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def leaf_digest(field: str, value: Any) -> bytes:
    """32-byte digest committing to one named resolution fact."""
    return hashlib.sha256(_LEAF_PREFIX + _canonical_bytes([field, value])).digest()


def _node_digest(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(_NODE_PREFIX + left + right).digest()


class MerkleTree:
    """A small Merkle tree over an *ordered* list of (field, value) leaves."""

    def __init__(self, leaves: list[tuple[str, Any]]) -> None:
        if not leaves:
            raise ValueError("a commitment needs at least one leaf")
        self.fields: list[str] = [field for field, _ in leaves]
        self._levels: list[list[bytes]] = [[leaf_digest(field, value) for field, value in leaves]]
        while len(self._levels[-1]) > 1:
            current = self._levels[-1]
            nxt: list[bytes] = []
            for i in range(0, len(current), 2):
                left = current[i]
                right = current[i + 1] if i + 1 < len(current) else current[i]  # duplicate last
                nxt.append(_node_digest(left, right))
            self._levels.append(nxt)

    @property
    def root(self) -> str:
        """The 32-byte commitment as 64 lowercase hex characters."""
        return self._levels[-1][0].hex()

    @property
    def leaf_digests(self) -> list[bytes]:
        return list(self._levels[0])

    def proof(self, index: int) -> list[dict[str, str]]:
        """Inclusion proof (sibling path) for the leaf at ``index``."""
        if not 0 <= index < len(self._levels[0]):
            raise IndexError("leaf index out of range")
        path: list[dict[str, str]] = []
        for level in self._levels[:-1]:
            sibling_index = index ^ 1
            if sibling_index < len(level):
                sibling = level[sibling_index]
                position = "left" if sibling_index < index else "right"
            else:
                sibling = level[index]  # duplicated self on an odd level
                position = "right"
            path.append({"sibling": sibling.hex(), "position": position})
            index //= 2
        return path


def verify_proof(leaf_hash_hex: str, proof: list[dict[str, str]], root_hex: str) -> bool:
    """Re-derive the root from a single leaf digest and its inclusion proof."""
    try:
        accumulator = bytes.fromhex(leaf_hash_hex)
    except ValueError:
        return False
    for step in proof:
        try:
            sibling = bytes.fromhex(step["sibling"])
        except (ValueError, KeyError, TypeError):
            return False
        if step.get("position") == "left":
            accumulator = _node_digest(sibling, accumulator)
        else:
            accumulator = _node_digest(accumulator, sibling)
    return accumulator.hex() == root_hex


def _resolution_leaves(decision: dict[str, Any]) -> list[tuple[str, Any]]:
    payload = decision.get("receipt_payload", decision)
    market = payload.get("market", {}) if isinstance(payload.get("market"), dict) else {}
    evidence = payload.get("evidence", {}) if isinstance(payload.get("evidence"), dict) else {}
    return [
        ("market_id", market.get("market_id")),
        ("fixture_id", market.get("fixture_id")),
        ("market_type", market.get("market_type")),
        ("selections", market.get("selections")),
        ("policy_version", market.get("policy_version")),
        ("fixture_status", evidence.get("fixture_status")),
        ("home_score", evidence.get("home_score")),
        ("away_score", evidence.get("away_score")),
        ("declared_result", evidence.get("declared_result")),
        ("proof_status", evidence.get("proof_status")),
        ("root_status", evidence.get("root_status")),
        ("proof_reference", evidence.get("proof_reference")),
        ("expected_root", evidence.get("expected_root")),
        ("observed_root", evidence.get("observed_root")),
        ("state", payload.get("state")),
        ("resolved_selection", payload.get("resolved_selection")),
    ]


def build_commitment(decision: dict[str, Any]) -> dict[str, Any]:
    """Build the Merkle settlement commitment for a resolution decision.

    Includes the 32-byte root, every leaf digest, and a per-field inclusion
    proof, plus a transparent comparison to the evidence's declared
    ``observed_root`` / ``expected_root`` (informational; the resolver's
    fail-closed state is unchanged).
    """

    leaves = _resolution_leaves(decision)
    tree = MerkleTree(leaves)
    root = tree.root
    leaf_digests = tree.leaf_digests
    proofs = {field: tree.proof(index) for index, field in enumerate(tree.fields)}

    payload = decision.get("receipt_payload", decision)
    evidence = payload.get("evidence", {}) if isinstance(payload.get("evidence"), dict) else {}
    observed_root = evidence.get("observed_root")
    expected_root = evidence.get("expected_root")

    return {
        "schema": "finalitygate.resolution-commitment.v1",
        "algorithm": "sha256-merkle (domain-separated leaves 0x00 / nodes 0x01, duplicate-last)",
        "root": root,
        "root_bytes": 32,
        "leaf_count": len(leaves),
        "leaves": [
            {"field": field, "value": value, "leaf_hash": leaf_digests[index].hex()}
            for index, (field, value) in enumerate(leaves)
        ],
        "proofs": proofs,
        "declared_observed_root": observed_root,
        "declared_expected_root": expected_root,
        "computed_root_matches_observed": isinstance(observed_root, str) and observed_root.lower() == root,
        "computed_root_matches_expected": isinstance(expected_root, str) and expected_root.lower() == root,
        "onchain_note": (
            "this 32-byte root is the commitment a Solana validateStat-style program would "
            "store and compare; no on-chain transaction or view is executed by this service"
        ),
    }
