import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, get_worker_session_maker
from app.models import CircuitBreakerRow, Brand
from app.auth import tenant_id, verify_operator_auth, validate_id
from app.tasks import enqueue_drain
from app.whatsapp import send_whatsapp_card_task
from app.kernel import loop
from app.kernel.services import audit_verify

logger = logging.getLogger(__name__)

router = APIRouter(tags=["actions"])


class ChatIn(BaseModel):
    brand_id: str = Field(max_length=100)
    text: str = Field(max_length=5000)


@router.post("/chat")
async def chat(body: ChatIn, background_tasks: BackgroundTasks,
               s: AsyncSession = Depends(get_db),
               worker_session_maker = Depends(get_worker_session_maker),
               tid: str = Depends(tenant_id)):
    """Conversational intent routing endpoint. Translates text to structured adapter intents."""
    validate_id(body.brand_id, "brand_id")
    from app.kernel.tools import registry as tool_registry, parse_chat_to_tool_call
    tool_match = parse_chat_to_tool_call(body.text)
    if tool_match:
        tool_name, args = tool_match
        tool = tool_registry.get_tool(tool_name)
        if tool:
            handler = tool["handler"]
            # Call the handler with tenant_id injected
            specs = handler(tenant_id=tid, **args)
            
            from app.kernel.services import resolve_brand_tier
            tier = await resolve_brand_tier(s, tenant_id=tid, brand_id=body.brand_id, domain=specs[0].domain)

            cards = []
            for spec in specs:
                # Propose and gate the operation! RLS and safety gates apply unconditionally.
                row = await loop.propose(s, spec, actor="chat:tool")
                gate, requirement = await loop.preview_and_gate(s, row, tier=tier, actor="chat:tool")
                
                cards.append({
                    "op_id": row.id, "action": row.action, "state": row.state,
                    "requirement": requirement,
                    "preview": row.preview_summary,
                    "cost_estimate": (f"{row.cost_amount_minor/100:.2f} {row.cost_currency}/mo"
                                      if row.cost_amount_minor else None),
                    "violations": [v.as_dict() for v in gate.violations],
                })
                if row.state == "AWAITING_APPROVAL":
                    background_tasks.add_task(send_whatsapp_card_task, row.id, worker_session_maker)

            await s.commit()
            # Drain so auto-approved (within-policy) Ops actually execute — they are
            # APPROVED with a PENDING outbox item but no /decision call fires otherwise.
            enqueue_drain(background_tasks, worker_session_maker)
            return {
                "reply": f"Structured request parsed. Generated {len(cards)} proposal(s) under safety gates.",
                "cards": cards
            }

    normalized = body.text.lower()
    has_domain = any("." in w and not w.startswith(".") for w in body.text.replace(",", " ").split())

    # --- Read-only conversational intents: answer directly, propose nothing. ---
    # "check budgets" — cost-to-date for the tenant (read-only; no Op, no gate).
    if any(w in normalized for w in ["budget", "cost", "spend", "how much", "profit", "margin"]):
        from app.kernel.services import get_tenant_cost_rollup
        rollup = await get_tenant_cost_rollup(s, tid)
        if rollup:
            parts = ", ".join(f"{k}: {v / 100:.2f} INR" for k, v in rollup.items())
            total = sum(rollup.values()) / 100
            reply = f"Cost-to-date for this tenant — {parts}. Total: {total:.2f} INR."
        else:
            reply = "No costs have been recorded for this tenant yet."
        return {"reply": reply, "cards": []}

    # "trigger diagnostics" — audit-chain integrity + circuit-breaker status (read-only).
    if any(w in normalized for w in ["diagnostic", "status", "health", "audit", "integrity", "breaker"]):
        ok, first_bad = await audit_verify(s)
        br = (await s.execute(select(CircuitBreakerRow).where(CircuitBreakerRow.tenant_id == tid))).scalars().all()
        tripped = [b.domain for b in br if (b.state or "").upper() == "OPEN"]
        audit_line = "audit chain intact" if ok else f"AUDIT CHAIN BROKEN at block {first_bad}"
        br_line = (f"{len(tripped)} circuit breaker(s) tripped ({', '.join(tripped)})"
                   if tripped else "all circuit breakers healthy")
        return {"reply": f"Diagnostics — {audit_line}; {br_line}.", "cards": []}

    # --- Provisioning intents: route to the governed loop (propose -> gate). ---
    if "static" in normalized:
        words = body.text.replace(",", " ").split()
        domain = next((w for w in words if "." in w and not w.startswith(".")), "example.in")
        intent_text = f"static website hosting for {domain}"
        domain_name = "provision"
    elif any(w in normalized for w in ["email", "dns", "mx", "spf", "dkim"]):
        words = body.text.replace(",", " ").split()
        domain = next((w for w in words if "." in w and not w.startswith(".")), "example.in")
        intent_text = f"configure email dns routing for domain {domain}"
        domain_name = "provision"
    elif any(w in normalized for w in ["bootstrap", "onboard", "host", "provision", "deploy", "website", "launch"]) or has_domain:
        intent_text = body.text
        domain_name = "provision"
    elif any(w in normalized for w in ["build", "change", "update", "modify", "fix", "color", "style", "design", "css", "html"]):
        intent_text = body.text
        domain_name = "build"
    else:
        # Unrecognized — guide the operator instead of silently proposing a deploy.
        return {
            "reply": (
                "I didn't recognize that as an action. I can: host a site "
                "(e.g. \"host ableys.in\"), modify code/styling (e.g. \"change hero color to blue\"), "
                "pause a campaign (\"pause campaign camp-1\"), "
                "adjust a bid (\"adjust bid for campaign camp-1 to 50 inr\"), "
                "check budgets (\"what's my spend?\"), or run diagnostics (\"show system status\")."
            ),
            "cards": [],
        }

    from app.kernel.loop import is_domain_disabled
    if is_domain_disabled(domain_name):
        raise HTTPException(400, f"Domain {domain_name!r} is disabled via kill-switch")
    adapter = loop.REGISTRY.get(domain_name)
    if not adapter:
        raise HTTPException(400, f"no adapter for domain {domain_name!r}")

    # Derive tier from the latest TrustSnapshot for this brand and domain
    from app.kernel.services import resolve_brand_tier
    tier = await resolve_brand_tier(s, tenant_id=tid, brand_id=body.brand_id, domain=domain_name)

    cards = []
    for spec in adapter.plan(intent_text, tid, body.brand_id):
        row = await loop.propose(s, spec, actor="chat")
        
        # Record LLM planning cost (simulated gemini tokens)
        from app.kernel.services import emit_cost
        await emit_cost(
            s,
            tenant_id=tid,
            op_id=row.id,
            kind="llm_tokens",
            amount_minor=57,
            currency="INR",
            meta={"model": "gemini-1.5-pro", "prompt_tokens": 450, "completion_tokens": 120}
        )

        gate, requirement = await loop.preview_and_gate(s, row, tier=tier)
        
        if row.parent_op_id is None:
            cards.append({
                "op_id": row.id, "action": row.action, "state": row.state,
                "requirement": requirement,
                "preview": row.preview_summary,
                "cost_estimate": (f"{row.cost_amount_minor/100:.2f} {row.cost_currency}/mo"
                                  if row.cost_amount_minor else None),
                "violations": [v.as_dict() for v in gate.violations],
            })
            if row.state == "AWAITING_APPROVAL":
                background_tasks.add_task(send_whatsapp_card_task, row.id, worker_session_maker)

    await s.commit()
    # Drain so auto-approved (within-policy) Ops actually execute — they are APPROVED
    # with a PENDING outbox item but no /decision call fires otherwise.
    enqueue_drain(background_tasks, worker_session_maker)
    return {
        "reply": f"Understood. I have initiated the planning for your request: '{intent_text}'. Please approve the generated proposal.",
        "cards": cards
    }


