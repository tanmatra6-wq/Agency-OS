import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models import Tenant, Brand, BrandProperty, OpRow
from app.kernel.optypes import OpState

@pytest.mark.asyncio
async def test_presence_organic_social_marketing_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        tenant = Tenant(name="Presence Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Presence")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

    H = {"X-Tenant-ID": tenant_id}

    # 1. Test Social Post Drafting Flow
    resp = await client.post("/intents", headers=H, json={
        "domain": "presence",
        "brand_id": brand_id,
        "text": "curate instagram and linkedin updates for Ableys"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "Social Media Draft Curation" in data["cards"][0]["preview"]
    assert "Instagram Carousel Draft" in data["cards"][0]["preview"]
    assert "LinkedIn Thought-Leadership Update" in data["cards"][0]["preview"]

    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    async with async_session() as s:
        op_res = await s.get(OpRow, op_id)
        assert op_res.state == "DONE"

        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "social_content_drafts"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert prop.status == "drafted"
        assert "instagram_carousel" in prop.findings
        assert "experience organic hydration" in prop.findings["instagram_carousel"]["slide_1"].lower()
        assert "conversion funnels" in prop.findings["linkedin_post"].lower()


@pytest.mark.asyncio
async def test_presence_email_funnel_audit_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        tenant = Tenant(name="Presence Email Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Email Presence")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

    H = {"X-Tenant-ID": tenant_id}

    # 1. Test Success Path
    resp = await client.post("/intents", headers=H, json={
        "domain": "presence",
        "brand_id": brand_id,
        "text": "run email marketing funnel review"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "Email Funnel Audit Report" in data["cards"][0]["preview"]
    assert "CTR Index" in data["cards"][0]["preview"]

    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    async with async_session() as s:
        op_res = await s.get(OpRow, op_id)
        assert op_res.state == "DONE"

        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "email_marketing_audit"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert prop.status == "completed"
        assert prop.findings["passed"] is True
        assert prop.findings["spam_risk_score"] == 12

    # 2. Test Failure Path (Spam/Capitalization issues trigger failed status)
    resp_fail = await client.post("/intents", headers=H, json={
        "domain": "presence",
        "brand_id": brand_id,
        "text": "run email campaign review fail_email"
    })
    assert resp_fail.status_code == 200
    data_fail = resp_fail.json()
    op_fail_id = data_fail["cards"][0]["op_id"]

    resp_dec_fail = await client.post(f"/ops/{op_fail_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec_fail.status_code == 200

    async with async_session() as s:
        op_res_fail = await s.get(OpRow, op_fail_id)
        assert op_res_fail.state == "ROLLED_BACK"

        stmt_prop_fail = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "email_marketing_audit",
            BrandProperty.status == "failed"
        )
        res_prop_fail = await s.execute(stmt_prop_fail)
        prop_fail = res_prop_fail.scalar_one()
        assert prop_fail.findings["passed"] is False
        assert prop_fail.findings["spam_risk_score"] == 75
        assert "capitalization characters in subject lines" in prop_fail.findings["redesign_suggestions"][0]
