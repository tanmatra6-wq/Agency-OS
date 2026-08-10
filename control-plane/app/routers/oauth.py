import datetime as dt
import logging
import os
import urllib.parse as urlparse
import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Header
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.database import get_db, get_worker_db, get_worker_session_maker
from app.models import Connection, CircuitBreakerRow, ShadowDecision, Brand
from app.auth import tenant_id, validate_id
from app.tasks import enqueue_drain
from app.services.oauth import generate_oauth_state, validate_redirect_uri, verify_oauth_state, OauthService, normalize_shopify_domain
from app.services.oauth_registry import OauthProviderRegistry
from app.services.secrets import SecretManagerClient
from app.kernel.optypes import OpSpec, Severity, Reversibility
from app.kernel.loop import propose, preview_and_gate

logger = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])



class ConnectionOut(BaseModel):
    id: str
    provider: str
    scope: str
    credential: str | None
    status: str
    last_verified_at: dt.datetime | None = None
    last_error: str | None = None
    revoked_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    config: dict
    created_at: dt.datetime


@router.get("/connections", response_model=list[ConnectionOut])
async def list_connections(s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    stmt = select(Connection).where(Connection.tenant_id == tid)
    res = await s.execute(stmt)
    conns = res.scalars().all()
    return [
        {
            "id": c.id,
            "provider": c.provider,
            "scope": c.scope,
            "credential": c.credential,
            "status": c.status,
            "last_verified_at": c.last_verified_at,
            "last_error": c.last_error,
            "revoked_at": c.revoked_at,
            "expires_at": c.expires_at,
            "config": c.config,
            "created_at": c.created_at
        } for c in conns
    ]


@router.get("/connections/oauth/authorize")
async def oauth_authorize(
    request: Request,
    provider: str,
    brand_id: str,
    redirect_uri: str,
    shop: str | None = None,
    tid: str = Depends(tenant_id),
    db: AsyncSession = Depends(get_db)
):
    validate_id(brand_id, "brand_id")
    # Verify brand belongs to tenant
    stmt_brand = select(Brand).where(Brand.id == brand_id, Brand.tenant_id == tid)
    res_brand = await db.execute(stmt_brand)
    brand = res_brand.scalar_one_or_none()
    if not brand:
        raise HTTPException(status_code=404, detail="Brand not found for this tenant")

    if not validate_redirect_uri(redirect_uri):
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    state = await generate_oauth_state(tid, brand_id, redirect_uri, provider=provider, shop=shop)

    callback_uri = str(request.url_for('oauth_callback'))
    if provider == "shopify":
        shop_domain = normalize_shopify_domain(shop or brand_id)
        state = await generate_oauth_state(tid, brand_id, redirect_uri, provider=provider, shop=shop_domain)
        client_id = os.getenv("SHOPIFY_CLIENT_ID", "mock-shopify-client-id")
        auth_url = (
            f"https://{shop_domain}/admin/oauth/authorize?"
            f"client_id={client_id}&"
            f"scope=read_products,write_products&"
            f"redirect_uri={urlparse.quote(callback_uri)}&"
            f"state={state}"
        )
    else:
        try:
            auth_url = OauthProviderRegistry.get_authorize_url(provider, state, callback_uri)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url=auth_url, status_code=302)


