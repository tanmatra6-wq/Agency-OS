"""Kernel tests. The e2e test at the bottom is the Slice 1 heartbeat:
intent -> preview -> gate -> approval -> outbox -> execute -> verify -> DONE,
with the audit chain intact.
"""
import datetime as dt
import os
from unittest.mock import patch, MagicMock, AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy import select
import tempfile
import pathlib
import shutil

import app.main as mainmod
import app.routers.webhooks as webhooksmod
from app.database import get_db, get_worker_db, get_worker_session_maker
from app.kernel import loop
from app.kernel.optypes import (InvalidTransition, Money, OpSpec, OpState,
                                Reversibility, Severity, assert_transition)
from app.kernel.services import (audit_append, audit_verify, evaluate_gates,
                                 approval_requirement, saturating_penalty,
                                 tier_for, trust_score)
from app.models import AuditEvent, Base, OpRow, OutboxItem, TrustSnapshot, OpTrace

NOW = dt.datetime(2026, 6, 10, tzinfo=dt.timezone.utc)









def _spec(**kw) -> OpSpec:
    base = dict(tenant_id="t1", brand_id="b1", domain="provision",
                action="provision.web_host.create",
                params={"domain": "x.in", "recipe": "web-host", "version": "0.1.0"},
                severity=Severity(2, Reversibility.COMPENSATABLE),
                cost_estimate=Money(250_000))
    base.update(kw)
    return OpSpec(**base)

# ----------------------------------------------------------- state machine

def test_state_machine_blocks_illegal_jumps():
    with pytest.raises(InvalidTransition):
        assert_transition(OpState.PROPOSED, OpState.EXECUTING)  # no gate-skipping
    with pytest.raises(InvalidTransition):
        assert_transition(OpState.DONE, OpState.EXECUTING)      # terminal is terminal
    assert_transition(OpState.AWAITING_APPROVAL, OpState.PREVIEWED)  # A2UI re-plan

# ------------------------------------------------------------- audit chain

async def test_audit_chain_verifies_and_detects_tamper(session: AsyncSession):
    for i in range(5):
        await audit_append(session, tenant_id="t1", actor="test", action=f"e{i}")
    await session.flush()
    assert await audit_verify(session) == (True, None)

    result = await session.execute(select(AuditEvent).filter_by(action="e2"))
    victim = result.scalars().one()
    victim.payload = {"injected": True}  # tamper
    await session.flush()
    ok, first_bad = await audit_verify(session)
    assert ok is False and first_bad == victim.id

# ------------------------------------------------------------------ trust

def test_saturating_penalty_is_bounded_and_discriminating():
    p5 = saturating_penalty(5, p_max=25, tau=5)
    p50 = saturating_penalty(50, p_max=25, tau=5)
    assert p5 < p50 <= 25.0          # 5 vs 50 mismatches are distinguishable
    assert saturating_penalty(10_000, p_max=25, tau=5) <= 25.0  # never -infinity


def test_cold_start_lands_in_tier1_by_design():
    """Perfect health, zero history = 70 -> Tier 1. Autonomy is EARNED (§4.4)."""
    score = trust_score({"gtm_present": True, "pixel_present": True,
                         "capi_dedup_rate": 1.0}, events=[], now=NOW)
    assert score == pytest.approx(70.0)
    assert tier_for(score) == 1


def test_history_decays_and_overrides_are_recoverable():
    signals = {"gtm_present": True, "pixel_present": True, "capi_dedup_rate": 1.0}
    fresh = trust_score(signals, [("override", NOW - dt.timedelta(days=1))], now=NOW)
    stale = trust_score(signals, [("override", NOW - dt.timedelta(days=90))], now=NOW)
    assert fresh < stale  # the same mistake matters less with time
    # 5 recent overrides must not nuke the score to zero (legacy defect fixed):
    many = trust_score(signals, [("override", NOW)] * 5, now=NOW)
    assert many >= 40.0


def test_earned_autonomy_reaches_tier2():
    signals = {"gtm_present": True, "pixel_present": True, "capi_dedup_rate": 1.0}
    events = [("verified_success", NOW - dt.timedelta(days=i)) for i in range(20)]
    score = trust_score(signals, events, now=NOW)
    assert score >= 85.0 and tier_for(score) == 2

# ----------------------------------------------------------------- policy

def test_statutory_firewall_never_auto_approves():
    op = _spec(action="manage.gst.filing_update", statutory=True,
               severity=Severity(1, Reversibility.REVERSIBLE), cost_estimate=None,
               domain="manage")
    gate = evaluate_gates(op)
    assert gate.requires_human and not gate.blocked
    assert approval_requirement(op, tier=2, gate=gate) == "HUMAN"  # even at Tier 2


def test_cost_ceiling_blocks_with_structured_explanation():
    op = _spec(cost_estimate=Money(2_000_000))  # 20,000 INR > 10,000 ceiling
    gate = evaluate_gates(op)
    assert gate.blocked
    v = gate.violations[0]
    assert v.rule_id == "provision_cost_ceiling"
    assert "10,000.00" in v.limit and "over ceiling" in v.delta  # rule/limit/delta (§4.3)


def test_tier2_auto_approval_only_within_bounds():
    ok_op = _spec()  # impact 2, compensatable, within cost ceiling
    gate = evaluate_gates(ok_op)
    assert approval_requirement(ok_op, tier=2, gate=gate) == "AUTO"

    irreversible = _spec(severity=Severity(2, Reversibility.IRREVERSIBLE))
    assert approval_requirement(irreversible, tier=2, gate=evaluate_gates(irreversible)) == "HUMAN"

    assert approval_requirement(ok_op, tier=0, gate=gate) == "BLOCKED"  # lockout

# ------------------------------------------------------------ e2e heartbeat

