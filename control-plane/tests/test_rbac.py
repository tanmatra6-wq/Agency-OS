import pytest
from httpx import AsyncClient
from sqlalchemy import select
from app.models import OpRow, Tenant
from app.kernel.optypes import OpState

@pytest.mark.asyncio
async def test_rbac_enforcement_cases(client: AsyncClient, session):
    import app.main as mainmod
    from app.main import verify_operator_auth, resolved_operator_role

    # 1. Remove overrides to test real security
    overrides_to_restore = {}
    for dep in [verify_operator_auth, resolved_operator_role]:
        if dep in mainmod.app.dependency_overrides:
            overrides_to_restore[dep] = mainmod.app.dependency_overrides[dep]
            del mainmod.app.dependency_overrides[dep]

    try:
        # 2. Create tenant (requires operator auth now that /tenants is gated)
        r = await client.post(
            "/tenants",
            headers={"Authorization": "Bearer default-dev-token"},
            json={"name": "Tanmatra", "brand_name": "Wok-Tok"}
        )
        assert r.status_code == 200
        tid = r.json()["tenant_id"]
        bid = r.json()["brand_id"]
        H = {"X-Tenant-ID": tid}
        OP_H = {**H, "Authorization": "Bearer default-dev-token"}

        # 3. Propose Op
        r = await client.post("/intents", headers=H, json={"brand_id": bid, "text": "host woktok.in please"})
        op_id = r.json()["cards"][0]["op_id"]

        # Fetch and update Op to different test scenarios
        async def set_op_properties(impact: int, cost_minor: int, domain: str, statutory: bool, reversibility: str):
            row = await session.get(OpRow, op_id)
            row.impact = impact
            row.reversibility = reversibility
            row.cost_amount_minor = cost_minor
            row.domain = domain
            row.statutory = statutory
            row.state = "AWAITING_APPROVAL"
            await session.commit()

        # --- Scenario A: OPERATOR tries to approve HIGH impact (5) Op (requires operator token)
        await set_op_properties(impact=5, cost_minor=50_000, domain="provision", statutory=False, reversibility="REVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=OP_H, json={
            "decision": "approve",
            "actor": "op_bob",
            "role": "OPERATOR",
            "surface": "whatsapp",
            "reason": "Deploying standard host"
        })
        assert res.status_code == 403
        assert "OPERATOR cannot approve Ops with severity impact 5" in res.json()["detail"]

        # --- Scenario B: CLIENT tries to approve statutory Op (no token, defaults to CLIENT)
        await set_op_properties(impact=2, cost_minor=50_000, domain="grow", statutory=True, reversibility="REVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=H, json={
            "decision": "approve",
            "actor": "client_alice",
            "role": "CLIENT",
            "surface": "web",
            "reason": "Authorized"
        })
        assert res.status_code == 403
        assert "CLIENT cannot approve statutory Ops" in res.json()["detail"]

        # --- Scenario C: BRAND_VIEWER tries to approve provision Op (requires operator token to trust role)
        await set_op_properties(impact=1, cost_minor=0, domain="provision", statutory=False, reversibility="REVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=OP_H, json={
            "decision": "approve",
            "actor": "viewer_charlie",
            "role": "BRAND_VIEWER",
            "surface": "web",
            "reason": "View check"
        })
        assert res.status_code == 403
        assert "BRAND_VIEWER cannot approve provision Ops" in res.json()["detail"]

        # --- Scenario D: CLIENT tries to approve Op exceeding cost limit (no token, defaults to CLIENT)
        await set_op_properties(impact=2, cost_minor=1_200_000, domain="grow", statutory=False, reversibility="REVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=H, json={
            "decision": "approve",
            "actor": "client_alice",
            "role": "CLIENT",
            "surface": "web",
            "reason": "Approve expensive Grow"
        })
        assert res.status_code == 403
        assert "CLIENT cannot approve Ops costing 12000.00 INR (max allowed 10000.00 INR)" in res.json()["detail"]

        # --- Scenario E: CLIENT tries to approve IRREVERSIBLE Op (no token, defaults to CLIENT)
        await set_op_properties(impact=2, cost_minor=50_000, domain="grow", statutory=False, reversibility="IRREVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=H, json={
            "decision": "approve",
            "actor": "client_alice",
            "role": "CLIENT",
            "surface": "web",
            "reason": "Approve irreversible Grow"
        })
        assert res.status_code == 403
        assert "CLIENT cannot approve IRREVERSIBLE Ops" in res.json()["detail"]

        # --- Scenario F: AGENCY_OWNER approves HIGH impact statutory irreversible Op (requires operator token, succeeds)
        await set_op_properties(impact=5, cost_minor=5_000_000, domain="provision", statutory=True, reversibility="IRREVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=OP_H, json={
            "decision": "approve",
            "actor": "owner_dave",
            "role": "AGENCY_OWNER",
            "surface": "whatsapp",
            "reason": "Override statutory high-risk baseline"
        })
        assert res.status_code == 200
        assert res.json()["state"] == "APPROVED"

        # --- Scenario G: Client tries to masquerade as AGENCY_OWNER without token (should be forced to CLIENT and fail)
        await set_op_properties(impact=1, cost_minor=0, domain="provision", statutory=False, reversibility="REVERSIBLE")
        res = await client.post(f"/ops/{op_id}/decision", headers=H, json={
            "decision": "approve",
            "actor": "malicious_client",
            "role": "AGENCY_OWNER",
            "surface": "web",
            "reason": "I am owner trust me"
        })
        # Rejected because a CLIENT cannot approve provision Ops
        assert res.status_code == 403
        assert "CLIENT cannot approve provision Ops" in res.json()["detail"]

    finally:
        # 4. Restore overrides to prevent leaking to other tests
        for dep, val in overrides_to_restore.items():
            mainmod.app.dependency_overrides[dep] = val


