# RELEASE RUNBOOK — Deployment & Verification Protocol

This runbook outlines the step-by-step deployment, smoke-testing, verification, and rollback procedures for an Agency-OS release.

---

## 1. Pre-Flight Checklist

Before deploying any version to production, verify that:
1. All critical and high security findings are remediated and resolved in Git.
2. The full test suite passes: `cd control-plane && pytest`.
3. The frontend passes linting: `npm run lint`.
4. Terraform configuration templates are validated: `terraform validate` inside target recipes.

---

## 2. Deployment Sequence

The release must be executed in the following order:

### Step 1: Database Migration (Alembic)
Apply schema migrations to the production Postgres database before updating application code. This ensures schema compatibility for running instances.
- Run command:
  ```bash
  cd control-plane
  alembic upgrade head
  ```
- *Check*: Query `alembic_version` table to verify current migration revision matches the latest version in the repo.

### Step 2: Backend Cloud Run Service Deployment
Build and deploy the FastAPI container to Google Cloud Run.
- Command (using gcloud SDK):
  ```bash
  gcloud builds submit --config=cloudbuild.yaml
  gcloud run deploy aos-control-plane --image=gcr.io/<PROJECT>/aos-api:latest --region=asia-south1
  ```

### Step 3: Next.js Frontend Deployment
Compile the Next.js static export and deploy to Static Web Hosting or Cloud CDN bucket.
- Command:
  ```bash
  cd control-plane/web
  npm run build
  gcloud storage rsync out/ gs://aos-console-static-bucket/ --recursive
  ```

### Step 4: Outbox Worker Drain & Task Queues
Ensure Cloud Tasks or subsecond triggers are configured and running to process outbox items.
- *Verification check*: Poll the backend `/readyz` or `/metrics` to ensure outbox tasks are executing.

---

## 3. Post-Deployment Smoke Tests

Execute the following verification commands to ensure application health:

1. **System Health Check**:
   ```bash
   curl -I https://api.agencyos.domain/healthz
   ```
   *Expected Response*: `HTTP/1.1 200 OK`

2. **Tenant Auth Isolation Verification**:
   Try accessing a tenant route without the `X-Tenant-ID` header:
   ```bash
   curl -I https://api.agencyos.domain/ops
   ```
   *Expected Response*: `HTTP/1.1 401 Unauthorized` (missing tenant ID header).

3. **Cryptographic Audit Verify Sweep**:
   Check if the ledger has not been tampered with:
   ```bash
   curl https://api.agencyos.domain/audit/verify
   ```
   *Expected Response*: `{"status":"ok","tamper_evident_status":"clean"}`

---

## 4. Rollback Protocols

In the event of a deployment failure (smoke test failure, error spikes in Prometheus, or a security incident):

### 4.1 Rollback Application Services
1. **Cloud Run Backend**:
   Revert Cloud Run traffic immediately to the previous stable revision:
   ```bash
   gcloud run services update-traffic aos-control-plane --to-revisions=aos-control-plane-<PREVIOUS_REV>=100
   ```
2. **Frontend Console Static Assets**:
   Restore the static bucket assets from the backup directory:
   ```bash
   gcloud storage rsync gs://aos-console-backup-vX/ gs://aos-console-static-bucket/ --recursive
   ```

### 4.2 Database Rollback (Alembic Downgrade)
If the deployment failed due to migration schema errors, downgrade the migration:
- Run command:
  ```bash
  cd control-plane
  alembic downgrade -1
  ```
  *(Repeat or specify target migration ID as needed to reach the target rollback version).*

---

## 5. Disaster Recovery (DR) Drill Guideline

To execute an automated database recovery drill as required by system policy:
1. Propose a DR restore drill operation:
   `POST /actions` with body `{"intent": "verify DR restore", "tenant_id": "<ID>", "brand_id": "<ID>"}`.
2. Approve the proposed operation row in the operations dashboard.
3. The system will restore the database dump into a temporary SQLite file in `/tmp/scratch_restore_{op.id}.db` and verify the cryptographic signature continuity.
4. On success, the task will return status `DONE` and delete the temporary database file.