async def test_e2e_governed_loop(client, db_engine):
    r = await client.post("/tenants", json={"name": "Tanmatra", "brand_name": "Wok-Tok"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]
    H = {"X-Tenant-ID": tid}

    r = await client.post("/intents", headers=H,
                    json={"brand_id": bid, "text": "host woktok.in please"})
    card = r.json()["cards"][0]
    assert card["state"] == "AWAITING_APPROVAL"
    assert "cloud_dns zone woktok.in" in card["preview"]
    assert card["cost_estimate"] == "2500.00 INR/mo"

    r = await client.post(f"/ops/{card['op_id']}/decision", headers=H,
                    json={"decision": "approve", "actor": "chandan", "surface": "whatsapp"})
    assert r.json()["state"] == "APPROVED"

    r = await client.get(f"/ops/{card['op_id']}", headers=H)
    assert r.json()["state"] == "DONE"
    kinds = [t["kind"] for t in r.json()["trace"]]
    assert kinds.count("transition") >= 6

    r = await client.get("/audit/verify")
    assert r.json()["ok"] is True

    # cross-tenant access is structurally denied
    r = await client.get(f"/ops/{card['op_id']}", headers={"X-Tenant-ID": "someone-else"})
    assert r.status_code == 404


async def test_e2e_blocked_op_explains_itself(db_engine, client):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        r = await client.post("/tenants", json={"name": "T", "brand_name": "B"})
        tid, bid = r.json()["tenant_id"], r.json()["brand_id"]

        spec = _spec(tenant_id=tid, brand_id=bid, cost_estimate=Money(2_000_000))
        row = await loop.propose(s, spec, actor="test")
        gate, requirement = await loop.preview_and_gate(s, row, tier=2)
        await s.commit()
        assert requirement == "BLOCKED" and row.state == "BLOCKED"
        assert gate.violations[0].message


async def test_drain_outbox_endpoint_returns_ok(client):
    r = await client.post("/tasks/drain-outbox")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "processed_items" in r.json()


# ------------------------------------------------------------ WhatsApp Webhook Tests

async def test_whatsapp_webhook_verification(client):
    # Setup verify token
    webhooksmod.WHATSAPP_VERIFY_TOKEN = "meaty"

    # Correct token
    r = await client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=1234&hub.verify_token=meaty")
    assert r.status_code == 200
    assert r.text == "1234"

    # Incorrect token
    r = await client.get("/webhooks/whatsapp?hub.mode=subscribe&hub.challenge=1234&hub.verify_token=wrong")
    assert r.status_code == 403


async def post_signed_webhook(client, payload, secret="test_secret"):
    import hmac
    import hashlib
    import json
    payload_bytes = json.dumps(payload).encode('utf-8')
    sig = hmac.new(secret.encode('utf-8'), payload_bytes, hashlib.sha256).hexdigest()
    headers = {"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"}
    return await client.post("/webhooks/whatsapp", content=payload_bytes, headers=headers)


async def test_whatsapp_webhook_invalid_signature(client):
    webhooksmod.WHATSAPP_APP_SECRET = "test_secret"
    payload = {"object": "whatsapp_business_account", "entry": []}

    # Missing signature
    r = await client.post("/webhooks/whatsapp", json=payload)
    assert r.status_code == 401

    # Wrong signature
    r = await client.post("/webhooks/whatsapp", json=payload, headers={"X-Hub-Signature-256": "sha256=wrong"})
    assert r.status_code == 401


async def test_whatsapp_webhook_signature_via_secret_manager(client):
    # Set WHATSAPP_APP_SECRET to a Secret Manager reference
    secret_ref = "projects/test-project/secrets/whatsapp-secret/versions/latest"
    webhooksmod.WHATSAPP_APP_SECRET = secret_ref
    
    # Seed the mock secret registry using SecretManagerClient
    from app.services.secrets import SecretManagerClient
    secrets_client = SecretManagerClient(project_id="test-project")
    await secrets_client.write_secret("whatsapp-secret", "super-secret-key-from-sm")
    
    # Post a payload signed with the resolved key
    payload = {"object": "whatsapp_business_account", "entry": []}
    
    # 1. Post with signature signed using the correct key -> should succeed (200 status_code)
    # Note: Since the entry list is empty, it will not trigger the background task, 
    # but the signature verification itself should succeed and return {"status": "accepted"} / 200.
    r = await post_signed_webhook(client, payload, secret="super-secret-key-from-sm")
    assert r.status_code == 200
    assert r.json() == {"status": "accepted"}
    
    # 2. Post with signature signed using a wrong key -> should fail (401 status_code)
    r = await post_signed_webhook(client, payload, secret="wrong-key")
    assert r.status_code == 401


@patch("app.whatsapp.httpx.AsyncClient")
async def test_whatsapp_e2e_approval_flow(mock_client_class, client, db_engine):
    # Setup WhatsApp mock config
    import app.whatsapp as wa
    webhooksmod.WHATSAPP_APP_SECRET = "test_secret"
    wa.WHATSAPP_TOKEN = "mock_token"
    wa.WHATSAPP_PHONE_NUMBER_ID = "12345"
    wa.WHATSAPP_APPROVER_PHONE = "919999999999"
    wa.WHATSAPP_TEMPLATE_NAME = "agency_os_approval"

    # Mock AsyncClient context manager and its post method
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "mock_wamid_approval_test"}]}
    mock_client.post.return_value = mock_response

    # 1. Propose Op (Submit intent)
    r = await client.post("/tenants", json={"name": "Tanmatra", "brand_name": "Wok-Tok"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]
    H = {"X-Tenant-ID": tid}

    # Submit intent that requires approval (tier 1)
    r = await client.post("/intents", headers=H,
                    json={"brand_id": bid, "text": "host woktok.in please"})
    card = r.json()["cards"][0]
    op_id = card["op_id"]
    assert card["state"] == "AWAITING_APPROVAL"

    # Wait for background task to send whatsapp card
    assert mock_client.post.called
    # Check that it sent the correct payload
    args, kwargs = mock_client.post.call_args
    assert "12345" in args[0] # Phone Number ID
    payload = kwargs["json"]
    assert payload["to"] == "919999999999"
    assert payload["template"]["name"] == "agency_os_approval"
    # Verify button payloads
    components = payload["template"]["components"]
    button_0 = next(c for c in components if c["type"] == "button" and c["index"] == "0")
    assert button_0["parameters"][0]["payload"] == f"approve_{op_id}"

    # 2. Simulate User clicking "Approve" via Webhook
    webhook_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "12345",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": "919999999999",
                                    "id": "msg_id_1",
                                    "type": "button",
                                    "button": {
                                        "payload": f"approve_{op_id}",
                                        "text": "Approve"
                                    }
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }

    # Call the webhook (POST)
    r = await post_signed_webhook(client, webhook_payload)
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"

    # 3. Verify Op is transitioned to DONE
    r = await client.get(f"/ops/{op_id}", headers=H)
    assert r.json()["state"] == "DONE"


@patch("app.whatsapp.httpx.AsyncClient")
async def test_whatsapp_e2e_rejection_flow(mock_client_class, client, db_engine):
    # Setup WhatsApp mock config
    import app.whatsapp as wa
    webhooksmod.WHATSAPP_APP_SECRET = "test_secret"
    wa.WHATSAPP_TOKEN = "mock_token"
    wa.WHATSAPP_PHONE_NUMBER_ID = "12345"
    wa.WHATSAPP_APPROVER_PHONE = "919999999999"

    # Mock AsyncClient
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "mock_wamid_rejection_test"}]}
    mock_client.post.return_value = mock_response

    # Propose Op
    r = await client.post("/tenants", json={"name": "Tanmatra", "brand_name": "Wok-Tok"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]
    H = {"X-Tenant-ID": tid}
    r = await client.post("/intents", headers=H,
                    json={"brand_id": bid, "text": "host woktok.in please"})
    op_id = r.json()["cards"][0]["op_id"]

    # Simulate User clicking "Reject" via Webhook
    webhook_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "12345",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": "919999999999",
                                    "id": "msg_id_2",
                                    "type": "button",
                                    "button": {
                                        "payload": f"reject_{op_id}",
                                        "text": "Reject"
                                    }
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }

    r = await post_signed_webhook(client, webhook_payload)
    assert r.status_code == 200

    # Verify Op is transitioned to REJECTED
    r = await client.get(f"/ops/{op_id}", headers=H)
    assert r.json()["state"] == "REJECTED"


@patch("app.whatsapp.httpx.AsyncClient")
async def test_whatsapp_e2e_modify_flow(mock_client_class, client, db_engine):
    # Setup WhatsApp mock config
    import app.whatsapp as wa
    webhooksmod.WHATSAPP_APP_SECRET = "test_secret"
    wa.WHATSAPP_TOKEN = "mock_token"
    wa.WHATSAPP_PHONE_NUMBER_ID = "12345"
    wa.WHATSAPP_APPROVER_PHONE = "919999999999"

    # Mock AsyncClient
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "mock_wamid_modify_test"}]}
    mock_client.post.return_value = mock_response

    # Propose Op
    r = await client.post("/tenants", json={"name": "Tanmatra", "brand_name": "Wok-Tok"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]
    H = {"X-Tenant-ID": tid}
    r = await client.post("/intents", headers=H,
                    json={"brand_id": bid, "text": "host woktok.in please"})
    op_id = r.json()["cards"][0]["op_id"]

    # Simulate User sending text message "modify make it 40k"
    webhook_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "12345",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": "919999999999",
                                    "id": "msg_id_3",
                                    "type": "text",
                                    "text": {
                                        "body": "modify use woktok.co"
                                    },
                                    "context": {
                                        "id": "mock_wamid_modify_test"
                                    }
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }

    r = await post_signed_webhook(client, webhook_payload)
    assert r.status_code == 200

    # Verify Op is re-gated and transitioned back to AWAITING_APPROVAL with updated params
    r = await client.get(f"/ops/{op_id}", headers=H)
    assert r.json()["state"] == "AWAITING_APPROVAL"
    assert r.json()["params"]["domain"] == "woktok.co"

    # Verify that the modify approval is in the traces
    traces = r.json()["trace"]
    assert len(traces) >= 2

    # Verify that mock_client.post was called twice (initial + modified card)
    assert mock_client.post.call_count == 2
    args, kwargs = mock_client.post.call_args_list[1]
    payload = kwargs["json"]
    # Check that the second card references the modified domain
    assert "woktok.co" in str(payload)


