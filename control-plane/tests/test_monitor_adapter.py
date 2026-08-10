import pytest
from app.adapters.monitor import MonitorAdapter
from app.kernel.optypes import OpSpec, Severity, Reversibility, Money

@pytest.fixture
def adapter():
    return MonitorAdapter()

def test_monitor_adapter_plan(adapter):
    # health check
    ops = adapter.plan("run health check on system status", "t1", "b1")
    assert len(ops) == 1
    assert ops[0].action == "monitor.health_check"
    assert ops[0].cost_estimate.amount_minor == 0

    # SLA audit
    ops2 = adapter.plan("audit SLA compliance latency", "t1", "b1")
    assert len(ops2) == 1
    assert ops2[0].action == "monitor.sla_audit"

    # Cost forecast
    ops3 = adapter.plan("predict month-end cost forecast", "t1", "b1")
    assert len(ops3) == 1
    assert ops3[0].action == "monitor.cost_forecast"

def test_monitor_adapter_preview(adapter):
    op = adapter.plan("run health check", "t1", "b1")[0]
    preview_art = adapter.preview(op)
    assert preview_art.kind == "monitor_health_preview"
    assert "health check report" in preview_art.summary

    op2 = adapter.plan("audit SLA", "t1", "b1")[0]
    preview_art2 = adapter.preview(op2)
    assert preview_art2.kind == "monitor_sla_preview"

    op3 = adapter.plan("cost forecast", "t1", "b1")[0]
    preview_art3 = adapter.preview(op3)
    assert preview_art3.kind == "monitor_forecast_preview"

@pytest.mark.asyncio
async def test_monitor_adapter_execute(adapter, session):
    # health check
    op = adapter.plan("run health check", "t1", "b1")[0]
    res = await adapter.execute(op, "idem_health_123", session)
    assert res.ok is True
    assert res.detail["status"] == "HEALTHY"

    # SLA audit
    op2 = adapter.plan("audit SLA", "t1", "b1")[0]
    res2 = await adapter.execute(op2, "idem_sla_123", session)
    assert res2.ok is True
    assert res2.detail["compliance_rate"] == "100.0%"

    # Cost forecast
    op3 = adapter.plan("cost forecast", "t1", "b1")[0]
    res3 = await adapter.execute(op3, "idem_forecast_123", session)
    assert res3.ok is True
    assert "projected_month_end_spend_minor" in res3.detail

@pytest.mark.asyncio
async def test_monitor_adapter_verify_and_compensate(adapter):
    op = adapter.plan("run health check", "t1", "b1")[0]
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["execution_logged"] is True

    comp = adapter.compensate(op)
    assert len(comp) == 0
