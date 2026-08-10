import datetime as dt
import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import get_db, get_worker_session_maker
from app.models import OpRow, OpTrace, Brand
from app.auth import tenant_id, resolved_operator_role, validate_id
from app.tasks import enqueue_drain
from app.kernel import loop
from app.kernel.optypes import OpState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])


class DecisionIn(BaseModel):
    decision: str  # approve | reject
    actor: str
    role: str = "AGENCY_OWNER"
    surface: str = "web"
    reason: str | None = None


@router.post("/ops/{op_id}/decision")
async def decide(
    op_id: str,
    body: DecisionIn,
    background_tasks: BackgroundTasks,
    operator_status: str | None = Depends(resolved_operator_role),
    s: AsyncSession = Depends(get_db),
    worker_session_maker = Depends(get_worker_session_maker),
    tid: str = Depends(tenant_id)
):
    row = await s.get(OpRow, op_id)
    if not row or row.tenant_id != tid:
        raise HTTPException(404, "op not found for tenant")

    # Resolve secure role provenance via Dual-Auth Role Routing
    resolved_role = "CLIENT"
    if operator_status == "OPERATOR_AUTHENTICATED":
        resolved_role = body.role

    try:
        await loop.decide(s, row, decision=body.decision, actor=body.actor, role=resolved_role,
                    surface=body.surface, reason=body.reason)
        await s.commit()
    except loop.RBACError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    enqueue_drain(background_tasks, worker_session_maker)
    return {"op_id": row.id, "state": row.state}


@router.get("/ops/{op_id}")
async def get_op(op_id: str, s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    row = await s.get(OpRow, op_id)
    if not row or row.tenant_id != tid:
        raise HTTPException(404, "op not found for tenant")
    result = await s.execute(select(OpTrace).filter_by(op_id=op_id).order_by(OpTrace.id))
    traces = [
        {"ts": t.ts.isoformat(), "kind": t.kind, "detail": t.detail}
        for t in result.scalars()
    ]
    return {
        "op_id": row.id,
        "action": row.action,
        "state": row.state,
        "params": row.params,
        "preview": row.preview_summary,
        "trace": traces,
        "impact": row.impact,
        "reversibility": row.reversibility,
        "statutory": row.statutory,
        "cost_estimate": (f"{row.cost_amount_minor/100:.2f} {row.cost_currency}/mo"
                          if row.cost_amount_minor else None)
    }


class OpOut(BaseModel):
    op_id: str
    tenant_id: str
    brand_id: str
    domain: str
    action: str
    state: str
    preview: str | None = None
    cost_estimate: str | None = None


@router.get("/ops", response_model=list[OpOut])
async def list_ops(
    state: str | None = None,
    domain: str | None = None,
    brand_id: str | None = None,
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    if brand_id:
        validate_id(brand_id, "brand_id")
    stmt = select(OpRow).where(OpRow.tenant_id == tid)
    if state:
        stmt = stmt.where(OpRow.state == state)
    if domain:
        stmt = stmt.where(OpRow.domain == domain)
    if brand_id:
        stmt = stmt.where(OpRow.brand_id == brand_id)
        
    stmt = stmt.order_by(OpRow.id.desc()).offset(offset).limit(limit)
    res = await s.execute(stmt)
    rows = res.scalars().all()
    
    return [
        {
            "op_id": row.id,
            "tenant_id": row.tenant_id,
            "brand_id": row.brand_id,
            "domain": row.domain,
            "action": row.action,
            "state": row.state,
            "preview": row.preview_summary,
            "cost_estimate": (f"{row.cost_amount_minor/100:.2f} {row.cost_currency}/mo"
                              if row.cost_amount_minor else None),
        }
        for row in rows
    ]


class RecommendationOut(BaseModel):
    action: str
    domain: str
    params: dict
    preview_summary: str
    impact: int
    reversibility: str
    cost_minor: int


@router.get("/brands/{brand_id}/recommendations", response_model=list[RecommendationOut])
async def get_brand_recommendations(
    brand_id: str,
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
        
    from app.services.recommender import get_recommendations
    specs = await get_recommendations(s, brand_id)
    
    recs = []
    for spec in specs:
        if spec.action == "presence.google.connect":
            summary = "Connect Google Search Console and Merchant Center channels to establish presence."
        elif spec.action == "presence.search_console.audit":
            summary = "Run Search Console organic search indexing and crawl health audit."
        elif spec.action == "presence.merchant_center.audit":
            summary = "Run Merchant Center product feed formatting and sync health audit."
        elif spec.action == "presence.citation.audit":
            summary = "Run Playwright competitor citation gap and organic keywords audit."
        elif spec.action == "grow.budget.reallocate":
            summary = spec.params.get("preview_summary") or "Optimize marketing ad spend by reallocating budget to high-performing campaigns."
        elif spec.action == "presence.wordpress.connect":
            summary = "Connect WordPress blog to launch customer retention content marketing."
        else:
            summary = f"Govern operation for action: {spec.action}"

        recs.append({
            "action": spec.action,
            "domain": spec.domain,
            "params": spec.params,
            "preview_summary": summary,
            "impact": spec.severity.impact,
            "reversibility": spec.severity.reversibility.value,
            "cost_minor": spec.cost_estimate.amount_minor if spec.cost_estimate else 0
        })
        
    return recs
