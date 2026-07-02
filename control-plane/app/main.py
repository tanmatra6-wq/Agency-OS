import logging
import os
import datetime as dt
import subprocess

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Response, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.database import get_db, get_worker_db, get_worker_session_maker, tenant_context, AsyncSessionLocal
from app.tasks import enqueue_drain
from app.middleware import TenantIsolationMiddleware, TraceMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware, MetricsMiddleware
from app.observability import setup_logging
from app.whatsapp import send_whatsapp_card_task, process_whatsapp_webhook_payload
from app.adapters.provision import ProvisionAdapter
from app.adapters.presence import PresenceAdapter
from app.adapters.grow import GrowAdapter
from app.adapters.manage import ManageAdapter
from app.adapters.build import BuildAdapter
from app.adapters.governance import GovernanceAdapter
from app.adapters.dr import DRAdapter
from app.adapters.monitor import MonitorAdapter
from .kernel import loop
from .kernel.services import audit_verify, approval_latency_rollup
from .kernel.plugins import register_plugin, get_plugin, ShopifyPlugin
from .models import Brand, OpRow, OpTrace, Tenant, TrustSnapshot, Cadence, Order, Connection, CircuitBreakerRow, AuditEvent, BrandObjective

# Setup Sentry SDK if DSN is set
SENTRY_DSN = os.getenv("SENTRY_DSN")
if SENTRY_DSN:
    import sentry_sdk
    from sentry_sdk.integrations.fastapi import FastApiIntegration
    from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
    from sentry_sdk.integrations.httpx import HttpxIntegration
    
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[
            FastApiIntegration(),
            SqlalchemyIntegration(),
            HttpxIntegration(),
        ],
        # Default to light sampling in production; override via env. 100% tracing +
        # profiling is prohibitively expensive and noisy at real request volumes.
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
        profiles_sample_rate=float(os.getenv("SENTRY_PROFILES_SAMPLE_RATE", "0.1")),
    )

# Setup logging
log_level = os.getenv("LOG_LEVEL", "INFO")
json_format = os.getenv("LOG_FORMAT", "text").lower() == "json"
setup_logging(level=log_level, json_format=json_format)

loop.register(ProvisionAdapter())
loop.register(PresenceAdapter())
loop.register(GrowAdapter())
loop.register(ManageAdapter())
loop.register(BuildAdapter())
loop.register(GovernanceAdapter())
loop.register(DRAdapter())
loop.register(MonitorAdapter())

register_plugin(ShopifyPlugin())


logger = logging.getLogger(__name__)
RECIPES_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../recipes"))
logger.info(f"RECIPES_ROOT resolved to: {RECIPES_ROOT}")
try:
    if os.path.exists(RECIPES_ROOT):
        logger.info(f"Contents of RECIPES_ROOT: {os.listdir(RECIPES_ROOT)}")
    else:
        logger.warning(f"RECIPES_ROOT does not exist!")
except Exception as e:
    logger.error(f"Failed to list RECIPES_ROOT: {e}")
logger = logging.getLogger(__name__)

def validate_production_config():
    """Enforces strict, non-mock, non-local configuration checks in production mode."""
    if os.getenv("ENV") != "production":
        return
        
    # Helper to assert env var is present and not mock-like
    def assert_secure_var(name: str):
        val = os.getenv(name)
        if not val:
            raise RuntimeError(f"PRODUCTION BOOT ERROR: {name} environment variable is required in production mode!")
        val_lower = val.lower()
        if val_lower == "default-dev-token" or any(val_lower.startswith(p) for p in ("mock", "fake", "default")):
            raise RuntimeError(f"PRODUCTION BOOT ERROR: {name} cannot be configured to a development or mock value ('{val}') in production mode!")

    # 1. Operator Token Checks
    assert_secure_var("OPERATOR_TOKEN")

    # 2. Integration credentials
    integration_vars = [
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
        "WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_PHONE_NUMBER_ID"
    ]
    for var in integration_vars:
        assert_secure_var(var)

    # 3. Mocks must be disabled
    mock_vars = ["AOS_MOCK_CAMPAIGNS_FILE", "AOS_MOCK_SECRETS_FILE", "AOS_MOCK_STORAGE_FILE", "MOCK_PLAYWRIGHT"]
    for var in mock_vars:
        val = os.getenv(var)
        if val and val.lower() not in ("false", "0", ""):
            raise RuntimeError(f"PRODUCTION BOOT ERROR: Mock variable {var} must be disabled or unset in production mode!")

