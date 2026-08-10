import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models import Tenant, Brand, Connection, Campaign, BrandProperty, OpRow
from app.services.marketing import MockMarketingClient
from app.kernel.optypes import OpState

@pytest.fixture(autouse=True)
def clear_mock_marketing():
    MockMarketingClient.clear()
    yield
    MockMarketingClient.clear()

@pytest.mark.asyncio
async def test_ppc_campaign_audit_flow(client, db_engine):
    from app.services.secrets import SecretManagerClient
    sec = SecretManagerClient()
    google_ref = await sec.write_secret("mock-google-token", "google-token-value")
    meta_ref = await sec.write_secret("mock-meta-token", "meta-token-value")

    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    # 1. Bootstrap Tenant, Brand, Google Ads, Meta Ads Connections and Campaigns
    async with async_session() as s:
        tenant = Tenant(name="PPC Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra PPC")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

        # Google Ads & Meta Ads connections
        s.add(Connection(tenant_id=tenant_id, brand_id=brand_id, provider="google-ads", status="active", credential=google_ref, config={}))
        s.add(Connection(tenant_id=tenant_id, brand_id=brand_id, provider="meta-ads", status="active", credential=meta_ref, config={}))

        # Campaigns in local DB
        s.add(Campaign(id="camp-google", tenant_id=tenant_id, brand_id=brand_id, name="Google Search Ads", platform="google-ads", status="active"))
        s.add(Campaign(id="camp-google-fail", tenant_id=tenant_id, brand_id=brand_id, name="Google Display Ads Fail", platform="google-ads", status="active"))
        s.add(Campaign(id="camp-meta", tenant_id=tenant_id, brand_id=brand_id, name="Meta Retargeting", platform="meta-ads", status="active"))
        await s.commit()

    # Seed campaigns in mock external systems
    g_client = MockMarketingClient(provider="google-ads")
    await g_client.create_campaign("camp-google", "Google Search Ads", 1500000, 15000)
    await g_client.create_campaign("camp-google-fail", "Google Display Ads Fail", 1000000, 10000)

    m_client = MockMarketingClient(provider="meta-ads")
    await m_client.create_campaign("camp-meta", "Meta Retargeting", 3000000, 20000)

    H = {"X-Tenant-ID": tenant_id}

    # 2. Submit PPC audit intent
    resp = await client.post("/intents", headers=H, json={
        "domain": "grow",
        "brand_id": brand_id,
        "text": "run PPC campaign performance audit"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "PPC Strategist analysis" in data["cards"][0]["preview"]

    # 3. Approve the audit Op
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    # 4. Verify audit findings & proposed child Ops
    async with async_session() as s:
        # Check findings stored in BrandProperty
        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "ppc_audit"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert len(prop.findings["pauses"]) == 1
        assert len(prop.findings["recommendations"]) == 2

        # Check that the pause Op is proposed for camp-google-fail
        stmt_pause = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.action == "grow.campaign.pause",
            OpRow.parent_op_id == op_id
        )
        res_pause = await s.execute(stmt_pause)
        pause_ops = res_pause.scalars().all()
        assert len(pause_ops) == 1
        assert pause_ops[0].state == "AWAITING_APPROVAL"
        assert pause_ops[0].params["campaign_id"] == "camp-google-fail"
        pause_op_id = pause_ops[0].id

        # Check that the bid adjustment is proposed for camp-google
        stmt_bid = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.action == "grow.bid.adjust",
            OpRow.parent_op_id == op_id
        )
        res_bid = await s.execute(stmt_bid)
        bid_ops = res_bid.scalars().all()
        assert len(bid_ops) == 1
        assert bid_ops[0].state == "AWAITING_APPROVAL"
        assert bid_ops[0].params["campaign_id"] == "camp-google"
        assert bid_ops[0].params["new_bid_minor"] == 18000

        # Check that the budget reallocation is proposed
        stmt_reall = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.action == "grow.budget.reallocate",
            OpRow.parent_op_id == op_id
        )
        res_reall = await s.execute(stmt_reall)
        reall_ops = res_reall.scalars().all()
        assert len(reall_ops) == 1
        assert reall_ops[0].state == "AWAITING_APPROVAL"
        assert reall_ops[0].params["source_campaign_id"] == "camp-meta"
        assert reall_ops[0].params["target_campaign_id"] == "camp-google"

    # 5. Approve and run the campaign pause Op
    resp_dec_pause = await client.post(f"/ops/{pause_op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec_pause.status_code == 200

    # 6. Verify that camp-google-fail is now paused in both local DB and mock client
    async with async_session() as s:
        db_camp = await s.get(Campaign, "camp-google-fail")
        assert db_camp.status == "paused"

    ext_camp = await g_client.get_campaign("camp-google-fail")
    assert ext_camp["status"] == "PAUSED"
