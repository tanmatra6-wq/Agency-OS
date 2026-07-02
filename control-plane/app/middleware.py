from app.database import tenant_context
from app.observability import trace_context
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
import os
import time
import uuid
import time

from app.database import AsyncSessionLocal
from app.models import Tenant
from sqlalchemy import select, text


class TraceMiddleware(BaseHTTPMiddleware):
  """Generates or propagates trace IDs to coordinate logs and telemetry across service hops."""

  async def dispatch(self, request: Request, call_next):
    raw_trace_id = request.headers.get("X-Trace-ID") or request.headers.get("traceparent") or request.headers.get("x-cloud-trace-context")
    trace_id = None
    
    if raw_trace_id:
      # 1. Parse W3C traceparent: 00-trace_id-span_id-flags
      if raw_trace_id.startswith("00-") and len(raw_trace_id.split("-")) == 4:
        trace_id = raw_trace_id.split("-")[1]
      # 2. Parse X-Cloud-Trace-Context: trace_id/span_id;o=options
      elif "/" in raw_trace_id:
        trace_id = raw_trace_id.split("/")[0]
      else:
        trace_id = raw_trace_id[:32]  # fallback truncate
        
    if not trace_id:
      trace_id = f"tr-{uuid.uuid4().hex[:16]}"
      
    token = trace_context.set(trace_id)
    try:
      response = await call_next(request)
      response.headers["X-Trace-ID"] = trace_id
      return response
    finally:
      trace_context.reset(token)



class _TTLTenantCache:
  """Dict-like cache of tenant_id -> is_active with a per-entry TTL.

  The old plain-dict cache never expired, so a tenant suspended/deleted on one
  Cloud Run instance stayed cached as active in every OTHER instance's memory
  until that instance restarted. Bounding each entry with a TTL forces every
  instance to re-validate against the DB within TENANT_CACHE_TTL_SECONDS, while
  preserving the zero-latency fast path within the window. Drop-in for the dict
  API the call sites already use (item get/set, `in`, pop, clear).
  """

  def __init__(self, ttl_seconds: float):
    self._ttl = ttl_seconds
    self._d: dict[str, tuple[bool, float]] = {}

  def __setitem__(self, key: str, value: bool) -> None:
    self._d[key] = (value, time.monotonic())

  def __contains__(self, key: str) -> bool:
    entry = self._d.get(key)
    if entry is None:
      return False
    if time.monotonic() - entry[1] > self._ttl:
      self._d.pop(key, None)  # lazily evict on read
      return False
    return True

  def __getitem__(self, key: str) -> bool:
    return self._d[key][0]

  def pop(self, key: str, default=None):
    entry = self._d.pop(key, None)
    return default if entry is None else entry[0]

  def clear(self) -> None:
    self._d.clear()


# Memory cache mapping tenant_id -> is_active (bool) for a zero-latency fast-path,
# bounded by TTL so suspensions/deletions propagate across instances.
TENANT_CACHE_TTL_SECONDS = float(os.getenv("TENANT_CACHE_TTL_SECONDS", "60"))
VALID_TENANTS_CACHE = _TTLTenantCache(TENANT_CACHE_TTL_SECONDS)


class TenantIsolationMiddleware(BaseHTTPMiddleware):
  """Parses and asserts valid tenant headers across all endpoint executions,
  populating async-safe storage boundaries to prevent database leaks.
  """

  async def dispatch(self, request: Request, call_next):
    # Header format: X-Tenant-ID
    tenant_id = request.headers.get("X-Tenant-ID")

    # Bypass validation strictly on public, onboarding, or operator-scoped paths
    if (request.url.path.startswith("/webhooks/plugins/") or 
        request.url.path.startswith("/api/v1/onboarding") or
        request.url.path.startswith("/tenants") or 
        request.url.path in ["/healthz", "/readyz", "/health", "/docs", "/openapi.json", "/audit/verify", "/tasks/drain-outbox", "/webhooks/whatsapp", "/tasks/trust-snapshots", "/tasks/process-cadences", "/tasks/evaluate-trust", "/tasks/calibrate-attribution", "/dashboard", "/metrics", "/tasks/refresh-tokens", "/tasks/drift-detect", "/tasks/run-diagnostics", "/connections/oauth/callback", "/session/bootstrap"]):
      return await call_next(request)

    if not tenant_id:
      return JSONResponse(
          status_code=status.HTTP_401_UNAUTHORIZED,
          content={"detail": "X-Tenant-ID header is missing."},
      )

    # --- SECURE TENANT VALIDATION ---
    bypass_validation = getattr(request.app.state, "bypass_tenant_validation", False)
    if not bypass_validation:
      if tenant_id in VALID_TENANTS_CACHE:
        if not VALID_TENANTS_CACHE[tenant_id]:
          return JSONResponse(
              status_code=status.HTTP_403_FORBIDDEN,
              content={"detail": "Forbidden: Tenant account is suspended."},
          )
      else:
        # 2. Slow path: check database
        try:
          session_maker = getattr(request.app.state, "db_session_maker", AsyncSessionLocal)
          async with session_maker() as session:
            # Set the GUC to tenant_id before querying tenants table, so RLS policy
            # (id = current_setting('app.current_tenant_id')) evaluates to True for this tenant.
            if session.bind.dialect.name == "postgresql":
              await session.execute(
                  text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                  {"tenant_id": tenant_id},
              )
            stmt = select(Tenant.id, Tenant.is_active).where(Tenant.id == tenant_id)
            res = await session.execute(stmt)
            row = res.first()
            
            if not row:
              return JSONResponse(
                  status_code=status.HTTP_401_UNAUTHORIZED,
                  content={"detail": "Unauthorized: Tenant not registered."},
              )
            
            db_tenant_id, is_active = row
            
            # Cache the verified tenant status
            VALID_TENANTS_CACHE[tenant_id] = is_active
            
            if not is_active:
              return JSONResponse(
                  status_code=status.HTTP_403_FORBIDDEN,
                  content={"detail": "Forbidden: Tenant account is suspended."},
              )
        except Exception as e:
          # Safeguard: If DB query fails, log and block the request (fail closed)
          import logging
          logger = logging.getLogger("app.middleware")
          logger.error(f"Database error during tenant validation for {tenant_id}: {e}")
          return JSONResponse(
              status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
              content={"detail": "Internal server error during tenant verification."},
          )

    # Set tenant identification safely across the current thread-safe context
    token = tenant_context.set(tenant_id)
    try:
      response = await call_next(request)
      return response
    finally:
      # Safely clear the active context after processing ends
      tenant_context.reset(token)


