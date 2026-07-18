# ProofGuard Autonomous Agent

ProofGuard is the independent **Trading Tools and Agents** submission for the TxODDS World Cup Hackathon.

It is a live web application and autonomous **paper-trading** agent. TxLINE odds and scores are normalized, evaluated by a deterministic strategy, and passed through a non-bypassable market-integrity policy before the paper ledger can change.

The agent emits:

- `ENTER`
- `HOLD`
- `REJECT`
- `CLOSE`

Every action and refusal receives a deterministic SHA-256 decision receipt.

## What is live

The production service runs a background polling loop without per-cycle human approval:

```text
TxLINE odds and scores
        ↓
normalization and schema fingerprint
        ↓
signal + confidence policy
        ↓
non-bypassable integrity policy
        ↓
autonomous paper ledger
        ↓
decision receipt + public dashboard/API
```

The public dashboard labels the active source explicitly:

- `LIVE` — the current successful cycle used TxLINE;
- `REPLAY` — deterministic bundled replay;
- `REPLAY_FALLBACK` — a live request failed and the service stayed demonstrable using labelled replay;
- `ERROR` — no usable cycle is available.

Replay is never presented as live data.

## Public web application

Start locally:

```powershell
cd products\proofguard-agent
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
proofguard-web
```

Open:

```text
http://127.0.0.1:8080
```

Public endpoints:

```text
GET /
GET /api/health
GET /api/snapshot
GET /api/status
GET /api/market/latest
GET /api/decision/latest
GET /api/positions
GET /api/receipts
GET /api/docs
```

The browser refreshes automatically. It shows the fixture, source mode, timestamps, normalized probabilities, model edge, confidence, integrity state, action, reasons, paper exposure, positions, and recent receipt hashes.

## Live configuration

Copy `.env.example` only for local reference. Store real values in the deployment provider's private environment-variable interface.

Required for `LIVE` mode:

```text
TXLINE_GUEST_JWT
TXLINE_API_TOKEN
PROOFGUARD_FIXTURE_ID
PROOFGUARD_MODEL_PROBABILITIES_JSON
```

Recommended configuration:

```text
PROOFGUARD_MODE=AUTO
PROOFGUARD_POLL_SECONDS=60
PROOFGUARD_WITH_SCORES=true
PROOFGUARD_REPLAY_FALLBACK=true
PROOFGUARD_AUTO_START=true
PROOFGUARD_MAX_RECEIPTS=100
```

Example model probabilities:

```json
{
  "HOME": 0.58,
  "DRAW": 0.25,
  "AWAY": 0.17
}
```

`AUTO` enters `LIVE` only when TxLINE credentials, a fixture ID, and explicit model probabilities are all configured. Otherwise the service remains honestly labelled `REPLAY`.

The service never persists raw TxLINE responses. Public state contains only normalized events, safe identifiers, schema fingerprints, decisions, paper positions, bounded receipts, and redacted errors.

## CLI live evidence

Check configuration without exposing secrets:

```bash
proofguard doctor
```

Run one autonomous TxLINE cycle:

```bash
proofguard live-once \
  --fixture-id FIXTURE_ID \
  --models models.json \
  --with-scores \
  --out outputs/live_once.json
```

Run a finite autonomous loop:

```bash
proofguard live-loop \
  --fixture-id FIXTURE_ID \
  --models models.json \
  --cycles 20 \
  --interval-seconds 5 \
  --with-scores \
  --out outputs/live_loop.json
```

`models.json` is ignored and must remain private.

## Integrity policy

The policy evaluates:

- backwards timestamps;
- stale updates;
- market probability-sum deviation;
- proof readiness.

It returns:

- `PASS`
- `REVIEW`
- `BLOCK`

Hard invariant:

```text
REVIEW or BLOCK can never produce ENTER
```

## Paper-risk controls

The agent supports:

