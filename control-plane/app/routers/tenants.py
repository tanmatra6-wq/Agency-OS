import datetime as dt
import time
import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.database import get_db, get_worker_db, get_worker_session_maker
from app.models import Tenant, Brand
from app.auth import verify_operator_auth
from app.whatsapp import send_whatsapp_card_task
from app.kernel import loop

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tenants"])


class TenantIn(BaseModel):
    name: str = Field(max_length=200)
    brand_name: str = Field(max_length=200)


@router.post("/tenants", dependencies=[Depends(verify_operator_auth)])
async def create_tenant(body: TenantIn, s: AsyncSession = Depends(get_worker_db)):
    import uuid
    tenant_id = uuid.uuid4().hex
    # Set the tenant context so the INSERTs satisfy the RLS WITH CHECK policies on
    # tenants/brands (the worker role is RLS-enforced, not BYPASSRLS). set_config is a
    # Postgres function — guard on dialect so the SQLite test database isn't hit with it.
    if s.bind and s.bind.dialect.name == "postgresql":
        await s.execute(
            text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id}
        )

    t = Tenant(id=tenant_id, name=body.name)
    s.add(t)
    await s.flush()
    
    b = Brand(tenant_id=t.id, name=body.brand_name)
    s.add(b)
    await s.flush()
    
    from app.middleware import VALID_TENANTS_CACHE
    VALID_TENANTS_CACHE[t.id] = True
    
    return {"tenant_id": t.id, "brand_id": b.id}


class TenantBrandOut(BaseModel):
    tenant_id: str
    tenant_name: str
    brand_id: str
    brand_name: str


@router.get("/tenants", response_model=list[TenantBrandOut], dependencies=[Depends(verify_operator_auth)])
async def list_tenants(s: AsyncSession = Depends(get_worker_db)):
    """Lists all tenants and their brands.

    Bypasses RLS (uses get_worker_db) to allow the operator console to discover tenants.
    """
    stmt = select(
        Tenant.id.label("tenant_id"),
        Tenant.name.label("tenant_name"),
        Brand.id.label("brand_id"),
        Brand.name.label("brand_name")
    ).join(Brand, Brand.tenant_id == Tenant.id).order_by(Tenant.name, Brand.name)

    res = await s.execute(stmt)
    return [dict(row) for row in res.mappings().all()]


class TenantUpdateIn(BaseModel):
    is_active: bool


class TenantOut(BaseModel):
    id: str
    name: str
    hosting_tier: str
    gcp_project: str | None = None
    is_active: bool
    created_at: dt.datetime


@router.patch("/tenants/{tenant_id}", response_model=TenantOut, dependencies=[Depends(verify_operator_auth)])
async def update_tenant_status(
    tenant_id: str,
    body: TenantUpdateIn,
    s: AsyncSession = Depends(get_worker_db)
):
    tenant = await s.get(Tenant, tenant_id)
    
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
        
    tenant.is_active = body.is_active
    await s.commit()
    
    # Update the gateway memory cache
    from app.middleware import VALID_TENANTS_CACHE
    VALID_TENANTS_CACHE[tenant_id] = body.is_active
    
    return tenant


@router.delete("/tenants/{tenant_id}", status_code=202, dependencies=[Depends(verify_operator_auth)])
async def delete_tenant(
    tenant_id: str,
    background_tasks: BackgroundTasks,
    s: AsyncSession = Depends(get_worker_db)
):
    tenant = await s.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
        
    # Propose a governed offboard Op
    import uuid
    from app.kernel.optypes import OpSpec, Severity, Reversibility, Money
    from app.kernel.services import resolve_brand_tier
    from app.database import get_worker_session_maker
    
    op_id = f"op_offboard_{uuid.uuid4().hex[:12]}"
    spec = OpSpec(
        id=op_id,
        tenant_id=tenant_id,
        brand_id="_system",
        domain="manage",
        action="manage.tenant.offboard",
        params={"target_tenant_id": tenant_id},
        severity=Severity(impact=3, reversibility=Reversibility.IRREVERSIBLE),
        cost_estimate=Money(amount_minor=0, currency="INR")
    )
    
    # Propose Op using the RLS-bypassed worker session
    row = await loop.propose(s, spec, actor="api:operator")
    
    # Resolve tier (default 1) and gate
    tier = await resolve_brand_tier(s, tenant_id=tenant_id, brand_id="_system", domain="manage")
    gate, requirement = await loop.preview_and_gate(s, row, tier=tier)
    
    await s.commit()
    
    # If it needs approval, send notifications
    if row.state in ("AWAITING_APPROVAL", "BLOCKED"):
        background_tasks.add_task(send_whatsapp_card_task, row.id, get_worker_session_maker())
        
    return {
        "status": "proposed",
        "op_id": row.id,
        "state": row.state,
        "requirement": requirement,
        "preview": row.preview_summary
    }