class IntentIn(BaseModel):
    brand_id: str = Field(max_length=100)
    text: str = Field(max_length=5000)
    domain: str = Field(default="provision", max_length=50)


@router.post("/intents")
async def submit_intent(body: IntentIn, background_tasks: BackgroundTasks,
                        s: AsyncSession = Depends(get_db),
                        worker_session_maker = Depends(get_worker_session_maker),
                        tid: str = Depends(tenant_id)):
    validate_id(body.brand_id, "brand_id")
    from app.kernel.loop import is_domain_disabled
    if is_domain_disabled(body.domain):
        raise HTTPException(400, f"Domain {body.domain!r} is disabled via kill-switch")
    adapter = loop.REGISTRY.get(body.domain)
    if not adapter:
        raise HTTPException(400, f"no adapter for domain {body.domain!r}")
    
    # Derive tier from the latest TrustSnapshot for this brand and domain
    from app.kernel.services import resolve_brand_tier
    tier = await resolve_brand_tier(s, tenant_id=tid, brand_id=body.brand_id, domain=body.domain)

    cards = []
    for spec in adapter.plan(body.text, tid, body.brand_id):
        row = await loop.propose(s, spec, actor="chat")
        # Record LLM planning cost (simulated gemini tokens)
        from app.kernel.services import emit_cost
        await emit_cost(
            s,
            tenant_id=tid,
            op_id=row.id,
            kind="llm_tokens",
            amount_minor=57,
            currency="INR",
            meta={"model": "gemini-1.5-pro", "prompt_tokens": 450, "completion_tokens": 120},
            actor="chat"
        )

        gate, requirement = await loop.preview_and_gate(s, row, tier=tier)
        
        # Only return card / send notification for parent-less (top-level) Ops
        if row.parent_op_id is None:
            cards.append({
                "op_id": row.id, "action": row.action, "state": row.state,
                "requirement": requirement,
                "preview": row.preview_summary,
                "cost_estimate": (f"{row.cost_amount_minor/100:.2f} {row.cost_currency}/mo"
                                  if row.cost_amount_minor else None),
                "violations": [v.as_dict() for v in gate.violations],
            })
            if row.state in ("AWAITING_APPROVAL", "BLOCKED"):
                background_tasks.add_task(send_whatsapp_card_task, row.id, worker_session_maker)
    await s.commit()
    # Auto-approved (within-policy) Ops are now APPROVED with a PENDING outbox item
    # (loop.preview_and_gate -> enqueue), but nothing has triggered execution. Drain
    # the outbox so they actually run — mirrors POST /ops/{op_id}/decision. drain_once
    # only processes PENDING items, so this is a no-op when every Op awaits approval.
    enqueue_drain(background_tasks, worker_session_maker)
    return {"cards": cards}


