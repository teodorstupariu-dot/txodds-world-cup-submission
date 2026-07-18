# TxODDS World Cup Portfolio Host (PG-021)

A combined **one-service** deployment host that runs both hackathon products in a
single FastAPI/Uvicorn process for cost-efficient always-on hosting:

- `/` — landing page
- `/health` — combined health (each component reported separately)
- `/proofguard/` + `/proofguard/api/...` — the **real** ProofGuard app (`proofguard_agent.web.app`)
- `/finalitygate/` + `/finalitygate/api/...` — a **host-owned** FinalityGate adapter

The host does **not** modify either product's internal runtime. ProofGuard is
mounted as its own ASGI app (its autonomous background loop runs under the shared
process lifespan). FinalityGate has no ASGI app of its own, so this package adds a
thin adapter that calls only FinalityGate's public deterministic demo API
(`finalitygate.demo.run_demo`) and serves the result read-only.

Standalone product deployments and exports are unchanged; this host is an
additional, optional cost-saving deployment target.

## Local setup

```powershell
cd products\txodds-portfolio-host
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install ..\proofguard-agent ..\finalitygate-resolver
python -m pip install -e ".[dev]"
python -m pytest -q
```

## Run locally (credential-free REPLAY)

```powershell
$env:PROOFGUARD_MODE = "REPLAY"
$env:PROOFGUARD_AUTO_START = "true"
python -m txodds_portfolio_host
```

Then open <http://127.0.0.1:8080/>, `/health`, `/proofguard/`, `/finalitygate/`.

## Docker (one service)

Build context is the repository `products/` directory so both products are visible:

```bash
docker build -f products/txodds-portfolio-host/Dockerfile -t txodds-portfolio-host products
docker run --rm -p 8080:8080 txodds-portfolio-host
```

Healthcheck hits `/health`. Runs as a non-root user.

## Deploy metadata

- `render.yaml` — one Render web service (`runtime: docker`, `dockerContext: ./products`, `healthCheckPath: /health`).
- `railway.json` — one Railway service (`builder: DOCKERFILE`, `healthcheckPath: /health`).

Provide `TXLINE_GUEST_JWT` / `TXLINE_API_TOKEN` privately in the provider dashboard
to let the mounted ProofGuard app run in LIVE mode; otherwise it serves labelled
REPLAY. FinalityGate stays deterministic and credential-free.

## Isolation guarantees (tested)

- separate routes and OpenAPI schemas per product;
- both application lifespans execute (ProofGuard loop runs);
- one product failure does not hide or corrupt the other in `/health`;
- no credentials or state cross between mounted apps.
