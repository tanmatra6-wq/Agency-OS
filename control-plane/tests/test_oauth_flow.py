# Feature 2 OAuth Flow and Token Rotation tests
import datetime as dt
import json
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker
from httpx import AsyncClient, HTTPStatusError
from fastapi import HTTPException, Request

from app.models import Connection, AuditEvent, BrandProperty, Tenant, Brand
from app.database import get_db
import app.main as mainmod
import app.auth as authmod

# ---------------------------------------------------------
# Tier 1: OAuth Token Refresh and OIDC Worker Auth
# ---------------------------------------------------------

@pytest.mark.asyncio
async def test_oauth_refresh_service_success(session, mock_secrets_client):
    """Test 11: Verify OAuth refresh token service successfully rotates tokens."""
    try:
        from app.services.oauth import OauthService
    except ImportError:
        pytest.skip("OauthService not implemented yet (expected Red state)")

    mock_secrets_client.read_secret.return_value = "old-refresh-token"
    mock_secrets_client.write_secret.return_value = "projects/test-project/secrets/s1/versions/2"

    # Mock the token exchange endpoint
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "expires_in": 3600
        }
        mock_post.return_value = mock_resp

        service = OauthService(tenant_id="t1")
        result = await service.refresh_token(
            tenant_id="t1",
            brand_id="b1",
            provider="shopify",
            refresh_token_ref="projects/test-project/secrets/s1/versions/1"
        )

        assert result["access_token"] == "new-access-token"
        assert result["refresh_token"] == "new-refresh-token"
        assert mock_secrets_client.write_secret.call_count == 2  # One for access, one for refresh

@pytest.mark.asyncio
async def test_oauth_refresh_service_revoked_token(session, mock_secrets_client):
    """Test 12: Verify refresh failure due to revoked token marks connection in error."""
    try:
        from app.services.oauth import OauthService
    except ImportError:
        pytest.skip("OauthService not implemented yet (expected Red state)")

    conn = Connection(
        tenant_id="t1", brand_id="b1", provider="shopify",
        credential="projects/test-project/secrets/s1/versions/1", status="active"
    )
    session.add(conn)
    await session.commit()

    mock_secrets_client.read_secret.return_value = "revoked-refresh-token"

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        mock_post.return_value = mock_resp

        service = OauthService(tenant_id="t1")
        with pytest.raises(Exception):
            await service.refresh_token(
                tenant_id="t1",
                brand_id="b1",
                provider="shopify",
                refresh_token_ref=conn.credential
            )

@pytest.mark.asyncio
async def test_verify_worker_auth_valid_oidc(mock_oidc_verification):
    """Test 13: Verify OIDC authentication dependency accepts valid scheduler tokens."""
    mock_oidc_verification.return_value = {
        "iss": "https://accounts.google.com",
        "email": "scheduler-worker@aos.iam.gserviceaccount.com",
        "aud": "http://test/tasks/refresh-tokens"
    }

    request = MagicMock(spec=Request)
    request.url = MagicMock()
    request.url.scheme = "http"
    request.url.netloc = "test"
    request.url.path = "/tasks/refresh-tokens"

    # Override env in app.auth
    with patch("app.auth.AOS_ENV", "production"), \
         patch("app.auth.WORKER_SA", "scheduler-worker@aos.iam.gserviceaccount.com"):
        # Should not raise exception
        await authmod.verify_worker_auth(request, authorization="Bearer valid-token")

@pytest.mark.asyncio
async def test_verify_worker_auth_invalid_audience(mock_oidc_verification):
    """Test 14: Verify OIDC auth rejects tokens with incorrect audience."""
    def mock_verify(token, request, audience=None):
        if audience != "http://test/tasks/refresh-tokens":
            raise ValueError("Token focus audience mismatch")
        return {
            "iss": "https://accounts.google.com",
            "email": "scheduler-worker@aos.iam.gserviceaccount.com",
            "aud": audience
        }
    mock_oidc_verification.side_effect = mock_verify

    request = MagicMock(spec=Request)
    request.url = MagicMock()
    request.url.scheme = "http"
    request.url.netloc = "test"
    request.url.path = "/tasks/invalid-path"

    with patch("app.auth.AOS_ENV", "production"), \
         patch("app.auth.WORKER_SA", "scheduler-worker@aos.iam.gserviceaccount.com"):
        with pytest.raises(HTTPException) as exc:
            await authmod.verify_worker_auth(request, authorization="Bearer invalid-token")
        assert exc.value.status_code == 401