class ActionIn(BaseModel):
    tool: str = Field(max_length=100)
    brand_id: str = Field(max_length=100)
    params: dict = Field(default_factory=dict)


@router.get("/actions/catalog")
async def actions_catalog(tid: str = Depends(tenant_id)):
    """Tool schemas backing the console's explicit Action Panel (replaces the chat)."""
    from app.kernel.tools import registry as tool_registry
    return {"actions": tool_registry.get_schemas()}


@router.post("/actions")
async def submit_action(body: ActionIn, background_tasks: BackgroundTasks,
                        _ = Depends(verify_operator_auth),
                        s: AsyncSession = Depends(get_db),
                        worker_session_maker = Depends(get_worker_session_maker),
                        tid: str = Depends(tenant_id)):
    """Structured operator action -> governed Op(s). No free-text parsing: routes the
    chosen tool + params through the tool registry, then propose -> preview_and_gate ->
    drain. Gates and approval are unchanged (auto-approve within policy, else the Op
    waits in the queue / WhatsApp)."""
    validate_id(body.brand_id, "brand_id")
    from app.kernel.tools import registry as tool_registry
    tool = tool_registry.get_tool(body.tool)
    if not tool:
        raise HTTPException(400, f"unknown action {body.tool!r}")
    if "brand_id" in body.params or "tenant_id" in body.params:
        raise HTTPException(400, "brand_id/tenant_id are supplied by the request, not in params")
    try:
        specs = tool["handler"](tenant_id=tid, brand_id=body.brand_id, **body.params)
    except TypeError as e:
        raise HTTPException(400, f"invalid params for action {body.tool!r}: {e}")

    cards = []
    for spec in specs:
        # Derive tier from the latest TrustSnapshot for this brand+domain (default 1).
        from app.kernel.services import resolve_brand_tier
        tier = await resolve_brand_tier(s, tenant_id=tid, brand_id=body.brand_id, domain=spec.domain)

        row = await loop.propose(s, spec, actor="forms:operator")
        gate, requirement = await loop.preview_and_gate(s, row, tier=tier)
        if row.parent_op_id is None:
            cards.append({
                "op_id": row.id, "action": row.action, "state": row.state,
                "requirement": requirement,
                "preview": row.preview_summary,
                "cost_estimate": (f"{row.cost_amount_minor/100:.2f} {row.cost_currency}/mo"
                                  if row.cost_amount_minor else None),
                "violations": [v.as_dict() for v in gate.violations],
            })
            if row.state in ("AWAITING_APPROVAL", "BLOCKED"):
                background_tasks.add_task(send_whatsapp_card_task, row.id, worker_session_maker)
    await s.commit()
    # Execute auto-approved Ops (same governed drain path as /chat and /decision).
    enqueue_drain(background_tasks, worker_session_maker)
    return {"cards": cards}
