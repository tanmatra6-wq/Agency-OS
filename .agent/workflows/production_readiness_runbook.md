---
description: Agency OS Production Readiness Runbook
---

# Runbook
**Purpose:** Convert the current Agency OS foundation into a production-capable, pilot-ready release with demonstrable tenant isolation, controlled execution, recovery, and audit evidence.

**Important security position**
No checklist can guarantee that a system will never be breached. This runbook is designed to reduce likelihood, limit blast radius, detect failures quickly, preserve evidence, and support safe recovery.

**Use:**
* NIST SSDF 1.1 as the secure-development baseline. NIST recommends integrating secure-development practices into the SDLC to reduce released vulnerabilities, limit the impact of undetected vulnerabilities, and prevent recurrence.
* OWASP ASVS 5.0 Level 2 as the minimum application-security verification target, with selected Level 3 controls for tenant isolation, credentials, approvals, audit integrity, and high-impact operations. ASVS provides a testable technical-security baseline rather than only general guidance.

---

## 1. Agent operating instructions
Give the engineering agent these non-negotiable instructions before implementation.

You are implementing the production-readiness release of Agency OS.

**PRIMARY OBJECTIVE**
Make the defined V1 operation path safe, tenant-isolated, previewable,
approval-bound, idempotent, independently verifiable, auditable, and recoverable.

**OPERATING RULES**
* Never weaken, bypass, mock, or disable a security control to make a test pass.
* Never introduce a fallback from a tenant resource to a shared resource.
* Never retrieve or use a secret without an explicit tenant and project scope.
* Never use ambient Git, cloud, database, or operating-system credentials.
* Never log secrets, tokens, authorization headers, cookies, private keys, webhook bodies containing sensitive data, or decrypted customer content.
* Never allow an LLM response to authorize an operation.
* Never execute an operation whose approved artifact differs from the current execution artifact.
* Never mark an operation DONE based only on executor success.
* Never assume exactly-once delivery. All handlers must be safe under retries.
* Never allow an unsupported action to crash a worker.
* Never perform a destructive migration without a tested rollback or recovery plan.
* Never modify production directly from a developer workstation.
* Never merge code that lacks the required tests, evidence, and review.
* Stop immediately and create a security finding if tenant data can cross boundaries.
* Stop immediately if a secret, credential, or decrypted customer artifact is exposed.

**REQUIRED OUTPUT FOR EVERY CHANGE**
* Threat/failure scenario
* Security assumptions
* Implementation summary
* Tests added
* Negative tests added
* Tenant-isolation tests added
* Observability added
* Rollback procedure
* Residual risks
* Audit evidence paths

**DEFINITION OF DONE**
A change is not complete until:
* tests pass;
* security tests pass;
* static and dependency scans pass;
* audit evidence is generated;
* rollback is documented;
* a reviewer has approved the change;
* no unresolved Critical or High vulnerability exists.

---

## 2. Fixed scope for this runbook
The agent must not expand the product while performing this work.

**In scope**
* Governance Kernel
* Universal Op contract
* Approval and policy service
* Webhook router
* Transactional outbox
* Cloud Tasks workers
* Tenant and brand isolation
* Secret Manager resolution
* Git worker
* Terraform execution
* Shopify handler
* Advertising adapters
* Audit ledger
* Disaster-recovery processing
* Cloud Run and Cloud SQL deployment
* us-central1 cutover readiness
* Pilot observability and evidence reports

**Out of scope**
* New product pillars
* Broad AWS or Azure write access
* Unrestricted natural-language infrastructure
* Arbitrary Terraform generation
* Tier 2 autonomy for new customers
* Large production database transformations
* Large advertising-budget changes
* Taxes, statutory submissions or large refunds
* Any integration without a complete Op contract

---

## 3. Roles and separation of duties
A production release must not depend on one engineering agent approving its own output.

| Role | Responsibility | Must not |
|---|---|---|
| **Engineering agent** | Implements changes and tests | Approve its own production release |
| **Human engineering reviewer** | Reviews design, code and tests | Approve security exceptions alone |
| **Security reviewer** | Reviews threat model and security evidence | Implement and self-approve the same control |
| **Release manager** | Authorizes deployment | Modify artifacts after approval |
| **Operations owner** | Monitors and handles incidents | Alter audit evidence |
| **Data owner** | Approves data handling and retention | Grant unrestricted technical access |
| **Business approver** | Approves financial or customer impact | Bypass technical policies |
| **Verifier service** | Verifies resulting state | Execute the original change |

At minimum, production deployment must require:
* engineering review;
* security review for security-sensitive changes;
* immutable release artifact;
* release-manager approval;
* automated post-deployment verification.

---

## 4. Required repository structure
The exact names can vary, but the separation should be clear.