@pytest.mark.asyncio
async def test_refresh_tokens_task_endpoint_empty_db(client):
    """Test 15: Verify that calling refresh-tokens task on empty DB completes with 200."""
    # Ensure worker auth bypasses in test env, or pass dummy header
    resp = await client.post("/tasks/refresh-tokens")
    # If endpoint not implemented yet, it will return 404, which is expected Red state
    if resp.status_code == 404:
        pytest.skip("Endpoint /tasks/refresh-tokens not implemented yet (expected Red state)")
    assert resp.status_code == 200

# ---------------------------------------------------------
# Tier 2: OAuth Security and State Handlers
# ---------------------------------------------------------

@pytest.mark.parametrize(
    "invalid_key",
    [
        "projects/control-plane/secrets/oauth-key/versions/latest",
        "projects/tenant-a/secrets/oauth-key/versions/1",
        "oauth-state-signing-secret",
        "short-key",
        b"short",
        "",
        b"",
    ],
)
def test_oauth_signer_rejects_secret_references_and_invalid_keys(invalid_key):
    """Verify that OAuthStateSigner strictly rejects literal references, strings, and short keys."""
    from app.services.oauth import OAuthStateSigner
    with pytest.raises(
        ValueError,
        match="Literal secret references cannot be used",
    ):
        OAuthStateSigner(signing_key=invalid_key)


@pytest.mark.asyncio
async def test_oauth_state_signing_integrity():
    """Test 21: Verify that generated OAuth state contains a cryptographically secure signature."""
    try:
        from app.services.oauth import OAuthStateSigner
    except ImportError:
        pytest.skip("State signing functions not implemented yet (expected Red state)")

    tenant_id = "t1"
    brand_id = "b1"
    redirect_uri = "https://app.agencyos.com/callback"
    signing_key = b"unit-test-signing-key-with-at-least-32-bytes!!"
    
    signer = OAuthStateSigner(signing_key=signing_key)
    state = signer.generate_state(tenant_id, brand_id, redirect_uri)
    assert state is not None
    
    # Valid verification
    payload = signer.verify_state(state)
    assert payload["tenant_id"] == tenant_id
    assert payload["brand_id"] == brand_id
    assert payload["redirect_uri"] == redirect_uri

    # Invalid signature
    tampered_state = state[:-5] + "aaaaa"
    with pytest.raises(ValueError, match="Invalid state signature"):
        signer.verify_state(tampered_state)


@pytest.mark.asyncio
async def test_oauth_state_expiration():
    """Test 22: Verify that OAuth state tokens expire after a predefined duration (15 mins)."""
    try:
        from app.services.oauth import OAuthStateSigner
    except ImportError:
        pytest.skip("State signing functions not implemented yet (expected Red state)")

    signing_key = b"unit-test-signing-key-with-at-least-32-bytes!!"
    signer = OAuthStateSigner(signing_key=signing_key)
    state = signer.generate_state("t1", "b1", "https://app.agencyos.com/callback")
    
    # Verify immediately succeeds
    assert signer.verify_state(state) is not None

    # Fast forward time by 16 minutes
    future_time = dt.datetime.utcnow() + dt.timedelta(minutes=16)
    with patch("datetime.datetime") as mock_dt:
        mock_dt.utcnow.return_value = future_time
        with pytest.raises(ValueError, match="State token expired"):
            signer.verify_state(state)


def test_open_redirect_helper_validation():
    """Test 23: Verify that redirect URI helper rejects domains outside the allowed pattern."""
    try:
        from app.services.oauth import validate_redirect_uri
    except ImportError:
        pytest.skip("Redirect validation helper not implemented yet (expected Red state)")

    # Allowed domain pattern: *.agencyos.com or localhost
    assert validate_redirect_uri("https://app.agencyos.com/dashboard") is True
    assert validate_redirect_uri("http://localhost:3000/callback") is True
    
    # Forbidden domains
    assert validate_redirect_uri("https://attacker.com") is False
    assert validate_redirect_uri("https://app.agencyos.com.attacker.com/bypass") is False
    assert validate_redirect_uri("https://attacker.com/app.agencyos.com") is False


