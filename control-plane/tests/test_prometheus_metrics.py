import re
import pytest
import datetime as dt
from sqlalchemy import select
from app.kernel import loop
from app.kernel.optypes import OpSpec, Severity, Reversibility
from app.models import Tenant, Brand, Connection, OpRow, OutboxItem, CircuitBreakerRow
from app.observability import trace_context
from app.services.marketing import MockMarketingClient
from app.services.secrets import SecretManagerClient

@pytest.fixture(autouse=True)
def clean_mock_clients():
    MockMarketingClient.clear()
    yield
    MockMarketingClient.clear()


@pytest.fixture
async def setup_tenant_brand(session):
    tenant = Tenant(id="t_metrics", name="Metrics Tenant", hosting_tier="shared")
    brand = Brand(id="b_metrics", tenant_id="t_metrics", name="Metrics Brand")
    session.add(tenant)
    brand_obj = brand
    session.add(brand_obj)
    await session.commit()
    return tenant, brand_obj


def get_metric_value(metrics_text, metric_name, labels=None):
    """Helper to parse a specific metric value from Prometheus text output."""
    if labels:
        label_parts = []
        for k, v in sorted(labels.items()):
            label_parts.append(f'{k}="{v}"')
        label_str = ",".join(label_parts)
        pattern = rf'{metric_name}\{{{label_str}\}}\s+([\d\.e\+]+)'
    else:
        pattern = rf'{metric_name}\s+([\d\.e\+]+)'
    
    match = re.search(pattern, metrics_text)
    if match:
        return float(match.group(1))
    return 0.0


@pytest.mark.asyncio
async def test_prometheus_connector_metrics(session, client, setup_tenant_brand):
    """Verify that connector operations (connect, verify, rotate) increment the aos_connector_operations_total counter."""
    tenant, brand = setup_tenant_brand

    # Get baseline metrics before execution
    response_before = await client.get("/metrics")
    assert response_before.status_code == 200
    val_connect_before = get_metric_value(response_before.text, "aos_connector_operations_total", {"operation": "connect", "provider": "shopify", "result": "success"})
    val_verify_before = get_metric_value(response_before.text, "aos_connector_operations_total", {"operation": "verify", "provider": "shopify", "result": "success"})

    # 1. Propose and execute a connect operation (manage.shopify.connect)
    spec_connect = OpSpec(
        tenant_id=tenant.id,
        brand_id=brand.id,
        domain="manage",
        action="manage.shopify.connect",
        params={
            "provider": "shopify",
            "credential": "projects/test-project/secrets/shopify-token/versions/1",
            "config": {"shop_url": "test-shop.myshopify.com"}
        },
        severity=Severity(1, Reversibility.REVERSIBLE)
    )
    
    # We must mock write_secret to prevent actual Secret Manager calls in tests
    secrets_client = SecretManagerClient()
    original_write = secrets_client.write_secret
    original_read = secrets_client.read_secret
    
    async def mock_write(self, secret_id, val):
        return f"mocked-ref-for-{secret_id}"
    async def mock_read(self, secret_ref):
        return "mocked-token-value"
        
    SecretManagerClient.write_secret = mock_write
    SecretManagerClient.read_secret = mock_read

    try:
        row_connect = await loop.propose(session, spec_connect, actor="test")
        await loop.preview_and_gate(session, row_connect, tier=1)
        await session.commit()

        # Run execute directly to verify metric increment
        adapter = loop.REGISTRY["manage"]
        res_connect = await adapter.execute(spec_connect, "idem-connect", session=session)
        assert res_connect.ok
        await session.commit()

        # 2. Run verification (manage.connection.verify)
        spec_verify = OpSpec(
            tenant_id=tenant.id,
            brand_id=brand.id,
            domain="manage",
            action="manage.connection.verify",
            params={"provider": "shopify"},
            severity=Severity(1, Reversibility.REVERSIBLE)
        )
        # Mock McpClient call_tool to return mock shop info
        from app.adapters.manage import McpClient
        original_call = McpClient.call_tool
        async def mock_call(self, name, args):
            import json
            return {"content": [{"text": json.dumps({"name": "Test Shop"})}]}
        McpClient.call_tool = mock_call
        
        try:
            res_verify = await adapter.execute(spec_verify, "idem-verify", session=session)
            assert res_verify.ok
            await session.commit()
        finally:
            McpClient.call_tool = original_call

        # 3. Check /metrics endpoint for connector counters and assert relative increase
        response_after = await client.get("/metrics")
        assert response_after.status_code == 200
        val_connect_after = get_metric_value(response_after.text, "aos_connector_operations_total", {"operation": "connect", "provider": "shopify", "result": "success"})
        val_verify_after = get_metric_value(response_after.text, "aos_connector_operations_total", {"operation": "verify", "provider": "shopify", "result": "success"})
        
        assert val_connect_after - val_connect_before == 1.0
        assert val_verify_after - val_verify_before == 1.0
    finally:
        SecretManagerClient.write_secret = original_write
        SecretManagerClient.read_secret = original_read