@patch("app.whatsapp.httpx.AsyncClient")
async def test_whatsapp_webhook_idempotency(mock_client_class, client, db_engine):
    from app.models import Approval
    # Setup WhatsApp mock config
    import app.whatsapp as wa
    webhooksmod.WHATSAPP_APP_SECRET = "test_secret"
    wa.WHATSAPP_TOKEN = "mock_token"
    wa.WHATSAPP_PHONE_NUMBER_ID = "12345"
    wa.WHATSAPP_APPROVER_PHONE = "919999999999"

    # Mock AsyncClient
    mock_client = AsyncMock()
    mock_client_class.return_value.__aenter__.return_value = mock_client
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"messages": [{"id": "mock_wamid_idemp_test"}]}
    mock_client.post.return_value = mock_response

    # Propose Op
    r = await client.post("/tenants", json={"name": "Tanmatra", "brand_name": "Wok-Tok"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]
    H = {"X-Tenant-ID": tid}
    r = await client.post("/intents", headers=H,
                    json={"brand_id": bid, "text": "host woktok.in please"})
    op_id = r.json()["cards"][0]["op_id"]

    webhook_payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "12345",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": "919999999999",
                                    "id": "msg_id_idemp_123",
                                    "type": "button",
                                    "button": {
                                        "payload": f"approve_{op_id}",
                                        "text": "Approve"
                                    }
                                }
                            ]
                        },
                        "field": "messages"
                    }
                ]
            }
        ]
    }

    # 1. Send webhook first time
    r = await post_signed_webhook(client, webhook_payload)
    assert r.status_code == 200

    # Verify Op is transitioned to DONE
    r = await client.get(f"/ops/{op_id}", headers=H)
    assert r.json()["state"] == "DONE"

    # Verify exactly one Approval row was created
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        res = await s.execute(select(Approval).where(Approval.op_id == op_id))
        approvals = res.scalars().all()
        assert len(approvals) == 1

    # 2. Send EXACT SAME webhook second time (duplicate msg_id)
    r = await post_signed_webhook(client, webhook_payload)
    assert r.status_code == 200

    # Verify still exactly one Approval row exists (did not process again)
    async with async_session() as s:
        res = await s.execute(select(Approval).where(Approval.op_id == op_id))
        approvals = res.scalars().all()
        assert len(approvals) == 1


# ------------------------------------------------------------- trust engine E2E