validate_production_config()



app = FastAPI(title="Agency OS control plane", version="0.1.0")
app.state.db_session_maker = AsyncSessionLocal

from app.routers.onboarding import router as onboarding_router
app.include_router(onboarding_router)

from app.routers.tenants import router as tenants_router
app.include_router(tenants_router)

from app.routers.ops import router as ops_router
app.include_router(ops_router)

from app.routers.actions import router as actions_router
app.include_router(actions_router)

from app.routers.oauth import router as oauth_router
app.include_router(oauth_router)

from app.routers.webhooks import router as webhooks_router
app.include_router(webhooks_router)

from app.routers.tasks import router as tasks_router
app.include_router(tasks_router)

from app.routers.session import router as session_router
app.include_router(session_router)

from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=400,
        content={"detail": exc.errors(), "message": "Validation error"}
    )


# The operator/brand console (control-plane/web) is served from a separate
# Cloud Run origin, so browser calls to this API are cross-origin. Origins come
# from ALLOWED_ORIGINS (comma-separated); localhost + the deployed console URL
# are the defaults so the console works out of the box.
ALLOWED_ORIGINS = [
    o.strip()
    for o in os.getenv(
        "ALLOWED_ORIGINS",
        "http://localhost:3000,https://agency-os-web-730671240713.asia-south1.run.app",
    ).split(",")
    if o.strip()
]


def _cors_headers_for(request: Request) -> dict[str, str]:
    """CORS headers for a manually-built error response.

    Unhandled 500s are produced by Starlette's ServerErrorMiddleware, which sits
    OUTSIDE CORSMiddleware — so without this the browser sees a header-less error
    and reports an opaque "Failed to fetch" instead of the real status/detail.
    """
    origin = request.headers.get("origin")
    if origin and origin in ALLOWED_ORIGINS:
        return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
    return {}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    from app.observability import trace_context
    trace_id = trace_context.get("unknown")
    logger.exception(f"Unhandled exception on {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "trace_id": trace_id},
        headers=_cors_headers_for(request),
    )

app.add_middleware(TraceMiddleware)
app.add_middleware(TenantIsolationMiddleware)
app.add_middleware(RateLimitMiddleware, rate=0.2, capacity=5.0)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(MetricsMiddleware)
# Added LAST so it is the OUTERMOST middleware: CORSMiddleware answers the
# preflight OPTIONS itself, before TenantIsolationMiddleware (which would 400 a
# preflight that carries no X-Tenant-ID header).
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    allow_credentials=False,
)
from app.auth import verify_operator_auth, resolved_operator_role



@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/readyz")
async def readyz(s: AsyncSession = Depends(get_worker_db)):
    try:
        from sqlalchemy import func
        await s.execute(select(1))
        
        # Check if there are any onboarded tenants
        stmt = select(func.count(Tenant.id))
        res = await s.execute(stmt)
        count = res.scalar() or 0
        
        return {
            "status": "ready",
            "onboarded": count > 0
        }
    except Exception as e:
        logger.error(f"Readiness probe failed: {e}")
        return JSONResponse(
            status_code=503,
            content={"status": "unready", "error": str(e)}
        )


from prometheus_client import Gauge, REGISTRY

if "aos_connections_active" in REGISTRY._names_to_collectors:
    connections_active_gauge = REGISTRY._names_to_collectors["aos_connections_active"]
else:
    connections_active_gauge = Gauge(
        "aos_connections_active",
        "Total number of active connections"
    )

if "aos_outbox_lag" in REGISTRY._names_to_collectors:
    outbox_lag_gauge = REGISTRY._names_to_collectors["aos_outbox_lag"]
else:
    outbox_lag_gauge = Gauge(
        "aos_outbox_lag",
        "Total number of pending items in the outbox"
    )


