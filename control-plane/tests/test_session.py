import pytest
import time
from httpx import AsyncClient, ASGITransport
from app.main import app
from app.auth import OPERATOR_TOKEN, sign_jwt, verify_operator_auth, resolved_operator_role


@pytest.fixture(autouse=True)
def restore_auth_dependencies(client):
    print("DEBUG OVERRIDES BEFORE:", [getattr(k, "__name__", str(k)) for k in app.dependency_overrides.keys()])
    orig_verify = app.dependency_overrides.pop(verify_operator_auth, None)
    orig_resolved = app.dependency_overrides.pop(resolved_operator_role, None)
    print("DEBUG OVERRIDES AFTER:", [getattr(k, "__name__", str(k)) for k in app.dependency_overrides.keys()])
    yield
    if orig_verify is not None:
        app.dependency_overrides[verify_operator_auth] = orig_verify
    if orig_resolved is not None:
        app.dependency_overrides[resolved_operator_role] = orig_resolved


@pytest.mark.asyncio
async def test_session_bootstrap_success(client: AsyncClient):
    # Post valid operator token
    r = await client.post(
        "/session/bootstrap",
        json={"operator_token": OPERATOR_TOKEN}
    )
    assert r.status_code == 200
    data = r.json()
    assert "session_token" in data
    assert data["expires_in"] == 7200

    # Verify that we can access /tenants with this session token
    session_token = data["session_token"]
    r_tenants = await client.get(
        "/tenants",
        headers={"Authorization": f"Bearer {session_token}"}
    )
    assert r_tenants.status_code == 200


@pytest.mark.asyncio
async def test_session_bootstrap_invalid_token(client: AsyncClient):
    # Post invalid operator token
    r = await client.post(
        "/session/bootstrap",
        json={"operator_token": "wrong-secret-token"}
    )
    assert r.status_code == 403
    assert r.json()["detail"] == "Forbidden: Invalid operator token"


@pytest.mark.asyncio
async def test_session_token_expiration(client: AsyncClient):
    # Generate an expired session token (expires_in = -10 seconds)
    expired_token = sign_jwt({"role": "OPERATOR_AUTHENTICATED"}, OPERATOR_TOKEN, expires_in=-10)

    # Attempt to query /tenants
    r_tenants = await client.get(
        "/tenants",
        headers={"Authorization": f"Bearer {expired_token}"}
    )
    # Must fail with 403 Forbidden
    assert r_tenants.status_code == 403
    assert "session expired" in r_tenants.json()["detail"]
