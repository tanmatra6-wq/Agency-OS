import pytest
import os
import app.main
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.kernel.optypes import OpSpec, Severity, Reversibility, OpState
from app.models import OpRow, Order, TrustSnapshot, TrustEvent
from app.adapters.grow import GrowAdapter
from app.services.marketing import MockMarketingClient

@pytest.fixture(autouse=True)
def clear_marketing_client():
    MockMarketingClient.clear()

@pytest.fixture
def grow_adapter():
    return GrowAdapter()

@pytest.mark.asyncio
async def test_grow_capi_and_budget_reallocation_saga(grow_adapter, session: AsyncSession):
    # Enable test mode to bypass SA OIDC token verification
    os.environ["AOS_ENV"] = "test"
    
    # 1. Prepare: Create completed campaign create Ops for Google Ads and Meta Ads in DB
    google_op = OpRow(
        id="op_camp_google",
        tenant_id="t1",
        brand_id="b1",
        domain="grow",
        action="grow.campaign.create",
        params={
            "campaign_id": "camp-b1-google-sales",
            "name": "google-sales",
            "budget_minor": 500_000, # 5,000 INR
            "bid_minor": 5_000,
            "provider": "google-ads"
        },
        state="DONE",
        impact=2,
        reversibility="COMPENSATABLE",
        idem_key="idem_camp_google"
    )
    session.add(google_op)
    
    meta_op = OpRow(
        id="op_camp_meta",
        tenant_id="t1",
        brand_id="b1",
        domain="grow",
        action="grow.campaign.create",
        params={
            "campaign_id": "camp-b1-meta-sales",
            "name": "meta-sales",
            "budget_minor": 500_000, # 5,000 INR
            "bid_minor": 5_000,
            "provider": "meta-ads"
        },
        state="DONE",
        impact=2,
        reversibility="COMPENSATABLE",
        idem_key="idem_camp_meta"
    )
    session.add(meta_op)
    await session.commit()
    
    # 2. Add campaigns to MockMarketingClient in-memory state so get_performance can query them
    client_google = MockMarketingClient(provider="google-ads")
    await client_google.create_campaign("camp-b1-google-sales", "google-sales", 500_000, 5_000)
    
    client_meta = MockMarketingClient(provider="meta-ads")
    await client_meta.create_campaign("camp-b1-meta-sales", "meta-sales", 500_000, 5_000)
    
    # 3. Add orders to DB:
    # Google campaign has 5,000 INR spend (perf spend is 80% = 4,000 INR)
    # Let's add 4,000 INR revenue to Google campaign -> ROAS = 1.0 (doesn't trigger failure but doesn't trigger success either)
    order_g1 = Order(
        id="ord_g1",
        tenant_id="t1",
        brand_id="b1",
        amount_minor=400000,
        currency="INR",
        attributed_campaign_id="camp-b1-google-sales"
    )
    session.add(order_g1)
    
    # Meta campaign has 5,000 INR spend (perf spend is 80% = 4,000 INR)
    # Let's add 12,000 INR revenue to Meta campaign -> ROAS = 3.0 (verified_success)
    order_m1 = Order(
        id="ord_m1",
        tenant_id="t1",
        brand_id="b1",
        amount_minor=1200000,
        currency="INR",
        attributed_campaign_id="camp-b1-meta-sales"
    )
    session.add(order_m1)
    await session.commit()
    
    # 4. Act: Trigger /tasks/evaluate-trust task logic
    from app.routers.tasks import evaluate_trust
    res = await evaluate_trust(s=session)
    assert res["status"] == "ok"
    
    # Assert TrustEvents were created:
    # Meta should get verified_success (ROAS = 3.0 >= 1.2)
    stmt_events = select(TrustEvent).where(TrustEvent.tenant_id == "t1", TrustEvent.brand_id == "b1")
    q_events = await session.execute(stmt_events)
    events = q_events.scalars().all()
    assert len(events) >= 1
    assert any(e.kind == "verified_success" for e in events)
    
    # 5. Assert budget optimization proposed Saga Op and its child updates were created!
    stmt_saga = select(OpRow).where(OpRow.action == "grow.budget.reallocate", OpRow.state == "AWAITING_APPROVAL")
    q_saga = await session.execute(stmt_saga)
    sagas = q_saga.scalars().all()
    assert len(sagas) == 1
    saga = sagas[0]
    
    assert saga.params["source_campaign_id"] == "camp-b1-google-sales"
    assert saga.params["target_campaign_id"] == "camp-b1-meta-sales"
    assert saga.params["transfer_amount_minor"] == 100_000
    
    # Assert child Ops
    stmt_children = select(OpRow).where(OpRow.parent_op_id == saga.id).order_by(OpRow.sequence_order)
    q_children = await session.execute(stmt_children)
    children = q_children.scalars().all()
    assert len(children) == 2
    
    child1, child2 = children[0], children[1]
    assert child1.state == "AWAITING_APPROVAL"
    assert child2.state == "AWAITING_APPROVAL"
    
    # Child 1: Decrease Google Ads campaign by 1,000 INR (new budget: 4,000 INR = 400_000 minor)
    assert child1.action == "grow.campaign.update"
    assert child1.params["campaign_id"] == "camp-b1-google-sales"
    assert child1.params["budget_minor"] == 400_000
    assert child1.params["provider"] == "google-ads"
    assert child1.params["previous_budget_minor"] == 500_000
    
    # Child 2: Increase Meta Ads campaign by 1,000 INR (new budget: 6,000 INR = 600_000 minor)
    assert child2.action == "grow.campaign.update"
    assert child2.params["campaign_id"] == "camp-b1-meta-sales"
    assert child2.params["budget_minor"] == 600_000
    assert child2.params["provider"] == "meta-ads"
    assert child2.params["previous_budget_minor"] == 500_000
    
    # 6. Act: Approve the Saga and run the loop to execute it sequentially
    from app.kernel import loop
    from app.kernel.loop import decide
    await decide(session, saga, decision="approve", actor="owner", role="AGENCY_OWNER", surface="web")
    await session.commit()
    
    # Now run drain_once to execute Child 1
    processed = await loop.drain_once(session)
    assert processed > 0
    await session.commit()
    
    # Drain again to execute Child 2
    processed2 = await loop.drain_once(session)
    assert processed2 > 0
    await session.commit()
    
    # 7. Assert: Budgets have been updated inside MockMarketingClient!
    camp_g = await client_google.get_campaign("camp-b1-google-sales")
    assert camp_g["budget_minor"] == 400_000
    
    camp_m = await client_meta.get_campaign("camp-b1-meta-sales")
    assert camp_m["budget_minor"] == 600_000
    
    # Check parent Saga is now DONE
    await session.refresh(saga)
    assert saga.state == "DONE"