@pytest.mark.asyncio
async def test_oauth_authorize_redirect_generation(client, session):
    """Test 24: Verify authorize endpoint generates a valid provider redirect URI with signed state."""
    session.add(Tenant(id="t1", name="Test Tenant", hosting_tier="shared"))
    session.add(Brand(id="b1", tenant_id="t1", name="Test Brand"))
    await session.commit()

    resp = await client.get(
        "/connections/oauth/authorize?provider=shopify&brand_id=b1&redirect_uri=https://app.agencyos.com/callback",
        headers={"X-Tenant-ID": "t1"}
    )
    if resp.status_code == 404:
        pytest.skip("Authorize endpoint not implemented yet (expected Red state)")
    
    assert resp.status_code == 302
    location = resp.headers.get("location")
    assert "myshopify.com/admin/oauth/authorize" in location or "shopify" in location
    assert "state=" in location
    assert "redirect_uri=" in location


@pytest.mark.asyncio
async def test_oauth_callback_missing_state(client):
    """Test 25: Verify callback endpoint rejects requests missing state parameter."""
    resp = await client.get("/connections/oauth/callback?code=123456")
    if resp.status_code == 404:
        pytest.skip("Callback endpoint not implemented yet (expected Red state)")
    assert resp.status_code == 400
    assert "Missing state" in resp.text


# ---------------------------------------------------------
# Tier 2: Token Rotation and Scheduler Resiliency
# ---------------------------------------------------------

@pytest.mark.asyncio
async def test_periodic_token_rotation_flow(session, mock_secrets_client):
    """Test 36: Verify periodic task identifies expiring tokens and rotates them."""
    tenant = Tenant(id="t1", name="Test Tenant", gcp_project="tenant-a-secrets")
    session.add(tenant)
    brand = Brand(id="b1", tenant_id="t1", name="Test Brand")
    session.add(brand)
    # Seed a connection with an expiring token
    conn = Connection(
        tenant_id="t1", brand_id="b1", provider="google-ads",
        credential="projects/tenant-a-secrets/secrets/ads-secret/versions/1",
        config={"refresh_token": "rt-123"},
        status="active"
    )
    if hasattr(Connection, "last_rotated_at"):
        conn.last_rotated_at = dt.datetime.utcnow() - dt.timedelta(days=10)
    
    session.add(conn)
    await session.commit()

    try:
        from app.services.oauth import OauthService
    except ImportError:
        pytest.skip("OauthService not implemented yet (expected Red state)")

    with patch("app.services.oauth.OauthService.refresh_token") as mock_refresh:
        mock_refresh.return_value = {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600
        }
        
        # Call rotation runner
        from app.tasks.rotation import rotate_expiring_tokens
        await rotate_expiring_tokens(session)
        
        # The background task only PROPOSES the Op. Query it!
        from app.models import OpRow
        from app.kernel import loop
        
        stmt_op = select(OpRow).where(OpRow.action == "manage.connection.rotate")
        res_op = await session.execute(stmt_op)
        op_row = res_op.scalar_one()
        assert op_row.state == "AWAITING_APPROVAL"
        
        # Approve the Op
        await loop.decide(session, op_row, decision="approve", actor="operator", role="OPERATOR", surface="whatsapp")
        await session.commit()
        
        # Execute the Op
        await loop._execute_and_verify(session, op_row)
        await session.commit()
        
        mock_refresh.assert_called_once()
        await session.refresh(conn)
        assert conn.status == "active"


