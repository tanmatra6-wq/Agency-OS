import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models import Tenant, Brand, BrandProperty, OpRow
from app.kernel import loop
from app.kernel.optypes import OpSpec, Severity, Reversibility, Money, OpState
from app.kernel.services import audit_verify

@pytest.mark.asyncio
async def test_playwright_citation_audit_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    # 1. Bootstrap Tenant and Brand
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

    # 2. Submit Intent
    resp = await client.post("/intents", headers=H, json={
        "domain": "presence",
        "brand_id": brand_id,
        "text": "run competitor citation audit for rival-a.com rival-b.com"
    })
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["cards"]) == 1

    card = data["cards"][0]
    op_id = card["op_id"]
    assert "Playwright citation audit" in card["preview"]

    # 3. Approve the operation
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    # 4. Verify BrandProperty citation findings in DB
    async with async_session() as s:
        stmt = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "citation_audit"
        )
        res = await s.execute(stmt)
        prop = res.scalar_one()
        assert prop.status == "healthy"
        assert prop.findings["audited_competitors_count"] == 2
        
        citations = prop.findings["citations"]
        assert len(citations) == 2
        assert citations[0]["competitor"] == "rival-a.com"
        assert citations[1]["competitor"] == "rival-b.com"

        # Verify audit_verify has not been modified or corrupted
        db_op = await s.get(OpRow, op_id)
        assert db_op.state == "DONE"
        verified, bad_id = await audit_verify(s)
        assert verified is True


@pytest.mark.asyncio
async def test_playwright_citation_audit_failure_graceful_handling(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    # 1. Bootstrap
    async with async_session() as s:
        tenant = Tenant(name="Failure Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Fail")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

    H = {"X-Tenant-ID": tenant_id}

    # 2. Propose OpSpec manually with simulate_timeout parameter
    op_spec = OpSpec(
        tenant_id=tenant_id,
        brand_id=brand_id,
        domain="presence",
        action="presence.citation.audit",
        params={
            "brand_id": brand_id,
            "competitors": ["slow-competitor.com"],
            "simulate_timeout": True
        },
        severity=Severity(impact=1, reversibility=Reversibility.REVERSIBLE),
        cost_estimate=Money(0)
    )

    async with async_session() as s:
        row = await loop.propose(s, op_spec, actor="tester")
        await s.commit()
        op_id = row.id

    # Transition to APPROVED
    async with async_session() as s:
        db_row = await s.get(OpRow, op_id)
        await loop.transition(s, db_row, OpState.PREVIEWED, actor="tester")
        await loop.decide(s, db_row, decision="approve", actor="chandan", role="AGENCY_OWNER", surface="web")
        await s.commit()

    # 3. Drain background task (which runs the execute method)
    async with async_session() as s:
        processed = await loop.drain_once(s)
        await s.commit()
        assert processed == 1

    # 4. Verify findings are empty but no crash occurred, state is DONE
    async with async_session() as s:
        db_row = await s.get(OpRow, op_id)
        assert db_row.state == "DONE"

        stmt = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "citation_audit"
        )
        res = await s.execute(stmt)
        prop = res.scalar_one()
        # Should be marked degraded because findings/citations are empty
        assert prop.status == "degraded"
        assert prop.findings["citations"] == []
        assert prop.findings["keywords"] == []


@pytest.mark.asyncio
async def test_citation_audit_rls_cross_tenant_denied(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    # 1. Bootstrap two tenants
    async with async_session() as s:
        t1 = Tenant(name="Tenant 1", hosting_tier="shared")
        t2 = Tenant(name="Tenant 2", hosting_tier="shared")
        s.add_all([t1, t2])
        await s.commit()
        t1_id, t2_id = t1.id, t2.id

        b2 = Brand(tenant_id=t2_id, name="Brand 2")
        s.add(b2)
        await s.commit()
        b2_id = b2.id

    # 2. Propose a citation audit Op belonging to Tenant 2
    op_spec = OpSpec(
        tenant_id=t2_id,
        brand_id=b2_id,
        domain="presence",
        action="presence.citation.audit",
        params={
            "brand_id": b2_id,
            "competitors": ["rival.com"]
        },
        severity=Severity(impact=1, reversibility=Reversibility.REVERSIBLE),
        cost_estimate=Money(0)
    )

    async with async_session() as s:
        row = await loop.propose(s, op_spec, actor="optimizer")
        await s.commit()
        op_id = row.id

    # 3. Execute under Tenant 1's context. Should fail with RuntimeError
    from app.database import tenant_context
    token = tenant_context.set(t1_id)
    try:
        async with async_session() as s:
            db_row = await s.get(OpRow, op_id)
            adapter = loop.REGISTRY.get("presence")
            
            with pytest.raises(RuntimeError) as exc_info:
                await adapter.execute(db_row, "idem_rls_test", session=s)
            
            assert "RLS Violation" in str(exc_info.value)
    finally:
        tenant_context.reset(token)


@pytest.mark.asyncio
async def test_aeo_citation_audit_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    # 1. Bootstrap Tenant, Brand and active Repository connection
    async with async_session() as s:
        tenant = Tenant(name="AEO Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra AEO")
        s.add(brand)
        await s.commit()
        brand_id = brand.id
        
        # Seed active repository connection so that auto-remediation Build Op proposing succeeds
        repo_prop = BrandProperty(
            tenant_id=tenant_id,
            brand_id=brand_id,
            type="repository",
            provider="git",
            status="active",
            findings={"repo_url": "git@github.com:tanmatra/aeo-site.git"}
        )
        s.add(repo_prop)
        await s.commit()

    H = {"X-Tenant-ID": tenant_id}

    # 2. Submit citation audit intent
    resp = await client.post("/intents", headers=H, json={
        "domain": "presence",
        "brand_id": brand_id,
        "text": "run competitor citation audit for rival-a.com"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]

    # 3. Approve the audit Op
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    # Background tasks are automatically processed by test client during the request lifecycle.

    # 5. Verify the audit findings in DB have the AEO report
    async with async_session() as s:
        stmt = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "citation_audit"
        )
        res = await s.execute(stmt)
        prop = res.scalar_one()
        assert prop.findings["aeo_report"] is not None
        assert "Mock Gap Analysis" in prop.findings["aeo_report"]["gap_analysis"]

        # 6. Verify that a build.deliver Op was automatically proposed
        stmt_ops = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.domain == "build",
            OpRow.action == "build.deliver",
            OpRow.parent_op_id == op_id
        )
        res_ops = await s.execute(stmt_ops)
        proposed_build_ops = res_ops.scalars().all()
        assert len(proposed_build_ops) == 1
        
        build_op = proposed_build_ops[0]
        assert build_op.state == "PROPOSED"
        assert build_op.params["repo"] == "git@github.com:tanmatra/aeo-site.git"
        assert "Create public/llms.txt" in build_op.params["intent"]
        assert build_op.params["capability"] == "seo_auditor_fixer"
