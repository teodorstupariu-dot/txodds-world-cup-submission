"""Native FinalityGate web application.

FinalityGate is a deterministic, offline finality resolver. This module wraps
its public API (`OutcomeMarket`, `ResolutionEvidence`, `FinalityGateResolver`,
`verify_receipt`, and the deterministic demo) in a small FastAPI app so the
product can be hosted as an independent web service and mounted by the shared
portfolio host.

The app performs no network calls, reads no credentials or ``.env`` values,
persists nothing per request, and never claims real settlement or executed
Solana on-chain validation. The deterministic demo summary is computed once at
startup and kept in memory.
"""

from __future__ import annotations

import html
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, AsyncIterator, Literal

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class _NonFiniteNumber(Exception):
    """Raised while scanning a JSON body that contains NaN/Infinity constants."""


def _reject_non_finite(_: str) -> float:
    raise _NonFiniteNumber

from .. import __version__
from ..commitment import build_commitment, verify_proof
from ..core import (
    FinalityGateResolver,
    OutcomeMarket,
    ResolutionEvidence,
    verify_receipt,
)
from ..demo import build_demo_summary
from ..explain import explain_decision
from ..impact import settlement_impact
from ..ledger import build_ledger, verify_ledger
from ..onchain import commitment_anchor

# Bound on a single batch settlement request so the endpoint cost stays fixed.
MAX_BATCH = 50

# Ordered exactly as presented to judges on the dashboard.
REQUIRED_STATES: tuple[str, ...] = (
    "OPEN",
    "PENDING_FINALITY",
    "WAIT_FOR_PROOF",
    "DISPUTE",
    "RESOLVE",
)

# This service never executes the Solana validateStat view. The boundary is a
# hard constant so every route reports it identically and it can never silently
# flip to true.
ONCHAIN_VIEW_EXECUTED = False

CLAIM_BOUNDARY = (
    "deterministic finality resolution prototype; no custody, escrow release, "
    "wagering, or real-money settlement, and no Solana on-chain validation executed"
)


# ---------------------------------------------------------------------------
# Deterministic in-memory demo summary
# ---------------------------------------------------------------------------
def compute_summary() -> dict[str, Any]:
    """Compute the deterministic FinalityGate demo summary in memory.

    Delegates to the pure ``build_demo_summary`` so web startup performs no
    filesystem I/O and exposes no local paths or artifact manifest.
    """

    return build_demo_summary()


def build_health(summary: dict[str, Any]) -> dict[str, Any]:
    """Derive the health contract from explicit positive checks only."""

    counts = summary.get("state_counts", {})
    cases = summary.get("cases", [])
    demo_pass = summary.get("status") == "PASS"
    all_states_present = set(REQUIRED_STATES).issubset(counts)
    demo_receipts_verified = bool(cases) and all(
        case.get("receipt_verification", {}).get("status") == "PASS" for case in cases
    )
    onchain_view_not_executed = ONCHAIN_VIEW_EXECUTED is False
    ok = demo_pass and all_states_present and demo_receipts_verified and onchain_view_not_executed
    return {
        "status": "PASS" if ok else "FAIL",
        "component": "finalitygate",
        "checks": {
            "deterministic_demo_pass": demo_pass,
            "all_states_present": all_states_present,
            "demo_receipts_verified": demo_receipts_verified,
            "onchain_view_not_executed": onchain_view_not_executed,
        },
        "state_counts": counts,
        "case_count": summary.get("case_count", 0),
        "onchain_view_executed": ONCHAIN_VIEW_EXECUTED,
        "simulation_only": True,
    }


# ---------------------------------------------------------------------------
# Strict request models (unknown fields and wrong types are rejected)
# ---------------------------------------------------------------------------
class MarketModel(BaseModel):
    # Reject unknown fields and, via Strict* types, silent primitive coercion
    # (e.g. a JSON number for a string field, or a string for an int field).
    model_config = ConfigDict(extra="forbid")

    market_id: StrictStr
    fixture_id: StrictStr
    market_type: Literal["MATCH_RESULT"] = "MATCH_RESULT"
    selections: list[StrictStr]
    policy_version: StrictStr = "finalitygate-v1"


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fixture_id: StrictStr
    fixture_status: StrictStr
    home_score: StrictInt | None = None
    away_score: StrictInt | None = None
    declared_result: StrictStr | None = None
    # observed_at stays a plain datetime (not strict) so a JSON ISO-8601 string
    # is still accepted and converted to a timezone-aware datetime.
    observed_at: datetime
    proof_status: Literal["VALID", "MISSING", "INVALID", "UNVERIFIED"] = "UNVERIFIED"
    root_status: Literal["MATCH", "MISSING", "MISMATCH", "UNVERIFIED"] = "UNVERIFIED"
    proof_reference: StrictStr | None = None
    expected_root: StrictStr | None = None
    observed_root: StrictStr | None = None
    source_fingerprint: StrictStr | None = None


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: MarketModel
    evidence: EvidenceModel


class BatchResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolutions: list[ResolveRequest]


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
def _dashboard_html(summary: dict[str, Any]) -> str:
    counts = summary.get("state_counts", {})
    cases = summary.get("cases", [])
    decisions = [case["decision"] for case in cases]
    ledger = build_ledger(decisions) if decisions else None
    batch_root = (ledger or {}).get("batch_root") or "unavailable"
    impact = settlement_impact(decisions)

    chips = " ".join(
        f"<span class='chip {html.escape(state)}'>{html.escape(state)}: {counts.get(state, 0)}</span>"
        for state in REQUIRED_STATES
    )
    rows = []
    for decision, case in zip(decisions, cases):
        root = build_commitment(decision)["root"]
        rows.append(
            "<tr>"
            f"<td>{html.escape(case['name'])}</td>"
            f"<td><strong class='state {html.escape(decision['state'])}'>{html.escape(decision['state'])}</strong></td>"
            f"<td>{html.escape(str(decision.get('resolved_selection') or '—'))}</td>"
            f"<td>{html.escape(', '.join(decision['reasons']))}</td>"
            f"<td><code title='{html.escape(root)}'>{html.escape(root[:16])}…</code></td>"
            f"<td><code title='{html.escape(decision['receipt_sha256'])}'>{html.escape(decision['receipt_sha256'][:16])}…</code></td>"
            "</tr>"
        )

    template = R"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='color-scheme' content='dark'><title>FinalityGate World Cup Resolver</title>
