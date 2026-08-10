# REMEDIATION PLAN — Production Hardening Diffs

This plan contains the precise code-level fixes required to resolve the outstanding security findings prior to release.

---

## 1. Webhook Signature Verification Bypass (CRITICAL)

### Issue
In [main.py](../../control-plane/app/main.py#L2163-L2170), if the webhook secret key version cannot be resolved from Secret Manager (throwing a `ValueError`), the route falls back to using the raw `conn.credential` reference string (e.g. `'t1-b1-shopify-secret'`) as the HMAC signature validation key, allowing an attacker to bypass authentication.

### Remediation Diffs
Change the catch block to fail closed (returning an HTTP 401/400 error instead of fallback verification):

```diff
-        # Resolve actual secret key from Secret Manager (Falling back to credential if not in Secret Manager)
+        # Resolve actual secret key from Secret Manager
         from app.services.secrets import SecretManagerClient
         try:
             secrets_client = SecretManagerClient(project_id=gcp_project)
             secret_key = await secrets_client.read_secret(conn.credential)
         except ValueError as e:
-            logger.warning(f"Secret not found in registry: {e}. Falling back to literal ref.")
-            secret_key = conn.credential
+            logger.error(f"Failed to resolve webhook credential secret from Secret Manager: {e}")
+            raise HTTPException(status_code=401, detail="Webhook credential secret is unconfigured or invalid")
         except Exception as e:
             logger.error(f"Failed to read webhook secret from Secret Manager: {e}")
             raise HTTPException(500, "Internal secret resolution error")
```

---

## 2. Secrets Project Isolation Drift (HIGH)

### Issue
When connections are created or managed, `SecretManagerClient()` is initialized without passing a project ID, causing secrets to be saved in the global control plane project (`aos-control-plane`) instead of the tenant's dedicated GCP project scope.

### Remediation Diffs

#### A. Fixes in [manage.py](../../control-plane/app/adapters/manage.py):
In Connection write, delete, verify, and revoke operations, query the tenant's `gcp_project` dynamically and pass it to the `SecretManagerClient` constructor:

```diff
+            # Retrieve tenant to determine dedicated GCP project ID for secret isolation
+            from app.models import Tenant
+            stmt_tenant = select(Tenant).where(Tenant.id == op.tenant_id)
+            res_tenant = await session.execute(stmt_tenant)
+            tenant = res_tenant.scalar_one_or_none()
+            gcp_project = tenant.gcp_project if tenant else None
+
             # Write token to Secret Manager and get reference
             secret_id = f"{op.tenant_id}-{op.brand_id}-{provider}-secret"
-            secrets_client = SecretManagerClient()
+            secrets_client = SecretManagerClient(project_id=gcp_project)
             credential_ref = await secrets_client.write_secret(secret_id, raw_token)
```

```diff
             # Delete from Secret Manager first
             stmt = select(Connection).where(
                 Connection.tenant_id == op.tenant_id,
                 Connection.brand_id == op.brand_id,
                 Connection.provider == provider
             )
             res = await session.execute(stmt)
             conn = res.scalar_one_or_none()
             if conn and conn.credential:
-                secrets_client = SecretManagerClient()
+                from app.models import Tenant
+                stmt_tenant = select(Tenant).where(Tenant.id == conn.tenant_id)
+                res_tenant = await session.execute(stmt_tenant)
+                tenant = res_tenant.scalar_one_or_none()
+                gcp_project = tenant.gcp_project if tenant else None
+
+                secrets_client = SecretManagerClient(project_id=gcp_project)
                 await secrets_client.delete_secret(conn.credential)
```

```diff
             try:
-                secrets_client = SecretManagerClient()
+                from app.models import Tenant
+                stmt_tenant = select(Tenant).where(Tenant.id == conn.tenant_id)
+                res_tenant = await session.execute(stmt_tenant)
+                tenant = res_tenant.scalar_one_or_none()
+                gcp_project = tenant.gcp_project if tenant else None
+
+                secrets_client = SecretManagerClient(project_id=gcp_project)
                 token = await secrets_client.read_secret(conn.credential)
```

```diff
             if conn.credential:
                 try:
-                    secrets_client = SecretManagerClient()
+                    from app.models import Tenant
+                    stmt_tenant = select(Tenant).where(Tenant.id == conn.tenant_id)
+                    res_tenant = await session.execute(stmt_tenant)
+                    tenant = res_tenant.scalar_one_or_none()
+                    gcp_project = tenant.gcp_project if tenant else None
+
+                    secrets_client = SecretManagerClient(project_id=gcp_project)
                     await secrets_client.delete_secret(conn.credential)
                 except Exception as e:
```

#### B. Fixes in [grow.py](../../control-plane/app/adapters/grow.py):
Update Connection registration and disconnect routes to use tenant-scoped GCP project:

```diff
+            # Retrieve tenant to determine dedicated GCP project ID for secret isolation
+            from app.models import Tenant
+            stmt_tenant = select(Tenant).where(Tenant.id == op.tenant_id)
+            res_tenant = await session.execute(stmt_tenant)
+            tenant = res_tenant.scalar_one_or_none()
+            gcp_project = tenant.gcp_project if tenant else None
+
             secret_id = f"{op.tenant_id}-{op.brand_id}-{provider}-secret"
-            secrets_client = SecretManagerClient()
+            secrets_client = SecretManagerClient(project_id=gcp_project)
             credential_ref = await secrets_client.write_secret(secret_id, raw_token)
```

```diff
             # Delete from Secret Manager first
             stmt = select(Connection).where(
                 Connection.tenant_id == op.tenant_id,
                 Connection.brand_id == op.brand_id,
                 Connection.provider == provider
             )
             res = await session.execute(stmt)
             conn = res.scalar_one_or_none()
             if conn and conn.credential:
-                secrets_client = SecretManagerClient()
+                from app.models import Tenant
+                stmt_tenant = select(Tenant).where(Tenant.id == conn.tenant_id)
+                res_tenant = await session.execute(stmt_tenant)
+                tenant = res_tenant.scalar_one_or_none()
+                gcp_project = tenant.gcp_project if tenant else None
+
+                secrets_client = SecretManagerClient(project_id=gcp_project)
                 await secrets_client.delete_secret(conn.credential)
```

---

## 3. SQLite Residual Data Risk in `/tmp` (LOW)

### Issue
If a backup restore verification drill Op fails or is cancelled *before* verification (`verify`) executes, the temporary file created in `execute` (`/tmp/scratch_restore_{op.id}.db`) remains in disk storage.

### Remediation Strategy
Introduce a daily cron cleaner task inside `control-plane/app/tasks/clean_scratch.py` that walks `/tmp` and deletes any file matching the pattern `scratch_restore_*.db` that is older than 24 hours.