active_rate_limiter = None


class RateLimitMiddleware(BaseHTTPMiddleware):
  """Deterministic in-house token bucket rate limiter protecting public endpoints."""

  def __init__(self, app, rate: float = 0.2, capacity: float = 5.0):
    super().__init__(app)
    self.rate = rate  # tokens replenished per second
    self.capacity = capacity  # burst capacity
    global active_rate_limiter
    active_rate_limiter = self
    import time
    from collections import defaultdict
    self.time = time
    self.defaultdict = defaultdict
    if not hasattr(app, "_rate_limit_buckets"):
      app._rate_limit_buckets = self.defaultdict(lambda: (capacity, self.time.time()))
    self.buckets = app._rate_limit_buckets

  async def dispatch(self, request, call_next):
    # Rate limit POST /chat, public webhooks, and onboarding bootstrap
    rate_limited_paths = ["/chat", "/webhooks/whatsapp", "/tenants", "/intents", "/actions", "/policy-simulate", "/api/v1/onboarding/bootstrap"]
    if request.method == "POST" and request.url.path in rate_limited_paths:
      client_ip = request.client.host if request.client else "unknown"
      now = self.time.time()
      tokens, last_update = self.buckets[client_ip]

      # Replenish tokens
      elapsed = now - last_update
      replenished = tokens + elapsed * self.rate
      new_tokens = min(self.capacity, replenished)

      if new_tokens >= 1.0:
        # Consume 1 token
        self.buckets[client_ip] = (new_tokens - 1.0, now)
      else:
        # No tokens left: reject with 429 and Retry-After header
        self.buckets[client_ip] = (new_tokens, now)
        retry_after = int((1.0 - new_tokens) / self.rate) + 1
        return JSONResponse(
            status_code=429,
            content={"detail": "Too many requests. Please try again later."},
            headers={"Retry-After": str(retry_after)}
        )

    return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
  """Sets standard security headers on all outgoing HTTP responses to protect against common web vulnerabilities."""

  async def dispatch(self, request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Content-Security-Policy"] = "default-src 'self'"
    response.headers["Referrer-Policy"] = "no-referrer-when-downgrade"
    return response


from prometheus_client import Counter, Histogram, REGISTRY
import time

# Module-level globals, initialized once
if "http_requests_total" in REGISTRY._names_to_collectors:
  REQUEST_COUNT = REGISTRY._names_to_collectors["http_requests_total"]
else:
  REQUEST_COUNT = Counter(
      "http_requests_total",
      "Total number of HTTP requests processed",
      ["method", "path", "status"]
  )

if "http_request_duration_seconds" in REGISTRY._names_to_collectors:
  REQUEST_LATENCY = REGISTRY._names_to_collectors["http_request_duration_seconds"]
else:
  REQUEST_LATENCY = Histogram(
      "http_request_duration_seconds",
      "HTTP request latency in seconds",
      ["method", "path"]
  )

class MetricsMiddleware(BaseHTTPMiddleware):
  """Collects Prometheus metrics for API requests (latencies, counts, status codes)."""

  async def dispatch(self, request: Request, call_next):
    start_time = time.time()
    try:
      response = await call_next(request)
      status_code = response.status_code
      return response
    except Exception:
      status_code = 500
      raise
    finally:
      duration = time.time() - start_time
      path = request.url.path
      REQUEST_COUNT.labels(method=request.method, path=path, status=status_code).inc()
      REQUEST_LATENCY.labels(method=request.method, path=path).observe(duration)