```
/docs
  PRODUCT_BOUNDARY_V1.md
  THREAT_MODEL.md
  DATA_CLASSIFICATION.md
  OP_CATALOG.md
  SECURITY_ARCHITECTURE.md
  INCIDENT_RESPONSE.md
  BACKUP_RESTORE.md
  CUTOVER.md
  RELEASE_RUNBOOK.md
  ACCESS_CONTROL_MATRIX.md
  RETENTION_SCHEDULE.md
  VENDOR_REGISTER.md
  RESIDUAL_RISK_REGISTER.md

/services
  /control-plane
    /policy-engine
    /approval-service
    /webhook-router
    /outbox-worker
    /op-executor
    /op-verifier
    /audit-service
    /git-worker
    /dr-worker
  /adapters
    /gcp
    /terraform
    /github
    /shopify
    /meta-ads
    /google-ads
  /contracts
    /ops
    /events
    /policies
    /evidence

/infrastructure
  /terraform
    /control-plane
    /shared-tier
    /dedicated-tier
    /region-cutover

/security
  /policies
  /threat-models
  /test-cases
  /audit-queries
  /incident-playbooks

/tests
  /unit
  /contract
  /integration
  /security
    /tenant-isolation
    /failure-injection
    /recovery
  /end-to-end
  /evidence
    /templates
```
*Do not store real audit evidence, credentials, production exports, customer data, database dumps, or secret values in Git.*

---

## 5. Phase 0 — Establish the security baseline
### 5.1 Inventory assets
Create a machine-readable and human-readable inventory covering:
* GCP organizations, folders and projects;
* Cloud Run services;
* service accounts;
* Cloud SQL instances;
* databases and schemas;
* GCS buckets;
* Terraform state buckets;
* Cloud Tasks queues;
* Secret Manager secrets;
* Artifact Registry repositories;
* Git repositories and applications;
* Shopify stores;
* Meta and Google advertising accounts;
* customer domains;
* webhooks;
* cryptographic keys;
* external subprocessors;
* administrative users;
* all externally accessible endpoints.

Every asset record must include: `asset_id`, `tenant_id`, `environment`, `owner`, `data_classification`, `region`, `authentication_method`, `authorized_services`, `retention_rule`, `backup_rule`, `recovery_objective`, `monitoring_rule`, `decommission_rule`.

### 5.2 Classify data
Use at least: Public, Internal, Confidential, Restricted.
Restricted data should include:
* authentication tokens;
* Git credentials;
* cloud credentials;
* webhook secrets;
* advertising account credentials;
* customer PII;
* decrypted backups;
* private keys;
* database exports;
* recovery material.

### 5.3 Map trust boundaries
Document data and control flow between components. For every boundary, document: authenticated identity, authorization decision, tenant resolution, encryption, replay control, input validation, timeout, retry behavior, logging, sensitive fields, expected failure state.

### 5.4 Threat-model required scenarios
At minimum, model scenarios such as cross-tenant ID manipulation, webhook forgery, approval replay, compromised workers, leaked credentials, etc.

**Phase 0 exit gate**
* Asset inventory complete.
* Data classification approved.
* Trust-boundary diagram approved.
* Threat model reviewed.
* No unknown production service identity.
* Every V1 Op has an assigned risk class.

---

## 6. Phase 1 — Harden tenant isolation
### 6.1 GCP project boundary
Use central control-plane project, shared tier only for explicitly eligible small tenants, dedicated project per enterprise tenant. Never infer the tenant project from a user-provided project ID.

### 6.2 Identity isolation
Create single-purpose service accounts with explicit impersonation grants, short-lived credentials, and environment-specific constraints.

### 6.3 PostgreSQL RLS
Apply RLS to all tenant-bearing tables (e.g. `brands`, `users`, `ops`, `outbox`, `audit_events`, etc.).
* Required controls: `ENABLE ROW LEVEL SECURITY` and `FORCE ROW LEVEL SECURITY`.
* Application role must not own tables, be superuser, or have BYPASSRLS.

**Phase 1 exit gate**
* RLS inventory reports 100% coverage.
* Every tenant table has a policy.
* Application roles have no bypass property.
* Cross-tenant test suite passes.

---

## 7. Phase 2 — Correct secret and credential handling
### 7.1 Tenant-scoped secret resolution
Every secret request must include scope details (tenant, environment, purpose). Missing tenant configuration must fail closed. Secret values must not be logged, stored in Git, or sent to the LLM.

### 7.2 Rotation
Support current and next secret versions, automated rotation status, and emergency revocation.

### 7.3 Git credentials
The Git worker must start in an ephemeral tenant-scoped workspace with short-lived tokens, restrict scope, disable global Git configs, and destroy the workspace after completion.

**Phase 2 exit gate**
* Literal secret references cannot be used as cryptographic keys.
* Git worker has no ambient credential access.
* Secret access logs contain metadata only.
* Rotation and revocation drills pass.

---

## 8. Phase 3 — Secure webhook ingestion
**Required processing order:**
Receive bounded raw request → enforce TLS → apply limit → identify tenant safely → resolve secret → verify signature over raw bytes → constant-time comparison → check freshness → deduplicate → validate payload → map to Op → persist transactionally.

