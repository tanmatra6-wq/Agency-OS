# GITHUB ISSUE DRAFTS — Blockers Registry

These issue drafts are structured to be copied and pasted directly into the repository's GitHub issues tracker.

---

## Issue 1: Webhook Signature Verification Bypass on Ingress

- **Title**: [CRITICAL] Webhook Signature Verification Bypass via Insecure Secret Fallback
- **Category**: Security (Authentication Bypass)
- **Severity**: Critical

### Description
In `control-plane/app/main.py` under the `/webhooks/plugins/{provider}` POST route handler, if Secret Manager fails to resolve a webhook's secret key (raising a `ValueError` e.g., because a secret version is missing or unconfigured in dev/staging environments), the route catches the error, logs a warning, and falls back to using the literal connection credential reference name (`conn.credential`) as the HMAC signature key.

This enables a signature bypass: an attacker who knows or can guess the brand context and provider schema can construct a valid signature using the public credential identifier string as the HMAC key, bypass security checks, and execute arbitrary webhook-triggered actions (such as order sync operations) on behalf of any tenant.

### Code Location
- File: [control-plane/app/main.py:2163-2173](../../control-plane/app/main.py#L2163-L2173)

### How to Reproduce
1. Configure a shopify plugin webhook connection in database with credential reference `'t1-b1-shopify-secret'`.
2. Do not configure this secret version in Google Cloud Secret Manager (or mock client registry).
3. Send a POST request to `/webhooks/plugins/shopify` with header `X-Shopify-Hmac-Sha256` signed using `'t1-b1-shopify-secret'` as the HMAC-SHA256 key.
4. Note that the request is successfully verified and processed (status `accepted`) instead of rejected.

### Proposed Fix
The webhook handler must fail closed on secret resolution failure:
```python
        # Resolve actual secret key from Secret Manager
        from app.services.secrets import SecretManagerClient
        try:
            secrets_client = SecretManagerClient(project_id=gcp_project)
            secret_key = await secrets_client.read_secret(conn.credential)
        except ValueError as e:
            logger.error(f"Failed to resolve webhook credential secret from Secret Manager: {e}")
            raise HTTPException(status_code=401, detail="Webhook credential secret is unconfigured or invalid")
```

---

## Issue 2: Secrets Isolation Project Mismatch on Connect/Disconnect

- **Title**: [HIGH] Architectural Drift in Secret Isolation — Project ID Missing in Connection Actions
- **Category**: Security (Tenant Isolation)
- **Severity**: High

### Description
In both `manage.py` and `grow.py` adapters, Connection write and delete operations initialize `SecretManagerClient()` without passing the dedicated `project_id` associated with the tenant. This causes connection tokens to be written to or deleted from the global control plane project (`aos-control-plane`) instead of the tenant's dedicated GCP project, violating the core tenant isolation boundary (§2.2).

### Code Locations
- [control-plane/app/adapters/manage.py:191](../../control-plane/app/adapters/manage.py#L191)
- [control-plane/app/adapters/manage.py:239](../../control-plane/app/adapters/manage.py#L239)
- [control-plane/app/adapters/manage.py:358](../../control-plane/app/adapters/manage.py#L358)
- [control-plane/app/adapters/manage.py:446](../../control-plane/app/adapters/manage.py#L446)
- [control-plane/app/adapters/grow.py:635](../../control-plane/app/adapters/grow.py#L635)
- [control-plane/app/adapters/grow.py:684](../../control-plane/app/adapters/grow.py#L684)

### Proposed Fix
Query the tenant's dedicated `gcp_project` dynamically and pass it when instantiating the secrets client. For example, during Connection write:
```python
            # Retrieve tenant to determine dedicated GCP project ID for secret isolation
            from app.models import Tenant
            stmt_tenant = select(Tenant).where(Tenant.id == op.tenant_id)
            res_tenant = await session.execute(stmt_tenant)
            tenant = res_tenant.scalar_one_or_none()
            gcp_project = tenant.gcp_project if tenant else None

            # Write token to Secret Manager and get reference
            secret_id = f"{op.tenant_id}-{op.brand_id}-{provider}-secret"
            secrets_client = SecretManagerClient(project_id=gcp_project)
            credential_ref = await secrets_client.write_secret(secret_id, raw_token)
```
Apply similar updates to disconnect, verify, and revoke operations in both adapters.