@app.get("/metrics")
async def metrics_endpoint(s: AsyncSession = Depends(get_worker_db)):
    from app.models import Connection, OutboxItem
    from sqlalchemy import select, func
    
    try:
        # Count active connections
        stmt_conn = select(func.count()).select_from(Connection).where(Connection.status == "active")
        res_conn = await s.execute(stmt_conn)
        active_count = res_conn.scalar() or 0
        connections_active_gauge.set(active_count)

        # Count pending outbox items
        stmt_outbox = select(func.count()).select_from(OutboxItem).where(OutboxItem.status == "pending")
        res_outbox = await s.execute(stmt_outbox)
        pending_count = res_outbox.scalar() or 0
        outbox_lag_gauge.set(pending_count)

        # Count dead outbox items
        from app.metrics import OUTBOX_DEAD_GAUGE
        stmt_dead = select(func.count()).select_from(OutboxItem).where(OutboxItem.status == "DEAD")
        res_dead = await s.execute(stmt_dead)
        dead_count = res_dead.scalar() or 0
        OUTBOX_DEAD_GAUGE.set(dead_count)
    except Exception as e:
        logger.error(f"Failed to update prometheus gauges: {e}")

    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)



from app.auth import tenant_id, validate_id





class OpOut(BaseModel):
    op_id: str
    tenant_id: str
    brand_id: str
    domain: str
    action: str
    state: str
    preview: str | None = None
    cost_estimate: str | None = None








class AuditEventOut(BaseModel):
    id: int
    ts: dt.datetime
    actor: str
    action: str
    op_id: str | None = None
    payload: dict
    hash: str


class AuditVerifyOut(BaseModel):
    ok: bool
    first_bad_id: int | None = None





class BrandPortfolioItem(BaseModel):
    brand_id: str
    brand_name: str
    active_objective: str
    b_score: float
    trust_score: float
    trust_tier: int
    total_cost_minor: int

class TenantPortfolioOut(BaseModel):
    tenant_id: str
    tenant_name: str
    hosting_tier: str
    gcp_project: str
    portfolio: list[BrandPortfolioItem]

