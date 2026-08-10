"""Regression: /chat must enqueue an outbox drain.

Auto-approved (within-policy) Ops reach APPROVED with a PENDING outbox item via
loop.preview_and_gate, but nothing executes them unless the outbox is drained.
/ops/{id}/decision drains on manual approval; /chat (and /intents) auto-approve
without a /decision call, so they must drain themselves — otherwise approved Ops
sit at APPROVED forever (the prod "host <domain>" symptom)."""
import app.routers.actions as actionsmod


async def test_chat_enqueues_outbox_drain(client, monkeypatch):
    called = {}

    def fake_enqueue(background_tasks, session_maker=None):
        called["yes"] = True

    monkeypatch.setattr(actionsmod, "enqueue_drain", fake_enqueue)

    r = await client.post("/tenants", json={"name": "AutoExec", "brand_name": "Brand"})
    assert r.status_code == 200
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]

    # A recognized provisioning intent -> proposes + gates an Op, then must drain.
    r = await client.post(
        "/chat",
        headers={"X-Tenant-ID": tid},
        json={"brand_id": bid, "text": "host woktok.in please"},
    )
    assert r.status_code == 200
    assert called.get("yes"), "/chat must enqueue an outbox drain so approved Ops execute"
