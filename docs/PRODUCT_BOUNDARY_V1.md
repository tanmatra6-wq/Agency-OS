# Product Boundary & Trust Zones (V1)

This document maps the trust boundaries and control flows within Agency OS as required by Phase 0 of the Production Readiness Runbook.

## Trust Boundaries

### 1. Webhook Ingestion Boundary (External -> Webhook Router)
* **Authenticated Identity**: Unauthenticated initially, verified via provider HMAC (e.g., Shopify, Stripe).
* **Authorization Decision**: Verified if signature matches stored secret for the resolved connection.
* **Tenant Resolution**: Connection identifier (e.g., shop URL) is looked up in global `connections` to resolve `tenant_id` and `brand_id`.
* **Encryption**: TLS 1.2+ required.
* **Replay Control**: Webhook timestamps must be within freshness window; events are deduplicated based on provider event ID.
* **Input Validation**: Schema validation on payload; dropped if malformed.
* **Timeout / Retry**: Bounded 10s timeout; provider retries on 4xx/5xx.
* **Logging**: Metadata only (provider, event type, tenant). Webhook body is redacted.
* **Sensitive Fields**: OAuth tokens, customer PII (e.g., email, address).
* **Expected Failure State**: 401/404 on auth failure, 422 on bad payload. Fails closed.

### 2. Control Plane Database Boundary (API/Worker -> Cloud SQL)
* **Authenticated Identity**: IAM Service Account / Cloud SQL Auth Proxy.
* **Authorization Decision**: Postgres Row-Level Security (RLS) policies based on `app.current_tenant_id`.
* **Tenant Resolution**: Explicitly set in session middleware before queries execute.
* **Encryption**: TLS 1.3 in transit, AES-256 at rest.
* **Replay Control**: N/A (Standard DB transactions).
* **Input Validation**: Handled by SQLAlchemy / ORM layer.
* **Timeout / Retry**: Statement timeouts enforced; application-level retries on transient errors.
* **Logging**: Connection events logged; no PII or query values logged.
* **Sensitive Fields**: Database credentials, decrypted secrets.
* **Expected Failure State**: Access Denied if RLS session context is missing or invalid.

### 3. Third-Party API Boundary (Adapter -> External Platform)
* **Authenticated Identity**: Resolved short-lived token or OAuth credential from Secret Manager.
* **Authorization Decision**: Token scopes enforced by the external platform (e.g., Meta, Google Ads).
* **Tenant Resolution**: Token mapped to the specific `tenant_id` via adapter context.
* **Encryption**: TLS 1.2+ required.
* **Replay Control**: Op idempotency keys passed to external platforms.
* **Input Validation**: Strict formatting of requests per platform requirements.
* **Timeout / Retry**: Exponential backoff with circuit breaker and bounded timeouts.
* **Logging**: API call metadata (URL, status); no auth headers or sensitive payload bodies logged.
* **Sensitive Fields**: Auth tokens, budget data, PII.
* **Expected Failure State**: Circuit breaker trips on repeated failures, Op marks as `PARTIAL` or `FAILED`.
