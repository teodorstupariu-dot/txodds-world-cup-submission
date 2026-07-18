# FinalityGate World Cup Resolver

FinalityGate is an independent Markets-track project for the World Cup Hackathon powered by TxODDS.

It models outcome-market finality explicitly and emits one of:

- `OPEN`
- `PENDING_FINALITY`
- `WAIT_FOR_PROOF`
- `RESOLVE`
- `DISPUTE`

A market reaches `RESOLVE` only when the market definition, fixture identity, fixture finality, score-derived result, declared result, concrete proof reference, and concrete root evidence agree. Missing evidence fails closed to `WAIT_FOR_PROOF`; conflicting or tampered evidence produces `DISPUTE`.

## Independence

This directory is a standalone Python project. It does not import from or depend on ProofGuard, `worldcup_2026`, or another product directory. It has its own package metadata, CLI, tests, local gate, release report, demo, SPDX SBOM, security scan, deterministic judge pack, Dockerfile, Netlify configuration, and build artifacts.

## Local setup

### Windows PowerShell

```powershell
cd products\finalitygate-resolver
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
powershell -ExecutionPolicy Bypass -File scripts\run_local_gate.ps1
```

### Linux or macOS

```bash
cd products/finalitygate-resolver
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
bash scripts/run_local_gate.sh
```

The gate runs only on the local machine. It does not invoke GitHub Actions.

Required outputs:

```text
outputs/local_validation_report.json
outputs/test_collection.json
outputs/release_assets.json
RELEASE_REPORT.json
outputs/demo/index.html
dist/finalitygate_worldcup_resolver-0.1.0-py3-none-any.whl
dist/finalitygate-worldcup-resolver-0.1.0.tar.gz
dist/finalitygate-worldcup-resolver.spdx.json
dist/finalitygate-worldcup-resolver-judge-pack.zip
```

Exact package filenames are authoritative only after the local gate writes `RELEASE_REPORT.json`.

## Configuration check

```bash
finalitygate doctor
```

The doctor command reports whether the TxLINE guest JWT and activated API token are present without printing either value.

## Quick demo

```bash
finalitygate demo --out outputs/demo
finalitygate verify-demo outputs/demo
```

Open:

```text
outputs/demo/index.html
```

The deterministic demo covers:

- scheduled market → `OPEN`
- live market → `PENDING_FINALITY`
- final result without proof → `WAIT_FOR_PROOF`
- score/result conflict → `DISPUTE`
- root conflict → `DISPUTE`
- complete consistent evidence → `RESOLVE`

## Resolve a custom market

Create `market.json`:

```json
{
  "market_id": "market-1",
  "fixture_id": "fixture-1",
  "market_type": "MATCH_RESULT",
  "selections": ["HOME", "DRAW", "AWAY"],
  "policy_version": "finalitygate-v1"
}
```

Create `evidence.json`:

```json
{
  "fixture_id": "fixture-1",
  "fixture_status": "FINAL",
  "home_score": 2,
  "away_score": 1,
  "declared_result": "HOME",
  "observed_at": "2026-07-08T12:00:00Z",
  "proof_status": "VALID",
  "root_status": "MATCH",
  "proof_reference": "proof-batch-001",
  "expected_root": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "observed_root": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "source_fingerprint": "txline-schema-v1"
}
```

Run:

```bash
finalitygate resolve --market market.json --evidence evidence.json --out resolution.json
finalitygate verify-receipt --input resolution.json
```

## Official TxODDS score-validation inspection

TxODDS documents score-stat validation through `/api/scores/stat-validation` and a Solana read-only `validateStat(...).view()` call. FinalityGate now implements the strict offline portion of that workflow.

Inspect a saved, authorized validation response:

```bash
finalitygate inspect-score-validation \
  --input validation.json \
  --out outputs/validation_inspection.json
```

The inspector validates and normalizes:

- `summary.fixtureId`;
- update count and timestamp bounds;
- the event-stats subtree root;
- subtree and main-tree proof nodes;
- the statistic to prove;
- event-stat root and stat proof;
- optional second-stat evidence;
- exact 32-byte hash encodings from hex, base64, or byte arrays;
- the documented `daily_scores_roots` literal seed;
- the little-endian unsigned 16-bit epoch-day seed.

