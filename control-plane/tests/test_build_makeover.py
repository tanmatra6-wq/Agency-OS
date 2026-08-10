import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from app.models import Tenant, Brand, BrandProperty, OpRow
from app.kernel.optypes import OpState

@pytest.mark.asyncio
async def test_build_design_makeover_flow(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    async with async_session() as s:
        tenant = Tenant(name="Design Tenant", hosting_tier="shared")
        s.add(tenant)
        await s.commit()
        tenant_id = tenant.id

        brand = Brand(tenant_id=tenant_id, name="Tanmatra Redesign")
        s.add(brand)
        await s.commit()
        brand_id = brand.id

        # Seed active repository connection property
        s.add(BrandProperty(
            tenant_id=tenant_id,
            brand_id=brand_id,
            type="repository",
            status="active",
            provider="github",
            findings={"repo_url": "git@github.com:ableys/brand-site.git"}
        ))
        await s.commit()

    H = {"X-Tenant-ID": tenant_id}

    # 1. Submit Design Makeover Intent
    resp = await client.post("/intents", headers=H, json={
        "domain": "build",
        "brand_id": brand_id,
        "text": "plan complete website makeover for Ableys"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    assert "Design Blueprint" in data["cards"][0]["preview"]
    assert "UX Structural Wireframe Spec" in data["cards"][0]["preview"]
    assert "Visual Style Guide & Theme" in data["cards"][0]["preview"]

    # 2. Approve Design Makeover
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    from app.kernel.loop import drain_once
    async with async_session() as s:
        async with s.begin():
            await drain_once(s)

    # 3. Verify Design Blueprint written to BrandProperty
    async with async_session() as s:
        # Parent Op should be DONE
        op_res = await s.get(OpRow, op_id)
        assert op_res.state == "DONE"

        stmt_prop = select(BrandProperty).where(
            BrandProperty.tenant_id == tenant_id,
            BrandProperty.brand_id == brand_id,
            BrandProperty.type == "design_blueprint"
        )
        res_prop = await s.execute(stmt_prop)
        prop = res_prop.scalar_one()
        assert prop.status == "drafted"
        assert prop.findings["css_theme"]["primary"] == "#3F51B5"
        assert len(prop.findings["image_prompts"]) == 2

        # Verify child build.deliver code operation has been proposed
        stmt_child = select(OpRow).where(
            OpRow.tenant_id == tenant_id,
            OpRow.brand_id == brand_id,
            OpRow.action == "build.deliver",
            OpRow.parent_op_id == op_id
        )
        res_child = await s.execute(stmt_child)
        child_ops = res_child.scalars().all()
        assert len(child_ops) == 1
        assert child_ops[0].state == "AWAITING_APPROVAL"
        assert "Configure CSS variables" in child_ops[0].params["intent"]
        assert "Primary=#3F51B5" in child_ops[0].params["intent"]