@pytest.mark.asyncio
async def test_scheduler_batch_resiliency(session, mock_secrets_client):
    """Test 37: Verify failure in one connection's rotation does not abort the entire batch."""
    tenant = Tenant(id="t1", name="Test Tenant", gcp_project="tenant-a-secrets")
    session.add(tenant)
    brand = Brand(id="b1", tenant_id="t1", name="Test Brand")
    session.add(brand)
    conn1 = Connection(
        tenant_id="t1", brand_id="b1", provider="shopify",
        credential="projects/tenant-a-secrets/secrets/shopify-secret/versions/1",
        status="active",
        config={"refresh_token": "rt-shopify"}
    )
    conn2 = Connection(
        tenant_id="t1", brand_id="b1", provider="google-ads",
        credential="projects/tenant-a-secrets/secrets/ads-secret/versions/1",
        status="active",
        config={"refresh_token": "rt-ads"}
    )
    if hasattr(Connection, "last_rotated_at"):
        conn1.last_rotated_at = dt.datetime.utcnow() - dt.timedelta(days=10)
        conn2.last_rotated_at = dt.datetime.utcnow() - dt.timedelta(days=10)
    session.add_all([conn1, conn2])
    await session.commit()

    try:
        from app.tasks.rotation import rotate_expiring_tokens
    except ImportError:
        pytest.skip("Rotation tasks not implemented yet (expected Red state)")

    # Mock rotation to fail for conn1 and succeed for conn2
    async def mock_rotate(*args, **kwargs):
        provider = kwargs.get("provider") or (args[2] if len(args) > 2 else (args[0] if len(args) == 1 else None))
        if provider == "shopify":
            raise Exception("Shopify rotation failed!")
        return {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
            "access_token_ref": "projects/tenant-a-secrets/secrets/ads-access/versions/latest",
            "refresh_token_ref": "projects/tenant-a-secrets/secrets/ads-refresh/versions/latest"
        }

    with patch("app.services.oauth.OauthService.refresh_token", side_effect=mock_rotate):
        await rotate_expiring_tokens(session)
        
        # Both Ops must have been proposed!
        from app.models import OpRow
        from app.kernel import loop
        
        stmt_ops = select(OpRow).where(OpRow.action == "manage.connection.rotate").order_by(OpRow.id)
        res_ops = await session.execute(stmt_ops)
        op_rows = res_ops.scalars().all()
        assert len(op_rows) == 2
        
        # Approve and execute both!
        for op_row in op_rows:
            assert op_row.state == "AWAITING_APPROVAL"
            await loop.decide(session, op_row, decision="approve", actor="operator", role="OPERATOR", surface="whatsapp")
            await session.commit()
            try:
                await loop._execute_and_verify(session, op_row)
            except Exception:
                pass
            await session.commit()
            
        await session.refresh(conn1)
        await session.refresh(conn2)
        # conn1 should be in error status, but conn2 should remain active/successfully rotated
        assert conn1.status == "error"
        assert conn2.status == "active"


@pytest.mark.asyncio
async def test_scheduler_auth_enforcement(client):
    """Test 38: Verify that worker task endpoints return 401 unauthorized if no OIDC header is passed."""
    # Ensure WORKER_SA is configured to force verification
    with patch("app.auth.WORKER_SA", "scheduler-worker@aos.iam.gserviceaccount.com"), \
          patch("app.auth.AOS_ENV", "production"):
        resp = await client.post("/tasks/drain-outbox")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_auto_refresh_retries_during_audits(session, mock_secrets_client):
    """Test 39: Verify that active audits automatically refresh tokens on auth errors and retry."""
    try:
        from app.services.oauth import OauthService
    except ImportError:
        pytest.skip("OauthService not implemented yet (expected Red state)")

    tenant = Tenant(id="t1", name="Test Tenant", gcp_project="tenant-a-secrets")
    session.add(tenant)
    brand = Brand(id="b1", tenant_id="t1", name="Test Brand")
    session.add(brand)
    conn = Connection(
        tenant_id="t1", brand_id="b1", provider="google-search-console",
        credential="projects/tenant-a-secrets/secrets/sc-secret/versions/1", status="active"
    )
    session.add(conn)
    await session.commit()

    mock_secrets_client.read_secret.return_value = "expired-token"

    # Spy on refresh_token
    with patch("app.services.oauth.OauthService.refresh_token") as mock_refresh, \
          patch("httpx.AsyncClient.post") as mock_post:
        
        mock_refresh.return_value = {"access_token": "fresh-token", "refresh_token": "fresh-refresh"}
        
        # First API call returns 401, second returns 200
        resp1 = MagicMock()
        resp1.status_code = 401
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {"inspectionResult": {"indexStatusResult": {"verdict": "PASS"}}}
        mock_post.side_effect = [resp1, resp2]

        from app.services.google_audit import GoogleSearchConsoleAudit
        audit = GoogleSearchConsoleAudit(tenant_id="t1", brand_id="b1", session=session)
        
        result = await audit.run()
        assert result["status"] == "healthy"
            
        # Assert that refresh_token was triggered automatically on 401
        mock_refresh.assert_called_once()