<style>
:root{--bg:#07111f;--panel:#101d31;--panel2:#14243d;--border:#29405f;--text:#eef4ff;--muted:#a8b7cf;--blue:#7aa2ff;--green:#4ade80;--amber:#fbbf24;--red:#fb7185}
*{box-sizing:border-box}html,body{overflow-x:hidden}
body{margin:0;background:radial-gradient(circle at 18% -5%,#173461 0,transparent 40%),radial-gradient(circle at 92% 0,#231a3f 0,transparent 34%),var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:36px 20px 64px}p{color:var(--muted);line-height:1.6;max-width:900px}
.eyebrow{letter-spacing:.12em;text-transform:uppercase;color:var(--blue);font-size:.74rem;font-weight:700}
h1{font-size:clamp(2rem,5vw,3.4rem);margin:.2em 0}h2{font-size:1rem;margin:0 0 12px}
.states{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.chip{display:inline-block;padding:7px 12px;border:1px solid var(--border);border-radius:999px;font-size:.82rem;font-weight:700;background:#0d1a2c;color:var(--muted)}
.chip.RESOLVE{color:var(--green)}.chip.DISPUTE{color:var(--red)}.chip.WAIT_FOR_PROOF,.chip.PENDING_FINALITY{color:var(--amber)}.chip.OPEN{color:var(--blue)}
.impact{margin:18px 0 4px;padding:18px 20px;border:1px solid #2f5138;border-radius:16px;background:linear-gradient(120deg,#0c2418,#0d1b2e)}
.impact-k{font-weight:800;font-size:1.02rem}.impact-sub{color:var(--muted);font-weight:600;font-size:.82rem;margin-left:6px}
.impact-row{display:flex;flex-wrap:wrap;gap:14px;margin:12px 0 4px}
.impact-fig{flex:1 1 180px;min-width:0;background:#0a1728;border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.impact-n{font-size:2.1rem;font-weight:900;color:var(--green);line-height:1;font-variant-numeric:tabular-nums}
.impact-l{color:var(--muted);font-size:.82rem;margin-top:6px}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--border);border-radius:16px;padding:18px;margin-top:16px;box-shadow:0 18px 50px rgba(0,0,0,.2)}
.commit{display:flex;gap:14px;flex-wrap:wrap;align-items:center;justify-content:space-between}
.root{font-family:ui-monospace,monospace;font-size:.82rem;color:#c7d2fe;word-break:break-all;background:#0a1728;border:1px solid var(--border);border-radius:10px;padding:10px 12px;flex:1;min-width:260px}
.btn{cursor:pointer;font:inherit;font-weight:700;padding:8px 14px;border-radius:9px;border:1px solid var(--blue);background:#12233f;color:var(--text)}.btn:hover{background:#17304f}
.vbadge{font-weight:800;padding:6px 11px;border:1px solid var(--border);border-radius:999px;background:#0a1728}.ok{color:var(--green)}.bad{color:var(--red)}
.scroll{overflow:auto;margin-top:14px}
table{width:100%;border-collapse:collapse;min-width:900px}th,td{padding:11px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
th{background:#14243d;font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
code{font-size:.72rem;color:#c7d2fe;word-break:break-all}a{color:var(--blue)}
.state{padding:3px 8px;border-radius:8px;font-weight:800}.RESOLVE{color:var(--green)}.DISPUTE{color:var(--red)}.WAIT_FOR_PROOF,.PENDING_FINALITY{color:var(--amber)}.OPEN{color:var(--blue)}
.notice{border-left:4px solid var(--amber);padding:12px 14px;background:#211b0b;color:#fde68a;border-radius:10px;margin-top:18px}
.links{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.links a{display:inline-block;padding:8px 12px;border:1px solid var(--border);border-radius:10px;background:#0d1a2c;text-decoration:none;font-size:.85rem}
.track{display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#052012;background:var(--green);padding:4px 10px;border-radius:6px;margin:0 0 6px}
</style></head><body><main>
<div class='eyebrow'>TxODDS World Cup · deterministic finality resolver</div>
<div class='track'>Track · Prediction Markets &amp; Settlement</div>
<h1>FinalityGate</h1>
<p><strong>Fail-closed resolution engine for football outcome markets.</strong> A market resolves only when market definition, fixture finality, result evidence, proof status, and on-chain root status all agree. Missing evidence waits; conflicting evidence disputes. Every decision is a deterministic policy evaluation carrying its own SHA-256 receipt and a verifiable 32-byte Merkle settlement commitment — the exact value a settlement contract would compare on-chain. <a href='explorer'>Fold a proof yourself →</a></p>
<section class='impact'>
  <div class='impact-k'>Settlement impact <span class='impact-sub'>vs a naive settle-on-declared-result resolver</span></div>
  <div class='impact-row'>
    <div class='impact-fig'><div class='impact-n'>__IMP_PREVENTED__</div><div class='impact-l'>unsafe settlements prevented</div></div>
    <div class='impact-fig'><div class='impact-n'>__IMP_NAIVE__</div><div class='impact-l'>markets a naive resolver would have settled</div></div>
    <div class='impact-fig'><div class='impact-n'>__IMP_TOTAL__</div><div class='impact-l'>markets evaluated</div></div>
  </div>
  <p style='margin:.4em 0 0'>__IMP_NOTE__</p>
</section>
<div class='states'>__CHIPS__</div>
<section class='panel'>
  <h2>Batch settlement commitment</h2>
  <p style='margin:.2em 0 12px'>One 32-byte Merkle root anchors every resolution in the demo ledger — the single value a rollup-style settlement contract would store on-chain.</p>
  <div class='commit'><span class='root' title='batch settlement root'>__BATCH_ROOT__</span>
  <span><button class='btn' id='verifyLedgerBtn' type='button'>Verify ledger</button> <span id='ledgerResult' class='vbadge'>not yet verified</span></span></div>
  <p style='margin:12px 0 0'><a href='explorer' style='font-weight:700'>→ Open the interactive Merkle proof explorer</a> — click any resolution fact and watch its inclusion proof fold to the root, re-verified live in your browser.</p>
</section>
<section class='panel'><h2>Resolution cases</h2>
<p style='margin:-4px 0 12px'>World Cup fixture <code>wc-finality-001</code> · <code>MATCH_RESULT</code> outcome market — the same football market resolved under six different evidence scenarios, so a judge sees every fail-closed state on one page. <a href='resolver' style='font-weight:700'>→ Change the evidence yourself in the Resolver Playground</a></p>
<div class='scroll'><table><thead><tr><th>Case</th><th>State</th><th>Resolved</th><th>Reasons</th><th>Commitment root</th><th>Receipt SHA-256</th></tr></thead>
<tbody>__ROWS__</tbody></table></div></section>
<section class='panel'><h2>Reproduce offline — no credentials <button class='btn' id='copyRepro' type='button'>Copy</button></h2>
<p style='margin:.2em 0 10px'>Rebuild and verify the settlement commitment + batch-root ledger yourself in ~30s. Deterministic, offline, zero secrets.</p>
<pre id='reproCmd' style='overflow:auto;background:#0a1728;border:1px solid var(--border);border-radius:10px;padding:12px 14px;color:#c7d2fe;font-size:.78rem;white-space:pre;margin:0'>pip install -e products/finalitygate-resolver
finalitygate commitment \
  --market products/finalitygate-resolver/examples/market.json \
  --evidence products/finalitygate-resolver/examples/evidence_ok.json
finalitygate ledger    # hash-linked ledger + batch root, verified</pre>
</section>
<div class='notice'>Simulation only. No custody, escrow release, wagering, or real-money settlement. No Solana on-chain validation is executed by this service (<code>onchain_view_executed: false</code>).</div>
<div class='links'><a href='resolver'>resolver playground</a><a href='explorer'>proof explorer</a><a href='api/health'>health</a><a href='api/status'>status</a><a href='api/demo'>demo</a><a href='api/ledger'>ledger</a><a href='api/onchain-anchor'>on-chain anchor</a><a href='api/docs'>docs</a></div>
<script>
async function verifyLedger(){
  var el=document.getElementById('ledgerResult'); el.textContent='verifying…'; el.className='vbadge';
  try{var r=await fetch('api/ledger',{cache:'no-store'});var b=await r.json();
    var ok=b.verification.status==='PASS';
    el.textContent=(ok?'✓ LEDGER VERIFIED':'✗ LEDGER BROKEN')+' · '+b.ledger.count+' resolutions';
    el.className='vbadge '+(ok?'ok':'bad');
  }catch(e){el.textContent='verify failed: '+e.message; el.className='vbadge bad';}
}
document.getElementById('verifyLedgerBtn').addEventListener('click',verifyLedger);
document.getElementById('copyRepro').addEventListener('click',function(){var b=document.getElementById('copyRepro');try{navigator.clipboard.writeText(document.getElementById('reproCmd').textContent);b.textContent='Copied ✓';setTimeout(function(){b.textContent='Copy';},1500);}catch(e){b.textContent='select & copy';}});
</script>
</main></body></html>"""
    prevented = impact["unsafe_settlements_prevented"]
    reason_list = ", ".join(sorted(impact.get("unsafe_settlement_reasons", {}))) or "—"
    if prevented:
        imp_note = (
            f"A naive settle-on-declared-result resolver would have settled {prevented} of "
            f"{impact['naive_settlements']} final markets on incomplete or conflicting evidence "
            f"({html.escape(reason_list)}). FinalityGate held or disputed every one."
        )
    else:
        imp_note = "A naive resolver settles on the declared result the moment a fixture is final; FinalityGate additionally requires proof, root, and score agreement."
    return (
        template
        .replace("__CHIPS__", chips)
        .replace("__ROWS__", "".join(rows))
        .replace("__BATCH_ROOT__", html.escape(batch_root))
        .replace("__IMP_PREVENTED__", str(prevented))
        .replace("__IMP_NAIVE__", str(impact["naive_settlements"]))
        .replace("__IMP_TOTAL__", str(impact["markets_considered"]))
        .replace("__IMP_NOTE__", imp_note)
    )


EXPLORER_HTML = R"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='color-scheme' content='dark'><title>FinalityGate — Merkle Proof Explorer</title>
<style>
:root{--bg:#07111f;--panel:#101d31;--panel2:#14243d;--border:#29405f;--text:#eef4ff;--muted:#a8b7cf;--blue:#7aa2ff;--green:#4ade80;--amber:#fbbf24;--red:#fb7185}
*{box-sizing:border-box}html,body{overflow-x:hidden}body{margin:0;background:radial-gradient(circle at 18% -5%,#173461 0,transparent 42%),radial-gradient(circle at 92% 0,#231a3f 0,transparent 34%),var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1180px;margin:auto;padding:34px 20px 70px}
.eyebrow{letter-spacing:.12em;text-transform:uppercase;color:var(--blue);font-size:.74rem;font-weight:700}
h1{font-size:clamp(1.8rem,4vw,2.8rem);margin:.2em 0}p{color:var(--muted);max-width:900px;line-height:1.6}
a{color:var(--blue)}
.cases{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}
.case{cursor:pointer;font:inherit;font-weight:700;padding:8px 13px;border-radius:10px;border:1px solid var(--border);background:#0d1a2c;color:var(--text)}
.case.on{border-color:var(--blue);box-shadow:0 0 0 1px var(--blue) inset}
.case .st{font-size:.68rem;display:block;color:var(--muted);font-weight:600}
.rootbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--border);border-radius:14px;padding:14px 16px;margin-top:8px}
.rootbar .k{font-size:.7rem;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
.mono{font-family:ui-monospace,monospace;word-break:break-all}
.root{font-family:ui-monospace,monospace;color:#c7d2fe;font-size:.86rem;word-break:break-all;flex:1;min-width:240px}
.grid{display:grid;grid-template-columns:1.05fr 1fr;gap:16px;margin-top:16px}
@media(max-width:860px){.grid{grid-template-columns:1fr}}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--border);border-radius:16px;padding:16px}
h2{font-size:.95rem;margin:0 0 12px}
.leaf{display:flex;justify-content:space-between;gap:10px;align-items:center;cursor:pointer;padding:9px 11px;border:1px solid var(--border);border-radius:10px;background:#0a1728;margin:7px 0}
.leaf:hover{border-color:var(--blue)}.leaf.sel{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber) inset}
.leaf .f{font-weight:700}.leaf .v{color:var(--muted);font-size:.82rem;max-width:48%;text-align:right;word-break:break-word}
.step{border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin:8px 0;background:#0a1728;opacity:0;transform:translateY(6px);transition:opacity .25s,transform .25s}
.step.show{opacity:1;transform:none}
.step .lab{font-size:.7rem;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.step .h{font-family:ui-monospace,monospace;font-size:.76rem;color:#c7d2fe;word-break:break-all;margin-top:3px}
.step .op{color:var(--amber);font-weight:700}
.verdict{font-weight:800;padding:9px 13px;border-radius:999px;border:1px solid var(--border);background:#0a1728;display:inline-block;margin-top:6px}
.ok{color:var(--green);border-color:var(--green)}.bad{color:var(--red);border-color:var(--red)}
.hint{color:var(--muted);font-size:.86rem}
.links{margin-top:22px}.links a{margin-right:14px}
.notice{border-left:4px solid var(--amber);padding:12px 14px;background:#211b0b;color:#fde68a;border-radius:10px;margin-top:20px}
.panel{min-width:0}.leaf{min-width:0}.leaf .f{min-width:0;overflow-wrap:anywhere}
.leaf .v{max-width:62%;word-break:break-all;overflow-wrap:anywhere}
.step .h,.root,#root{overflow-wrap:anywhere}
</style></head><body><main>
<div class='eyebrow'>FinalityGate · interactive settlement proof</div>
<h1>Merkle Proof Explorer</h1>
<p>Every resolution is committed to a real 32-byte SHA-256 Merkle root. Pick a resolution case, then click any fact to watch its <strong>inclusion proof</strong> fold cryptographically up to the root — <strong>re-verified live in your browser</strong> with the Web Crypto API (no server trust). This is exactly what a settlement contract checks on-chain.</p>
<p style="border-left:4px solid var(--green);background:#0c2418;color:#bbf7d0;padding:11px 14px;border-radius:10px;font-weight:600">The 32-byte root below is the single value a Solana settlement program would store and compare — computed and proven here, with <code>onchain_view_executed: false</code> (no chain call).</p>
<div id='cases' class='cases'></div>
<div class='rootbar'><span class='k'>Committed root (32 bytes)</span><span id='root' class='root'>—</span></div>
<div class='grid'>
  <section class='panel'><h2>Resolution facts (click one)</h2><div id='leaves'><p class='hint'>Loading…</p></div></section>
  <section class='panel'><h2 style='display:flex;justify-content:space-between;gap:10px;align-items:center;flex-wrap:wrap'>Inclusion proof — folded &amp; verified in-browser <button id='tamperBtn' class='case' type='button'>🔨 Flip a byte (tamper)</button></h2><div id='proof'><p class='hint'>Select a fact on the left to verify its path to the root.</p></div><div id='verdict'></div></section>
</div>
<div class='notice'>Simulation only. The proof is verified client-side with SHA-256; no on-chain call is executed (<code>onchain_view_executed: false</code>).</div>
<div class='links'><a href='./'>← dashboard</a><a href='resolver'>resolver playground</a><a href='api/ledger'>settlement ledger</a><a href='api/onchain-anchor'>on-chain anchor</a><a href='api/docs'>docs</a></div>
</main>
<script>
const NODE=new Uint8Array([1]);
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
function hexToBytes(h){const a=new Uint8Array(h.length/2);for(let i=0;i<a.length;i++)a[i]=parseInt(h.substr(i*2,2),16);return a;}
function bytesToHex(b){return Array.from(b).map(x=>x.toString(16).padStart(2,'0')).join('');}
function concat(){let n=0;for(const a of arguments)n+=a.length;const o=new Uint8Array(n);let p=0;for(const a of arguments){o.set(a,p);p+=a.length;}return o;}
async function sha256(bytes){const d=await crypto.subtle.digest('SHA-256',bytes);return new Uint8Array(d);}
async function nodeHash(l,r){return await sha256(concat(NODE,l,r));}
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let DATA=[],cur=0,TAMPER=false,curField=null,curEl=null;
function render(){
  const c=document.getElementById('cases');
  c.innerHTML=DATA.map((d,i)=>`<button class='case ${i===cur?'on':''}' data-i='${i}'>${esc(d.name)}<span class='st'>${esc(d.state)}</span></button>`).join('');
  c.querySelectorAll('.case').forEach(b=>b.addEventListener('click',()=>{cur=+b.dataset.i;render();}));
  const commit=DATA[cur].commitment;
  document.getElementById('root').textContent=commit.root;
  document.getElementById('leaves').innerHTML=commit.leaves.map(l=>`<div class='leaf' data-f='${esc(l.field)}'><span class='f'>${esc(l.field)}</span><span class='v'>${esc(JSON.stringify(l.value))}</span></div>`).join('');
  document.querySelectorAll('.leaf').forEach(el=>el.addEventListener('click',()=>verify(el.dataset.f,el)));
  document.getElementById('proof').innerHTML="<p class='hint'>Select a fact on the left to verify its path to the root.</p>";
  document.getElementById('verdict').innerHTML='';
}
async function verify(field,el){
  if(field)curField=field; if(el)curEl=el;
  document.querySelectorAll('.leaf').forEach(x=>x.classList.remove('sel')); if(curEl)curEl.classList.add('sel');
  const commit=DATA[cur].commitment;
  const leaf=commit.leaves.find(l=>l.field===curField);
  if(!leaf)return;
  const proof=commit.proofs[curField]||[];
  const box=document.getElementById('proof'); box.innerHTML=''; document.getElementById('verdict').innerHTML='';
  let acc=hexToBytes(leaf.leaf_hash);
  if(TAMPER){acc=acc.slice();acc[0]^=1;}
  const add=(lab,op,hex)=>{const d=document.createElement('div');d.className='step';d.innerHTML=`<div class='lab'>${lab}</div>${op?`<div class='op'>${op}</div>`:''}<div class='h'>${hex}</div>`;box.appendChild(d);requestAnimationFrame(()=>d.classList.add('show'));};
  add('leaf hash · '+esc(curField)+(TAMPER?' · ⚠ TAMPERED (1 byte flipped)':''),'',bytesToHex(acc)); await sleep(300);
  for(let i=0;i<proof.length;i++){
    const step=proof[i]; const sib=hexToBytes(step.sibling);
    acc = step.position==='left' ? await nodeHash(sib,acc) : await nodeHash(acc,sib);
    add('step '+(i+1)+' · combine with '+step.position+' sibling','SHA-256( 0x01 ‖ '+(step.position==='left'?'sibling ‖ acc':'acc ‖ sibling')+' )',bytesToHex(acc));
    await sleep(320);
  }
  const got=bytesToHex(acc), ok=got===commit.root;
  const v=document.getElementById('verdict');
  v.innerHTML= ok
    ? `<span class='verdict ok'>✓ VERIFIED — folds to the committed root</span>`
    : `<span class='verdict bad'>✗ ${TAMPER?'TAMPER DETECTED — the flipped fact no longer folds to the committed root':'does not match root'}</span>`;
}
document.getElementById('tamperBtn').addEventListener('click',()=>{
  TAMPER=!TAMPER;const b=document.getElementById('tamperBtn');
  b.textContent=TAMPER?'✓ Restore original':'🔨 Flip a byte (tamper)';b.classList.toggle('on',TAMPER);
  if(curField)verify(curField,curEl); else document.getElementById('verdict').innerHTML="<span class='hint'>Select a fact on the left, then it will show as tamper-detected.</span>";
});
(async()=>{try{const r=await fetch('api/commitments/demo',{cache:'no-store'});const b=await r.json();DATA=b.commitments||[];
  if(DATA.length){render();
    if(new URLSearchParams(location.search).has('auto')){await sleep(500);const el=document.querySelector(".leaf[data-f='declared_result']")||document.querySelector('.leaf');if(el)el.click();}
  } else document.getElementById('leaves').innerHTML="<p class='hint'>No demo commitments.</p>";}
catch(e){document.getElementById('leaves').innerHTML="<p class='hint'>Failed to load: "+esc(e.message)+"</p>";}})();
</script></body></html>"""


RESOLVER_HTML = R"""<!doctype html>
<html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<meta name='color-scheme' content='dark'><title>FinalityGate — Resolver Playground</title>
<style>
:root{--bg:#07111f;--panel:#101d31;--panel2:#14243d;--border:#29405f;--text:#eef4ff;--muted:#a8b7cf;--blue:#7aa2ff;--green:#4ade80;--amber:#fbbf24;--red:#fb7185}
*{box-sizing:border-box}html,body{overflow-x:hidden}body{margin:0;background:radial-gradient(circle at 18% -5%,#173461 0,transparent 42%),radial-gradient(circle at 92% 0,#231a3f 0,transparent 34%),var(--bg);color:var(--text);font:15px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}
main{max-width:1080px;margin:auto;padding:34px 20px 70px}
.eyebrow{letter-spacing:.12em;text-transform:uppercase;color:var(--blue);font-size:.74rem;font-weight:700}
.track{display:inline-block;font-size:.7rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:#052012;background:var(--green);padding:4px 10px;border-radius:6px;margin:6px 0}
h1{font-size:clamp(1.8rem,4vw,2.8rem);margin:.15em 0}p{color:var(--muted);max-width:900px;line-height:1.6}a{color:var(--blue)}
.presets{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0}
.presets button{cursor:pointer;font:inherit;font-weight:700;padding:9px 13px;border-radius:10px;border:1px solid var(--border);background:#0d1a2c;color:var(--text)}
.presets button:hover{border-color:var(--blue)}.presets button.on{border-color:var(--amber);box-shadow:0 0 0 1px var(--amber) inset}
.panel{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--border);border-radius:16px;padding:18px;margin-top:14px}
h2{font-size:.95rem;margin:0 0 12px}
.controls{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}
.controls label{display:block;font-size:.7rem;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);margin-bottom:5px}
.controls select,.controls input{width:100%;font:inherit;padding:9px 10px;border-radius:9px;border:1px solid var(--border);background:#0a1728;color:var(--text)}
.gate{display:flex;gap:8px;flex-wrap:wrap;margin-top:4px}.gate span{flex:1;min-width:120px;text-align:center;padding:13px 8px;border:1px solid var(--border);border-radius:11px;background:#0a1728;color:var(--muted);font-weight:800;font-size:.82rem;letter-spacing:.03em;transition:all .25s}
.gate span.on.RESOLVE{border-color:var(--green);color:var(--green);box-shadow:0 0 22px rgba(74,222,128,.35),0 0 0 1px var(--green) inset}
.gate span.on.DISPUTE{border-color:var(--red);color:var(--red);box-shadow:0 0 22px rgba(251,113,133,.4),0 0 0 1px var(--red) inset}
.gate span.on.WAIT_FOR_PROOF,.gate span.on.PENDING_FINALITY{border-color:var(--amber);color:var(--amber);box-shadow:0 0 22px rgba(251,191,36,.32),0 0 0 1px var(--amber) inset}
.gate span.on.OPEN{border-color:var(--blue);color:var(--blue);box-shadow:0 0 22px rgba(122,162,255,.32),0 0 0 1px var(--blue) inset}
.verdict{font-size:1.9rem;font-weight:900;margin-top:6px}
.RESOLVE{color:var(--green)}.DISPUTE{color:var(--red)}.WAIT_FOR_PROOF,.PENDING_FINALITY{color:var(--amber)}.OPEN{color:var(--blue)}
.reasons span{display:inline-block;font-size:.72rem;padding:3px 8px;border:1px solid var(--border);border-radius:8px;margin:8px 5px 0 0;color:var(--muted)}
.checks{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:6px;margin-top:10px}
.chk{display:flex;justify-content:space-between;gap:8px;background:#0a1728;border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:.8rem}
.chk .y{color:var(--green);font-weight:700}.chk .n{color:var(--red);font-weight:700}
code{color:#c7d2fe;font-size:.72rem;word-break:break-all}
.notice{border-left:4px solid var(--amber);padding:12px 14px;background:#211b0b;color:#fde68a;border-radius:10px;margin-top:20px}
.links{margin-top:22px}.links a{margin-right:14px}
.controls>div{min-width:0}
@media(max-width:600px){.gate span{min-width:0;flex:1 1 46%}h1{font-size:2rem}}
</style></head><body><main>
<div class='eyebrow'>FinalityGate · interactive fail-closed resolver</div>
<div class='track'>Track · Prediction Markets &amp; Settlement</div>
<h1>Resolver Playground</h1>
<p>Change the evidence for a World Cup <code>MATCH_RESULT</code> market and watch the fail-closed state machine decide live. A market resolves <strong>only when finality, score, declared result, proof, and on-chain root all agree</strong> — otherwise it waits or disputes, each with an explicit reason and its own SHA-256 receipt. This is the real resolver (<code>POST /api/resolve</code>), not a mock.</p>
<div class='presets' id='presets'>
  <button data-p='resolve' class='on'>✓ Clean resolve</button>
  <button data-p='wait'>◷ Missing proof</button>
  <button data-p='conflict'>⚔ Score conflict</button>
  <button data-p='rootmismatch'>⚔ Root mismatch</button>
  <button data-p='inplay'>● Still in-play</button>
</div>
<section class='panel'><h2>Evidence</h2>
  <div class='controls'>
    <div><label>Fixture status</label><select id='status'><option>FINAL</option><option>IN_PLAY</option><option>HT</option><option>SCHEDULED</option></select></div>
    <div><label>Home score</label><input id='home' type='number' min='0' value='2'></div>
    <div><label>Away score</label><input id='away' type='number' min='0' value='1'></div>
    <div><label>Declared result</label><select id='declared'><option>HOME</option><option>DRAW</option><option>AWAY</option></select></div>
    <div><label>Proof status</label><select id='proof'><option>VALID</option><option>MISSING</option><option>UNVERIFIED</option><option>INVALID</option></select></div>
    <div><label>On-chain root</label><select id='root'><option>MATCH</option><option>MISSING</option><option>UNVERIFIED</option><option>MISMATCH</option></select></div>
  </div>
</section>
<section class='panel'><h2>Fail-closed decision</h2>
  <div class='gate'><span id='sOPEN' class='OPEN'>OPEN</span><span id='sPENDING_FINALITY' class='PENDING_FINALITY'>PENDING_FINALITY</span><span id='sWAIT_FOR_PROOF' class='WAIT_FOR_PROOF'>WAIT_FOR_PROOF</span><span id='sDISPUTE' class='DISPUTE'>DISPUTE</span><span id='sRESOLVE' class='RESOLVE'>RESOLVE</span></div>
  <div id='verdict' class='verdict'>—</div>
  <div id='resolved' class='reasons'></div>
  <div id='reasons' class='reasons'></div>
  <div class='checks' id='checks'></div>
  <p style='margin:12px 0 0'>Receipt <code id='receipt'>—</code></p>
</section>
<section class='panel'><h2>Auditor explanation &amp; remediation</h2>
  <p id='explSummary' style='margin:0 0 8px'>—</p>
  <div id='remediation'></div>
</section>
<div class='notice'>Simulation only. No custody, escrow release, wagering, or real-money settlement. No Solana on-chain validation is executed (<code>onchain_view_executed: false</code>).</div>
<div class='links'><a href='./'>← dashboard</a><a href='explorer?auto=1'>merkle proof explorer</a><a href='api/docs'>docs</a></div>
<script>
const ROOT_A='a3f1'+'0'.repeat(56)+'ab', ROOT_B='b7c2'+'0'.repeat(56)+'cd';
const esc=v=>String(v==null?'':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
const $=id=>document.getElementById(id);
const PRESETS={
  resolve:{status:'FINAL',home:2,away:1,declared:'HOME',proof:'VALID',root:'MATCH'},
  wait:{status:'FINAL',home:2,away:1,declared:'HOME',proof:'MISSING',root:'MATCH'},
  conflict:{status:'FINAL',home:1,away:2,declared:'HOME',proof:'VALID',root:'MATCH'},
  rootmismatch:{status:'FINAL',home:2,away:1,declared:'HOME',proof:'VALID',root:'MISMATCH'},
  inplay:{status:'IN_PLAY',home:1,away:1,declared:'DRAW',proof:'UNVERIFIED',root:'UNVERIFIED'}
};
function applyPreset(p){const v=PRESETS[p];if(!v)return;$('status').value=v.status;$('home').value=v.home;$('away').value=v.away;$('declared').value=v.declared;$('proof').value=v.proof;$('root').value=v.root;}
function buildBody(){
  const proof=$('proof').value, root=$('root').value;
  const ev={fixture_id:'wc-finality-001',fixture_status:$('status').value,
    home_score:parseInt($('home').value,10),away_score:parseInt($('away').value,10),
    declared_result:$('declared').value,observed_at:new Date().toISOString(),
    proof_status:proof,root_status:root};
  if(proof==='VALID')ev.proof_reference='txodds://validation/wc-finality-001/seq-4821';
  if(root==='MATCH'){ev.expected_root=ROOT_A;ev.observed_root=ROOT_A;}
  else if(root==='MISMATCH'){ev.expected_root=ROOT_A;ev.observed_root=ROOT_B;}
  return {market:{market_id:'market-worldcup-match-result-001',fixture_id:'wc-finality-001',selections:['HOME','DRAW','AWAY']},evidence:ev};
}
async function explain(body){
  try{
    const r=await fetch('api/explain',{method:'POST',headers:{'content-type':'application/json'},cache:'no-store',body:JSON.stringify(body)});
    const x=await r.json();
    $('explSummary').textContent=x.summary||'—';
    const rem=x.remediation||[];
    $('remediation').innerHTML = x.settled
      ? "<div class='chk'><span>all required evidence agrees</span><span class='y'>✓ settled</span></div>"
      : (rem.map(m=>`<div class='chk'><span><b>${esc(m.reason)}</b> — ${esc(m.action)}</span></div>`).join('') || "");
  }catch(e){$('explSummary').textContent='explain failed: '+e.message;}
}
async function resolve(){
  ['OPEN','PENDING_FINALITY','WAIT_FOR_PROOF','DISPUTE','RESOLVE'].forEach(s=>$('s'+s).classList.remove('on'));
  const body=buildBody();
  try{
    const r=await fetch('api/resolve',{method:'POST',headers:{'content-type':'application/json'},cache:'no-store',body:JSON.stringify(body)});
    const d=await r.json();
    const st=d.state||'—';
    if($('s'+st))setTimeout(()=>$('s'+st).classList.add('on'),100);
    const v=$('verdict');v.textContent=st;v.className='verdict '+st;
    $('resolved').innerHTML=d.resolved_selection?`<span style='border-color:var(--green);color:var(--green)'>resolved → ${esc(d.resolved_selection)}</span>`:'';
    $('reasons').innerHTML=(d.reasons||[]).map(x=>`<span>${esc(x)}</span>`).join('');
    const c=d.checks||{},keys=['fixture_final','score_complete','declared_result_allowed','declared_matches_score','proof_reference_present','root_values_present'];
    $('checks').innerHTML=keys.map(k=>`<div class='chk'><span>${esc(k)}</span><span class='${c[k]?'y':'n'}'>${c[k]?'✓':'✗'}</span></div>`).join('');
    $('receipt').textContent=d.receipt_sha256||'—';
    explain(body);
  }catch(e){$('verdict').textContent='ERROR';$('receipt').textContent='resolve failed: '+e.message;}
}
document.querySelectorAll('#presets button').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('#presets button').forEach(x=>x.classList.remove('on'));b.classList.add('on');applyPreset(b.dataset.p);resolve();}));
['status','home','away','declared','proof','root'].forEach(id=>$(id).addEventListener('change',()=>{document.querySelectorAll('#presets button').forEach(x=>x.classList.remove('on'));resolve();}));
const ip=new URLSearchParams(location.search).get('preset');if(ip&&PRESETS[ip])applyPreset(ip);
resolve();
</script></body></html>"""


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The demo is deterministic and offline, so compute it once at startup
        # and keep only the summary in memory.
        app.state.summary = compute_summary()
        yield

    app = FastAPI(
        title="FinalityGate World Cup Resolver",
        version=__version__,
        description="Deterministic proof-aware finality resolver with fail-closed evidence and SHA-256 receipts",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    def _summary() -> dict[str, Any]:
        summary = getattr(app.state, "summary", None)
        if summary is None:
            summary = compute_summary()
            app.state.summary = summary
        return summary

    @app.middleware("http")
    async def reject_non_finite_numbers(request: Request, call_next: Any) -> Any:
        # JSON's NaN/Infinity are non-standard and no field legitimately accepts
        # them. Reject them with a clean 422 before model binding, so a raw
        # non-finite number can never reach (or crash) validation error rendering.
        if (
            request.method == "POST"
            and request.url.path.startswith("/api/")
            and request.headers.get("content-type", "").startswith("application/json")
        ):
            body = await request.body()
            if body:
                try:
                    json.loads(body, parse_constant=_reject_non_finite)
                except _NonFiniteNumber:
                    return JSONResponse(
                        {"detail": "non-finite numbers (NaN/Infinity) are not allowed"},
                        status_code=422,
                    )
                except ValueError:
                    # Malformed JSON is left to normal request/model validation.
                    pass
        return await call_next(request)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Any:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_dashboard_html(_summary()))

    @app.get("/api/health")
    async def health() -> JSONResponse:
        payload = build_health(_summary())
        return JSONResponse(payload, status_code=200 if payload["status"] == "PASS" else 503)

    @app.get("/api/status")
    async def status() -> dict[str, Any]:
        summary = _summary()
        counts = summary.get("state_counts", {})
        return {
            "component": "finalitygate",
            "version": __version__,
            "status": summary.get("status"),
            "case_count": summary.get("case_count", 0),
            "state_counts": counts,
            "state_coverage": sorted(counts),
            "required_states": summary.get("required_states", sorted(REQUIRED_STATES)),
            "claim_boundary": CLAIM_BOUNDARY,
            "onchain_view_executed": ONCHAIN_VIEW_EXECUTED,
            "simulation_only": True,
        }

    @app.get("/api/demo")
    async def demo() -> dict[str, Any]:
        return _summary()

    def _decide(request: ResolveRequest) -> dict[str, Any]:
        try:
            market = OutcomeMarket(
                market_id=request.market.market_id,
                fixture_id=request.market.fixture_id,
                market_type=request.market.market_type,
                selections=tuple(request.market.selections),
                policy_version=request.market.policy_version,
            )
            evidence = ResolutionEvidence(
                fixture_id=request.evidence.fixture_id,
                fixture_status=request.evidence.fixture_status,
                home_score=request.evidence.home_score,
                away_score=request.evidence.away_score,
                declared_result=request.evidence.declared_result,
                observed_at=request.evidence.observed_at,
                proof_status=request.evidence.proof_status,
                root_status=request.evidence.root_status,
                proof_reference=request.evidence.proof_reference,
                expected_root=request.evidence.expected_root,
                observed_root=request.evidence.observed_root,
                source_fingerprint=request.evidence.source_fingerprint,
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}")
        return FinalityGateResolver().resolve(market, evidence).to_dict()

    @app.post("/api/resolve")
    async def resolve(request: ResolveRequest) -> dict[str, Any]:
        return _decide(request)

    @app.post("/api/commitment")
    async def commitment(request: ResolveRequest) -> dict[str, Any]:
        # Turn the resolution into a verifiable 32-byte Merkle settlement
        # commitment and self-check that a single fact (the declared result) is
        # provably included under the root. No on-chain call is executed.
        decision = _decide(request)
        commit = build_commitment(decision)
        fields = [leaf["field"] for leaf in commit["leaves"]]
        sample_field = "declared_result"
        index = fields.index(sample_field)
        leaf_hash = commit["leaves"][index]["leaf_hash"]
        verified = verify_proof(leaf_hash, commit["proofs"][sample_field], commit["root"])
        return {
            "decision": decision,
            "commitment": commit,
            "inclusion_proof_self_check": {"field": sample_field, "leaf_hash": leaf_hash, "verified": verified},
        }

    @app.post("/api/resolve/batch")
    async def resolve_batch(request: BatchResolveRequest) -> dict[str, Any]:
        # Resolve a batch and commit it to a hash-linked ledger with a single
        # batch Merkle root. Bounded so the cost is fixed and predictable.
        if not request.resolutions:
            raise HTTPException(status_code=422, detail="resolutions must be a non-empty list")
        if len(request.resolutions) > MAX_BATCH:
            raise HTTPException(status_code=422, detail=f"batch exceeds MAX_BATCH={MAX_BATCH}")
        decisions = [_decide(item) for item in request.resolutions]
        ledger = build_ledger(decisions)
        return {"ledger": ledger, "verification": verify_ledger(ledger)}

    @app.get("/api/ledger")
    async def ledger() -> dict[str, Any]:
        # An always-available settlement ledger over the deterministic demo
        # cases, so judges see a hash-linked, batch-committed ledger with no POST.
        decisions = [case["decision"] for case in _summary().get("cases", [])]
        built = build_ledger(decisions)
        return {"ledger": built, "verification": verify_ledger(built)}

    @app.get("/api/impact")
    async def impact() -> dict[str, Any]:
        # Quantify FinalityGate's value vs a naive resolver over the demo cases:
        # how many markets a settle-on-declared-result bot would have settled on
        # incomplete or conflicting evidence, that FinalityGate held or disputed.
        decisions = [case["decision"] for case in _summary().get("cases", [])]
        return settlement_impact(decisions)

    @app.get("/api/onchain-anchor")
    async def onchain_anchor() -> dict[str, Any]:
        # Frame the demo ledger's batch Merkle root as the on-chain settlement
        # anchor a Solana program would store — without executing any chain call.
        decisions = [case["decision"] for case in _summary().get("cases", [])]
        batch_root = build_ledger(decisions).get("batch_root") if decisions else None
        return commitment_anchor(batch_root)

    @app.post("/api/explain")
    async def explain(request: ResolveRequest) -> dict[str, Any]:
        # Resolve, then return an auditor-facing explanation: which evidence
        # checks passed/failed, the dispute taxonomy, and concrete remediation.
        return explain_decision(_decide(request))

    @app.get("/api/commitments/demo")
    async def commitments_demo() -> dict[str, Any]:
        # Every demo resolution with its full Merkle commitment (root + leaves +
        # inclusion proofs), so the interactive explorer can verify proofs
        # entirely client-side.
        out = []
        for case in _summary().get("cases", []):
            decision = case["decision"]
            out.append({
                "name": case["name"],
                "state": decision.get("state"),
                "resolved_selection": decision.get("resolved_selection"),
                "commitment": build_commitment(decision),
            })
        return {"count": len(out), "commitments": out}

    @app.get("/explorer", response_class=HTMLResponse, include_in_schema=False)
    async def explorer() -> HTMLResponse:
        return HTMLResponse(EXPLORER_HTML)

    @app.get("/resolver", response_class=HTMLResponse, include_in_schema=False)
    async def resolver() -> HTMLResponse:
        # Interactive fail-closed resolver: the judge changes evidence and each
        # change POSTs the real /api/resolve, so the state machine (OPEN /
        # PENDING_FINALITY / WAIT_FOR_PROOF / DISPUTE / RESOLVE) is exercised
        # live. No mock — the same resolver as the API and CLI.
        return HTMLResponse(RESOLVER_HTML)

    @app.post("/api/verify-receipt")
    async def verify(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return verify_receipt(payload)

    return app


app = create_app()


def run() -> None:
    import uvicorn

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("finalitygate.web.app:app", host=host, port=port, log_level=os.getenv("UVICORN_LOG_LEVEL", "info"))


if __name__ == "__main__":
    run()