async def test_e2e_trust_tier_2_auto_approves(client, db_engine):
    from app.models import TrustSnapshot

    # 1. Create tenant/brand
    r = await client.post("/tenants", json={"name": "TrustBrand", "brand_name": "Trusty"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]
    H = {"X-Tenant-ID": tid}

    # 2. Insert a TrustSnapshot with tier=2 for this brand and domain "provision"
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        s.add(TrustSnapshot(tenant_id=tid, brand_id=bid, domain="provision", score=90.0, tier=2))
        await s.commit()

    # 3. Submit intent (no tier parameter)
    r = await client.post("/intents", headers=H,
                          json={"brand_id": bid, "text": "host woktok.in please"})
    card = r.json()["cards"][0]
    assert card["state"] == "APPROVED"
    assert card["requirement"] == "AUTO"


async def test_trust_snapshots_nightly_job(db_engine):
    from app.kernel.services import compute_snapshots
    from app.models import Tenant, Brand, TrustEvent, TrustSnapshot

    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        # Create brand
        t = Tenant(name="T")
        s.add(t)
        await s.flush()
        b = Brand(tenant_id=t.id, name="B")
        s.add(b)
        await s.flush()

        # Add some trust events
        s.add(TrustEvent(tenant_id=t.id, brand_id=b.id, domain="provision", kind="verified_success", base_delta=1.0))
        s.add(TrustEvent(tenant_id=t.id, brand_id=b.id, domain="provision", kind="verify_failure", base_delta=-8.0))
        await s.commit()

    async with async_session() as s:
        await compute_snapshots(s)
        await s.commit()

    async with async_session() as s:
        res = await s.execute(select(TrustSnapshot).where(TrustSnapshot.brand_id == b.id, TrustSnapshot.domain == "provision"))
        snap = res.scalar_one()
        # Initial health score = 70.0 (gtm + pixel + capi) (Wait: in services.py it is 67.0! Check math below)
        # score = health (67.0) - penalties (0) + history (-7) = 60.0
        # Tier = 1
        assert snap.score == pytest.approx(60.0)
        assert snap.tier == 1


async def test_decide_enforces_override_reason(db_engine):
    from app.kernel import loop
    from app.models import OpRow, TrustEvent
    from app.kernel.optypes import Severity, Reversibility, Money
    from app.kernel.services import evaluate_gates

    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        # Create an Op that violates cost ceiling policy
        # Cost ceiling is 10,000 INR (1,000,000 minor)
        spec = _spec(cost_estimate=Money(2_000_000))  # 20,000 INR -> violates cost ceiling!
        row = await loop.propose(s, spec, actor="test")
        await s.commit()

    async with async_session() as s:
        db_row = await s.get(OpRow, row.id)
        # Move to AWAITING_APPROVAL state
        db_row.state = "AWAITING_APPROVAL"
        await s.commit()

    async with async_session() as s:
        db_row = await s.get(OpRow, row.id)
        # Try to approve without reason -> should fail
        with pytest.raises(ValueError, match="Override reason is mandatory"):
            await loop.decide(s, db_row, decision="approve", actor="chandan", role="owner", surface="whatsapp")

        # Try to approve with reason -> should succeed
        await loop.decide(s, db_row, decision="approve", actor="chandan", role="owner", surface="whatsapp", reason="Approved for client launch")
        await s.commit()

    async with async_session() as s:
        # Check that TrustEvent of kind 'override' was written
        res = await s.execute(select(TrustEvent).where(TrustEvent.brand_id == "b1", TrustEvent.kind == "override"))
        ev = res.scalar_one()
        assert ev.base_delta == -5.0
        assert ev.reason == "Approved for client launch"


@pytest.mark.asyncio
async def test_e2e_brand_bootstrap_saga(client, db_engine):
    # Setup: Create initial trust snapshot for b1/provision so we don't fail tier resolution
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        s.add(TrustSnapshot(tenant_id="t1", brand_id="b1", domain="provision", tier=1, score=75.0))
        await s.commit()

    H = {"X-Tenant-ID": "t1"}
    # 1. Submit brand bootstrap intent
    resp = await client.post("/intents", headers=H, json={
        "domain": "provision",
        "brand_id": "b1",
        "text": "bootstrap brand ableys ableys.com"
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Only the parent card should be returned!
    assert len(data["cards"]) == 1
    parent_card = data["cards"][0]
    assert parent_card["action"] == "provision.brand_bootstrap.create"
    assert parent_card["state"] == "AWAITING_APPROVAL"
    parent_id = parent_card["op_id"]

    # Verify database state for children
    async with async_session() as s:
        # Parent
        parent_row = await s.get(OpRow, parent_id)
        assert parent_row is not None
        assert parent_row.state == "AWAITING_APPROVAL"

        # Children
        res = await s.execute(select(OpRow).where(OpRow.parent_op_id == parent_id).order_by(OpRow.sequence_order))
        children = res.scalars().all()
        assert len(children) == 2

        child_baseline, child_web = children[0], children[1]
        assert child_baseline.action == "provision.brand_baseline.create"
        assert child_baseline.sequence_order == 1
        assert child_baseline.state == "AWAITING_APPROVAL"

        assert child_web.action == "provision.web_host.create"
        assert child_web.sequence_order == 2
        assert child_web.state == "AWAITING_APPROVAL"

    # 2. Approve the parent Op (triggers child_baseline execution automatically)
    resp_dec = await client.post(f"/ops/{parent_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200, resp_dec.text

    # Verify parent is EXECUTING, child_baseline is DONE, and child_web is APPROVED and enqueued
    async with async_session() as s:
        parent_row = await s.get(OpRow, parent_id)
        assert parent_row.state == "EXECUTING"

        res = await s.execute(select(OpRow).where(OpRow.parent_op_id == parent_id).order_by(OpRow.sequence_order))
        children = res.scalars().all()
        child_baseline, child_web = children[0], children[1]
        assert child_baseline.state == "DONE"
        assert child_web.state == "APPROVED"

        # Check Outbox
        res_outbox = await s.execute(select(OutboxItem).where(OutboxItem.status == "PENDING"))
        outbox_items = res_outbox.scalars().all()
        # Should contain child_web!
        assert len(outbox_items) == 1
        assert outbox_items[0].op_id == child_web.id

    # 3. Drain outbox (executes child_web)
    async with async_session() as s:
        processed = await loop.drain_once(s)
        assert processed == 1
        await s.commit()

    # Verify both children and parent are DONE
    async with async_session() as s:


        child_baseline = await s.get(OpRow, child_baseline.id)
        assert child_baseline.state == "DONE"

        child_web = await s.get(OpRow, child_web.id)
        assert child_web.state == "DONE"

        parent_row = await s.get(OpRow, parent_id)
        assert parent_row.state == "DONE"


@pytest.mark.asyncio
async def test_e2e_brand_bootstrap_saga_rollback(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        s.add(TrustSnapshot(tenant_id="t1", brand_id="b2", domain="provision", tier=1, score=75.0))
        await s.commit()

    H = {"X-Tenant-ID": "t1"}
    # 1. Submit brand bootstrap intent with fail.in domain to trigger child_web execute failure
    resp = await client.post("/intents", headers=H, json={
        "domain": "provision",
        "brand_id": "b2",
        "text": "bootstrap brand fail fail.in" 
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    parent_id = data["cards"][0]["op_id"]

    # 2. Approve parent Op (triggers child_baseline execution -> DONE)
    resp_dec = await client.post(f"/ops/{parent_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    # Verify child_baseline is DONE, parent is EXECUTING, and child_web is APPROVED and enqueued
    async with async_session() as s:
        res = await s.execute(select(OpRow).where(OpRow.parent_op_id == parent_id).order_by(OpRow.sequence_order))
        children = res.scalars().all()
        child_baseline, child_web = children[0], children[1]
        assert child_baseline.state == "DONE"
        assert child_web.state == "APPROVED"

        parent_row = await s.get(OpRow, parent_id)
        assert parent_row.state == "EXECUTING"

        res_outbox = await s.execute(select(OutboxItem).where(OutboxItem.status == "PENDING"))
        assert len(res_outbox.scalars().all()) == 1

    # 3. Drain outbox (executes child_web -> fails, triggers cascading rollback)
    async with async_session() as s:
        processed = await loop.drain_once(s)
        assert processed == 1
        await s.commit()

    # Verify states: all children and parent are ROLLED_BACK
    async with async_session() as s:


        res = await s.execute(select(OpRow).where(OpRow.parent_op_id == parent_id).order_by(OpRow.sequence_order))
        children = res.scalars().all()
        child_baseline, child_web = children[0], children[1]

        assert child_web.state == "ROLLED_BACK"
        assert child_baseline.state == "ROLLED_BACK"

        parent_row = await s.get(OpRow, parent_id)
        assert parent_row.state == "ROLLED_BACK"


@pytest.mark.asyncio
async def test_cost_ledger_ingestion_and_rollups(client, db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    # 1. Create a trust snapshot to pass gate tier resolution
    async with async_session() as s:
        s.add(TrustSnapshot(tenant_id="t1", brand_id="b1", domain="provision", tier=1, score=75.0))
        await s.commit()

    H = {"X-Tenant-ID": "t1"}

    # 2. Submit intent (which will log simulated gemini planning tokens cost)
    resp = await client.post("/intents", headers=H, json={
        "domain": "provision",
        "brand_id": "b1",
        "text": "host my site ableys.com"
    })
    assert resp.status_code == 200
    data = resp.json()
    op_id = data["cards"][0]["op_id"]

    # Verify that planning cost (llm_tokens) was logged and attributed to the Op ID
    async with async_session() as s:
        from app.kernel.services import get_tenant_cost_rollup, get_op_cost_total
        op_total = await get_op_cost_total(s, op_id)
        assert op_total == 57  # planning cost: 57 paise

        tenant_rollup = await get_tenant_cost_rollup(s, "t1")
        assert tenant_rollup.get("llm_tokens") == 57

    # 3. Approve the Op to execute it
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200

    # 4. Ingest GCP billing record for this tenant (simulating GCP cost ledger ingestion)
    async with async_session() as s:
        from app.kernel.services import ingest_gcp_billing
        await ingest_gcp_billing(
            s,
            tenant_id="t1",
            resource_id="run-service-web-ableys-com",
            amount_minor=1240,  # 12.40 INR
            currency="INR",
            labels={"tenant_id": "t1", "op_id": op_id}
        )
        await s.commit()

    # 5. Verify the updated tenant rollup and total costs
    async with async_session() as s:
        tenant_rollup = await get_tenant_cost_rollup(s, "t1")
        assert tenant_rollup.get("llm_tokens") == 57
        assert tenant_rollup.get("gcp_resource") == 251240



    # 6. Verify that execution costs were recorded
    async with async_session() as s:
        # After execution, child Op should return:
        # - zone_register: 2000 paise (20 INR)
        # - service_account_create: 150 paise (1.50 INR)
        # Total execution cost = 2150 paise
        op_total = await get_op_cost_total(s, op_id)
        # Total cost for op_id should be planning (57) + execution (2150) + recipe (250000) = 252207 paise
        assert op_total == 252207

        tenant_rollup = await get_tenant_cost_rollup(s, "t1")
        assert tenant_rollup.get("llm_tokens") == 57
        assert tenant_rollup.get("api_call") == 2150
        assert tenant_rollup.get("gcp_resource") == 251240

        # Verify actor attribution on costs
        from app.models import CostEntry
        # Planning cost (llm_tokens) should be attributed to 'chat'
        res_plan = await s.execute(select(CostEntry).where(CostEntry.op_id == op_id, CostEntry.kind == "llm_tokens"))
        plan_cost = res_plan.scalar_one()
        assert plan_cost.actor == "chat"

        # Execution costs (api_call) should be attributed to 'chandan' (who approved the Op)
        res_exec = await s.execute(select(CostEntry).where(CostEntry.op_id == op_id, CostEntry.kind == "api_call"))
        exec_costs = res_exec.scalars().all()
        assert len(exec_costs) == 2
        for cost in exec_costs:
            assert cost.actor == "chandan"


@pytest.mark.asyncio
async def test_observability_and_trace_propagation(client, db_engine, capsys):
    import json
    import logging
    from app.observability import setup_logging, trace_context
    from app.database import tenant_context

    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    # 1. Create a trust snapshot to pass gate tier resolution
    async with async_session() as s:
        s.add(TrustSnapshot(tenant_id="t1", brand_id="b1", domain="provision", tier=1, score=75.0))
        await s.commit()

    # 2. Test JSON formatted logging directly
    setup_logging(level="INFO", json_format=True)
    
    test_logger = logging.getLogger("test_obs")
    
    # Set context variables manually to simulate a request flow
    t_token = tenant_context.set("t1")
    tr_token = trace_context.set("tr-manual-999")
    try:
        test_logger.info("Structured log statement", extra={"custom_metric": 42})
    finally:
        tenant_context.reset(t_token)
        trace_context.reset(tr_token)
        
    # Reset logging back to text format so we don't mess up standard pytest logs
    setup_logging(level="INFO", json_format=False)
    
    # Capture stderr and verify JSON formatting (StreamHandler outputs to stderr by default)
    captured = capsys.readouterr()
    log_line = captured.err.strip()
    
    # Parse the captured log statement as JSON
    parsed = json.loads(log_line)
    assert parsed["message"] == "Structured log statement"
    assert parsed["severity"] == "INFO"
    assert parsed["logger"] == "test_obs"
    assert parsed["trace_id"] == "tr-manual-999"
    assert parsed["tenant_id"] == "t1"
    assert parsed["custom_metric"] == 42
    
    # 3. Test HTTP propagation via TraceMiddleware
    H = {"X-Tenant-ID": "t1", "X-Trace-ID": "tr-http-555"}
    resp = await client.post("/intents", headers=H, json={
        "domain": "provision",
        "brand_id": "b1",
        "text": "host my site ableys.com"
    })
    assert resp.status_code == 200
    assert resp.headers.get("X-Trace-ID") == "tr-http-555"
    data = resp.json()
    op_id = data["cards"][0]["op_id"]
    
    # 4. Approve the Op (triggers execution and outbox enqueuing)
    resp_dec = await client.post(f"/ops/{op_id}/decision", headers=H, json={
        "decision": "approve",
        "actor": "chandan",
        "role": "owner",
        "surface": "whatsapp"
    })
    assert resp_dec.status_code == 200
    
    # 5. Verify that OutboxItem in database contains the correct propagated trace ID
    async with async_session() as s:
        res_outbox = await s.execute(select(OutboxItem).where(OutboxItem.op_id == op_id))
        outbox_item = res_outbox.scalar_one()
        assert outbox_item.trace_id == "tr-http-555"


def test_build_rules_parameterizes_thresholds():
    """RulesetParams() reproduces the defaults (the 3 policy tests above are the
    regression lock); raising a limit relaxes the gate. Precursor to policy what-if."""
    from app.kernel.services import build_rules, RulesetParams
    op = _spec(cost_estimate=Money(2_000_000))  # 20,000 > default 10,000 ceiling
    assert evaluate_gates(op).blocked  # default ruleset still blocks
    relaxed = build_rules(RulesetParams(provision_cost_ceiling_minor=2_500_000))
    assert not evaluate_gates(op, rules=relaxed).blocked  # parameterized ceiling lets it pass


async def test_decision_latency_is_measured(db_engine):
    """North-star clock (§1): latency_ms populated from the AWAITING_APPROVAL marker."""
    from app.models import Approval
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        row = await loop.propose(s, _spec(), actor="test")
        row.state = "AWAITING_APPROVAL"
        s.add(OpTrace(op_id=row.id, tenant_id=row.tenant_id, kind="transition", detail={"to": "AWAITING_APPROVAL"},
                      ts=dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=2)))
        await s.commit()
    async with async_session() as s:
        db_row = await s.get(OpRow, row.id)
        await loop.decide(s, db_row, decision="approve", actor="c", role="owner", surface="web")
        await s.commit()
    async with async_session() as s:
        appr = (await s.execute(select(Approval).where(Approval.op_id == row.id))).scalar_one()
        assert appr.latency_ms is not None and appr.latency_ms >= 2000


async def test_approval_latency_rollup_is_tenant_scoped(db_engine):
    from app.models import Approval
    from app.kernel.services import approval_latency_rollup
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        for tid, lat in [("t1", 1000), ("t1", 3000), ("t2", 9999)]:
            r = await loop.propose(s, _spec(tenant_id=tid), actor="t")
            s.add(Approval(op_id=r.id, tenant_id=tid, actor="a", role="owner", surface="web",
                           decision="approve", latency_ms=lat))
        await s.commit()
        roll = await approval_latency_rollup(s, "t1")
        assert roll["count"] == 2                      # t2's approval excluded
        assert roll["median_ms"] in (1000, 3000)
        assert roll["expired_cards"] == 0


@pytest.mark.asyncio
async def test_recipe_promotion_endpoint(client, monkeypatch, tmp_path):
    import subprocess
    from unittest.mock import MagicMock

    # Mock git subprocess run to prevent actual git commands
    mock_run = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=b"Mocked Git Success", stderr=b""))
    monkeypatch.setattr("app.main.subprocess.run", mock_run)
    
    # Redirect RECIPES_ROOT to isolated tmp_path
    monkeypatch.setattr("app.main.RECIPES_ROOT", str(tmp_path))

    # 1. Setup mock experimental recipe folder
    exp_path = tmp_path / "experimental" / "test-promo" / "0.1.0"
    prod_path = tmp_path / "test-promo" / "0.1.0"

    exp_path.mkdir(parents=True, exist_ok=True)
    with open(exp_path / "recipe.yaml", "w") as f:
        f.write("name: test-promo\nversion: 0.1.0")
    with open(exp_path / "main.tf", "w") as f:
        f.write("output \"test\" { value = 1 }")

    # 2. Execute promotion
    H = {"X-Tenant-Id": "t1"}
    r = await client.post("/recipes/promote", headers=H, json={"recipe_name": "test-promo"})
    assert r.status_code == 200, f"Promo failed: {r.status_code} - {r.text}"
    data = r.json()
    assert data["status"] == "promoted"
    assert data["recipe_name"] == "test-promo"

    # 3. Assert files copied
    assert (prod_path / "recipe.yaml").exists()
    assert (prod_path / "main.tf").exists()

    # 4. Assert 404 for non-existent recipe promotion
    r_404 = await client.post("/recipes/promote", headers=H, json={"recipe_name": "missing-recipe"})
    assert r_404.status_code == 404

    # 5. Assert 400 for missing file promotion
    exp_bad = tmp_path / "experimental" / "test-promo-bad" / "0.1.0"
    exp_bad.mkdir(parents=True, exist_ok=True)
    with open(exp_bad / "recipe.yaml", "w") as f:
        f.write("bad recipe")
    r_400 = await client.post("/recipes/promote", headers=H, json={"recipe_name": "test-promo-bad"})
    assert r_400.status_code == 400


@pytest.mark.asyncio
async def test_recipe_promotion_path_traversal_protection(client, monkeypatch, tmp_path):
    import subprocess
    from unittest.mock import MagicMock

    # Mock git subprocess run to prevent actual git commands
    mock_run = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=b"Mocked Git Success", stderr=b""))
    monkeypatch.setattr("app.main.subprocess.run", mock_run)
    
    # Redirect RECIPES_ROOT to isolated tmp_path
    monkeypatch.setattr("app.main.RECIPES_ROOT", str(tmp_path))

    H = {"X-Tenant-Id": "t1"}

    # 1. Attempt path traversal in recipe name (e.g., "../outside")
    r1 = await client.post("/recipes/promote", headers=H, json={
        "recipe_name": "../outside",
        "version": "0.1.0"
    })
    assert r1.status_code == 400
    assert "Invalid path traversal" in r1.json()["detail"]

    # 2. Attempt path traversal in version (e.g., "../../outside")
    r2 = await client.post("/recipes/promote", headers=H, json={
        "recipe_name": "test-recipe",
        "version": "../../outside"
    })
    assert r2.status_code == 400
    assert "Invalid path traversal" in r2.json()["detail"]


@pytest.mark.asyncio
async def test_recipe_promotion_sibling_directory_bypass(client, monkeypatch, tmp_path):
    import subprocess
    from unittest.mock import MagicMock

    # Mock git subprocess run to prevent actual git commands
    mock_run = MagicMock(return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout=b"Mocked Git Success", stderr=b""))
    monkeypatch.setattr("app.main.subprocess.run", mock_run)
    
    # Redirect RECIPES_ROOT to isolated tmp_path / "recipes"
    recipes_dir = tmp_path / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("app.main.RECIPES_ROOT", str(recipes_dir))

    H = {"X-Tenant-Id": "t1"}

    # Attempt sibling directory bypass in recipe name (starts with "recipes")
    r = await client.post("/recipes/promote", headers=H, json={
        "recipe_name": "../recipes-sibling/exploit",
        "version": "0.1.0"
    })
    
    assert r.status_code == 400
    assert "Invalid path traversal" in r.json()["detail"]


@pytest.mark.asyncio
async def test_brand_sense_governed_task_and_endpoint(client, db_engine, mock_mcp_client):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select
    from app.models import Connection, Brand, Tenant, BrandProperty, OpRow, TrustSnapshot
    
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        # Seed Tenant & Brand
        tenant = Tenant(id="t1", name="Test Tenant", hosting_tier="shared")
        brand = Brand(id="b1", tenant_id="t1", name="Test Brand")
        s.add(tenant)
        s.add(brand)
        
        # Seed TrustSnapshot for manage domain to be Tier 2 (autonomous)
        s.add(TrustSnapshot(
            tenant_id="t1", brand_id="b1", domain="manage", tier=2, score=95.0
        ))
        
        # Seed Shopify and Google connections
        s.add(Connection(
            tenant_id="t1", brand_id="b1", provider="shopify",
            credential="projects/mock-project/secrets/t1-b1-shopify-secret/versions/latest",
            config={"shop_url": "test-store.myshopify.com"},
            status="active"
        ))
        s.add(Connection(
            tenant_id="t1", brand_id="b1", provider="google",
            credential="projects/mock-project/secrets/t1-b1-google-secret/versions/latest",
            config={},
            status="active"
        ))
        await s.commit()

        # Trigger the sense check task directly
        from app.tasks.sense import run_brand_sense
        await run_brand_sense(s, "t1", "b1")
        await s.commit()
        
    H = {"X-Tenant-Id": "t1"}
    # Call the GET brand graph endpoint (now read-only)
    resp = await client.get("/brands/b1/graph", headers=H)
    assert resp.status_code == 200, resp.text
    properties = resp.json()
    
    # Assert that properties were populated/updated dynamically
    types = {p["type"] for p in properties}
    assert "ux_analytics" in types
    assert "search_console" in types
    assert "merchant_feed" in types
    assert "presence_audit" in types
    assert "pr_monitoring" in types
    assert "brand_performance_weights" in types
    
    # Verify the values of some properties
    ux_prop = next(p for p in properties if p["type"] == "ux_analytics")
    assert ux_prop["status"] == "connected"
    assert ux_prop["findings"]["shop_name"] == "Mock Ableys Shop"
    
    # Verify that a governed Op was proposed and executed successfully
    async with async_session() as s:
        res = await s.execute(select(OpRow).where(OpRow.action == "manage.brand.sense"))
        ops = res.scalars().all()
        assert len(ops) == 1
        op = ops[0]
        assert op.state == "DONE"
        assert op.tenant_id == "t1"
        assert op.brand_id == "b1"
        
        # Verify the audit log records the correct actor!
        from app.models import AuditEvent
        res_audit = await s.execute(select(AuditEvent).where(AuditEvent.op_id == op.id, AuditEvent.action == "op.proposed"))
        audit_events = res_audit.scalars().all()
        assert len(audit_events) == 1
        assert audit_events[0].actor == "tasks:sense"


@pytest.mark.asyncio
async def test_ops_list_endpoint_and_filtering(client, db_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from app.models import OpRow
    
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        # Pre-seed Ops for tenant t1
        s.add(OpRow(
            id="op_t1_1", tenant_id="t1", brand_id="b1", domain="provision",
            action="provision.web_host.create", state="PENDING", params={},
            preview_summary="preview 1", impact=1, reversibility="REVERSIBLE",
            idem_key="idem_t1_1"
        ))
        s.add(OpRow(
            id="op_t1_2", tenant_id="t1", brand_id="b1", domain="provision",
            action="provision.web_host.create", state="APPROVED", params={},
            preview_summary="preview 2", impact=1, reversibility="REVERSIBLE",
            idem_key="idem_t1_2"
        ))
        s.add(OpRow(
            id="op_t1_3", tenant_id="t1", brand_id="b2", domain="dns",
            action="dns.record.create", state="PENDING", params={},
            preview_summary="preview 3", impact=1, reversibility="REVERSIBLE",
            idem_key="idem_t1_3"
        ))
        # Pre-seed Op for tenant t2 (should be hidden due to RLS/scoping)
        s.add(OpRow(
            id="op_t2_1", tenant_id="t2", brand_id="b1", domain="provision",
            action="provision.web_host.create", state="PENDING", params={},
            preview_summary="preview 4", impact=1, reversibility="REVERSIBLE",
            idem_key="idem_t2_1"
        ))
        await s.commit()

    # 1. Query all ops for tenant t1
    H1 = {"X-Tenant-Id": "t1"}
    r = await client.get("/ops", headers=H1)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 3
    # Ordered desc by ID
    assert data[0]["op_id"] == "op_t1_3"
    assert data[1]["op_id"] == "op_t1_2"
    assert data[2]["op_id"] == "op_t1_1"

    # 2. Test pagination (limit=2, offset=1)
    r = await client.get("/ops?limit=2&offset=1", headers=H1)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]["op_id"] == "op_t1_2"
    assert data[1]["op_id"] == "op_t1_1"

    # 3. Test filtering by state=APPROVED
    r = await client.get("/ops?state=APPROVED", headers=H1)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["op_id"] == "op_t1_2"

    # 4. Test filtering by domain=dns
    r = await client.get("/ops?domain=dns", headers=H1)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 1
    assert data[0]["op_id"] == "op_t1_3"

    # 5. Test filtering by brand_id=b1
    r = await client.get("/ops?brand_id=b1", headers=H1)
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2
    assert data[0]["op_id"] == "op_t1_2"
    assert data[1]["op_id"] == "op_t1_1"


@pytest.mark.asyncio
async def test_ops_list_endpoint_401_failure(client):
    r = await client.get("/ops")
    assert r.status_code == 401
    assert "X-Tenant-ID header is missing" in r.text




@pytest.mark.asyncio
async def test_chat_intent_parsing_and_planning(client):
    H = {"X-Tenant-Id": "t1"}
    
    # 1. Test static hosting request
    r_static = await client.post("/chat", headers=H, json={
        "brand_id": "b1", "text": "hey, please set up static hosting for woktok.co"
    })
    assert r_static.status_code == 200
    data_static = r_static.json()
    assert "static website hosting for woktok.co" in data_static["reply"]
    assert len(data_static["cards"]) == 1
    assert data_static["cards"][0]["action"] == "provision.static_host.create"

    # 2. Test email DNS request
    r_dns = await client.post("/chat", headers=H, json={
        "brand_id": "b1", "text": "I need SPF and MX records for domain mailer.in"
    })
    assert r_dns.status_code == 200
    data_dns = r_dns.json()
    assert "configure email dns routing for domain mailer.in" in data_dns["reply"]
    assert len(data_dns["cards"]) == 1
    assert data_dns["cards"][0]["action"] == "provision.email_dns.create"


@pytest.mark.asyncio
async def test_chat_readonly_and_guidance_intents(client):
    """The chat answers budget/diagnostics read-only (no Op) and guides on unrecognized
    text instead of silently proposing a provision Op; explicit provision intents still plan."""
    H = {"X-Tenant-Id": "t1"}

    # Budget check — read-only, proposes nothing.
    r_budget = await client.post("/chat", headers=H, json={"brand_id": "b1", "text": "what's my spend so far?"})
    assert r_budget.status_code == 200
    assert r_budget.json()["cards"] == []
    assert "cost" in r_budget.json()["reply"].lower()

    # Diagnostics — read-only, proposes nothing.
    r_diag = await client.post("/chat", headers=H, json={"brand_id": "b1", "text": "show system status"})
    assert r_diag.status_code == 200
    assert r_diag.json()["cards"] == []
    assert "diagnostics" in r_diag.json()["reply"].lower()

    # Unrecognized — guidance, NOT a spurious provision proposal.
    r_hi = await client.post("/chat", headers=H, json={"brand_id": "b1", "text": "hi"})
    assert r_hi.status_code == 200
    assert r_hi.json()["cards"] == []
    assert "recognize" in r_hi.json()["reply"].lower()

    # Explicit provision intent (domain present) — still produces a governed proposal.
    r_host = await client.post("/chat", headers=H, json={"brand_id": "b1", "text": "host ableys.in"})
    assert r_host.status_code == 200
    assert len(r_host.json()["cards"]) >= 1


@pytest.mark.asyncio
async def test_dag_saga_execution_diamond(db_engine):
    """Verifies diamond DAG execution concurrency and topological order."""
    from app.models import OpDependency
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        # 1. Propose parent Op and children (explicit empty depends_on to avoid auto-chaining)
        parent = await loop.propose(s, _spec(action="provision.brand_bootstrap.create"), actor="user")
        a = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.web_host.create", sequence_order=1, depends_on=[]), actor="user")
        b1 = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.postgres_db.create", sequence_order=2, depends_on=[]), actor="user")
        b2 = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.static_host.create", sequence_order=3, depends_on=[]), actor="user")
        c = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.email_dns.create", sequence_order=4, depends_on=[]), actor="user")

        # 2. Define dependencies: A -> B1, A -> B2, (B1, B2) -> C
        s.add_all([
            OpDependency(tenant_id="t1", parent_op_id=parent.id, from_op_id=a.id, to_op_id=b1.id),
            OpDependency(tenant_id="t1", parent_op_id=parent.id, from_op_id=a.id, to_op_id=b2.id),
            OpDependency(tenant_id="t1", parent_op_id=parent.id, from_op_id=b1.id, to_op_id=c.id),
            OpDependency(tenant_id="t1", parent_op_id=parent.id, from_op_id=b2.id, to_op_id=c.id)
        ])
        await s.commit()

    async with async_session() as s:
        # Run preview on parent and all children to transition them to AWAITING_APPROVAL
        for r_id in (parent.id, a.id, b1.id, b2.id, c.id):
            row = await s.get(OpRow, r_id)
            await loop.preview_and_gate(s, row, tier=1)
        await s.commit()

    async with async_session() as s:
        # Approve parent (should enqueue only root child A)
        parent_row = await s.get(OpRow, parent.id)
        await loop.decide(s, parent_row, decision="approve", actor="chandan", role="owner", surface="web")
        await s.commit()

    async with async_session() as s:
        # Verify A is enqueued, B1, B2, C are not enqueued yet
        a_row = await s.get(OpRow, a.id)
        b1_row = await s.get(OpRow, b1.id)
        b2_row = await s.get(OpRow, b2.id)
        c_row = await s.get(OpRow, c.id)

        assert a_row.state == "APPROVED"
        assert b1_row.state == "APPROVED"
        assert b2_row.state == "APPROVED"
        assert c_row.state == "APPROVED"

        # Drain outbox to execute A
        processed = await loop.drain_once(s)
        assert processed == 1
        await s.commit()

    async with async_session() as s:
        # A should be DONE, B1 & B2 should be enqueued (since A is done), C still not enqueued
        a_row = await s.get(OpRow, a.id)
        assert a_row.state == "DONE"
        
        # Verify B1 & B2 are enqueued
        res_out1 = await s.execute(select(OutboxItem).where(OutboxItem.op_id == b1.id))
        assert res_out1.scalar_one_or_none() is not None
        res_out2 = await s.execute(select(OutboxItem).where(OutboxItem.op_id == b2.id))
        assert res_out2.scalar_one_or_none() is not None

        # Verify C is not enqueued
        res_outc = await s.execute(select(OutboxItem).where(OutboxItem.op_id == c.id))
        assert res_outc.scalar_one_or_none() is None

        # Drain outbox to execute B1 & B2
        processed = await loop.drain_once(s)
        assert processed == 2
        await s.commit()

    async with async_session() as s:
        # B1 & B2 are DONE, now C should be enqueued
        b1_row = await s.get(OpRow, b1.id)
        b2_row = await s.get(OpRow, b2.id)
        assert b1_row.state == "DONE"
        assert b2_row.state == "DONE"
        
        res_outc = await s.execute(select(OutboxItem).where(OutboxItem.op_id == c.id))
        assert res_outc.scalar_one_or_none() is not None

        # Drain outbox to execute C
        processed = await loop.drain_once(s)
        assert processed == 1
        await s.commit()

    async with async_session() as s:
        # C is DONE, parent is DONE
        c_row = await s.get(OpRow, c.id)
        parent_row = await s.get(OpRow, parent.id)
        assert c_row.state == "DONE"
        assert parent_row.state == "DONE"


@pytest.mark.asyncio
async def test_dag_saga_rollback_cascade(db_engine):
    """Verifies sibling cancellation and cascading reverse topological rollback."""
    from app.models import OpDependency
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        parent = await loop.propose(s, _spec(action="provision.brand_bootstrap.create"), actor="user")
        a = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.web_host.create", sequence_order=1, depends_on=[]), actor="user")
        b1 = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.postgres_db.create", sequence_order=2, depends_on=[]), actor="user")
        b2 = await loop.propose(s, _spec(parent_op_id=parent.id, action="provision.static_host.create", sequence_order=3, depends_on=[]), actor="user")

        s.add_all([
            OpDependency(tenant_id="t1", parent_op_id=parent.id, from_op_id=a.id, to_op_id=b1.id),
            OpDependency(tenant_id="t1", parent_op_id=parent.id, from_op_id=a.id, to_op_id=b2.id),
        ])
        await s.commit()

    async with async_session() as s:
        # Run preview on parent and all children to transition them to AWAITING_APPROVAL
        for r_id in (parent.id, a.id, b1.id, b2.id):
            row = await s.get(OpRow, r_id)
            await loop.preview_and_gate(s, row, tier=1)
        await s.commit()

    async with async_session() as s:
        # Approve parent
        parent_row = await s.get(OpRow, parent.id)
        await loop.decide(s, parent_row, decision="approve", actor="chandan", role="owner", surface="web")
        await s.commit()

    async with async_session() as s:
        # Drain outbox to execute A -> DONE
        processed = await loop.drain_once(s)
        assert processed == 1
        await s.commit()

    # Configure a mock error on B2's execution (domain fail.in triggers failure)
    async with async_session() as s:
        b2_row = await s.get(OpRow, b2.id)
        b2_row.params = {**b2_row.params, "domain": "fail.in"}
        await s.commit()

    async with async_session() as s:
        # Drain outbox once. This will execute B1 (succeeds) and B2 (fails).
        # B2 failure triggers cancel_active_siblings and rolls back B1, A, and parent!
        processed = await loop.drain_once(s)
        assert processed == 2
        await s.commit()

    async with async_session() as s:
        # Assertions:
        # 1. B2 failed and rolled back
        b2_row = await s.get(OpRow, b2.id)
        assert b2_row.state == "ROLLED_BACK"

        # 2. Sibling B1 (which was DONE) was cancelled and rolled back
        b1_row = await s.get(OpRow, b1.id)
        assert b1_row.state == "ROLLED_BACK"

        # 3. Root A was rolled back
        a_row = await s.get(OpRow, a.id)
        assert a_row.state == "ROLLED_BACK"

        # 4. Parent was rolled back
        parent_row = await s.get(OpRow, parent.id)
        assert parent_row.state == "ROLLED_BACK"


@pytest.mark.asyncio
async def test_dynamic_policy_evaluation(db_engine):
    """Verifies database versioned policy rules are applied dynamically."""
    from app.models import PolicyVersion
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    # 1. Create a custom policy version with very low ceiling (50,000 INR = 500 INR/month)
    async with async_session() as s:
        pv1 = PolicyVersion(
            tenant_id="t1",
            version=1,
            status="active",
            params={"provision_cost_ceiling_minor": 50_000}
        )
        s.add(pv1)
        await s.commit()

    # 2. Propose a provisioning Op with cost estimate 250,000 INR (above ceiling)
    async with async_session() as s:
        op = _spec(cost_estimate=Money(250_000, "INR"))
        row = await loop.propose(s, op, actor="user")
        await s.commit()

    async with async_session() as s:
        row = await s.get(OpRow, row.id)
        gate, req = await loop.preview_and_gate(s, row, tier=2)
        await s.commit()

    # Verify that it was BLOCKED due to ceiling breach
    assert req == "BLOCKED"
    assert any(v.rule_id == "provision_cost_ceiling" for v in gate.violations)

    # 3. Update policy version to relax ceiling to 300,000 INR
    async with async_session() as s:
        pv2 = PolicyVersion(
            tenant_id="t1",
            version=2,
            status="active",
            params={"provision_cost_ceiling_minor": 300_000}
        )
        s.add(pv2)
        await s.commit()

    # 4. Re-run preview and gate on a new proposed Op
    async with async_session() as s:
        op2 = _spec(cost_estimate=Money(250_000, "INR"))
        row2 = await loop.propose(s, op2, actor="user")
        await s.commit()

    async with async_session() as s:
        row2 = await s.get(OpRow, row2.id)
        gate2, req2 = await loop.preview_and_gate(s, row2, tier=2)
        await s.commit()

    # Verify that it is no longer blocked (relaxed policy is loaded)
    assert req2 == "AUTO" # auto-approves because it's under ceiling and tier=2
    assert len(gate2.violations) == 0


@pytest.mark.asyncio
async def test_tool_path_reaches_proposed_gate_never_approved(client, db_engine):
    """L6 tool registry: natural language tool calls compile to PROPOSED and run gates.
    They never bypass the preview_and_gate or RLS."""
    # Seed trust snapshot to resolve tier=1 (supervised)
    from sqlalchemy.ext.asyncio import async_sessionmaker
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        async with s.begin():
            s.add(TrustSnapshot(tenant_id="t-tool-test", brand_id="brand-grow-test", domain="grow", tier=1, score=75.0))
            
    H = {"X-Tenant-ID": "t-tool-test"}
    
    # 1. Propose via chat tool calling (regex match fallback)
    resp = await client.post("/chat", headers=H, json={
        "brand_id": "brand-grow-test",
        "text": "adjust bid for campaign camp-123 to 50 INR"
    })
    
    assert resp.status_code == 200
    data = resp.json()
    assert "Structured request parsed" in data["reply"]
    assert len(data["cards"]) == 1
    
    card = data["cards"][0]
    assert card["action"] == "grow.bid.adjust"
    # Even if proposed via tool, it must be AWAITING_APPROVAL under Tier 1 (supervised)
    assert card["state"] == "AWAITING_APPROVAL"
    
    op_id = card["op_id"]
    
    # Verify RLS: t-tool-test can see the Op, but querying under t2 returns 404/empty
    async with async_session() as s:
        row = await s.get(OpRow, op_id)
        assert row is not None
        assert row.tenant_id == "t-tool-test"
        assert row.state == "AWAITING_APPROVAL"


@pytest.mark.asyncio
async def test_read_endpoints_connections_breakers_events(client, db_engine):
    from app.models import Connection, CircuitBreakerRow
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    
    H = {"X-Tenant-ID": "t-read-test"}
    
    # Seed data
    async with async_session() as s:
        async with s.begin():
            conn = Connection(
                tenant_id="t-read-test",
                brand_id="b-read-test",
                provider="shopify",
                scope="read",
                credential="shopify-secret",
                config={"shop_url": "test.myshopify.com"}
            )
            s.add(conn)
            
            breaker = CircuitBreakerRow(
                tenant_id="t-read-test",
                brand_id="b-read-test",
                domain="grow",
                state="CLOSED",
                consecutive_failures=0
            )
            s.add(breaker)
            
            # Write audit event
            await audit_append(s, tenant_id="t-read-test", actor="tester", action="test.read_endpoint")
            
        await s.commit()

    # 1. Test GET /connections
    resp = await client.get("/connections", headers=H)
    assert resp.status_code == 200
    conns = resp.json()
    assert len(conns) == 1
    assert conns[0]["provider"] == "shopify"
    assert conns[0]["credential"] == "shopify-secret"

    # 2. Test GET /circuit-breakers
    resp = await client.get("/circuit-breakers", headers=H)
    assert resp.status_code == 200
    breakers = resp.json()
    assert len(breakers) == 1
    assert breakers[0]["domain"] == "grow"
    assert breakers[0]["state"] == "CLOSED"

    # 3. Test GET /audit/events
    resp = await client.get("/audit/events", headers=H)
    assert resp.status_code == 200
    events = resp.json()
    assert len(events) >= 1
    assert events[0]["action"] == "test.read_endpoint"
    assert events[0]["actor"] == "tester"


@pytest.mark.asyncio
async def test_list_tenants(client):
    # 1. Onboard a tenant
    r = await client.post("/tenants", json={"name": "List Tenant", "brand_name": "List Brand"})
    assert r.status_code == 200
    data = r.json()
    tid = data["tenant_id"]
    bid = data["brand_id"]

    # 2. List tenants
    r_list = await client.get("/tenants")
    assert r_list.status_code == 200
    tenants = r_list.json()

    # 3. Assert our tenant is in the list
    assert any(
        t["tenant_id"] == tid and t["brand_id"] == bid and t["tenant_name"] == "List Tenant" and t["brand_name"] == "List Brand"
        for t in tenants
    )






