import pytest
import asyncio
from app.adapters.grow import GrowAdapter
from app.kernel.optypes import OpSpec, Severity, Reversibility, Money
from app.services.marketing import MockMarketingClient

@pytest.fixture
def adapter():
    return GrowAdapter()

@pytest.fixture(autouse=True)
def clear_marketing_client():
    MockMarketingClient.clear()
    yield
    MockMarketingClient.clear()

@pytest.fixture
def create_intent():
    return "create ad campaign SummerSale budget 10000 bid 150"

@pytest.fixture
def create_op(adapter, create_intent):
    ops = adapter.plan(create_intent, "t1", "b1")
    assert len(ops) == 1
    return ops[0]

def test_grow_adapter_plan(adapter, create_intent):
    ops = adapter.plan(create_intent, "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "grow.campaign.create"
    assert op.params["name"] == "summersale"
    assert op.params["budget_minor"] == 1000000
    assert op.params["bid_minor"] == 15000
    assert op.params["campaign_id"] == "camp-b1-summersale"

def test_grow_adapter_preview(adapter, create_op):
    preview_art = adapter.preview(create_op)
    assert preview_art.kind == "campaign_create_preview"
    assert "summersale" in preview_art.summary
    assert "10000.00 INR" in preview_art.summary
    assert "150.00 INR" in preview_art.summary

async def test_grow_adapter_execute_create(adapter, create_op):
    res = await adapter.execute(create_op, "idem_create_123")
    assert res.ok is True
    assert res.detail["campaign_id"] == "camp-b1-summersale"
    
    client = MockMarketingClient()
    camp = await client.get_campaign("camp-b1-summersale")
    assert camp is not None
    assert camp["name"] == "summersale"
    assert camp["budget_minor"] == 1000000

@pytest.mark.asyncio
async def test_grow_adapter_verify(adapter, create_op):
    client = MockMarketingClient()
    await client.create_campaign("camp-b1-summersale", "summersale", 1000000, 15000)

    verdict = await adapter.verify(create_op)
    assert verdict.ok is True
    assert verdict.checks["campaign_active"] is True

def test_grow_adapter_compensate(adapter, create_op):
    compensations = adapter.compensate(create_op)
    assert len(compensations) == 1
    comp = compensations[0]
    assert comp.action == "grow.campaign.delete"
    assert comp.params["campaign_id"] == "camp-b1-summersale"

async def test_grow_adapter_execute_delete(adapter, create_op):
    client = MockMarketingClient()
    await client.create_campaign("camp-b1-summersale", "summersale", 1000000, 15000)
    
    compensations = adapter.compensate(create_op)
    delete_op = compensations[0]
    
    res = await adapter.execute(delete_op, "idem_delete_123")
    assert res.ok is True
    
    camp = await client.get_campaign("camp-b1-summersale")
    assert camp is None


def test_grow_adapter_plan_bid_adjust(adapter):
    ops = adapter.plan("adjust bid of campaign camp-b1-summersale to 200", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "grow.bid.adjust"
    assert op.params["campaign_id"] == "camp-b1-summersale"
    assert op.params["new_bid_minor"] == 20000
    assert op.params["previous_bid_minor"] == 5000


async def test_grow_adapter_execute_and_verify_bid_adjust(adapter):
    client = MockMarketingClient()
    await client.create_campaign("camp-b1-summersale", "summersale", 1000000, 15000)
    
    ops = adapter.plan("adjust bid of campaign camp-b1-summersale to 200", "t1", "b1")
    op = ops[0]
    
    res = await adapter.execute(op, "idem_bid_123")
    assert res.ok is True
    
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["bid_adjusted"] is True
    
    camp = await client.get_campaign("camp-b1-summersale")
    assert camp["bid_minor"] == 20000


def test_grow_adapter_compensate_bid_adjust(adapter):
    op = OpSpec(
        id="op_bid_123",
        tenant_id="t1",
        brand_id="b1",
        domain="grow",
        action="grow.bid.adjust",
        params={
            "campaign_id": "camp-b1-summersale",
            "new_bid_minor": 20000,
            "previous_bid_minor": 15000,
            "provider": "google-ads"
        },
        severity=Severity(impact=1, reversibility=Reversibility.COMPENSATABLE),
        cost_estimate=Money(amount_minor=0, currency="INR")
    )
    compensations = adapter.compensate(op)
    assert len(compensations) == 1
    comp = compensations[0]
    assert comp.action == "grow.bid.adjust"
    assert comp.params["campaign_id"] == "camp-b1-summersale"
    assert comp.params["new_bid_minor"] == 15000
    assert comp.params["previous_bid_minor"] == 20000


def test_grow_adapter_plan_pause_resume(adapter):
    ops = adapter.plan("pause campaign camp-b1-summersale", "t1", "b1")
    assert len(ops) == 1
    assert ops[0].action == "grow.campaign.pause"
    assert ops[0].params["campaign_id"] == "camp-b1-summersale"

    ops2 = adapter.plan("resume campaign camp-b1-summersale", "t1", "b1")
    assert len(ops2) == 1
    assert ops2[0].action == "grow.campaign.resume"
    assert ops2[0].params["campaign_id"] == "camp-b1-summersale"


async def test_grow_adapter_execute_and_verify_pause_resume(adapter):
    client = MockMarketingClient()
    await client.create_campaign("camp-b1-summersale", "summersale", 1000000, 15000)

    # Pause
    pause_op = adapter.plan("pause campaign camp-b1-summersale", "t1", "b1")[0]
    res = await adapter.execute(pause_op, "idem_pause_123")
    assert res.ok is True
    
    verdict = await adapter.verify(pause_op)
    assert verdict.ok is True
    assert verdict.checks["campaign_paused"] is True

    camp = await client.get_campaign("camp-b1-summersale")
    assert camp["status"] == "PAUSED"

    # Resume
    resume_op = adapter.plan("resume campaign camp-b1-summersale", "t1", "b1")[0]
    res2 = await adapter.execute(resume_op, "idem_resume_123")
    assert res2.ok is True

    verdict2 = await adapter.verify(resume_op)
    assert verdict2.ok is True
    assert verdict2.checks["campaign_resumed"] is True

    camp2 = await client.get_campaign("camp-b1-summersale")
    assert camp2["status"] == "ACTIVE"


def test_grow_adapter_compensate_pause_resume(adapter):
    pause_op = adapter.plan("pause campaign camp-b1-summersale", "t1", "b1")[0]
    comp_pause = adapter.compensate(pause_op)
    assert len(comp_pause) == 1
    assert comp_pause[0].action == "grow.campaign.resume"
    assert comp_pause[0].params["campaign_id"] == "camp-b1-summersale"

    resume_op = adapter.plan("resume campaign camp-b1-summersale", "t1", "b1")[0]
    comp_resume = adapter.compensate(resume_op)
    assert len(comp_resume) == 1
    assert comp_resume[0].action == "grow.campaign.pause"
    assert comp_resume[0].params["campaign_id"] == "camp-b1-summersale"


def test_grow_adapter_plan_alert(adapter):
    ops = adapter.plan("alert Budget mismatch found on camp-1", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "grow.alert.dispatch"
    assert op.params["message"] == "budget mismatch found on camp-1"


async def test_grow_adapter_execute_and_verify_alert(adapter):
    ops = adapter.plan("alert Budget mismatch", "t1", "b1")
    op = ops[0]
    
    res = await adapter.execute(op, "idem_alert_123")
    assert res.ok is True
    assert res.detail["message"] == "Alert dispatched"
    
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["alert_dispatched"] is True


def test_grow_adapter_compensate_alert(adapter):
    ops = adapter.plan("alert Budget mismatch", "t1", "b1")
    op = ops[0]
    comp = adapter.compensate(op)
    assert len(comp) == 0

@pytest.mark.asyncio
async def test_grow_adapter_audience_create(adapter):
    # Plan
    ops = adapter.plan("create audience HighValueUsers lookalike 2%", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "grow.audience.create"
    assert op.params["audience_name"] == "HighValueUsers"
    assert op.params["lookalike_params"]["ratio"] == "2%"

    # Preview
    preview_art = adapter.preview(op)
    assert preview_art.kind == "audience_create_preview"
    assert "HighValueUsers" in preview_art.summary

    # Execute
    res = await adapter.execute(op, "idem_aud_123")
    assert res.ok is True
    assert "audience_id" in res.detail

    # Verify & Compensate
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["audience_created"] is True
    
    assert len(adapter.compensate(op)) == 0

@pytest.mark.asyncio
async def test_grow_adapter_keyword_bid_strategy(adapter):
    # Plan
    ops = adapter.plan("set keyword bid strategy for campaign camp-123 target_roas value 4.5", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "grow.strategy.keyword_bid"
    assert op.params["campaign_id"] == "camp-123"
    assert op.params["strategy_type"] == "target_roas"
    assert op.params["value"] == 4.5

    # Preview
    preview_art = adapter.preview(op)
    assert preview_art.kind == "strategy_keyword_bid_preview"

    # Execute
    res = await adapter.execute(op, "idem_strategy_123")
    assert res.ok is True

    # Verify
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["bid_strategy_applied"] is True

@pytest.mark.asyncio
async def test_grow_adapter_creative_audit(adapter):
    # Plan
    ops = adapter.plan("audit creative performance for campaign camp-123", "t1", "b1")
    assert len(ops) == 1
    op = ops[0]
    assert op.action == "grow.audit.creative"
    assert op.params["campaign_id"] == "camp-123"

    # Preview
    preview_art = adapter.preview(op)
    assert preview_art.kind == "audit_creative_preview"

    # Execute
    res = await adapter.execute(op, "idem_audit_123")
    assert res.ok is True
    assert res.detail["total_creatives_audited"] == 2
    assert len(res.detail["underperforming_creatives"]) == 1

    # Verify
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["creative_audited"] is True

@pytest.mark.asyncio
async def test_grow_adapter_execute_and_verify_budget_reallocate(adapter):
    client = MockMarketingClient()
    await client.create_campaign("camp-src", "source-camp", 200000, 15000)
    await client.create_campaign("camp-tgt", "target-camp", 100000, 15000)
    
    op = OpSpec(
        id="op_reallocate_123",
        tenant_id="t1",
        brand_id="b1",
        domain="grow",
        action="grow.budget.reallocate",
        params={
            "source_campaign_id": "camp-src",
            "target_campaign_id": "camp-tgt",
            "transfer_amount_minor": 50000,
            "provider": "google-ads"
        },
        severity=Severity(impact=1, reversibility=Reversibility.REVERSIBLE),
        cost_estimate=Money(amount_minor=0, currency="INR")
    )
    
    res = await adapter.execute(op, "idem_reallocate_123")
    assert res.ok is True
    
    verdict = await adapter.verify(op)
    assert verdict.ok is True
    assert verdict.checks["budget_reallocated"] is True
    
    src_camp = await client.get_campaign("camp-src")
    tgt_camp = await client.get_campaign("camp-tgt")
    assert src_camp["budget_minor"] == 150000
    assert tgt_camp["budget_minor"] == 150000

def test_grow_adapter_compensate_budget_reallocate(adapter):
    op = OpSpec(
        id="op_reallocate_123",
        tenant_id="t1",
        brand_id="b1",
        domain="grow",
        action="grow.budget.reallocate",
        params={
            "source_campaign_id": "camp-src",
            "target_campaign_id": "camp-tgt",
            "transfer_amount_minor": 50000,
            "provider": "google-ads"
        },
        severity=Severity(impact=1, reversibility=Reversibility.REVERSIBLE),
        cost_estimate=Money(amount_minor=0, currency="INR")
    )
    compensations = adapter.compensate(op)
    assert len(compensations) == 1
    comp = compensations[0]
    assert comp.action == "grow.budget.reallocate"
    assert comp.params["source_campaign_id"] == "camp-tgt"
    assert comp.params["target_campaign_id"] == "camp-src"
    assert comp.params["transfer_amount_minor"] == 50000