@router.get("/connections/oauth/callback", name="oauth_callback")
async def oauth_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
    s: AsyncSession = Depends(get_worker_db),
    background_tasks: BackgroundTasks = None,
    worker_session_maker = Depends(get_worker_session_maker),
    x_tenant_id: str | None = Header(default=None)
):
    if not state:
        raise HTTPException(status_code=400, detail="Missing state")

    if error:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error} - {error_description}")

    try:
        payload = await verify_oauth_state(state)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if x_tenant_id and x_tenant_id != payload.get("tenant_id"):
        raise HTTPException(status_code=400, detail="Tenant mismatch")

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    tenant_id_val = validate_id(payload["tenant_id"], "tenant_id")
    brand_id = validate_id(payload["brand_id"], "brand_id")
    provider = payload.get("provider", "shopify")
    redirect_uri = payload.get("redirect_uri")

    # Set up DB RLS tenant context explicitly for this session
    if s.bind.dialect.name == "postgresql":
        await s.execute(
            text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
            {"tenant_id": tenant_id_val},
        )

    oauth_service = OauthService(tenant_id=tenant_id_val)
    try:
        shop = payload.get("shop")
        callback_uri = str(request.url_for("oauth_callback"))
        creds = await oauth_service.exchange_code_for_token(
            tenant_id=tenant_id_val,
            brand_id=brand_id,
            provider=provider,
            code=code,
            shop=shop,
            redirect_uri=callback_uri
        )
    except Exception as e:
        logger.error(f"OAuth token exchange failed: {e}")
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {str(e)}")

    access_token_ref = creds["access_token_ref"]
    refresh_token_ref = creds.get("refresh_token_ref")
    expires_in = creds.get("expires_in", 3600)
    scope_str = creds.get("scope") or "read_products,write_products"

    if provider == "shopify":
        required_scopes = {"read_products", "write_products"}
        returned_scopes = {s.strip() for s in scope_str.split(",")}
        if not required_scopes.issubset(returned_scopes):
            raise HTTPException(status_code=400, detail="Scope mismatch: missing required permissions")

    action_map = {
        "shopify": "manage.shopify.connect",
        "google-ads": "grow.google.connect",
        "meta-ads": "grow.meta.connect",
        "google": "presence.google.connect",
        "google-search-console": "presence.google.connect",
        "google-analytics": "presence.google.connect",
    }
    action = action_map.get(provider)
    if not action:
        raise HTTPException(status_code=400, detail=f"Unsupported OAuth provider: {provider}")
        
    domain = action.split(".")[0]
    
    # Derive tier from the latest TrustSnapshot for this brand+domain (default 1).
    from app.kernel.services import resolve_brand_tier
    tier = await resolve_brand_tier(s, tenant_id=tenant_id_val, brand_id=brand_id, domain=domain)

    op_id = f"op_{uuid.uuid4().hex[:12]}"
    
    env_prefix = provider.replace("-", "_").upper()
    client_id = os.getenv("SHOPIFY_CLIENT_ID" if provider == "shopify" else f"{env_prefix}_CLIENT_ID", f"mock-{provider}-client-id")
    config = {
        "scopes": scope_str,
        "client_id": client_id,
    }
    if shop:
        config["shop"] = shop
    if refresh_token_ref:
        config["refresh_token_ref"] = refresh_token_ref
    if expires_in:
        now_dt = dt.datetime.utcnow()
        expires_at_dt = now_dt + dt.timedelta(seconds=expires_in)
        config["expires_at"] = expires_at_dt.isoformat()

    spec = OpSpec(
        id=op_id,
        tenant_id=tenant_id_val,
        brand_id=brand_id,
        domain=domain,
        action=action,
        params={
            "provider": provider,
            "credential": access_token_ref,
            "config": config,
        },
        severity=Severity(impact=1, reversibility=Reversibility.COMPENSATABLE)
    )

    row = await propose(s, spec, actor="oauth:callback")
    gate, requirement = await preview_and_gate(s, row, tier=tier, actor="oauth:callback")
    
    # Commit the transaction so that the proposed Op is persisted and outbox items are created!
    await s.commit()
    
    # Drain the outbox to execute any auto-approved Ops
    if background_tasks and worker_session_maker:
        enqueue_drain(background_tasks, worker_session_maker)

    if redirect_uri:
        return RedirectResponse(url=redirect_uri, status_code=302)
        
    return {"status": "success", "message": "Connection proposed and queued", "op_id": op_id}


class CircuitBreakerOut(BaseModel):
    brand_id: str
    domain: str
    state: str
    consecutive_failures: int
    tripped_at: dt.datetime | None = None
    last_failure_at: dt.datetime | None = None


@router.get("/circuit-breakers", response_model=list[CircuitBreakerOut])
async def list_circuit_breakers(s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    stmt = select(CircuitBreakerRow).where(CircuitBreakerRow.tenant_id == tid)
    res = await s.execute(stmt)
    breakers = res.scalars().all()
    return [
        {
            "brand_id": cb.brand_id,
            "domain": cb.domain,
            "state": cb.state,
            "consecutive_failures": cb.consecutive_failures,
            "tripped_at": cb.tripped_at,
            "last_failure_at": cb.last_failure_at
        } for cb in breakers
    ]


@router.get("/autonomy-confidence")
async def autonomy_confidence(
    brand_id: str | None = None,
    domain: str | None = None,
    window_days: int | None = None,
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    """Computes autonomy confidence metrics (agreement rate, critical disagreements)
    for shadow Tier-2 decisions against human Tier-1 decisions.
    """
    import datetime as _dt
    from app.models import ShadowDecision, OpRow
    
    stmt = select(ShadowDecision).join(OpRow, ShadowDecision.op_id == OpRow.id)
    stmt = stmt.where(ShadowDecision.tenant_id == tid)
    
    if brand_id:
        stmt = stmt.where(OpRow.brand_id == brand_id)
    if domain:
        stmt = stmt.where(OpRow.domain == domain)
    if window_days:
        since = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
        # SQLite stores naive datetimes; compare with naive UTC
        since = since.replace(tzinfo=None)
        stmt = stmt.where(ShadowDecision.ts >= since)
        
    res = await s.execute(stmt)
    decisions = res.scalars().all()
    
    total = len(decisions)
    if total == 0:
        return {
            "tenant_id": tid,
            "brand_id": brand_id,
            "domain": domain,
            "window_days": window_days,
            "total_decisions": 0,
            "agreement_rate": 1.0,
            "critical_disagreements": 0,
            "recommendation": "OBSERVE",
            "message": "No shadow decisions recorded in this window."
        }
        
    agreed_count = sum(1 for d in decisions if d.agreed)
    critical_count = sum(1 for d in decisions if not d.agreed and d.human_decision == "reject" and d.shadow_requirement == "AUTO")
    
    agreement_rate = agreed_count / total
    
    if total < 5:
        recommendation = "OBSERVE"
        message = f"Insufficient data ({total} decision(s)). Recommend observing further."
    elif agreement_rate >= 0.90 and critical_count == 0:
        recommendation = "PROCEED"
        message = "High agreement rate and zero critical disagreements. Autonomy promotion recommended."
    else:
        recommendation = "HOLD"
        message = "Agreement rate below 90% or critical disagreements detected. Review shadow logs."
        
    return {
        "tenant_id": tid,
        "brand_id": brand_id,
        "domain": domain,
        "window_days": window_days,
        "total_decisions": total,
        "agreement_rate": agreement_rate,
        "critical_disagreements": critical_count,
        "recommendation": recommendation,
        "message": message
    }

