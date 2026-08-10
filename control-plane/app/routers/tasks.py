import datetime as dt
import logging
import os
import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text, func

from app.database import get_db, get_worker_db
from app.auth import verify_worker_auth
from app.kernel import loop
from app.models import Cadence, Brand, Order, OpRow, TrustEvent
from app.kernel.services import compute_snapshots

logger = logging.getLogger(__name__)

router = APIRouter(tags=["tasks"], prefix="/tasks", dependencies=[Depends(verify_worker_auth)])


@router.post("/drain-outbox")
async def drain_outbox_task(s: AsyncSession = Depends(get_worker_db)):
    """Background task endpoint to drain the outbox.

    Bypasses RLS by using get_worker_db.
    """
    processed = await loop.drain_once(s)
    return {"status": "ok", "processed_items": processed}


@router.post("/refresh-tokens")
async def refresh_tokens_task(s: AsyncSession = Depends(get_worker_db)):
    """Background task to rotate all expiring OAuth tokens across all tenants.

    Bypasses RLS by using get_worker_db.
    """
    from app.tasks.rotation import rotate_expiring_tokens
    await rotate_expiring_tokens(s)
    return {"status": "ok", "message": "Token rotation completed"}


@router.post("/drift-detect")
async def drift_detect_task(s: AsyncSession = Depends(get_worker_db)):
    """Background task to run periodic configuration drift detection sweeps across all tenants.

    Bypasses RLS by using get_worker_db.
    """
    from app.tasks.drift import run_drift_detection_sweep
    await run_drift_detection_sweep(s)
    return {"status": "ok", "message": "Drift detection sweep completed"}


@router.post("/run-diagnostics")
async def run_diagnostics_task(s: AsyncSession = Depends(get_worker_db)):
    """Background task to run periodic diagnostics log sweeps across all tenants.

    Bypasses RLS by using get_worker_db.
    """
    from app.tasks.diagnostics import run_diagnostics_sweep
    await run_diagnostics_sweep(s)
    return {"status": "ok", "message": "Diagnostics logs sweep completed"}


@router.post("/check-graduations")
async def check_graduations_task(s: AsyncSession = Depends(get_worker_db)):
    """Background task to check for shared tenants exceeding revenue threshold and propose graduation.

    Bypasses RLS by using get_worker_db.
    """
    from app.tasks.graduation import check_and_propose_graduations
    await check_and_propose_graduations(s)
    return {"status": "ok", "message": "Tenant graduation checks completed"}


@router.post("/process-cadences")
async def process_cadences(s: AsyncSession = Depends(get_worker_db)):
    """Periodic task to scan and propose recurring audit Ops from Cadences.

    Bypasses RLS by using get_worker_db to execute across all tenants.
    """
    from app.kernel.optypes import OpSpec, Severity, Reversibility, Money

    now = dt.datetime.now(dt.timezone.utc)

    # Query due cadences
    stmt = select(Cadence).where(Cadence.next_run <= now, Cadence.status.in_(["on_track", "due", "active"]))
    res = await s.execute(stmt)
    due_cadences = res.scalars().all()

    proposed_ops_count = 0
    for cadence in due_cadences:
        # Determine schedule delta
        if cadence.schedule == "daily":
            delta = dt.timedelta(days=1)
        elif cadence.schedule == "weekly":
            delta = dt.timedelta(days=7)
        elif cadence.schedule == "monthly":
            delta = dt.timedelta(days=30)
        else:
            logger.error(f"Unknown schedule type: {cadence.schedule} for cadence {cadence.id}")
            continue

        # Compile OpSpec
        op_spec = OpSpec(
            tenant_id=cadence.tenant_id,
            brand_id=cadence.brand_id,
            domain=cadence.domain,
            action=cadence.action,
            params={"brand_id": cadence.brand_id, "cadence_id": cadence.id},
            severity=Severity(impact=1, reversibility=Reversibility.REVERSIBLE),
            cost_estimate=Money(0)
        )

        # Propose in DB
        row = await loop.propose(s, op_spec, actor="scheduler")

        # Fetch brand trust score to determine current tier
        from app.kernel.services import resolve_brand_tier
        tier = await resolve_brand_tier(s, tenant_id=cadence.tenant_id, brand_id=cadence.brand_id, domain=cadence.domain)

        await loop.preview_and_gate(s, row, tier=tier)
        # Update Cadence scheduling fields
        cadence.last_run = now
        cadence.next_run = now + delta
        cadence.status = "on_track"

        proposed_ops_count += 1

    await s.commit()
    return {"status": "ok", "proposed_ops_count": proposed_ops_count}


