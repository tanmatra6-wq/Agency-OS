import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models import Tenant, Brand, Connection, BrandProperty, OpRow
from app.kernel import loop
from app.kernel.optypes import OpSpec, Severity, Reversibility, Money

@pytest.mark.asyncio
async def test_storefront_pricing_flow(client, db_engine):
    from app.services.shopify import ShopifyStorefront
    ShopifyStorefront.reset_mock()
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    # 1. Bootstrap Tenant, Brand and active Shopify Connection
    async with async_session() as s:
        tenant = Tenant(name="Pricing Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Grow")
        s.add(brand)
        await s.commit()
        brand_id = brand.id
        
        # Seed active Shopify connection
        shopify_conn = Connection(
            tenant_id=tenant_id,
            brand_id=brand_id,
            provider="shopify",
            status="active",
            config={"shop_url": "mock-shop.myshopify.com"}
        )
        s.add(shopify_conn)
        await s.commit()

    H = {"X-Tenant-ID": tenant_id}

    # 2. Submit pricing audit intent
    resp = await client.post("/intents", headers=H, json={
        "domain": "grow",
        "brand_id": brand_id,
        "text": "run storefront margin pricing audit"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "competitor price signals" in data["cards"][0]["preview"]

    # 3. Approve the audit Op
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    # Background task drains automatically.
    # 4. Verify the audit findings in DB have the pricing report and recommendations
    async with async_session() as s:
        stmt = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "pricing_audit"
        )
        res = await s.execute(stmt)
        prop = res.scalar_one()
        assert prop.findings["pricing_report"] is not None
        assert "AOS-T-SHIRT" in prop.findings["pricing_report"]

        # 5. Verify that a grow.storefront.update_price Op was automatically proposed
        stmt_ops = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.domain == "grow",
            OpRow.action == "grow.storefront.update_price",
            OpRow.parent_op_id == op_id
        )
        res_ops = await s.execute(stmt_ops)
        proposed_ops = res_ops.scalars().all()
        assert len(proposed_ops) == 1
        
        update_op = proposed_ops[0]
        assert update_op.state == "AWAITING_APPROVAL"
        assert update_op.params["sku"] == "AOS-T-SHIRT-M"
        assert update_op.params["new_price"] == 899
        assert update_op.params["current_price"] == 799
        assert "Align with market pricing" in update_op.params["reason"]

        update_op_id = update_op.id

    # 6. Preview and Approve the proposed price update
    resp_dec_update = await client.post(f"/ops/{update_op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec_update.status_code == 200

    # Verify after drain that it transitioned to DONE
    async with async_session() as s:
        db_op = await s.get(OpRow, update_op_id)
        assert db_op.state == "DONE"
