"""Combined one-service host mounting both TxODDS World Cup products.

Routes:
    /                       landing page
    /health                 combined health (each component reported separately)
    /proofguard/            mounted ProofGuard app (real product ASGI app)
    /proofguard/api/...     ProofGuard API
    /finalitygate/          mounted FinalityGate app (real product ASGI app)
    /finalitygate/api/...   FinalityGate API

One Uvicorn process runs both application lifespans (so the ProofGuard
autonomous background loop starts) with route and OpenAPI isolation. A failure
in one mounted app does not silently corrupt the other, and no credentials or
state cross between them.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

import httpx
from finalitygate.web.app import create_app as create_finalitygate_app
from proofguard_agent.web.app import create_app as create_proofguard_app


async def _probe_component(mounted_app: FastAPI) -> tuple[bool, dict[str, Any]]:
    """Call a mounted app's /api/health in-process and summarize the result.

    Any failure (non-200, non-PASS body, or a raised handler exception) is
    reported as FAIL for that component only; it never propagates to the other.
    """

    transport = httpx.ASGITransport(app=mounted_app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://component") as client:
            response = await client.get("/api/health")
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        status = body.get("status") if isinstance(body, dict) else None
        component_ok = response.status_code == 200 and status == "PASS"
        detail: dict[str, Any] = {"status": status or ("PASS" if component_ok else "FAIL"), "http_status": response.status_code}
        if isinstance(body, dict) and body.get("source_mode"):
            detail["source_mode"] = body["source_mode"]
        return component_ok, detail
    except Exception as exc:  # noqa: BLE001 - one component must never crash /health
        return False, {"status": "FAIL", "error": type(exc).__name__}

LANDING_HTML = """<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='color-scheme' content='dark'><title>ProofGuard + FinalityGate — TxODDS World Cup</title>
<style>
:root{--bg:#07111f;--panel:#101d31;--panel2:#14243d;--border:#29405f;--text:#eef4ff;--muted:#a8b7cf;--blue:#7aa2ff;--green:#4ade80;--amber:#fbbf24}
*{box-sizing:border-box}html,body{overflow-x:hidden}
body{margin:0;background:radial-gradient(circle at 20% 0,#11294a 0,transparent 32%),var(--bg);color:var(--text);font:16px/1.6 system-ui,ui-sans-serif,sans-serif}
main{max-width:1080px;margin:auto;padding:52px 22px 72px}
a{color:var(--blue)}
.eyebrow{letter-spacing:.12em;text-transform:uppercase;color:var(--blue);font-size:.74rem;font-weight:700}
h1{font-size:clamp(2.2rem,5vw,3.6rem);line-height:1.02;margin:.15em 0 .12em}
.sub{color:var(--muted);font-size:clamp(1.05rem,2.2vw,1.3rem);margin:.2em 0 0;max-width:820px}
.lead{margin:20px 0 0;max-width:860px}
.pill{display:inline-flex;align-items:center;gap:8px;padding:6px 12px;border:1px solid var(--border);border-radius:999px;background:#0d1a2c;font-size:.8rem;font-weight:700}
.pill .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 12px currentColor}
h2{font-size:1.05rem;margin:0 0 12px}
.section{margin-top:34px}
.steps{list-style:none;counter-reset:s;margin:0;padding:0;display:grid;gap:10px}
.steps li{counter-increment:s;display:flex;gap:12px;align-items:baseline;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:12px 15px}
.steps li::before{content:counter(s);flex:0 0 auto;width:24px;height:24px;border-radius:50%;background:#0a1728;border:1px solid var(--border);color:var(--blue);font-weight:800;font-size:.8rem;display:grid;place-items:center}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px}
.card{display:block;text-decoration:none;color:inherit;background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--border);border-radius:16px;padding:22px}
.card:hover{border-color:var(--blue)}
.card h3{margin:0 0 6px;font-size:1.32rem}
.card .tag{font-size:.72rem;text-transform:uppercase;letter-spacing:.08em;color:var(--blue);font-weight:700}
.card p{color:var(--muted);margin:10px 0 12px}
.states span{display:inline-block;font-size:.7rem;font-weight:700;padding:3px 8px;border:1px solid var(--border);border-radius:8px;margin:3px 4px 0 0;color:var(--muted)}
.go{color:var(--blue);font-weight:700}
.notice{border-left:4px solid var(--amber);padding:14px 16px;background:#211b0b;color:#fde68a;border-radius:10px}
.links{display:flex;flex-wrap:wrap;gap:8px}
.links a{display:inline-block;padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:#0d1a2c;text-decoration:none;font-size:.85rem}
.links a:hover{border-color:var(--blue)}
.foot{margin-top:34px;color:var(--muted);font-size:.82rem}
.xp{background:linear-gradient(180deg,#152a4a,#0f2038);border-color:#3a5da0}
.xp:hover{border-color:var(--green)}.xp .tag{color:var(--green)}
.badge2{display:inline-block;font-size:.64rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#052012;background:var(--green);padding:3px 8px;border-radius:6px;margin-left:8px;vertical-align:middle}
.trackmap{margin:16px 0 0;display:flex;flex-wrap:wrap;gap:10px}
.trackmap span{font-size:.84rem;color:var(--muted);background:#0d1a2c;border:1px solid var(--border);border-radius:999px;padding:6px 13px}
.trackmap b{color:var(--text)}.trackmap b.pg{color:var(--blue)}.trackmap b.fg{color:var(--green)}
.tour{list-style:none;margin:0;padding:0;display:grid;gap:10px}
.tour a{display:flex;gap:14px;align-items:center;text-decoration:none;color:inherit;background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.tour a:hover{border-color:var(--blue)}
.tour .n{flex:0 0 auto;width:30px;height:30px;border-radius:50%;background:#0a1728;border:1px solid var(--blue);color:var(--blue);font-weight:800;display:grid;place-items:center}
.tour .t{color:var(--muted);font-size:.9rem}
.btnbig{cursor:pointer;font:inherit;font-weight:800;padding:11px 18px;border-radius:10px;border:1px solid var(--blue);background:#12233f;color:var(--text)}
.btnbig:hover{background:#17304f}
.trust{margin-top:12px;display:grid;gap:8px}
.trust .row{display:flex;justify-content:space-between;gap:12px;align-items:center;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:11px 14px}
.trust .row .lab{font-weight:600}.trust .row .st{font-weight:800;font-size:.85rem;white-space:nowrap}
.trust .st.ok{color:var(--green)}.trust .st.bad{color:#fb7185}.trust .st.run{color:var(--muted)}
.trust .allbanner{font-weight:800;padding:11px 14px;border-radius:10px;border:1px solid var(--border);text-align:center}
.trust .allbanner.ok{color:var(--green);border-color:var(--green);background:#0c2418}
.trust .allbanner.bad{color:#fb7185;border-color:#fb7185;background:#2a1018}
</style></head><body><main>

<div class='eyebrow'>World Cup Hackathon · powered by TxODDS</div>
<h1>ProofGuard + FinalityGate</h1>
<p class='sub'>A simulation-only integrity stack for TxODDS-style football market applications.</p>
<p class='lead'>ProofGuard checks trading-agent decisions before execution. FinalityGate checks whether a football-market outcome can resolve safely from finality, proof, root, and score evidence.</p>
<p style='margin-top:16px'><span class='pill'><span class='dot'></span>Live public demo · REPLAY mode · simulation-only</span></p>
<div class='trackmap'>
  <span><b class='pg'>ProofGuard</b> → Trading Tools &amp; Agents</span>
  <span><b class='fg'>FinalityGate</b> → Prediction Markets &amp; Settlement</span>
</div>

<section class='section'>
<h2>60-second judge tour</h2>
<ol class='tour'>
  <li><a href='/proofguard/playground?scenario=corrupt_block'><span class='n'>1</span><div><strong>Attack the safety gate</strong> · Trading Agents<br><span class='t'>Feed the autonomous agent a corrupted feed — a 36%-edge signal is still rejected by the non-bypassable integrity gate.</span></div></a></li>
  <li><a href='/proofguard/api/receipts/verify'><span class='n'>2</span><div><strong>Verify the receipt chain</strong> · Trading Agents<br><span class='t'>Re-prove the live decision log is a tamper-evident hash chain — read <code>status: PASS</code> yourself.</span></div></a></li>
  <li><a href='/finalitygate/explorer?auto=1'><span class='n'>3</span><div><strong>Fold a Merkle settlement proof</strong> · Markets<br><span class='t'>Click a resolution fact and watch its inclusion proof fold to the 32-byte root, re-verified live in your browser — then hit “flip a byte” and watch tamper detection fire.</span></div></a></li>
  <li><a href='/finalitygate/resolver'><span class='n'>4</span><div><strong>Flip the evidence yourself</strong> · Markets<br><span class='t'>Change proof / root / score and watch the fail-closed state machine resolve, wait, or dispute live — the real resolver, not a mock.</span></div></a></li>
</ol>
</section>

<section class='section'>
<h2>Interactive judge demos <span class='badge2'>start here</span></h2>
<div class='cards'>
  <a class='card xp' href='/proofguard/playground'>
    <div class='tag'>Try it · Trading Agents</div>
    <h3>Integrity Gate Playground →</h3>
    <p>Feed the autonomous agent a deliberate <strong>attack</strong> — corrupted odds, backwards timestamp, no proof — and watch the non-bypassable integrity gate reject it live. A 36%-edge signal still cannot pass a failed integrity check.</p>
    <span class='go'>Attack the gate →</span>
  </a>
  <a class='card xp' href='/finalitygate/explorer'>
    <div class='tag'>Try it · Markets</div>
    <h3>Merkle Proof Explorer →</h3>
    <p>Click any resolution fact and watch its inclusion proof fold cryptographically up to the 32-byte settlement root — <strong>re-verified live in your browser</strong> with real SHA-256, no server trust.</p>
    <span class='go'>Verify a proof →</span>
  </a>
</div>
</section>

<section class='section'>
<div class='cards'>
  <a class='card' href='/proofguard/'>
    <div class='tag'>Track · Trading Agents</div>
    <h3>ProofGuard →</h3>
    <p>Pre-execution safety layer for autonomous trading agents. Runs in REPLAY mode in this public demo. Shows decisions, safety state, receipts, and credential-safe snapshots.</p>
    <span class='go'>Open dashboard →</span>
  </a>
  <a class='card' href='/finalitygate/'>
    <div class='tag'>Track · Markets</div>
    <h3>FinalityGate →</h3>
    <p>Fail-closed market-resolution engine. Resolves only when finality, proof, root, and score evidence agree.</p>
    <div class='states'><span>OPEN</span><span>PENDING_FINALITY</span><span>WAIT_FOR_PROOF</span><span>DISPUTE</span><span>RESOLVE</span></div>
    <p style='margin-top:14px'><span class='go'>Open dashboard →</span></p>
  </a>
</div>
</section>

<section class='section'>
<h2>Verify it yourself — no server trust</h2>
<p class='sub' style='font-size:1rem;margin:0 0 12px'>Every integrity claim here is independently checkable. Click once to re-run all cryptographic verifications live — the Merkle settlement proof is folded in <strong>your own browser</strong> with SHA-256.</p>
<button class='btnbig' id='verifyAllBtn' type='button'>▶ Verify all cryptographic claims now</button>
<div class='trust' id='trust'>
  <div class='row' data-k='pg'><span class='lab'>ProofGuard — hash-linked receipt chain re-proved</span><span class='st run'>idle</span></div>
  <div class='row' data-k='fg'><span class='lab'>FinalityGate — batch settlement ledger re-proved</span><span class='st run'>idle</span></div>
  <div class='row' data-k='merkle'><span class='lab'>FinalityGate — Merkle proof folded in your browser (SHA-256)</span><span class='st run'>idle</span></div>
  <div class='allbanner' id='allbanner' style='display:none'></div>
</div>
</section>

<section class='section'>
<div class='notice'>Simulation-only public demo. No custody. No wagering. No real-money settlement. No financial advice. No on-chain execution in this demo.</div>
</section>

<section class='section'>
<h2>All demo links</h2>
<div class='links'>
<a href='/health'>/health</a>
<a href='/proofguard/'>/proofguard/</a>
<a href='/proofguard/api/snapshot'>/proofguard/api/snapshot</a>
<a href='/finalitygate/'>/finalitygate/</a>
<a href='/finalitygate/api/demo'>/finalitygate/api/demo</a>
<a href='/finalitygate/api/status'>/finalitygate/api/status</a>
<a href='/finalitygate/api/docs'>/finalitygate/api/docs</a>
</div>
</section>

<p class='foot'>One always-on service · two independent products. Each product keeps its own routes, OpenAPI schema, health, and state.</p>

<script>
const NODE=new Uint8Array([1]);
function hexToBytes(h){const a=new Uint8Array(h.length/2);for(let i=0;i<a.length;i++)a[i]=parseInt(h.substr(i*2,2),16);return a;}
function bytesToHex(b){return Array.from(b).map(x=>x.toString(16).padStart(2,'0')).join('');}
function concat(){let n=0;for(const a of arguments)n+=a.length;const o=new Uint8Array(n);let p=0;for(const a of arguments){o.set(a,p);p+=a.length;}return o;}
async function sha256(b){return new Uint8Array(await crypto.subtle.digest('SHA-256',b));}
async function nodeHash(l,r){return await sha256(concat(NODE,l,r));}
function setRow(k,cls,txt){const el=document.querySelector('#trust .row[data-k="'+k+'"] .st');if(el){el.className='st '+cls;el.textContent=txt;}}
async function foldMerkle(commit){
  const field=commit.leaves[0].field;const leaf=commit.leaves.find(l=>l.field===field);
  let acc=hexToBytes(leaf.leaf_hash);
  for(const step of (commit.proofs[field]||[])){const sib=hexToBytes(step.sibling);acc=step.position==='left'?await nodeHash(sib,acc):await nodeHash(acc,sib);}
  return bytesToHex(acc)===commit.root;
}
async function verifyAll(){
  const btn=document.getElementById('verifyAllBtn');btn.disabled=true;
  const banner=document.getElementById('allbanner');banner.style.display='none';
  ['pg','fg','merkle'].forEach(k=>setRow(k,'run','checking…'));
  const ok={pg:false,fg:false,merkle:false};
  try{const b=await (await fetch('/proofguard/api/receipts/verify',{cache:'no-store'})).json();ok.pg=b.status==='PASS';setRow('pg',ok.pg?'ok':'bad',(ok.pg?'✓ PASS · ':'✗ FAIL · ')+(b.window||0)+' receipts');}catch(e){setRow('pg','bad','✗ '+e.message);}
  try{const b=await (await fetch('/finalitygate/api/ledger',{cache:'no-store'})).json();ok.fg=!!(b.verification&&b.verification.status==='PASS');setRow('fg',ok.fg?'ok':'bad',(ok.fg?'✓ PASS · ':'✗ FAIL · ')+((b.ledger&&b.ledger.count)||0)+' resolutions');}catch(e){setRow('fg','bad','✗ '+e.message);}
  try{const b=await (await fetch('/finalitygate/api/commitments/demo',{cache:'no-store'})).json();const c=(b.commitments||[])[0];ok.merkle=c?await foldMerkle(c.commitment):false;setRow('merkle',ok.merkle?'ok':'bad',ok.merkle?'✓ folds to the committed root':'✗ did not fold');}catch(e){setRow('merkle','bad','✗ '+e.message);}
  const all=ok.pg&&ok.fg&&ok.merkle;banner.style.display='block';banner.className='allbanner '+(all?'ok':'bad');
  banner.textContent=all?'✓ ALL CRYPTOGRAPHIC CLAIMS INDEPENDENTLY VERIFIED':'✗ one or more checks did not pass — try again once the demo has warmed up';
  btn.disabled=false;
}
document.getElementById('verifyAllBtn').addEventListener('click',verifyAll);
</script>

</main></body></html>"""


def create_host_app(
    *,
    proofguard_app: FastAPI | None = None,
    finalitygate_app: FastAPI | None = None,
) -> FastAPI:
    proofguard = proofguard_app or create_proofguard_app()
    finalitygate = finalitygate_app or create_finalitygate_app()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Run both mounted application lifespans so each background loop and
        # startup hook executes. Nesting keeps them independent.
        async with proofguard.router.lifespan_context(proofguard):
            async with finalitygate.router.lifespan_context(finalitygate):
                yield

    app = FastAPI(
        title="TxODDS World Cup Portfolio Host",
        version="0.1.0",
        description="One service mounting the ProofGuard and FinalityGate products independently",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )
    app.state.proofguard = proofguard
    app.state.finalitygate = finalitygate

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        if request.url.path.startswith(("/health", "/proofguard/api/", "/finalitygate/api/")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def landing() -> HTMLResponse:
        return HTMLResponse(LANDING_HTML)

    @app.get("/health")
    async def health() -> JSONResponse:
        # Probe each mounted app's real /api/health through an in-process ASGI
        # call. This reflects the actually-mounted component and isolates it:
        # one product's failure cannot hide or crash the other's report.
        components: dict[str, Any] = {}
        overall_ok = True
        for name, mounted in (("proofguard", proofguard), ("finalitygate", finalitygate)):
            component_ok, detail = await _probe_component(mounted)
            components[name] = detail
            overall_ok = overall_ok and component_ok

        payload = {
            "status": "PASS" if overall_ok else "FAIL",
            "service": "txodds-portfolio-host",
            "components": components,
            "claim_boundary": "combined always-on host; both products remain independently deployable and simulation-only",
        }
        return JSONResponse(payload, status_code=200 if overall_ok else 503)

    # Mount products under isolated prefixes (each keeps its own OpenAPI/docs).
    app.mount("/finalitygate", finalitygate)
    app.mount("/proofguard", proofguard)
    return app


app = create_host_app()


def run() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("txodds_portfolio_host.app:app", host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    run()