@app.get("/brands/portfolio", response_model=TenantPortfolioOut)
async def get_portfolio(s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    """Returns the portfolio overview of all brands under the tenant.

    Includes active objective, B-score, trust telemetry, hosting tier, and total costs.
    """
    from app.models import Tenant, Brand, BrandObjective, CostEntry, TrustSnapshot
    from sqlalchemy import func
    
    tenant_stmt = select(Tenant).where(Tenant.id == tid)
    tenant_res = await s.execute(tenant_stmt)
    tenant = tenant_res.scalar_one_or_none()
    if not tenant:
        raise HTTPException(404, "Tenant not found")
        
    brands_stmt = select(Brand).where(Brand.tenant_id == tid).order_by(Brand.name)
    brands_res = await s.execute(brands_stmt)
    brands = brands_res.scalars().all()
    
    portfolio = []
    for brand in brands:
        # Fetch active objective and B-score
        objective_stmt = select(BrandObjective).where(BrandObjective.brand_id == brand.id)
        obj_res = await s.execute(objective_stmt)
        obj = obj_res.scalar_one_or_none()
        
        active_objective = obj.objective if obj else "footprint"
        b_score = obj.b_score if obj else 72.5
        
        # Fetch latest Trust Snapshot
        trust_stmt = (
            select(TrustSnapshot)
            .where(TrustSnapshot.tenant_id == tid, TrustSnapshot.brand_id == brand.id)
            .order_by(TrustSnapshot.ts.desc())
            .limit(1)
        )
        trust_res = await s.execute(trust_stmt)
        trust = trust_res.scalar_one_or_none()
        
        trust_score = trust.score if trust else 82.0
        trust_tier = trust.tier if trust else 1
        
        # Sum total costs from cost_ledger
        cost_stmt = select(func.sum(CostEntry.amount_minor)).where(
            CostEntry.tenant_id == tid,
            CostEntry.op_id.in_(
                select(OpRow.id).where(OpRow.tenant_id == tid, OpRow.brand_id == brand.id)
            )
        )
        cost_res = await s.execute(cost_stmt)
        total_cost_minor = cost_res.scalar() or 0
        
        portfolio.append(BrandPortfolioItem(
            brand_id=brand.id,
            brand_name=brand.name,
            active_objective=active_objective,
            b_score=b_score,
            trust_score=trust_score,
            trust_tier=trust_tier,
            total_cost_minor=total_cost_minor
        ))
        
    return TenantPortfolioOut(
        tenant_id=tid,
        tenant_name=tenant.name,
        hosting_tier=tenant.hosting_tier,
        gcp_project=tenant.gcp_project or f"aos-tenant-{tid[:8]}",
        portfolio=portfolio
    )


@app.get("/costs/rollup")
async def get_costs_rollup(s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    """Returns the tenant's cost ledger rollup grouped by resource kind."""
    from app.kernel.services import get_tenant_cost_rollup
    rollup = await get_tenant_cost_rollup(s, tid)
    return {"tenant_id": tid, "rollup": rollup}








@app.get("/audit/verify", response_model=AuditVerifyOut)
async def verify_audit(s: AsyncSession = Depends(get_db)):
    ok, first_bad = await audit_verify(s)
    return {"ok": ok, "first_bad_id": first_bad}







@app.get("/audit/events", response_model=list[AuditEventOut])
async def list_audit_events(limit: int = 50, s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    stmt = select(AuditEvent).where(AuditEvent.tenant_id == tid).order_by(AuditEvent.id.desc()).limit(limit)
    res = await s.execute(stmt)
    events = res.scalars().all()
    return [
        {
            "id": ev.id,
            "ts": ev.ts,
            "actor": ev.actor,
            "action": ev.action,
            "op_id": ev.op_id,
            "payload": ev.payload,
            "hash": ev.hash
        } for ev in events
    ]


@app.get("/metrics/approval-latency")
async def approval_latency(domain: str | None = None, window_days: int | None = None,
                           s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    """North-star metric (§1): median/p90 approval latency, tenant-scoped. Read-only."""
    import datetime as _dt
    since = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=window_days)
             if window_days else None)
    rollup = await approval_latency_rollup(s, tid, domain=domain, since=since)
    return {"tenant_id": tid, "domain": domain, "window_days": window_days, **rollup}




class PolicySimulateIn(BaseModel):
    proposed_params: dict
    window_days: int | None = 30
    max_ops: int = 500
    save_draft: bool = False
    note: str | None = None
    created_by: str | None = None


@app.post("/policy-simulate", dependencies=[Depends(verify_operator_auth)])
async def policy_simulate(
    body: PolicySimulateIn,
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    """Replays historical operations against proposed ruleset changes and returns the differences."""
    from app.kernel.services import simulate_policy
    from app.models import PolicyVersion

    sim_res = await simulate_policy(
        s,
        tenant_id=tid,
        proposed_params_dict=body.proposed_params,
        window_days=body.window_days,
        max_ops=body.max_ops
    )

    draft_version = None
    if body.save_draft:
        # Determine next version
        stmt = (
            select(PolicyVersion.version)
            .where(PolicyVersion.tenant_id == tid)
            .order_by(PolicyVersion.version.desc())
            .limit(1)
        )
        res = await s.execute(stmt)
        last_version = res.scalar_one_or_none() or 0
        next_version = last_version + 1

        from app.kernel.services import load_active_ruleset_params
        baseline_params = await load_active_ruleset_params(s, tid)
        base_dict = baseline_params.__dict__.copy()
        base_dict.update(body.proposed_params)

        draft = PolicyVersion(
            tenant_id=tid,
            version=next_version,
            status="proposed",
            params=base_dict,
            note=body.note,
            created_by=body.created_by
        )
        s.add(draft)
        await s.commit()
        draft_version = next_version

    return {
        "simulation": sim_res,
        "draft_version": draft_version
    }


class RecipePromoteIn(BaseModel):
    recipe_name: str
    version: str = "0.1.0"


@app.post("/recipes/promote", dependencies=[Depends(verify_operator_auth)])
async def promote_recipe(body: RecipePromoteIn):
    """Promotes an experimental recipe to the production catalog and commits it to version control."""
    import shutil

    # Enforce strict regex character whitelist on recipe_name and version to block path traversal
    import re
    if not re.match(r"\A[a-zA-Z0-9_-]+\Z", body.recipe_name):
        raise HTTPException(status_code=400, detail="Invalid path traversal in recipe name or version")
    if not re.match(r"\A[a-zA-Z0-9_.-]+\Z", body.version):
        raise HTTPException(status_code=400, detail="Invalid path traversal in recipe name or version")

    recipes_root_abs = os.path.abspath(RECIPES_ROOT)
    recipes_root_prefix = recipes_root_abs + os.path.sep
    experimental_path = os.path.abspath(os.path.join(recipes_root_abs, "experimental", body.recipe_name, body.version))
    production_path = os.path.abspath(os.path.join(recipes_root_abs, body.recipe_name, body.version))

    if not experimental_path.startswith(recipes_root_prefix):
        raise HTTPException(status_code=400, detail="Invalid path traversal in recipe name or version")
    if not production_path.startswith(recipes_root_prefix):
        raise HTTPException(status_code=400, detail="Invalid path traversal in recipe name or version")

    if not os.path.exists(experimental_path):
        raise HTTPException(404, f"experimental recipe {body.recipe_name} v{body.version} not found")

    required_files = ["recipe.yaml", "main.tf"]
    for rf in required_files:
        if not os.path.exists(os.path.join(experimental_path, rf)):
            raise HTTPException(400, f"missing required file {rf} in experimental recipe")

    os.makedirs(os.path.dirname(production_path), exist_ok=True)

    if os.path.exists(production_path):
         shutil.rmtree(production_path)
    shutil.copytree(experimental_path, production_path)

    try:
        repo_dir = os.path.abspath(os.path.join(RECIPES_ROOT, ".."))
        subprocess.run(["git", "add", f"recipes/{body.recipe_name}/{body.version}/"], cwd=repo_dir, check=True, capture_output=True)
        commit_res = subprocess.run(
            ["git", "commit", "-m", f"prod(catalog): promote {body.recipe_name} {body.version} to production", "-m", "TAG=agy"],
            cwd=repo_dir, check=True, capture_output=True
        )
        commit_stdout = commit_res.stdout.decode()
    except Exception as e:
        commit_stdout = f"Git commit skipped or failed: {e}"

    return {
        "status": "promoted",
        "recipe_name": body.recipe_name,
        "version": body.version,
        "catalog_path": f"recipes/{body.recipe_name}/{body.version}",
        "commit": commit_stdout
    }










# =========================================================================
# Brand Twin & Recommender Engine Endpoints (Epic #166)
# =========================================================================

class BrandObjectiveIn(BaseModel):
    objective: str



class BrandPropertyOut(BaseModel):
    id: str
    type: str
    provider: str
    connection_ref: str | None = None
    status: str
    last_checked: dt.datetime | None = None
    findings: dict

@app.get("/brands/{brand_id}/graph", response_model=list[BrandPropertyOut])
async def get_brand_graph(brand_id: str, s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    """Returns the current Brand Graph properties from the database without mutating state."""
    validate_id(brand_id, "brand_id")
    from app.models import BrandProperty, Brand
    
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
        
    # Query and return all properties for this brand
    stmt = select(BrandProperty).where(BrandProperty.brand_id == brand_id).order_by(BrandProperty.type)
    res = await s.execute(stmt)
    properties = res.scalars().all()
    return properties


@app.get("/brands/{brand_id}/objective")
async def get_brand_objective(
    brand_id: str,
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    validate_id(brand_id, "brand_id")
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
    
    stmt = select(BrandObjective).where(BrandObjective.brand_id == brand_id)
    res = await s.execute(stmt)
    obj = res.scalar_one_or_none()
    return {"brand_id": brand_id, "objective": obj.objective if obj else "footprint"}


@app.post("/brands/{brand_id}/objective")
async def set_brand_objective(
    brand_id: str,
    body: BrandObjectiveIn,
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    validate_id(brand_id, "brand_id")
    if body.objective not in ("footprint", "growth", "retention"):
        raise HTTPException(400, "Invalid objective value. Must be footprint, growth, or retention.")
        
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
        
    stmt = select(BrandObjective).where(BrandObjective.brand_id == brand_id)
    res = await s.execute(stmt)
    obj = res.scalar_one_or_none()
    
    if obj:
        obj.objective = body.objective
    else:
        obj = BrandObjective(tenant_id=tid, brand_id=brand_id, objective=body.objective)
        s.add(obj)
        
    return {"brand_id": brand_id, "objective": body.objective}






@app.get("/brands/{brand_id}/status")
async def get_brand_status(brand_id: str, s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    """Fetches Shopify connection status and metrics for a brand."""
    validate_id(brand_id, "brand_id")
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")

    stmt = select(Connection).where(
        Connection.tenant_id == tid,
        Connection.brand_id == brand_id,
        Connection.provider == "shopify"
    )
    res = await s.execute(stmt)
    conn = res.scalar_one_or_none()
    if not conn:
        return {
            "brand_id": brand_id,
            "shopify_connected": False,
            "metrics": {}
        }

    # Mock token retrieval from Secret Manager
    mock_token = f"mocked-token-for-{conn.credential}"

    from app.services.shopify import MockShopifyClient
    client = MockShopifyClient(shop_url=conn.config.get("shop_url"), token=mock_token)
    metrics = await client.get_metrics()

    return {
        "brand_id": brand_id,
        "shopify_connected": True,
        "metrics": metrics
    }


@app.post("/brands/{brand_id}/sense")
async def trigger_brand_sense(
    brand_id: str,
    background_tasks: BackgroundTasks,
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id),
    worker_session_maker = Depends(get_worker_session_maker)
):
    """Triggers a comprehensive brand sensing task via the microkernel."""
    validate_id(brand_id, "brand_id")
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
        
    from app.tasks.sense import run_brand_sense
    
    async def _run_sense():
        async with worker_session_maker() as session:
            async with session.begin():
                await run_brand_sense(session, tid, brand_id)
                
    background_tasks.add_task(_run_sense)
    return {"status": "accepted"}


@app.get("/brands/{brand_id}/poas")
async def get_brand_poas(brand_id: str, s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    validate_id(brand_id, "brand_id")
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
    
    from app.profit.poas import calculate_campaign_poas
    reports = await calculate_campaign_poas(s, tid, brand_id)
    return {"brand_id": brand_id, "reports": reports}

@app.get("/brands/{brand_id}/performance-score")
async def get_brand_performance_score(brand_id: str, s: AsyncSession = Depends(get_db), tid: str = Depends(tenant_id)):
    validate_id(brand_id, "brand_id")
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found")
    
    from app.profit.brand_score import calculate_brand_score
    score = await calculate_brand_score(s, tid, brand_id)
    return {"brand_id": brand_id, "performance_score": score}

@app.get("/metrics/brand-performance")
async def brand_performance(
    brand_id: str,
    w_ux: float | None = Query(default=None, ge=0.0, le=10.0),
    w_organic: float | None = Query(default=None, ge=0.0, le=10.0),
    w_paid: float | None = Query(default=None, ge=0.0, le=10.0),
    w_pr: float | None = Query(default=None, ge=0.0, le=10.0),
    s: AsyncSession = Depends(get_db),
    tid: str = Depends(tenant_id)
):
    """Computes the composite Brand Performance Score. Advisory only; read-only."""
    validate_id(brand_id, "brand_id")
    brand = await s.get(Brand, brand_id)
    if not brand or brand.tenant_id != tid:
        raise HTTPException(404, "Brand not found for tenant")

    from app.profit.brand_score import calculate_brand_performance_score
    score_report = await calculate_brand_performance_score(
        s,
        tenant_id=tid,
        brand_id=brand_id,
        w_ux=w_ux,
        w_organic=w_organic,
        w_paid=w_paid,
        w_pr=w_pr
    )
    return score_report






