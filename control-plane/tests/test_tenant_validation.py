import pytest
from httpx import AsyncClient
from app.middleware import VALID_TENANTS_CACHE
from app.main import app

@pytest.mark.asyncio
async def test_tenant_validation_lifecycle(client: AsyncClient, session):
    # Enable tenant validation strictly for this security test
    app.state.bypass_tenant_validation = False
    
    try:
        # Ensure a clean slate for the cache
        VALID_TENANTS_CACHE.clear()
        
        # 1. Create a tenant (requires operator auth, write-through caching should trigger)
        r = await client.post(
            "/tenants",
            headers={"Authorization": "Bearer default-dev-token"},
            json={"name": "Test Tenant", "brand_name": "Test Brand"}
        )
        assert r.status_code == 200
        tenant_id = r.json()["tenant_id"]
        brand_id = r.json()["brand_id"]
        
        # Assert Write-Through: The ID must be immediately in the cache!
        assert tenant_id in VALID_TENANTS_CACHE
        
        # 2. Clear the cache manually to test database fallback (slow-path)
        VALID_TENANTS_CACHE.clear()
        assert tenant_id not in VALID_TENANTS_CACHE
        
        # Hit a tenant-gated endpoint. The database must be queried, and the ID must be cached.
        r = await client.get("/actions/catalog", headers={"X-Tenant-ID": tenant_id})
        assert r.status_code == 200
        
        # Assert that the ID was added back to the cache via database fallback
        assert tenant_id in VALID_TENANTS_CACHE
        
        # 3. Test Invalid Tenant (rejection)
        r = await client.get("/actions/catalog", headers={"X-Tenant-ID": "unregistered-tenant-999"})
        assert r.status_code == 401
        assert r.json()["detail"] == "Unauthorized: Tenant not registered."
        
        # 4. Assert Cache Fast-Path (DB bypass)
        # If the ID is in the cache, the database should NEVER be queried.
        # We assert this by overriding the app state db_session_maker to a function that raises an error.
        # If the DB is hit, the request will fail with a 500 (fail-closed).
        # If the cache is hit, the request will succeed with 200.
        assert tenant_id in VALID_TENANTS_CACHE
        
        def raise_db_error(*args, **kwargs):
            raise RuntimeError("CRITICAL FAILURE: Database was queried on a cached fast-path!")
            
        original_session_maker = app.state.db_session_maker
        app.state.db_session_maker = raise_db_error
        
        try:
            r = await client.get("/actions/catalog", headers={"X-Tenant-ID": tenant_id})
            # The request must succeed because the database was bypassed!
            assert r.status_code == 200, "Database was hit during a cached fast-path!"
        finally:
            # Restore the original session maker
            app.state.db_session_maker = original_session_maker
            
        # 5. Assert Fail-Closed Safeguard
        # If the cache is a miss, and the database query fails, it must return 500 (fail-closed)
        VALID_TENANTS_CACHE.clear()
        assert tenant_id not in VALID_TENANTS_CACHE
        
        app.state.db_session_maker = raise_db_error
        try:
            r = await client.get("/actions/catalog", headers={"X-Tenant-ID": tenant_id})
            # Must fail closed with 500
            assert r.status_code == 500
            assert r.json()["detail"] == "Internal server error during tenant verification."
        finally:
            app.state.db_session_maker = original_session_maker
            
    finally:
        # Restore bypass to ensure other tests in the suite do not break
        app.state.bypass_tenant_validation = True


@pytest.mark.asyncio
async def test_tenant_validation_cache_ttl(client: AsyncClient, session):
    from unittest.mock import patch
    import time
    
    app.state.bypass_tenant_validation = False
    try:
        VALID_TENANTS_CACHE.clear()
        
        # 1. Create a tenant
        r = await client.post(
            "/tenants",
            headers={"Authorization": "Bearer default-dev-token"},
            json={"name": "TTL Tenant", "brand_name": "TTL Brand"}
        )
        assert r.status_code == 200
        tenant_id = r.json()["tenant_id"]
        
        # Verify it's in cache
        assert tenant_id in VALID_TENANTS_CACHE
        
        # Mock database session maker to raise error if hit
        def raise_db_error(*args, **kwargs):
            raise RuntimeError("DB_WAS_HIT")
            
        original_session_maker = app.state.db_session_maker
        
        # 2. Case A: Check cache access within TTL (e.g. +10 seconds).
        # We mock time.monotonic to simulate 10 seconds passing.
        current_time = time.monotonic()
        with patch("time.monotonic", return_value=current_time + 10):
            app.state.db_session_maker = raise_db_error
            try:
                r = await client.get("/actions/catalog", headers={"X-Tenant-ID": tenant_id})
                # Must succeed (200) since it hits the cache
                assert r.status_code == 200
            finally:
                app.state.db_session_maker = original_session_maker

        # 3. Case B: Check cache access past TTL (e.g. +600 seconds).
        # We mock time.monotonic to simulate 10 minutes passing.
        with patch("time.monotonic", return_value=current_time + 600):
            app.state.db_session_maker = raise_db_error
            try:
                r = await client.get("/actions/catalog", headers={"X-Tenant-ID": tenant_id})
                # Must fail-closed with 500 because cache expired and it hit the raise_db_error session maker
                assert r.status_code == 500
                assert r.json()["detail"] == "Internal server error during tenant verification."
            finally:
                app.state.db_session_maker = original_session_maker

    finally:
        app.state.bypass_tenant_validation = True