@pytest.mark.asyncio
async def test_db_session_rollback_on_rotation_db_failure(session, mock_secrets_client):
    """Test 40: Verify database rollback on rotation database write failure."""
    tenant = Tenant(id="t1", name="Test Tenant", gcp_project="tenant-a-secrets")
    session.add(tenant)
    brand = Brand(id="b1", tenant_id="t1", name="Test Brand")
    session.add(brand)
    conn = Connection(
        tenant_id="t1", brand_id="b1", provider="shopify",
        credential="projects/tenant-a-secrets/secrets/s1/versions/1", status="active"
    )
    session.add(conn)
    await session.commit()

    try:
        from app.services.oauth import OauthService
    except ImportError:
        pytest.skip("OauthService not implemented yet (expected Red state)")

    # Simulate database crash during rotation update
    with patch("app.services.oauth.OauthService.refresh_token") as mock_refresh, \
          patch.object(session, "commit", side_effect=Exception("Database connection lost!")):
        
        mock_refresh.return_value = {"access_token": "new-access", "refresh_token": "new-refresh"}
        
        from app.tasks.rotation import rotate_expiring_tokens
        with pytest.raises(Exception):
            await rotate_expiring_tokens(session)
            
        # Verify transaction rolled back (conn remains in active status, not updated)
        await session.rollback()
        await session.refresh(conn)
        assert conn.status == "active"

@pytest.mark.asyncio
async def test_rotation_prunes_old_versions(session, mock_secrets_client):
    """Test 41: Verify rotation prunes old secret versions in Secret Manager."""
    try:
        from app.services.oauth import OauthService
    except ImportError:
        pytest.skip("OauthService not implemented yet (expected Red state)")

    # We don't reassign the mock attribute, as it is already patched on SecretManagerClient in conftest
    
    service = OauthService(tenant_id="t1")
    # Mocking internal prune logic if implemented, or call it directly
    if hasattr(service, "prune_old_versions"):
        await service.prune_old_versions("projects/test-project/secrets/s1/versions/2")
        mock_secrets_client.delete_secret.assert_called_with("projects/test-project/secrets/s1/versions/1")
    else:
        pytest.skip("Pruning old secret versions not implemented yet (expected Red state)")

# ---------------------------------------------------------
# Tier 2: End-to-End OAuth Scenarios and Security
# ---------------------------------------------------------

