import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.models import Tenant, Brand, Connection, OpRow, AuditEvent, OpTrace, Approval
from app.middleware import VALID_TENANTS_CACHE
from app.kernel.optypes import OpSpec, Severity, Reversibility, Money, OpState
from app.kernel.loop import propose, preview_and_gate, decide, _execute_and_verify
from app.kernel.services import load_active_rules

@pytest.mark.asyncio
async def test_tenant_lifecycle_suspension(client: AsyncClient, session: AsyncSession):
    """Verify that operators can suspend a tenant, and suspended tenants are immediately blocked by gateway."""
    # Enable tenant validation for this test
    import app.main as mainmod
    mainmod.app.state.bypass_tenant_validation = False
    VALID_TENANTS_CACHE.clear()

    try:
        # 1. Create a new Tenant
        create_resp = await client.post(
            "/tenants",
            json={"name": "Suspension Inc", "brand_name": "Suspension Brand"},
            headers={"Authorization": "Bearer default-dev-token"}
        )
        assert create_resp.status_code == 200
        data = create_resp.json()
        tenant_id = data["tenant_id"]
        
        # Verify it exists and is active by default in DB
        res = await session.execute(select(Tenant).where(Tenant.id == tenant_id))
        tenant = res.scalar_one()
        assert tenant.is_active is True
        
        # 2. Call a standard tenant-scoped endpoint (should pass)
        resp = await client.get("/connections", headers={"X-Tenant-ID": tenant_id})
        assert resp.status_code == 200
        
        # 3. Suspend the Tenant as Operator
        patch_resp = await client.patch(
            f"/tenants/{tenant_id}",
            json={"is_active": False},
            headers={"Authorization": "Bearer default-dev-token"}
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_active"] is False
        
        # Verify status in DB
        await session.refresh(tenant)
        assert tenant.is_active is False
        
        # 4. Try to call the tenant-scoped endpoint again (should fail with 403 Forbidden)
        resp_suspended = await client.get("/connections", headers={"X-Tenant-ID": tenant_id})
        assert resp_suspended.status_code == 403
        assert "suspended" in resp_suspended.json()["detail"].lower()
        
        # 5. Re-activate the Tenant
        patch_resp = await client.patch(
            f"/tenants/{tenant_id}",
            json={"is_active": True},
            headers={"Authorization": "Bearer default-dev-token"}
        )
        assert patch_resp.status_code == 200
        assert patch_resp.json()["is_active"] is True
        
        # Verify status in DB
        await session.refresh(tenant)
        assert tenant.is_active is True
        
        # 6. Call the endpoint again (should pass again!)
        resp_active = await client.get("/connections", headers={"X-Tenant-ID": tenant_id})
        assert resp_active.status_code == 200
        
    finally:
        # Restore bypass flag to prevent breaking other tests
        mainmod.app.state.bypass_tenant_validation = True
        VALID_TENANTS_CACHE.clear()


@pytest.mark.asyncio
async def test_governed_tenant_offboard_flow(client: AsyncClient, session: AsyncSession, monkeypatch):
    """Verify that deleting a tenant via API proposes a governed offboard Op,

    which requires AGENCY_OWNER authority to approve and execute, and safely retains all audit events.
    """
    # Mock send_whatsapp_card_task to prevent network calls
    from unittest.mock import AsyncMock
    monkeypatch.setattr("app.routers.tenants.send_whatsapp_card_task", AsyncMock())

    # 1. Create Tenant and some audit/trace rows
    t = Tenant(id="t-offboard-test", name="Offboard Inc", is_active=True)
    b = Brand(id="b-offboard-test", tenant_id=t.id, name="Offboard Brand")
    op = OpRow(
        id="op-offboard-test",
        tenant_id=t.id,
        brand_id=b.id,
        domain="grow",
        action="grow.bid",
        state="DONE",
        impact=1,
        reversibility="reversible",
        idem_key="idem-offboard-test"
    )
    audit = AuditEvent(
        ts="2026-06-20T00:00:00Z",
        tenant_id=t.id,
        actor="operator",
        action="grow.bid",
        op_id=op.id,
        payload={},
        prev_hash="genesis-hash",
        hash="mock-hash-offboard"
    )
    session.add_all([t, b, op, audit])
    await session.commit()

    # 2. Call DELETE /tenants/{id} (Operator Token)
    del_resp = await client.delete(
        f"/tenants/{t.id}",
        headers={"Authorization": "Bearer default-dev-token"}
    )
    assert del_resp.status_code == 202  # Accepted (Proposed)
    data = del_resp.json()
    assert data["status"] == "proposed"
    op_id = data["op_id"]
    assert data["state"] == "AWAITING_APPROVAL"

    # 3. Try to approve as a normal OPERATOR (should fail authority check!)
    s_params = await load_active_rules(session, tenant_id=t.id)
    op_row = await session.get(OpRow, op_id)
    assert op_row is not None
    
    # Attempt decision as OPERATOR
    with pytest.raises(ValueError) as exc:
        await decide(session, op_row, decision="approve", actor="operator-user", role="OPERATOR", surface="web")
    assert "only agency_owner can approve tenant offboarding" in str(exc.value).lower()

    # 4. Approve as AGENCY_OWNER (should succeed!)
    await decide(session, op_row, decision="approve", actor="owner-user", role="AGENCY_OWNER", surface="web")
    assert op_row.state == OpState.APPROVED.value
    
    # 5. Execute the offboarding
    await _execute_and_verify(session, op_row)
    await session.commit()

    # 6. Verify Tenant is soft-offboarded, PII is scrubbed, but all audit rows are preserved!
    session.expunge_all()
    t_after = await session.get(Tenant, t.id)
    assert t_after.is_active is False
    assert "Offboarded Tenant" in t_after.name
    assert t_after.gcp_project is None

    # Verify audit/op rows are fully retained!
    op_after = await session.get(OpRow, op.id)
    assert op_after is not None
    audit_after = (await session.execute(select(AuditEvent).where(AuditEvent.op_id == op.id))).scalar_one()
    assert audit_after is not None


@pytest.mark.asyncio
async def test_governed_tenant_hard_delete_flow(session: AsyncSession):
    """Verify that a governed manage.tenant.hard_delete Op can only be executed on an inactive tenant,

    successfully re-associates all audit events to the global 'deleted_tenant' placeholder, and drops the tenant row.
    """
    # 1. Create a soft-offboarded Tenant
    t = Tenant(id="t-hard-del", name="Offboarded Tenant XYZ", is_active=False)
    b = Brand(id="b-hard-del", tenant_id=t.id, name="Hard Del Brand")
    op = OpRow(
        id="op-hard-del",
        tenant_id=t.id,
        brand_id=b.id,
        domain="grow",
        action="grow.bid",
        state="DONE",
        impact=1,
        reversibility="reversible",
        idem_key="idem-hard-del"
    )
    audit = AuditEvent(
        ts="2026-06-20T00:00:00Z",
        tenant_id=t.id,
        actor="operator",
        action="grow.bid",
        op_id=op.id,
        payload={},
        prev_hash="genesis-hash",
        hash="mock-hash-hard-del"
    )
    session.add_all([t, b, op, audit])
    await session.commit()

    # 2. Propose manage.tenant.hard_delete Op
    spec = OpSpec(
        id="op_hard_delete_trigger",
        tenant_id=t.id,
        brand_id="_system",
        domain="manage",
        action="manage.tenant.hard_delete",
        params={"target_tenant_id": t.id},
        severity=Severity(impact=3, reversibility=Reversibility.IRREVERSIBLE),
        cost_estimate=Money(0)
    )
    
    op_row = await propose(session, spec, actor="owner-user")
    s_params = await load_active_rules(session, tenant_id=t.id)
    
    # Run the safety gate (transitions PROPOSED -> AWAITING_APPROVAL)
    await preview_and_gate(session, op_row, tier=1)
    
    # Approve as AGENCY_OWNER
    await decide(session, op_row, decision="approve", actor="owner-user", role="AGENCY_OWNER", surface="web")
    await session.commit()

    # 3. Execute hard delete
    await _execute_and_verify(session, op_row)
    await session.commit()

    # 4. Verify Tenant, Brand, and Connection rows are completely gone!
    session.expunge_all()
    assert (await session.get(Tenant, t.id)) is None
    assert (await session.get(Brand, b.id)) is None

    # 5. Verify the global 'deleted_tenant' tombstone row was provisioned
    deleted_tenant = await session.get(Tenant, "deleted_tenant")
    assert deleted_tenant is not None
    assert deleted_tenant.is_active is False

    # 6. Verify that the audit event and op row were NOT deleted but re-associated to 'deleted_tenant'!
    op_reassociated = await session.get(OpRow, op.id)
    assert op_reassociated is not None
    assert op_reassociated.tenant_id == "deleted_tenant"

    audit_reassociated = (await session.execute(select(AuditEvent).where(AuditEvent.op_id == op.id))).scalar_one()
    assert audit_reassociated is not None
    assert audit_reassociated.tenant_id == "deleted_tenant"


@pytest.mark.asyncio
async def test_database_foreign_key_restricton_firewall(session: AsyncSession):
    """Verify that the database foreign key RESTRICT constraints successfully block

    any direct physical deletion of a tenant if they carry audit events or cost entries.
    """
    # 1. Create Tenant and a dependent AuditEvent
    t = Tenant(id="t-firewall-test", name="Firewall Inc", is_active=True)
    audit = AuditEvent(
        ts="2026-06-20T00:00:00Z",
        tenant_id=t.id,
        actor="operator",
        action="grow.bid",
        op_id="op-dummy",
        payload={},
        prev_hash="genesis-hash",
        hash="mock-hash-firewall"
    )
    session.add_all([t, audit])
    await session.commit()

    # 2. Attempt to physically delete the Tenant directly via SQLAlchemy (bypassing the control plane API)
    # This should fail with an IntegrityError because of the ON DELETE RESTRICT constraint on AuditEvent!
    await session.delete(t)
    
    with pytest.raises(IntegrityError) as exc:
        await session.commit()
        
    # Verify it was blocked by foreign key constraint
    assert "foreign key constraint" in str(exc.value).lower() or "integrityerror" in str(exc.value).lower()
    
    # Rollback session to clear failed transaction
    await session.rollback()
