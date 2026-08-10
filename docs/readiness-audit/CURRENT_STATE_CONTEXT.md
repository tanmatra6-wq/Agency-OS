# CURRENT_STATE_CONTEXT — Deep Context Report

This document records the exact architectural implementation, topology, tech stack, and configurations of Agency-OS at the pinned audit state.

**Repository Pinned State:**
- **Target Branch**: `feature/s4-grow-premium-remediation`
- **Target Commit SHA**: `20646e345bba6fb2756872ce1b78a27b9401173a`

---

## 1. Codebase Topology

Agency-OS is structured as a full-stack monorepo containing a Python FastAPI backend and a Next.js frontend web application inside a single repository:

```
/ (root)
├── control-plane/              # Main control plane codebase
│   ├── app/                    # FastAPI backend codebase
│   │   ├── adapters/           # Pillar adapters (provision, build, grow, manage, presence, dr)
│   │   ├── kernel/             # Governance kernel (action loop, policy gates, plugins)
│   │   ├── routers/            # HTTP routers (onboarding router)
│   │   ├── services/           # Core background and auxiliary services
│   │   ├── database.py         # DB connection pool, transaction contexts & RLS middleware setup
│   │   ├── main.py             # Backend main API entrypoint and background task routes
│   │   ├── models.py           # Core DB SQLAlchemy models
│   │   └── observability.py    # Structured logging, Sentry initialization & metrics setup
│   ├── tests/                  # Pytest backend unit, integration, and RLS validation suite
│   └── web/                    # Next.js frontend web application
│       ├── src/
│       │   ├── app/            # App Router pages, layouts, and route paths
│       │   ├── components/     # Reusable shadcn/Tailwind UI elements and drawers
│       │   ├── contexts/       # React contexts (TenantContext)
│       │   ├── lib/            # Shared libraries (api-client hook)
│       │   └── providers/      # React provider composition (QueryProvider)
│       ├── package.json        # Frontend node dependencies & scripts
│       └── tsconfig.json       # TypeScript compiler settings
├── recipes/                    # Terraform recipes/modules for infrastructure provisioning
│   ├── brand-baseline/         # Project baseline provisioning (SAs, logging, budgets)
│   ├── postgres-db/            # Cloud SQL PostgreSQL database provisioning
│   ├── web-host/               # Cloud Run service, DNS zone, and SSL setup
│   └── ...                     # Other service blueprints
├── scripts/                    # Platform CLI maintenance & onboarding setup scripts
├── docs/                       # Platform specifications and documentation
├── legacy/                     # Read-only reference archives (Do not import or edit)
├── AGENTS.md                   # Binding rules and constraints for AI agents
├── ARCHITECTURE.md             # Authoritative master architecture specification
└── setup.sh                    # One-time developer environment setup script
```

---

## 2. Tech Stack & Runtime Matrix

- **Backend Runtime**: Python `3.13.12`
- **Backend Framework**: FastAPI `0.110.x` + Pydantic `2.6.x`
- **ORM & Driver**: SQLAlchemy `2.0` + `asyncpg` (PostgreSQL async driver)
- **Database**: SQLite (local development/tests) / PostgreSQL (production Cloud SQL)
- **Database Migrations**: Alembic
- **Background Queue**: Cloud Tasks (in production, emulated locally via transactional outbox worker queue)
- **Monitoring & Observability**: Sentry SDK, Prometheus metrics client (`/metrics` endpoint), structured JSON logging to Cloud Logging
- **Advisory MMM Engine**: Google Meridian (run in background sweeps for Grow calibrations)
- **Web Scraping & Competitor Audits**: Playwright Headless Chromium (under Presence Sense adapter)
- **Frontend Runtime**: Node.js `20.x`
- **Frontend Framework**: Next.js `16.2.9` (App Router) + React `19.2.4` + `@tanstack/react-query` `5.101.0`
- **UI Styling**: Tailwind CSS `4.0`, shadcn/ui components, `lucide-react` icons
- **Frontend Testing**: Vitest `4.1.8`