@pytest.mark.asyncio
@patch("app.services.brand_identity.VertexAIClient")
async def test_complete_oauth_flow(mock_llm_cls, client, session, mock_secrets_client):
    """Test 48: Verify full OAuth flow: authorize redirect, code callback, token exchange, and active state."""
    # Mock Shopify products response
    shopify_products_response = {
        "products": [
            {
                "title": "Weighted Sensory Blanket",
                "product_type": "Sensory Toy",
                "body_html": "<p>Empathy-centered calming blanket for sensory seeking toddlers.</p>"
            }
        ]
    }
    # Mock Gemini response
    gemini_identity_response = {
        "tone_of_voice": "Empathetic, sensory-friendly, clinical",
        "target_persona": "Parents of sensory-seeking children",
        "past_experience": "Avoid discounts."
    }
    mock_llm = MagicMock()
    async def mock_generate(*args, **kwargs):
        return json.dumps(gemini_identity_response)
    mock_llm.generate_personalized_content.side_effect = mock_generate
    mock_llm_cls.return_value = mock_llm

    session.add(Tenant(id="t1", name="Test Tenant", hosting_tier="shared"))
    session.add(Brand(id="b1", tenant_id="t1", name="Test Brand"))
    await session.commit()

    # Step 1: GET /connections/oauth/authorize
    auth_resp = await client.get(
        "/connections/oauth/authorize?provider=shopify&brand_id=b1&redirect_uri=https://app.agencyos.com/callback",
        headers={"X-Tenant-ID": "t1"}
    )
    if auth_resp.status_code == 404:
        pytest.skip("OAuth endpoints not implemented yet (expected Red state)")
        
    assert auth_resp.status_code == 302
    location = auth_resp.headers.get("location")
    
    # Extract state from redirect location
    import urllib.parse as urlparse
    parsed = urlparse.urlparse(location)
    queries = urlparse.parse_qs(parsed.query)
    state = queries["state"][0]
    
    # Step 2: GET /connections/oauth/callback with state and code
    with patch("httpx.AsyncClient.post") as mock_post, \
         patch("app.services.brand_identity.AsyncClient") as mock_brand_client_cls:
         
        mock_resp_post = MagicMock()
        mock_resp_post.status_code = 200
        mock_resp_post.json.return_value = {
            "access_token": "shpat_mock_access_token",
            "refresh_token": "mock_refresh_token",
            "expires_in": 3600,
            "scope": "read_products,write_products"
        }
        mock_post.return_value = mock_resp_post
        
        mock_brand_client = MagicMock()
        mock_resp_get = MagicMock()
        mock_resp_get.status_code = 200
        mock_resp_get.json.return_value = shopify_products_response
        mock_brand_client.get = AsyncMock(return_value=mock_resp_get)
        mock_brand_client_cls.return_value.__aenter__.return_value = mock_brand_client
        
        callback_resp = await client.get(
            f"/connections/oauth/callback?code=mock_auth_code&state={state}"
        )
        assert callback_resp.status_code == 302
        assert callback_resp.headers.get("location") == "https://app.agencyos.com/callback"
        
        # The callback proposes the Op. Query and process it using a fresh session.
        from app.models import OpRow
        from app.kernel import loop
        
        fresh_maker = async_sessionmaker(session.bind, expire_on_commit=False)
        async with fresh_maker() as fresh_s:
            stmt_op = select(OpRow).where(OpRow.action == "manage.shopify.connect")
            res_op = await fresh_s.execute(stmt_op)
            op_row = res_op.scalar_one()
            assert op_row.state == "AWAITING_APPROVAL"
            
            # Approve the Op
            await loop.decide(fresh_s, op_row, decision="approve", actor="operator", role="OPERATOR", surface="whatsapp")
            await fresh_s.commit()
            
            # Execute the Op
            await loop._execute_and_verify(fresh_s, op_row)
            await fresh_s.commit()
            
            # Check Connection record created and is active
            stmt = select(Connection).where(Connection.tenant_id == "t1", Connection.provider == "shopify")
            res = await fresh_s.execute(stmt)
            conn = res.scalar_one()
            assert conn.status == "active"
            assert conn.credential is not None

            # Check BrandProperty (brand_identity) was created
            stmt_prop = select(BrandProperty).where(
                BrandProperty.tenant_id == "t1",
                BrandProperty.brand_id == "b1",
                BrandProperty.type == "brand_identity"
            )
            res_prop = await fresh_s.execute(stmt_prop)
            bp = res_prop.scalar_one_or_none()
            assert bp is not None
            assert bp.status == "active"
            assert bp.findings["tone_of_voice"] == "Empathetic, sensory-friendly, clinical"

@pytest.mark.asyncio
async def test_callback_code_exchange_failure(client, session, mock_secrets_client):
    """Test 49: Verify callback handles token exchange failure gracefully, showing error page."""
    session.add(Tenant(id="t1", name="Test Tenant", hosting_tier="shared"))
    session.add(Brand(id="b1", tenant_id="t1", name="Test Brand"))
    await session.commit()

    # Step 1: Generate a valid state
    try:
        from app.services.oauth import generate_oauth_state
        state = await generate_oauth_state("t1", "b1", "https://app.agencyos.com/callback")
    except ImportError:
        pytest.skip("State signing functions not implemented yet (expected Red state)")

    # Step 2: Callback with code but token exchange fails
    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant", "error_description": "Authorization code expired."}
        mock_post.return_value = mock_resp
        
        resp = await client.get(f"/connections/oauth/callback?code=expired_code&state={state}")
        if resp.status_code == 404:
            pytest.skip("Callback endpoint not implemented yet (expected Red state)")
            
        assert resp.status_code == 400
        assert "Authorization code expired" in resp.text