**Phase 3 exit gate**
* No webhook can propose an Op before authentication.
* Duplicate concurrent deliveries create one accepted event.
* Webhook body is redacted from normal logs.

---

## 9. Phase 4 — Implement the authoritative Op contract
Every state-changing change must be represented by one immutable operation envelope with required metadata (tenant, action, risk class, etc.).

**Required state model:** `PROPOSED`, `PREVIEWING`, `PREVIEWED`, `AWAITING_APPROVAL`, `APPROVED`, `EXECUTING`, `VERIFYING`, `DONE`, etc.

**Phase 4 exit gate**
* Illegal transitions are rejected.
* Unsupported actions do not crash workers.
* Every enabled Op has preview, policy, execution, and verification tests.

---

## 10. Phase 5 — Deterministic policy and approval security
The policy engine receives structured data, not free-form LLM conclusions. Approval must bind the approver to the exact immutable change (including preview digest and maximum impact). High-risk approvals require step-up authentication.

**Phase 5 exit gate**
* Approved preview digest equals execution digest.
* Expired/replayed approvals cannot execute.
* LLM-generated output cannot alter a policy decision.

---

## 11. Phase 6 — Idempotent outbox and worker execution
**Transactional outbox:** Create Op, append transition, write outbox row, and commit within one transaction.
**Worker sequence:** Authenticate → validate schema → establish tenant context → verify expected state → execute with idempotency key → update lease → enqueue verification.

**Phase 6 exit gate**
* Repeated delivery produces one logical side effect.
* Concurrent workers cannot both execute the same Op.
* Ambiguous external result triggers reconciliation.

---

## 12. Phase 7 — Secure adapter execution
Terraform execution must use versioned recipes, tenant-specific state, encrypted state, and verify resources independently. Code generation and build workers must treat LLM output as untrusted and run in isolated ephemeral environments.

**Phase 7 exit gate**
* Terraform applies the approved plan only.
* Generated code cannot merge/deploy without gates.
* Advertising spend caps are enforced outside the LLM.

---

## 13. Phase 8 — Independent verification and rollback
Verification must be performed by a separate component where feasible. Every Op must declare a truthful rollback class (`REVERSIBLE`, `COMPENSATABLE`, `RECOVERABLE`, `IRREVERSIBLE`).

**Phase 8 exit gate**
* Executor cannot mark its own Op DONE.
* Failed verification produces `VERIFICATION_FAILED`.
* Irreversible consequences are visible before approval.

---

## 14. Phase 9 — Audit ledger and evidence integrity
Audit events must be append-only, chained via hashes, monotonically ordered, and periodically anchored externally. Logs must not contain secrets or sensitive customer payloads.

**Phase 9 exit gate**
* Ledger verification passes from genesis.
* Tampering and truncation simulations are detected.
* Security team can reconstruct one complete Op from evidence.

---

## 15. Phase 10 — Data protection and disaster recovery
Minimize data, enforce encryption at rest and in transit.
**DR Process:** Use private ephemeral directories, try/finally cleanups, scheduled sweepers, and test encrypted restores.

**Phase 10 exit gate**
* Forced DR exception leaves no plaintext artifact.
* Restored database retains RLS.

---

## 16. Phase 11 — CI/CD and software supply-chain controls
Pull requests must pass SAST, dependency scans, IaC scans, secret scans, etc. Production deploy must use immutable artifact digests.

**Phase 11 exit gate**
* Agent cannot merge its own security-sensitive change.
* SBOM is generated and retained.
* Emergency bypass produces immediate audit and alert.

---

## 17. Phase 12 — Detection, incident response and kill switches
Configure required alerts (e.g., cross-tenant denial, RLS policy changes, secret anomalies). Provide independently operable kill switches (global, tenant-level, credential-level).

**Phase 12 exit gate**
* Global and tenant lockout drills pass.
* Credential revocation propagates.

---

## 18. End-to-end golden-path test
Execute a full lifecycle test in a non-production tenant environment demonstrating full tenant isolation and end-to-end completion of an Op without leaks, ambient credentials, or cross-tenant access.

---

## 19. Mandatory adversarial and failure tests
Inject failures (e.g., forge signatures, replay events, kill workers, lock state, alter audit events) and ensure the system behaves securely.

---

## 20. Master audit checklist
Detailed checklist for Governance, Assets, Identity, Isolation, Secrets, Webhooks, Queues, Builds, Infrastructure, DR, Verification, Logging, Detection, and Release readiness. Every PASS must reference evidence.

---

## 21. Required audit evidence bundle
Each release should produce an immutable evidence bundle (manifest, SBOM, SAST results, audit-chain-verification, etc.).

---

## 22. Final production decision rule
Production release is permitted ONLY when:
* All 7 critical findings = CLOSED
* Critical/High vulnerabilities = 0
* All isolation, E2E, duplicate-execution, approval, audit, restore, and kill-switch tests = PASS
* Rollback plan = TESTED
* Security/Operations/Release reviews = APPROVED

**The engineering agent must output:**
`RELEASE_READY`
