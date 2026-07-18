# ProofGuard + FinalityGate — TxODDS World Cup Submission

Public submission repository for the TxODDS World Cup Hackathon.

This repository documents the two submitted products and points judges to the live deployment, demo videos, and technical documentation. The implementation is deployed as a combined FastAPI host with two independently mounted products.

## Live demo

Root:

```text
https://txodds-portfolio-host.onrender.com/
```

ProofGuard:

```text
https://txodds-portfolio-host.onrender.com/proofguard/
https://txodds-portfolio-host.onrender.com/proofguard/playground
```

FinalityGate:

```text
https://txodds-portfolio-host.onrender.com/finalitygate/
https://txodds-portfolio-host.onrender.com/finalitygate/explorer
```

## Track 1 — Trading Tools and Agents: ProofGuard

ProofGuard is a simulation-only pre-execution safety layer for autonomous trading agents using a TxODDS-style football-market data model.

It runs in deterministic REPLAY mode and demonstrates a non-bypassable integrity gate. Even when a corrupted market signal appears profitable, the gate blocks the action before execution.

Core features:

- autonomous paper-trading agent loop;
- TxODDS-style odds, score, fixture-state and market-update model;
- deterministic REPLAY mode for reproducible judging;
- market de-vig and fair-probability display;
- in-play model preview;
- non-bypassable integrity gate;
- tamper-evident SHA-256 receipt chain;
- live playground scenario: `corrupt_block` -> `BLOCK / REJECT`;
- comparison against a naive edge-only agent.

Primary links:

```text
Dashboard:  https://txodds-portfolio-host.onrender.com/proofguard/
Playground: https://txodds-portfolio-host.onrender.com/proofguard/playground
```

## Track 2 — Prediction Markets and Settlement: FinalityGate

FinalityGate is a simulation-only fail-closed market-resolution engine for TxODDS-style football outcome markets.

A market resolves only when finality, result evidence, proof status, and root evidence all agree. Otherwise it remains open, waits for proof, waits for finality, or disputes.

Core features:

- outcome-market settlement state machine;
- fail-closed resolution logic;
- SHA-256 resolution receipts;
- Merkle settlement commitment root;
- verifiable settlement ledger;
- in-browser Merkle proof explorer;
- tamper-evident proof verification;
- comparison against a naive resolver baseline;
- Solana-shaped anchor view with no on-chain execution in the public demo.

Primary links:

```text
Dashboard: https://txodds-portfolio-host.onrender.com/finalitygate/
Explorer:  https://txodds-portfolio-host.onrender.com/finalitygate/explorer
```

## Safety boundary

This is a hackathon demo and simulation system only.

The public service:

- does not custody funds;
- does not enable wagering;
- does not execute real-money settlement;
- does not provide financial advice;
- does not execute on-chain transactions;
- does not require wallet connection;
- does not expose or require live credentials, JWTs, API tokens, private keys, or seed phrases.

Both products run in deterministic replay/simulation mode for public judging.

## Technical documentation

See:

```text
TECHNICAL_DOCUMENTATION.md
```

## Submission status

Ready for Superteam Earn submission.

Manual fields still supplied through the Superteam form:

- live demo video URL;
- Solana payout wallet;
- track-specific form text.
