import pytest
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock
from app.main import app
from app.database import get_db, get_worker_db

@pytest.mark.asyncio
async def test_healthz_returns_200():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        response = await ac.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

@pytest.mark.asyncio
async def test_readyz_returns_200_when_db_up(client):
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "onboarded": False}

@pytest.mark.asyncio
async def test_readyz_returns_503_when_db_down():
    # Override get_worker_db to return a failing session mock
    async def override_get_db_fail():
        mock_session = AsyncMock()
        mock_session.execute.side_effect = Exception("Simulated DB connection failure")
        yield mock_session

    app.dependency_overrides[get_worker_db] = override_get_db_fail
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
            response = await ac.get("/readyz")
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "unready"
            assert "Simulated DB connection failure" in data["error"]
    finally:
        # Clean up dependency override
        app.dependency_overrides.pop(get_worker_db, None)
