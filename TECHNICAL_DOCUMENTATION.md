# Technical Documentation

## Overview

This submission contains two complementary products for the TxODDS World Cup Hackathon:

1. **ProofGuard** — Trading Tools and Agents track.
2. **FinalityGate** — Prediction Markets and Settlement track.

Both are deployed on a shared public FastAPI host:

```text
https://txodds-portfolio-host.onrender.com/
```

The public deployment is simulation-only and deterministic. It is designed to remain judgeable after live match windows close.

---

## ProofGuard

**Track:** Trading Tools and Agents

**Live app:**

```text
https://txodds-portfolio-host.onrender.com/proofguard/
```

**Interactive playground:**

```text
https://txodds-portfolio-host.onrender.com/proofguard/playground
```

### Core idea

ProofGuard is a pre-execution integrity layer for autonomous trading agents. It models a football-market agent that reads a TxODDS-style feed, computes a market signal, and proposes an action. Before that action is allowed, a deterministic integrity gate evaluates whether the evidence is safe.

The agent can emit:

```text
ENTER / HOLD / CLOSE / REJECT
```

The integrity gate can emit:

```text
PASS / REVIEW / BLOCK
```

A profitable-looking edge cannot override a failed integrity check.

### Demonstrated scenario

The playground exposes a `corrupt_block` scenario. It feeds the agent a corrupted book, backwards timestamp, and missing proof material. Even with a strong apparent edge, ProofGuard refuses the action:

```text
BLOCK / REJECT
```

This demonstrates the central claim: the safety layer is not bypassed by profitable-looking but untrusted input.

### Technical highlights

- deterministic replay mode;
- TxODDS-style odds, score, market and fixture-state modeling;
- market de-vig / fair-probability calculations;
- in-play model preview;
- autonomous agent action loop;
- non-bypassable market-integrity gate;
- tamper-evident SHA-256 receipt chain;
- chain verification endpoint;
- naive-agent comparison showing prevented unsafe entries.

### Useful endpoints

```text
/proofguard/
/proofguard/playground
/proofguard/api/health
/proofguard/api/snapshot
/proofguard/api/receipts/verify
/proofguard/api/model/preview
/proofguard/api/simulate?scenario=corrupt_block
```

---

## FinalityGate

**Track:** Prediction Markets and Settlement

**Live app:**

```text
https://txodds-portfolio-host.onrender.com/finalitygate/
```

**Merkle explorer:**

```text
https://txodds-portfolio-host.onrender.com/finalitygate/explorer
```

### Core idea

FinalityGate is a fail-closed resolution engine for outcome-market settlement. A market should resolve only when all critical evidence agrees:

- market definition;
- fixture identity;
- fixture finality;
- score-derived result;
- declared result;
- proof status;
- root evidence.

When evidence is incomplete or contradictory, FinalityGate refuses to settle.

Possible states:

```text
OPEN
PENDING_FINALITY
WAIT_FOR_PROOF
RESOLVE
DISPUTE
```

### Demonstrated scenario

The live explorer lets judges verify settlement facts against a committed Merkle root. The browser folds inclusion proofs to the root. If evidence is tampered with, proof verification fails.

### Technical highlights

- fail-closed settlement state machine;
- SHA-256 resolution receipts;
- Merkle settlement commitment;
- verifiable settlement ledger;
- proof explorer with tamper detection;
- dispute/provenance explanation;
- impact comparison against a naive resolver;
- Solana-shaped anchor view with explicit no on-chain execution in the public demo.

### Useful endpoints

```text
/finalitygate/
/finalitygate/explorer
/finalitygate/api/health
/finalitygate/api/ledger
/finalitygate/api/impact
/finalitygate/api/onchain-anchor
/finalitygate/api/commitment
/finalitygate/api/resolve/batch
```

---

## TxLINE usage and claim boundary

The public demo uses a TxODDS/TxLINE-style normalized football-market data model rather than live credentials. This choice makes the product reproducible for judging after the match window and avoids exposing credentials or creating real-money behavior.

Mode:

```text
REPLAY / simulation-only
```

No live credential is required by the public service.

---

## Safety and legal boundary

The public demo:

- does not custody funds;
- does not enable betting or wagering;
- does not execute real-money settlement;
- does not provide financial advice;
- does not execute on-chain transactions;
- does not release escrow;
- does not require a wallet connection;
- does not expose JWTs, API tokens, private keys, seed phrases, or secrets.

The product is a technical hackathon demonstration of market-data integrity, autonomous-agent safety, and settlement verification.

---

## Recommended judge walkthrough

### ProofGuard

1. Open `https://txodds-portfolio-host.onrender.com/proofguard/`.
2. Review the dashboard and receipt-chain verification.
3. Open `https://txodds-portfolio-host.onrender.com/proofguard/playground`.
4. Run or view `corrupt_block`.
5. Confirm the system lands on `BLOCK / REJECT`.

### FinalityGate

1. Open `https://txodds-portfolio-host.onrender.com/finalitygate/`.
2. Review ledger and impact cards.
3. Open `https://txodds-portfolio-host.onrender.com/finalitygate/explorer`.
4. Verify the Merkle proof.
5. Observe that tampered evidence fails verification.