@pytest.mark.asyncio
async def test_cross_tenant_session_attack(client, session, mock_secrets_client):
    """Test 50: Verify that state tokens generated for tenant A cannot be used by tenant B (state hijacking protection)."""
    session.add(Tenant(id="tenant-A", name="Tenant A", hosting_tier="shared"))
    session.add(Tenant(id="tenant-B", name="Tenant B", hosting_tier="shared"))
    session.add(Brand(id="b1", tenant_id="tenant-A", name="Brand A"))
    await session.commit()

    try:
        from app.services.oauth import generate_oauth_state
        # Generate state for tenant A
        state_tenant_A = await generate_oauth_state("tenant-A", "b1", "https://app.agencyos.com/callback")
    except ImportError:
        pytest.skip("State signing functions not implemented yet (expected Red state)")

    # Callback is invoked, but caller's active session/context is tenant B
    # Callback endpoint should verify the tenant context matches the state payload
    resp = await client.get(
        f"/connections/oauth/callback?code=mock_code&state={state_tenant_A}",
        headers={"X-Tenant-ID": "tenant-B"}
    )
    if resp.status_code == 404:
        pytest.skip("Callback endpoint not implemented yet (expected Red state)")
        
    # Should reject the callback with 400 Bad Request / state mismatch
    assert resp.status_code == 400
    assert "Tenant mismatch" in resp.text or "state" in resp.text.lower()

