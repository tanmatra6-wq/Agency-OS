# Threat Model

This document outlines the primary threat scenarios for Agency OS and the mitigating controls, satisfying Phase 0 of the Production Readiness Runbook.

## Scenarios & Mitigations

### 1. Cross-Tenant ID Manipulation
* **Threat**: An attacker or compromised service manipulates `tenant_id` or `brand_id` parameters in API requests to access or modify another tenant's data.
* **Control**: 
  - **GCP Boundary**: Dedicated tier tenants run in separate GCP projects.
  - **PostgreSQL RLS**: All tenant-bearing tables have Row-Level Security (RLS) enabled and forced.
  - **Database Context**: `app.current_tenant_id` is set securely by the session middleware based on authenticated connection metadata, preventing parameter tampering.

### 2. Webhook Forgery
* **Threat**: An attacker sends fabricated webhook payloads to `/webhooks/plugins/{provider}` to trigger unauthorized operations.
* **Control**:
  - **Signature Verification**: The webhook router strictly verifies cryptographic signatures using provider-specific secrets (`secret_ref`).
  - **Constant-time Comparison**: Hashes and signatures are compared using constant-time functions.
  - **Replay Protection**: Timestamps/nonces in webhooks are checked for freshness. Duplicate events are deduplicated before processing.

### 3. Approval Replay / Forgery
* **Threat**: An attacker intercepts an approval token or re-submits a stale approval action to force execution of a rejected or expired operation.
* **Control**:
  - **Deterministic State Machine**: Operations can only transition from `AWAITING_APPROVAL` to `APPROVED`.
  - **Approval Binding**: Approvals cryptographically bind to the exact preview digest. Any modification requires re-planning and a new approval.
  - **Expiry (TTL)**: Operations expire if not approved within their TTL window.

### 4. Compromised Worker (LLM Output / Adapters)
* **Threat**: A worker executing untrusted LLM output (e.g., Code generation, Terraform recipes) is compromised and attempts to escalate privileges or access shared secrets.
* **Control**:
  - **Isolated Ephemeral Environments**: Git workers and execution runners use ephemeral workspaces with short-lived tokens.
  - **Strict Scoping**: Service accounts used by workers have no ambient credentials; they are bound to the specific operation's scope.
  - **No LLM Auth**: LLM output is explicitly prevented from altering policy decisions or bypassing the verification gates.

### 5. Leaked Credentials in Logs or Git
* **Threat**: Sensitive credentials (e.g., Shopify tokens, ad credentials) are inadvertently logged, stored in Git, or exposed in audit trails.
* **Control**:
  - **Audit Ledger Redaction**: Audit events contain only metadata and references; raw secrets and customer payloads are filtered.
  - **Secret Resolution**: Secrets are resolved dynamically via Secret Manager and never injected into command-line arguments or environment variables visible in process lists.
  - **CI/CD Scanning**: Required Secret Scanning in CI pipelines prevents credential commits.
