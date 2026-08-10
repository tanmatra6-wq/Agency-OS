import base64
import hashlib
import hmac
import pytest
from sqlalchemy import select

from app.models import Connection, OpRow, TrustSnapshot


def _generate_shopify_signature(payload_bytes: bytes, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.fixture(autouse=True)
async def setup_connection_and_trust(db_engine):
    from app.services.secrets import SecretManagerClient
    from app.models import Tenant

    from sqlalchemy.ext.asyncio import async_sessionmaker
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        async with s.begin():
            tenant = Tenant(id="tenant-webhook-test", name="Webhook Test Tenant", gcp_project="tenant-webhook-gcp")
            s.add(tenant)

    secrets_client = SecretManagerClient(tenant_id="tenant-webhook-test", project_id="tenant-webhook-gcp")
    # Write connection credential key to Secret Manager mock registry
    credential_ref = await secrets_client.write_secret("shopify-secret-key-123", "shopify-secret-key-123")

    async with async_session() as s:
        async with s.begin():
            # Seed Shopify connection
            conn = Connection(
                tenant_id="tenant-webhook-test",
                brand_id="brand-shopify-test",
                provider="shopify",
                credential=credential_ref,
                config={"shop_url": "test-store.myshopify.com"}
            )
            s.add(conn)

            # Seed Trust Snapshot (Tier 1 - Supervised)
            snap = TrustSnapshot(
                tenant_id="tenant-webhook-test",
                brand_id="brand-shopify-test",
                domain="manage",
                score=75.0,
                tier=1
            )
            s.add(snap)


@pytest.mark.asyncio
async def test_shopify_webhook_proposes_op_successfully(client, db_engine):
    from app.services.secrets import SecretManagerClient
    await SecretManagerClient(tenant_id="tenant-webhook-test", project_id="tenant-webhook-gcp").write_secret("shopify-secret-key-123", "shopify-secret-key-123")

    payload = b'{"id": 998877, "total_price": "149.99", "created_at": "2026-06-15T05:00:00Z"}'
    signature = _generate_shopify_signature(payload, "shopify-secret-key-123")

    headers = {
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Shop-Domain": "test-store.myshopify.com",
        "X-Shopify-Topic": "orders/create",
        "Content-Type": "application/json"
    }

    response = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "accepted"
    assert len(data["proposed_ops"]) == 1

    op_id = data["proposed_ops"][0]

    # Verify the Op exists in the database with the correct tenant context
    from sqlalchemy.ext.asyncio import async_sessionmaker
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        # We query as admin/worker to bypass RLS since we don't have header injection in the direct DB check
        stmt = select(OpRow).where(OpRow.id == op_id)
        res = await s.execute(stmt)
        row = res.scalar_one_or_none()

        assert row is not None
        assert row.tenant_id == "tenant-webhook-test"
        assert row.brand_id == "brand-shopify-test"
        assert row.domain == "manage"
        assert row.action == "manage.shopify.sync_order"
        assert row.params["order_id"] == "998877"
        assert row.params["amount_minor"] == 14999
        # Tier 1 supervised -> goes to AWAITING_APPROVAL
        assert row.state == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_shopify_webhook_bad_signature_rejected(client):
    from app.services.secrets import SecretManagerClient
    await SecretManagerClient(tenant_id="tenant-webhook-test", project_id="tenant-webhook-gcp").write_secret("shopify-secret-key-123", "shopify-secret-key-123")

    payload = b'{"id": 998877, "total_price": "149.99"}'
    headers = {
        "X-Shopify-Hmac-Sha256": "bad-signature-value-here",
        "X-Shopify-Shop-Domain": "test-store.myshopify.com",
        "X-Shopify-Topic": "orders/create",
        "Content-Type": "application/json"
    }

    response = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )

    assert response.status_code == 401
    assert "signature mismatch" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_shopify_webhook_unknown_brand_rejected(client):
    payload = b'{"id": 998877, "total_price": "149.99"}'
    signature = _generate_shopify_signature(payload, "shopify-secret-key-123")
    headers = {
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Shop-Domain": "unknown-store.myshopify.com",
        "X-Shopify-Topic": "orders/create",
        "Content-Type": "application/json"
    }

    response = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )

    assert response.status_code == 404
    assert "unknown brand connection" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_shopify_webhook_resolves_secret_from_secret_manager(client, db_engine):
    from app.services.secrets import SecretManagerClient
    secrets_client = SecretManagerClient(tenant_id="tenant-webhook-test", project_id="tenant-webhook-gcp")
    
    # 1. Write the secret token value to Secret Manager mock registry
    secret_id = "tenant-webhook-test-brand-shopify-test-shopify-secret"
    credential = await secrets_client.write_secret(secret_id, "super-secret-mcp-key")

    # 2. Update the connection in the database to use this credential
    from sqlalchemy.ext.asyncio import async_sessionmaker
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        async with s.begin():
            stmt = select(Connection).where(Connection.tenant_id == "tenant-webhook-test", Connection.provider == "shopify")
            res = await s.execute(stmt)
            conn = res.scalar_one()
            conn.credential = credential
            s.add(conn)

    # 3. Generate signature using the actual secret value
    payload = b'{"id": 112233, "total_price": "99.99"}'
    signature = _generate_shopify_signature(payload, "super-secret-mcp-key")

    headers = {
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Shop-Domain": "test-store.myshopify.com",
        "X-Shopify-Topic": "orders/create",
        "Content-Type": "application/json"
    }

    # 4. Post webhook and verify it passes (which means it resolved and matched the secret!)
    response = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_shopify_webhook_uses_tenant_gcp_project_for_secrets(client, db_engine):
    from app.services.secrets import SecretManagerClient
    from app.models import Tenant
    
    from sqlalchemy.ext.asyncio import async_sessionmaker
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    dedicated_project = "tenant-dedicated-project-xyz"
    secret_id = "tenant-webhook-test-brand-shopify-test-shopify-secret"
    
    secrets_client = SecretManagerClient(tenant_id="tenant-webhook-test", project_id=dedicated_project)
    credential = await secrets_client.write_secret(secret_id, "dedicated-mcp-key-999")
    
    async with async_session() as s:
        async with s.begin():
            tenant = await s.get(Tenant, "tenant-webhook-test")
            tenant.hosting_tier = "dedicated"
            tenant.gcp_project = dedicated_project
            
            stmt_c = select(Connection).where(Connection.tenant_id == "tenant-webhook-test", Connection.provider == "shopify")
            res_c = await s.execute(stmt_c)
            conn = res_c.scalar_one()
            conn.credential = credential
            s.add(conn)

    payload = b'{"id": 224466, "total_price": "199.99"}'
    signature = _generate_shopify_signature(payload, "dedicated-mcp-key-999")

    headers = {
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Shop-Domain": "test-store.myshopify.com",
        "X-Shopify-Topic": "orders/create",
        "Content-Type": "application/json"
    }

    response = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )
    assert response.status_code == 200
    assert response.json()["status"] == "accepted"