@pytest.mark.asyncio
async def test_connection_scope_verification(client, session, mock_secrets_client):
    """Test 51: Verify callback rejects token exchanges that don't return all required scopes."""
    session.add(Tenant(id="t1", name="Test Tenant", hosting_tier="shared"))
    session.add(Brand(id="b1", tenant_id="t1", name="Test Brand"))
    await session.commit()

    try:
        from app.services.oauth import generate_oauth_state
        state = await generate_oauth_state("t1", "b1", "https://app.agencyos.com/callback")
    except ImportError:
        pytest.skip("State signing functions not implemented yet (expected Red state)")

    with patch("httpx.AsyncClient.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # Shopify callback returns scopes, let's return limited scopes
        mock_resp.json.return_value = {
            "access_token": "mock_token",
            "scope": "read_products",  # Missing write_products
            "expires_in": 3600
        }
        mock_post.return_value = mock_resp
        
        resp = await client.get(f"/connections/oauth/callback?code=mock_code&state={state}")
        if resp.status_code == 404:
            pytest.skip("Callback endpoint not implemented yet (expected Red state)")
            
        assert resp.status_code == 400
        assert "scope" in resp.text.lower() or "permission" in resp.text.lower()

def test_open_redirect_bypass_payloads():
    """Test 52: Verify redirect URI validation rejects advanced open redirect bypass payloads."""
    try:
        from app.services.oauth import validate_redirect_uri
    except ImportError:
        pytest.skip("Redirect validation helper not implemented yet (expected Red state)")

    # Bypasses using subdomains or path traversal
    assert validate_redirect_uri("https://app.agencyos.com.attacker.com/bypass") is False
    assert validate_redirect_uri("https://attacker.com/app.agencyos.com") is False
    assert validate_redirect_uri("https://app.agencyos.com@attacker.com/bypass") is False
    assert validate_redirect_uri("https://app.agencyos.com\\@attacker.com/bypass") is False
    assert validate_redirect_uri("https://localhost.attacker.com") is False

@pytest.mark.asyncio
async def test_oauth_authorize_redirect_generation_with_explicit_shop(client, session):
    """Verify authorize endpoint with explicit shop handle targets that store instead of brand_id."""
    session.add(Tenant(id="t1", name="Test Tenant", hosting_tier="shared"))
    session.add(Brand(id="b1", tenant_id="t1", name="Test Brand"))
    await session.commit()

    resp = await client.get(
        "/connections/oauth/authorize?provider=shopify&brand_id=b1&redirect_uri=https://app.agencyos.com/callback&shop=ableys",
        headers={"X-Tenant-ID": "t1"}
    )
    assert resp.status_code == 302
    location = resp.headers.get("location")
    assert "ableys.myshopify.com/admin/oauth/authorize" in location
    assert "b1.myshopify.com" not in location


def test_salesforce_oauth_domain_fallback():
    """Verify Salesforce OAuth URL fallback logic when custom_domain is or isn't passed."""
    from app.services.oauth_registry import OauthProviderRegistry
    
    # 1. Without custom_domain -> falls back to login.salesforce.com
    auth_url = OauthProviderRegistry.get_authorize_url("salesforce", "state123", "https://app/callback")
    assert "https://login.salesforce.com/services/oauth2/authorize" in auth_url
    
    token_url, _ = OauthProviderRegistry.get_exchange_payload("salesforce", "code123", "https://app/callback")
    assert token_url == "https://login.salesforce.com/services/oauth2/token"
    
    # 2. With custom_domain -> uses custom_domain
    auth_url_custom = OauthProviderRegistry.get_authorize_url("salesforce", "state123", "https://app/callback", custom_domain="mycorp.my.salesforce.com")
    assert "https://mycorp.my.salesforce.com/services/oauth2/authorize" in auth_url_custom
    
    token_url_custom, _ = OauthProviderRegistry.get_exchange_payload("salesforce", "code123", "https://app/callback", custom_domain="mycorp.my.salesforce.com")
    assert token_url_custom == "https://mycorp.my.salesforce.com/services/oauth2/token"


@pytest.mark.asyncio
async def test_callback_provider_denial_clean_failure(client):
    """Verify callback handles provider denial cleanly with 400."""
    resp = await client.get(
        "/connections/oauth/callback?error=access_denied&error_description=User+denied+access&state=some_state"
    )
    assert resp.status_code == 400
    assert "access_denied" in resp.text
    assert "User denied access" in resp.text


# ---------------------------------------------------------
# Security Invariant and Fail-Closed Regression Suite
# ---------------------------------------------------------

@pytest.mark.asyncio
async def test_rotation_fails_closed_without_tenant_secret_project(session):
    """Verify token rotation fails closed when tenant configuration is missing."""
    tenant = Tenant(id="t-no-config", name="No Config Tenant", gcp_project=None)
    session.add(tenant)
    brand = Brand(id="b-no-config", tenant_id="t-no-config", name="Brand")
    session.add(brand)
    conn = Connection(
        tenant_id="t-no-config", brand_id="b-no-config", provider="shopify",
        credential="projects/test/secrets/s1/versions/1", status="active",
        config={"refresh_token": "rt"}
    )
    session.add(conn)
    await session.commit()

    from app.adapters.manage import ManageAdapter
    from app.kernel.optypes import OpSpec, Severity, Reversibility
    adapter = ManageAdapter()
    spec = OpSpec(
        tenant_id="t-no-config",
        brand_id="b-no-config",
        domain="manage",
        action="manage.connection.rotate",
        params={"provider": "shopify"},
        severity=Severity(impact=1, reversibility=Reversibility.COMPENSATABLE)
    )
    res = await adapter.execute(spec, idem_key="idem-no-config", session=session)
    assert res.ok is False


@pytest.mark.asyncio
async def test_rotation_does_not_fall_back_to_control_plane_project():
    """Verify tenant SecretManagerClient strictly preserves tenant GCP project isolation."""
    from app.services.secrets import SecretManagerClient
    client = SecretManagerClient(tenant_id="tenant-alpha", project_id="tenant-alpha-gcp")
    assert client.project_id == "tenant-alpha-gcp"
    assert client.tenant_id == "tenant-alpha"
    assert client.project_id != "aos-control-plane"


@pytest.mark.asyncio
async def test_audit_error_classification_no_refresh_on_403(session, mock_secrets_client):
    """Verify provider 403 Forbidden does NOT trigger an automatic refresh loop."""
    tenant = Tenant(id="t1", name="Test Tenant", gcp_project="tenant-a-secrets")
    session.add(tenant)
    brand = Brand(id="b1", tenant_id="t1", name="Test Brand")
    session.add(brand)
    conn = Connection(
        tenant_id="t1", brand_id="b1", provider="google-search-console",
        credential="projects/tenant-a-secrets/secrets/sc-secret/versions/1", status="active"
    )
    session.add(conn)
    await session.commit()

    with patch("app.services.oauth.OauthService.refresh_token") as mock_refresh, \
         patch("httpx.AsyncClient.post") as mock_post:
        resp = MagicMock()
        resp.status_code = 403
        resp.text = "Forbidden: Insufficient permissions"
        mock_post.return_value = resp

        from app.services.google_audit import GoogleSearchConsoleAudit
        audit = GoogleSearchConsoleAudit(tenant_id="t1", brand_id="b1", session=session)
        with pytest.raises(Exception):
            await audit.run()
        mock_refresh.assert_not_called()
