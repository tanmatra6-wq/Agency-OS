import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models import Tenant, Brand, BrandProperty, OpRow, Lead, ConsentBasis
from app.kernel.optypes import OpState

@pytest.mark.asyncio
async def test_privacy_compliance_audit_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        tenant = Tenant(name="Privacy Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Privacy")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

        # Add active ConsentBasis
        s.add(ConsentBasis(tenant_id=tenant_id, category="pii_upload", action_or_vendor="marketing", status="granted", granted_by="user@test.com"))
        await s.commit()

    H = {"X-Tenant-ID": tenant_id}

    # 1. Test Success Path
    resp = await client.post("/intents", headers=H, json={
        "domain": "manage",
        "brand_id": brand_id,
        "text": "run privacy compliance audit"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "privacy guidelines" in data["cards"][0]["preview"]

    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    async with async_session() as s:
        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "privacy_audit"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert prop.status == "compliant"
        assert prop.findings["passed"] is True
        assert prop.findings["score_percent"] == 95

    # 2. Test Failure Path
    async with async_session() as s:
        fail_op = OpRow(
            id="op_fail_privacy",
            tenant_id=tenant_id,
            brand_id=brand_id,
            domain="manage",
            action="manage.compliance.privacy_audit",
            params={"fail_privacy": True},
            state="AWAITING_APPROVAL",
            impact=1,
            reversibility="REVERSIBLE",
            idem_key="idem_fail_privacy"
        )
        s.add(fail_op)
        await s.commit()

    resp_dec_fail = await client.post("/ops/op_fail_privacy/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec_fail.status_code == 200

    async with async_session() as s:
        # Check op failed
        op_res = await s.get(OpRow, "op_fail_privacy")
        assert op_res.state == "ROLLED_BACK"

        # Check BrandProperty contains failure
        stmt_prop_fail = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "privacy_audit"
        )
        res_prop_fail = await s.execute(stmt_prop_fail)
        prop_fail = res_prop_fail.scalar_one()
        assert prop_fail.status == "violating"
        assert prop_fail.findings["passed"] is False
        assert "PII data uploaded without active ConsentBasis" in prop_fail.findings["violations"]


@pytest.mark.asyncio
async def test_financial_accounts_audit_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        tenant = Tenant(name="Finance Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Finance")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

    H = {"X-Tenant-ID": tenant_id}

    # 1. Test Success Path
    resp = await client.post("/intents", headers=H, json={
        "domain": "manage",
        "brand_id": brand_id,
        "text": "run financial accounts audit"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "financial ledger details" in data["cards"][0]["preview"]

    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    async with async_session() as s:
        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "finance_audit"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert prop.status == "completed"
        assert prop.findings["passed"] is True
        assert prop.findings["score_percent"] == 100

    # 2. Test Failure Path
    async with async_session() as s:
        fail_op = OpRow(
            id="op_fail_finance",
            tenant_id=tenant_id,
            brand_id=brand_id,
            domain="manage",
            action="manage.billing.accounts_audit",
            params={"fail_finance": True},
            state="AWAITING_APPROVAL",
            impact=1,
            reversibility="REVERSIBLE",
            idem_key="idem_fail_finance"
        )
        s.add(fail_op)
        await s.commit()

    resp_dec_fail = await client.post("/ops/op_fail_finance/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec_fail.status_code == 200

    async with async_session() as s:
        op_res = await s.get(OpRow, "op_fail_finance")
        assert op_res.state == "ROLLED_BACK"

        stmt_prop_fail = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "finance_audit"
        )
        res_prop_fail = await s.execute(stmt_prop_fail)
        prop_fail = res_prop_fail.scalar_one()
        assert prop_fail.status == "flagged"
        assert prop_fail.findings["passed"] is False
        assert "Unreconciled outbound ad spend transaction" in prop_fail.findings["discrepancies"]


@pytest.mark.asyncio
async def test_crm_sales_pipeline_audit_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        tenant = Tenant(name="CRM Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra CRM")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

        # Seed CRM Lead
        s.add(Lead(tenant_id=tenant_id, brand_id=brand_id, lead_id="deal-1", email_hashed="hash-1", status="mql", deal_value_minor=50000))
        await s.commit()

    H = {"X-Tenant-ID": tenant_id}

    # 1. Test Success Path
    resp = await client.post("/intents", headers=H, json={
        "domain": "grow",
        "brand_id": brand_id,
        "text": "run sales CRM pipeline audit"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "sales pipeline deals" in data["cards"][0]["preview"]

    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    async with async_session() as s:
        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "crm_pipeline_audit"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert prop.status == "healthy"
        assert prop.findings["passed"] is True
        assert prop.findings["score_percent"] == 95

    # 2. Test Failure Path with Proposed Child Adjustments
    async with async_session() as s:
        fail_op = OpRow(
            id="op_fail_sales",
            tenant_id=tenant_id,
            brand_id=brand_id,
            domain="grow",
            action="grow.crm.pipeline_audit",
            params={"fail_sales": True},
            state="AWAITING_APPROVAL",
            impact=1,
            reversibility="REVERSIBLE",
            idem_key="idem_fail_sales"
        )
        s.add(fail_op)
        await s.commit()

    resp_dec_fail = await client.post("/ops/op_fail_sales/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec_fail.status_code == 200

    async with async_session() as s:
        op_res = await s.get(OpRow, "op_fail_sales")
        assert op_res.state == "ROLLED_BACK"

        stmt_prop_fail = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "crm_pipeline_audit"
        )
        res_prop_fail = await s.execute(stmt_prop_fail)
        prop_fail = res_prop_fail.scalar_one()
        assert prop_fail.status == "bottlenecked"
        assert prop_fail.findings["passed"] is False
        assert "High drop-off between MQL and SQL stages" in prop_fail.findings["bottlenecks"]

        # Check child Op bid adjustment has been proposed
        stmt_child = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.action == "grow.bid.adjust",
            OpRow.parent_op_id == "op_fail_sales"
        )
        res_child = await s.execute(stmt_child)
        child_ops = res_child.scalars().all()
        assert len(child_ops) == 1
        assert child_ops[0].state == "AWAITING_APPROVAL"
        assert child_ops[0].params["campaign_id"] == "camp-default"
        assert child_ops[0].params["new_bid_minor"] == 7500
