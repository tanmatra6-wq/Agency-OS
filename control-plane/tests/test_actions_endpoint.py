"""Structured /actions endpoint — the console's explicit Action Panel backend.

Replaces free-text chat parsing: the operator picks a tool + structured params,
which routes through the tool registry -> propose -> preview_and_gate (gates kept).
"""


async def test_actions_catalog_lists_tools(client):
    r = await client.post("/tenants", json={"name": "Cat", "brand_name": "B"})
    tid = r.json()["tenant_id"]

    r = await client.get("/actions/catalog", headers={"X-Tenant-ID": tid})
    assert r.status_code == 200
    names = {a["name"] for a in r.json()["actions"]}
    # the 3 pre-existing grow tools + the new structured operator actions
    assert {"provision_web_host", "manage_diagnostics", "grow_campaign_pause"} <= names


async def test_actions_submit_provision_web_host(client):
    r = await client.post("/tenants", json={"name": "Prov", "brand_name": "B"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]

    r = await client.post(
        "/actions",
        headers={"X-Tenant-ID": tid},
        json={"tool": "provision_web_host", "brand_id": bid, "params": {"domain": "woktok.in"}},
    )
    assert r.status_code == 200, r.text
    cards = r.json()["cards"]
    assert len(cards) == 1
    assert cards[0]["action"] == "provision.web_host.create"

    # The proposed Op shows up in the governed queue (no chat involved).
    r = await client.get("/ops", headers={"X-Tenant-ID": tid})
    assert any(o["action"] == "provision.web_host.create" for o in r.json())


async def test_actions_connect_is_governed(client):
    """Connector directory connects flow through the governed Op path (not a raw
    Connection DB write): a connect tool proposes a *.connect Op in the queue."""
    r = await client.post("/tenants", json={"name": "Conn", "brand_name": "B"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]

    r = await client.post(
        "/actions",
        headers={"X-Tenant-ID": tid},
        json={"tool": "grow_google_ads_connect", "brand_id": bid, "params": {"credential": "google-ads-token"}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["cards"][0]["action"] == "grow.google.connect"

    r = await client.get("/ops", headers={"X-Tenant-ID": tid})
    assert any(o["action"] == "grow.google.connect" for o in r.json())


async def test_actions_rejects_unknown_tool(client):
    r = await client.post("/tenants", json={"name": "Bad", "brand_name": "B"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]

    r = await client.post(
        "/actions",
        headers={"X-Tenant-ID": tid},
        json={"tool": "does_not_exist", "brand_id": bid, "params": {}},
    )
    assert r.status_code == 400


async def test_actions_rejects_bad_params(client):
    r = await client.post("/tenants", json={"name": "BadP", "brand_name": "B"})
    tid, bid = r.json()["tenant_id"], r.json()["brand_id"]

    # provision_web_host requires `domain`; omitting it is a 400, not a 500.
    r = await client.post(
        "/actions",
        headers={"X-Tenant-ID": tid},
        json={"tool": "provision_web_host", "brand_id": bid, "params": {}},
    )
    assert r.status_code == 400