@pytest.mark.asyncio
async def test_prometheus_outbox_dead_gauge(session, client, setup_tenant_brand):
    """Verify that aos_outbox_dead_gauge accurately exposes the count of DEAD outbox items."""
    tenant, brand = setup_tenant_brand

    # Insert a DEAD outbox item
    dead_item = OutboxItem(
        op_id="op_dead_test",
        tenant_id=tenant.id,
        status="DEAD",
        attempts=5
    )
    session.add(dead_item)
    await session.commit()

    # Query metrics (Gauges are absolute and reset per DB session, so exact match is safe)
    response = await client.get("/metrics")
    assert response.status_code == 200
    val_dead = get_metric_value(response.text, "aos_outbox_dead_gauge")
    assert val_dead == 1.0


@pytest.mark.asyncio
async def test_prometheus_circuit_breaker_trips(session, client, setup_tenant_brand):
    """Verify that aos_circuit_breaker_trips_total increments when a circuit breaker transitions from CLOSED to OPEN."""
    tenant, brand = setup_tenant_brand

    # Trigger circuit breaker trips on consecutive failures (threshold is 3)
    # Consecutive failure 1
    await loop.record_failure(session, tenant.id, brand.id, "grow", max_failures=3)
    # Consecutive failure 2
    await loop.record_failure(session, tenant.id, brand.id, "grow", max_failures=3)
    
    # Get baseline count before tripping
    response_before = await client.get("/metrics")
    val_before = get_metric_value(response_before.text, "aos_circuit_breaker_trips_total", {"domain": "grow"})

    # Consecutive failure 3 -> transitions to OPEN (tripped!)
    await loop.record_failure(session, tenant.id, brand.id, "grow", max_failures=3)

    # Let's assert the breaker is actually OPEN in the DB
    stmt = select(CircuitBreakerRow).where(
        CircuitBreakerRow.tenant_id == tenant.id,
        CircuitBreakerRow.brand_id == brand.id,
        CircuitBreakerRow.domain == "grow"
    )
    res = await session.execute(stmt)
    breaker = res.scalar_one()
    assert breaker.state == "OPEN"
    assert breaker.consecutive_failures == 3

    # Query metrics and assert delta increase
    response_after = await client.get("/metrics")
    assert response_after.status_code == 200
    val_after = get_metric_value(response_after.text, "aos_circuit_breaker_trips_total", {"domain": "grow"})
    
    assert val_after - val_before == 1.0


@pytest.mark.asyncio
async def test_prometheus_approval_latency_seconds(session, client, setup_tenant_brand):
    """Verify that human approvals observe the decision latency in the aos_approval_latency_seconds histogram."""
    tenant, brand = setup_tenant_brand

    # 1. Propose an operation
    spec = OpSpec(
        tenant_id=tenant.id,
        brand_id=brand.id,
        domain="grow",
        action="grow.campaign.create",
        params={"campaign_name": "summer-promo", "budget_minor": 100_000, "provider": "google-ads"},
        severity=Severity(1, Reversibility.REVERSIBLE)
    )
    row = await loop.propose(session, spec, actor="test")
    await loop.preview_and_gate(session, row, tier=1)
    await session.commit()

    # Get baseline metrics before deciding
    response_before = await client.get("/metrics")
    count_before = get_metric_value(response_before.text, "aos_approval_latency_seconds_count", {"action": "grow.campaign.create", "domain": "grow"})
    sum_before = get_metric_value(response_before.text, "aos_approval_latency_seconds_sum", {"action": "grow.campaign.create", "domain": "grow"})

    # 2. Make a decision (approve) after simulating a delay
    await loop.decide(
        session, 
        row, 
        decision="approve", 
        actor="operator-jack", 
        role="operator", 
        surface="web", 
        latency_ms=15000  # 15 seconds
    )
    await session.commit()

    # 3. Query /metrics and assert relative delta increases
    response_after = await client.get("/metrics")
    assert response_after.status_code == 200
    count_after = get_metric_value(response_after.text, "aos_approval_latency_seconds_count", {"action": "grow.campaign.create", "domain": "grow"})
    sum_after = get_metric_value(response_after.text, "aos_approval_latency_seconds_sum", {"action": "grow.campaign.create", "domain": "grow"})
    
    assert count_after - count_before == 1.0
    assert sum_after - sum_before == pytest.approx(15.0)