@pytest.mark.asyncio
async def test_operator_authentication_enforcement(client: AsyncClient, session):
    import app.main as mainmod
    from app.main import verify_operator_auth
    from app.models import Tenant, Brand

    # 1. Seed tenant and brand for connection tests
    tenant = Tenant(id="t-rbac", name="RBAC Tenant")
    brand = Brand(id="b-rbac", tenant_id="t-rbac", name="RBAC Brand")
    session.add(tenant)
    session.add(brand)
    await session.commit()

    # 2. Remove the override temporarily
    if verify_operator_auth in mainmod.app.dependency_overrides:
        del mainmod.app.dependency_overrides[verify_operator_auth]

    try:
        # 2. GET /tenants without header -> 401
        res = await client.get("/tenants")
        assert res.status_code == 401
        assert "Missing or invalid Authorization header" in res.json()["detail"]

        # 3. GET /tenants with invalid Bearer token -> 403
        res = await client.get("/tenants", headers={"Authorization": "Bearer wrong-token"})
        assert res.status_code == 403
        assert "Forbidden: Invalid operator token" in res.json()["detail"]

        # 4. GET /tenants with valid Bearer token -> 200
        res = await client.get("/tenants", headers={"Authorization": "Bearer default-dev-token"})
        assert res.status_code == 200
        assert isinstance(res.json(), list)
        
        # 5. POST /tenants without header -> 401
        res = await client.post("/tenants", json={"name": "New Tenant", "brand_name": "New Brand"})
        assert res.status_code == 401
        
        # 6. POST /tenants with valid Bearer token -> 200
        res = await client.post(
            "/tenants", 
            headers={"Authorization": "Bearer default-dev-token"},
            json={"name": "New Tenant", "brand_name": "New Brand"}
        )
        assert res.status_code == 200
        assert "tenant_id" in res.json()

        # 7. POST /api/v1/onboarding/bootstrap without header -> 401
        res = await client.post("/api/v1/onboarding/bootstrap?name=Test&domain=test.com")
        assert res.status_code == 401
        
        # 8. POST /api/v1/onboarding/bootstrap with invalid Bearer token -> 403
        res = await client.post(
            "/api/v1/onboarding/bootstrap?name=Test&domain=test.com",
            headers={"Authorization": "Bearer wrong-token"}
        )
        assert res.status_code == 403
        
        # 9. POST /api/v1/onboarding/bootstrap with valid Bearer token -> 200
        res = await client.post(
            "/api/v1/onboarding/bootstrap?name=Test&domain=test.com",
            headers={"Authorization": "Bearer default-dev-token"}
        )
        assert res.status_code == 200
        assert "tenant_id" in res.json()

        # 10. POST /api/v1/onboarding/connection/direct without header -> 401
        res = await client.post("/api/v1/onboarding/connection/direct?tenant_id=t-rbac&brand_id=b-rbac&provider=klaviyo&api_key=key")
        assert res.status_code == 401

        # 11. POST /api/v1/onboarding/connection/direct with invalid token -> 403
        res = await client.post(
            "/api/v1/onboarding/connection/direct?tenant_id=t-rbac&brand_id=b-rbac&provider=klaviyo&api_key=key",
            headers={"Authorization": "Bearer wrong-token"}
        )
        assert res.status_code == 403

        # 12. POST /api/v1/onboarding/connection/direct with valid token -> 200
        res = await client.post(
            "/api/v1/onboarding/connection/direct?tenant_id=t-rbac&brand_id=b-rbac&provider=klaviyo&api_key=key",
            headers={"Authorization": "Bearer default-dev-token"}
        )
        assert res.status_code == 200

        # 13. POST /api/v1/onboarding/connection/config without header -> 401
        res = await client.post("/api/v1/onboarding/connection/config?tenant_id=t-rbac&brand_id=b-rbac&provider=klaviyo", json={})
        assert res.status_code == 401

        # 14. POST /api/v1/onboarding/connection/config with invalid token -> 403
        res = await client.post(
            "/api/v1/onboarding/connection/config?tenant_id=t-rbac&brand_id=b-rbac&provider=klaviyo",
            headers={"Authorization": "Bearer wrong-token"},
            json={}
        )
        assert res.status_code == 403

        # 15. POST /api/v1/onboarding/connection/config with valid token -> 200
        res = await client.post(
            "/api/v1/onboarding/connection/config?tenant_id=t-rbac&brand_id=b-rbac&provider=klaviyo",
            headers={"Authorization": "Bearer default-dev-token"},
            json={"foo": "bar"}
        )
        assert res.status_code == 200

    finally:
        # 7. Restore override to prevent leaking to other tests
        mainmod.app.dependency_overrides[verify_operator_auth] = lambda: None