@router.post("/trust-snapshots")
async def run_trust_snapshots(s: AsyncSession = Depends(get_worker_db)):
    """Nightly job to calculate and persist trust snapshots for all brands.

    Bypasses RLS by using get_worker_db to execute across all tenants.
    """
    from .kernel.services import compute_snapshots
    await compute_snapshots(s)
    await s.commit()
    return {"status": "ok"}


@router.post("/calibrate-attribution")
async def calibrate_attribution(s: AsyncSession = Depends(get_worker_db)):
    """Runs Meridian calibration to compute incrementality multipliers for all brands.

    Bypasses RLS by using get_worker_db.
    """
    from app.services.attribution import run_meridian_calibration
    
    stmt = select(Brand)
    res = await s.execute(stmt)
    brands = res.scalars().all()
    
    calibrated_count = 0
    for brand in brands:
        await run_meridian_calibration(s, brand.tenant_id, brand.id)
        calibrated_count += 1
        
    await s.commit()
    return {"status": "ok", "calibrated_count": calibrated_count}


@router.post("/evaluate-trust")
async def evaluate_trust(s: AsyncSession = Depends(get_worker_db)):
    """Background task evaluating campaign ROI and adjusting trust scores.

    Bypasses RLS to query across all tenants/brands.
    """
    from app.models import TrustEvent
    from app.services.marketing import MockMarketingClient
    from sqlalchemy import func

    # 1. Fetch all successful campaign creations
    stmt = select(OpRow).where(
        OpRow.action == "grow.campaign.create",
        OpRow.state == "DONE"
    )
    res = await s.execute(stmt)
    ops = res.scalars().all()

    client = MockMarketingClient()
    events_added = 0

    # Store performance results by platform for budget reallocation checks
    platform_performance = {}

    for op in ops:
        campaign_id = op.params.get("campaign_id")
        provider = op.params.get("provider", "google-ads")
        tenant_id = op.tenant_id
        brand_id = op.brand_id

        # Fetch platform spend
        perf = await client.get_performance(campaign_id)
        if not perf:
            continue

        spend_minor = perf.get("spend_minor", 0)
        spend_amount = spend_minor / 100.0

        # Query database orders to calculate total revenue attributed
        stmt_rev = select(func.sum(Order.amount_minor)).where(
            Order.tenant_id == tenant_id,
            Order.brand_id == brand_id,
            Order.attributed_campaign_id == campaign_id
        )
        res_rev = await s.execute(stmt_rev)
        total_revenue = (res_rev.scalar() or 0) / 100.0

        # Calculate real ROAS
        roas = total_revenue / spend_amount if spend_amount > 0 else 0.0
        logger.info(f"Campaign {campaign_id} ({provider}) - Spend: {spend_amount:.2f} INR, Database Revenue: {total_revenue:.2f} INR, ROAS: {roas:.2f}")

        # Store for reallocation comparison
        if provider not in platform_performance:
            platform_performance[provider] = {}
        platform_performance[provider][campaign_id] = {
            "roas": roas,
            "op": op,
            "budget_minor": op.params.get("budget_minor", 500_000)
        }

        # Check trust threshold logic
        kind = None
        if roas >= 1.2:
            kind = "verified_success"
            delta = 5.0
            reason = f"Campaign {campaign_id} DB ROAS {roas:.2f} >= 1.2"
        elif roas < 1.0:
            kind = "verify_failure"
            delta = -10.0
            reason = f"Campaign {campaign_id} DB ROAS {roas:.2f} < 1.0"

        if not kind:
            continue

        # Check duplicate event
        stmt_dup = select(TrustEvent).where(
            TrustEvent.tenant_id == tenant_id,
            TrustEvent.brand_id == brand_id,
            TrustEvent.domain == "grow",
            TrustEvent.kind == kind,
            TrustEvent.reason.like(f"Campaign {campaign_id}%")
        )
        res_dup = await s.execute(stmt_dup)
        dup = res_dup.scalar_one_or_none()
        if dup:
            continue

        # Record event
        event = TrustEvent(
            tenant_id=tenant_id,
            brand_id=brand_id,
            domain="grow",
            kind=kind,
            base_delta=delta,
            reason=reason
        )
        s.add(event)
        events_added += 1
        logger.info(f"Recorded trust event for {brand_id}: {kind} (delta {delta})")

    # 2. Check for budget optimization/reallocation (Cross-channel)
    google_campaigns = platform_performance.get("google-ads", {})
    meta_campaigns = platform_performance.get("meta-ads", {})

    if google_campaigns and meta_campaigns:
        best_meta_id, best_meta = max(meta_campaigns.items(), key=lambda x: x[1]["roas"])
        worst_google_id, worst_google = min(google_campaigns.items(), key=lambda x: x[1]["roas"])

        transfer_amount_minor = 100_000
        if best_meta["roas"] >= 1.5 * worst_google["roas"] and worst_google["budget_minor"] > transfer_amount_minor:
            tenant_id = worst_google["op"].tenant_id
            brand_id = worst_google["op"].brand_id

            stmt_dup_saga = select(OpRow).where(
                OpRow.tenant_id == tenant_id,
                OpRow.brand_id == brand_id,
                OpRow.action == "grow.budget.reallocate",
                OpRow.state == "PROPOSED"
            )
            res_dup_saga = await s.execute(stmt_dup_saga)
            if not res_dup_saga.scalar_one_or_none():
                logger.warning(f"Optimization triggered: Proposing budget reallocation from Google Ads ({worst_google_id}) to Meta Ads ({best_meta_id})")

                from app.kernel.optypes import OpSpec, Severity, Reversibility, Money

                # Propose Parent Saga
                parent_saga = await loop.propose(s, OpSpec(
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    domain="grow",
                    action="grow.budget.reallocate",
                    params={
                        "transfer_amount_minor": transfer_amount_minor,
                        "source_campaign_id": worst_google_id,
                        "source_provider": "google-ads",
                        "target_campaign_id": best_meta_id,
                        "target_provider": "meta-ads"
                    },
                    severity=Severity(2, Reversibility.COMPENSATABLE),
                    cost_estimate=Money(0, "INR"),
                ), actor="optimizer")

                # Propose Child 1: Decrease Google Ads campaign budget
                child1 = await loop.propose(s, OpSpec(
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    domain="grow",
                    action="grow.campaign.update",
                    params={
                        "campaign_id": worst_google_id,
                        "provider": "google-ads",
                        "budget_minor": worst_google["budget_minor"] - transfer_amount_minor,
                        "previous_budget_minor": worst_google["budget_minor"],
                        "bid_minor": worst_google["op"].params.get("bid_minor")
                    },
                    severity=Severity(2, Reversibility.COMPENSATABLE),
                    cost_estimate=Money(0, "INR"),
                    parent_op_id=parent_saga.id,
                    sequence_order=1
                ), actor="optimizer")

                # Propose Child 2: Increase Meta Ads campaign budget
                child2 = await loop.propose(s, OpSpec(
                    tenant_id=tenant_id,
                    brand_id=brand_id,
                    domain="grow",
                    action="grow.campaign.update",
                    params={
                        "campaign_id": best_meta_id,
                        "provider": "meta-ads",
                        "budget_minor": best_meta["budget_minor"] + transfer_amount_minor,
                        "previous_budget_minor": best_meta["budget_minor"],
                        "bid_minor": best_meta["op"].params.get("bid_minor")
                    },
                    severity=Severity(2, Reversibility.COMPENSATABLE),
                    cost_estimate=Money(0, "INR"),
                    parent_op_id=parent_saga.id,
                    sequence_order=2
                ), actor="optimizer")

                logger.info("Inserted budget reallocation proposed Saga Op with 2 children")

                await s.flush()

                # Run preview and gate to transition parent and children to AWAITING_APPROVAL
                await loop.preview_and_gate(s, parent_saga, tier=1)
                await loop.preview_and_gate(s, child1, tier=1)
                await loop.preview_and_gate(s, child2, tier=1)

    if events_added > 0:
        await s.flush()
        await compute_snapshots(s)

    await s.commit()
    return {"status": "ok", "events_added": events_added}