It produces a deterministic structural fingerprint and always reports:

```text
onchain_view_executed: false
```

This prevents structural inspection from being mislabeled as complete on-chain validation.

## Authorized live validation retrieval

Copy `.env.example` to a local `.env` workflow or set variables privately in the shell:

```text
TXLINE_NETWORK
TXLINE_ORIGIN
TXLINE_GUEST_JWT
TXLINE_API_TOKEN
```

Then run:

```bash
finalitygate live-score-validation \
  --fixture-id FIXTURE_ID \
  --seq SEQUENCE \
  --stat-key STAT_KEY \
  --out outputs/live_score_validation.json
```

For a two-stat request:

```bash
finalitygate live-score-validation \
  --fixture-id FIXTURE_ID \
  --seq SEQUENCE \
  --stat-key STAT_KEY_1 \
  --stat-key2 STAT_KEY_2 \
  --out outputs/live_score_validation.json
```

The command does not persist the raw licensed response. It retains the normalized structural inspection, fingerprint, safe request identifiers, explicit claim boundary, and no credentials.

## Receipt contract

Every resolution decision contains a deterministic SHA-256 receipt binding:

- market definition;
- fixture/result evidence;
- proof and root status;
- proof reference and expected/observed root values;
- policy version;
- finality state;
- resolved selection, when any;
- reasons and individual checks.

Changing any bound field invalidates the receipt hash.

## Fail-closed rules

FinalityGate cannot emit `RESOLVE` when any of the following is true:

- fixture identity does not match the market;
- fixture is not final;
- score/result data is incomplete;
- declared result conflicts with the score;
- proof is missing, invalid, or unverified;
- a `VALID` proof status has no concrete proof reference;
- root status is missing, mismatched, or unverified;
- a `MATCH` root status has no concrete expected and observed roots;
- expected and observed root values differ.

These conditions produce `OPEN`, `PENDING_FINALITY`, `WAIT_FOR_PROOF`, or `DISPUTE` depending on the evidence.

## Current proof boundary

The project currently provides:

- fail-closed proof/root evidence contracts;
- strict inspection of the documented TxODDS score-validation payload shape;
- exact proof-node hash and direction validation;
- documented PDA seed-input derivation;
- authorized validation-endpoint retrieval without raw-payload persistence.

It does **not** yet execute the Solana program's `validateStat(...).view()` method. Therefore it does not claim complete TxODDS end-to-end proof verification.

A complete proof claim requires:

1. official validation material for a known record;
2. the matching network, program ID, IDL, and daily-scores PDA;
3. execution of the documented read-only Solana validation method;
4. a known-valid result;
5. altered proof/root cases that fail;
6. local reproduction and evidence capture.

Until these are implemented and reproduced, FinalityGate retains the explicit limitation and uses `WAIT_FOR_PROOF` or `DISPUTE` as appropriate.

## Release evidence

The full local gate:

- compiles source, tests, and scripts;
- records the exact pytest collection count;
- runs all tests;
- checks the credential-safe doctor path;
- inspects an official-shape validation fixture;
- creates two deterministic demos and compares their complete hashes;
- verifies all required finality states;
- builds wheel and sdist;
- checks the TxLINE client and validation inspector exist in the wheel;
- executes doctor, validation inspection, demo, and verification from the isolated wheel;
- scans source and demo output for high-risk secret material;
- generates an SPDX 2.3 SBOM;
- creates a deterministic judge pack with its own internal SHA-256 manifest.

No historical workflow result counts as current release evidence.

## Deployment

The standalone export contains:

```text
netlify.toml
Dockerfile
SUBMISSION.md
LICENSE
```

Netlify builds the deterministic static demo. The Dockerfile produces an nginx image containing only the verified static judge site.

## Safety boundary

FinalityGate is a hackathon prototype. It does not:

- custody funds;
- release real escrow;
- execute wagers;
- settle real-money positions;
- sign user transactions;
- provide financial advice.

The public judge path is deterministic and requires no wallet, payment, account, API token, or private data.