---

## 3. Environment & Configuration Model

### 3.1 Local Development Credential Loading
The platform credentials are loaded into local shell variables or Secret Manager via `load_credentials.sh` from a local `credentials.env` file (copied from `credentials.example.env`). Obvious placeholders (e.g. `mock-*`) and blank values are skipped.

### 3.2 Secret Manager Client (`secrets.py`)
- **Production Mode**: Credentials are read dynamically from Google Cloud Secret Manager.
- **Development/Test Mode**: Falls back to a local JSON database mock `scratch/mock_secrets.json`.
- **Tenant Project Isolation Boundary**: The `SecretManagerClient` is initialized with a tenant's specific `gcp_project` if the tenant is on a `dedicated` hosting tier. If the project ID is omitted, it defaults to the platform's control plane project `aos-control-plane` (representing a physical isolation gap when adapters fail to pass the tenant's project ID).

---

## 4. Architecture as Implemented

### 4.1 Row-Level Security (RLS) & Database Isolation
- Every tenant-scoped table has RLS enabled and enforced at the database level.
- The `get_db()` FastAPI dependency sets the session variable `app.current_tenant_id` for every transaction block:
  `SELECT set_config('app.current_tenant_id', :tenant_id, true)`
- **Worker Bypass**: The background task runner (`get_worker_db()`) uses a separate connection pool and session maker. To bypass RLS without connecting as a superuser in production (which would override safety controls and cause security issues), a permissive `worker_bypass` policy is defined on all RLS-enabled tables:
  `CREATE POLICY worker_bypass ON public.<table> AS PERMISSIVE FOR ALL TO aos_api_rls_worker USING (true)`
  This allows background workers to schedule and query across tenants while keeping app-level transaction blocks isolated.

### 4.2 Webhook Routing & Trust Boundaries
- Webhooks land at `/webhooks/plugins/{provider}` or `/webhooks/whatsapp`.
- Signature verification is completed before setting the transaction-scoped tenant ID:
  - WhatsApp: Verified using Meta app secret.
  - Third-party plugins: Webhook router queries the connection registry (bypassing RLS using a worker DB session) to match the provider and connection endpoint, resolving the tenant ID. The signature is verified using the secret referenced by `conn.secret_ref` before setting the context variable `tenant_context` and proposing the Op.

---

## 5. Frontend Wiring and Rendering Model

### 5.1 Provider Composition Tree
The frontend UI boots up inside `control-plane/web/src/app/layout.tsx` by wrapping the child route layout in the following tree:
```
TenantProvider (React Context)
└── QueryProvider (React Query Client)
    └── DevGate (Development Gateway Bypass & Role Selector)
        └── Layout / Page Router (Next.js Children)
```

### 5.2 Routing & Navigation Layouts
- Next.js App Router uses nested folders under `src/app/(dashboard)`.
- Main pages:
  - `(dashboard)/twin` — Brand Twin view (objective selectors and organic citation audit findings).
  - `(dashboard)/ops` — Governance Queue view (listing proposed, executing, pending, and failed Ops).
  - `(dashboard)/poas` — POAS Analytics reports.
  - `(dashboard)/connections` — Shopify/Meta/Google connection management panel.
  - `(dashboard)/audit` — Append-only cryptographic ledger event listings with integrity verification.
  - `(dashboard)/safety` — Active Circuit Breaker toggles and status indicators.

### 5.3 Data Fetching & State Synchronization
- API requests are routed through the `useApi` hook (`api-client.ts`), which automatically resolves `tenantId` and `operatorToken` from the React `TenantContext` and attaches them as the headers `X-Tenant-ID` and `Authorization: Bearer <token>`.
- Client-side data fetching and mutations use `@tanstack/react-query` with a default `staleTime` of 60 seconds and `refetchOnWindowFocus` disabled to prevent excessive polling load.
- Exposing dynamic drawers: Opening detail parameters (e.g. `?opId=...`) is driven by URL search params, ensuring page refreshes preserve state.