- maximum stake per paper position;
- maximum aggregate paper exposure;
- confidence floor;
- minimum edge;
- reduced-risk mode;
- emergency kill switch;
- autonomous open, resize, maintain, safety-close, and fixture-final close.

No real-money position is created.

## Decision receipts

Each receipt binds:

- source event and schema fingerprint;
- market and model probabilities;
- edge and confidence;
- integrity result and checks;
- active controls and policy version;
- requested action;
- paper execution result;
- position before and after;
- total paper exposure after the action.

Changing a bound field invalidates the receipt hash.

## Deterministic offline demo

The live service is the application submitted to judges. A deterministic artifact demo remains available for reproducibility and post-match review:

```bash
proofguard demo --out outputs/demo
proofguard verify-demo outputs/demo
```

It demonstrates `ENTER`, `HOLD`, `REJECT`, `CLOSE`, reduced-risk resizing, kill-switch closure, and fixture-final closure.

## TxODDS validation inspection

Inspect an authorized `/api/odds/validation` response without claiming complete on-chain verification:

```bash
proofguard inspect-odds-validation \
  --input odds_validation.json \
  --out outputs/odds_validation_inspection.json
```

Or retrieve and inspect it without persisting the raw licensed response:

```bash
proofguard live-odds-validation \
  --message-id MESSAGE_ID \
  --ts TIMESTAMP_MILLISECONDS \
  --out outputs/live_odds_validation.json
```

The inspector validates fixture identity, message/timestamp fields, price alignment, timestamp bounds, 32-byte roots, directional proof nodes, signed/unsigned byte arrays, and documented PDA seed inputs.

It reports explicitly:

```text
exact_leaf_serialization_executed: false
onchain_validate_odds_executed: false
```

Complete validation still requires exact official record serialization, the matching network/program/IDL/account, an executed Solana validation method, a known-valid case, and altered proof/root failures.

## Local release gate

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run_local_gate.ps1
```

The gate:

- compiles source, tests, and scripts;
- records the exact test count;
- runs pytest;
- runs the credential-safe doctor;
- runs the FastAPI dashboard/API smoke test;
- inspects an official-shape odds-validation fixture;
- verifies deterministic demos;
- builds wheel and sdist;
- checks all live-web modules exist in the wheel;
- runs the web smoke test from the isolated wheel;
- scans source, deployment files, and demo output for secret material;
- creates an SPDX SBOM;
- creates and re-verifies the deterministic judge pack.

Required evidence:

```text
outputs/local_validation_report.json
outputs/test_collection.json
outputs/release_assets.json
RELEASE_REPORT.json
outputs/demo/index.html
dist/
```

No historical result counts as current release evidence.

## Deployment

### Docker

```bash
docker build -t proofguard-live .
docker run --rm -p 8080:8080 \
  -e PROOFGUARD_MODE=REPLAY \
  proofguard-live
```

For live mode, add the private TxLINE and ProofGuard environment variables through the hosting platform rather than the command history.

### Managed container platforms

The standalone project includes:

```text
render.yaml
railway.json
Dockerfile
```

Both deployment configurations use `/api/health` as the health check. The static-only Netlify deployment was removed from the live branch so it cannot be mistaken for the hackathon application.

## Independence

This directory is a standalone package. It does not import FinalityGate, `worldcup_2026`, or another project runtime. It has its own source, tests, CLI, FastAPI application, deployment metadata, release gate, SBOM, judge pack, documentation, and submission packet.

## Safety boundary

ProofGuard performs autonomous simulated paper decisions only. It does not:

- accept deposits or withdrawals;
- connect user wallets;
- place real-money bets;
- custody funds or crypto-assets;
- sign wagering or crypto transactions;
- execute orders for users;
- settle real positions;
- provide investment advice;
- promise profitability or production predictive accuracy.

The product demonstrates live data ingestion, autonomous strategy execution, paper-risk control, non-bypassable integrity, deterministic evidence, and transparent fallback behaviour.