@pytest.mark.asyncio
async def test_shopify_webhook_deduplicated(client):
    from app.services.secrets import SecretManagerClient
    await SecretManagerClient(tenant_id="tenant-webhook-test", project_id="tenant-webhook-gcp").write_secret("shopify-secret-key-123", "shopify-secret-key-123")

    payload = b'{"id": 554433, "total_price": "49.99"}'
    signature = _generate_shopify_signature(payload, "shopify-secret-key-123")

    headers = {
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Shop-Domain": "test-store.myshopify.com",
        "X-Shopify-Topic": "orders/create",
        "X-Shopify-Webhook-Id": "shopify-uniq-msg-id-888",
        "Content-Type": "application/json"
    }

    # First send -> Accepted
    resp1 = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )
    assert resp1.status_code == 200
    assert resp1.json()["status"] == "accepted"

    # Second send with same header ID -> Ignored
    resp2 = await client.post(
        "/webhooks/plugins/shopify",
        content=payload,
        headers=headers
    )
    assert resp2.status_code == 200
    assert resp2.json()["status"] == "ignored"
    assert "duplicate" in resp2.json()["detail"].lower()


@pytest.mark.asyncio
async def test_shopify_sync_order_executes_successfully(db_engine):
    from app.adapters.manage import ManageAdapter
    from app.kernel.optypes import OpSpec, Severity, Reversibility
    from app.models import Order

    adapter = ManageAdapter()
    
    op_spec = OpSpec(
        tenant_id="tenant-webhook-test",
        brand_id="brand-shopify-test",
        domain="manage",
        action="manage.shopify.sync_order",
        params={
            "order_id": "shopify-order-777",
            "amount_minor": 2999,
            "placed_at": "2026-06-18T12:00:00Z"
        },
        severity=Severity(impact=1, reversibility=Reversibility.COMPENSATABLE)
    )

    from sqlalchemy.ext.asyncio import async_sessionmaker
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        # Run execute on the adapter
        res = await adapter.execute(op_spec, "idem_sync_order_777", session=s)
        assert res.ok is True
        await s.commit()

    # Verify Order is persisted in DB
    async with async_session() as s:
        stmt = select(Order).where(Order.id == "shopify-order-777")
        db_res = await s.execute(stmt)
        order = db_res.scalar_one_or_none()
        assert order is not None
        assert order.tenant_id == "tenant-webhook-test"
        assert order.brand_id == "brand-shopify-test"
        assert order.amount_minor == 2999
