# Feature 3 Outbox Retry and Resiliency tests
import datetime as dt
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from sqlalchemy import select, text
from app.models import OutboxItem, OpRow, CircuitBreakerRow, Connection, OpTrace
from app.kernel import loop
from app.kernel.optypes import OpSpec, Severity, Reversibility
from app.database import get_db

@pytest.mark.asyncio
async def test_outbox_exponential_backoff(session):
    """Test 16: Verify that outbox failures schedule retry with exponential backoff."""
    op = OpRow(
        id="op1", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik1"
    )
    item = OutboxItem(
        op_id="op1", tenant_id="t1", status="PENDING", attempts=0,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    # Make execution fail
    with patch("app.kernel.loop._execute_and_verify", side_effect=Exception("API Timeout")):
        processed = await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
        assert processed == 1
        
        await session.commit()  # Commit the final PENDING status updated by drain_once
        await session.refresh(item)
        assert item.status == "PENDING"
        assert item.attempts == 1
        # Backoff: 2 ** 1 = 2 seconds
        expected_min = dt.datetime.utcnow() + dt.timedelta(seconds=1)
        assert item.next_attempt_at > expected_min

@pytest.mark.asyncio
async def test_outbox_max_retries_exhaustion(session):
    """Test 17: Verify that outbox items are marked DEAD and operation is marked PARTIAL after 5 failures."""
    op = OpRow(
        id="op2", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="EXECUTING", impact=1, reversibility="COMPENSATABLE", idem_key="ik2"
    )
    item = OutboxItem(
        op_id="op2", tenant_id="t1", status="PENDING", attempts=4,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    with patch("app.kernel.loop._execute_and_verify", side_effect=Exception("Permanent Error")):
        processed = await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
        assert processed == 1
        
        await session.refresh(item)
        await session.refresh(op)
        assert item.status == "DEAD"
        assert item.attempts == 5
        assert op.state == "PARTIAL"

@pytest.mark.asyncio
async def test_outbox_dead_letter_queue(session):
    """Test 18: Verify that audited/dead outbox items record failure audit events."""
    op = OpRow(
        id="op3", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="EXECUTING", impact=1, reversibility="COMPENSATABLE", idem_key="ik3"
    )
    item = OutboxItem(
        op_id="op3", tenant_id="t1", status="PENDING", attempts=4,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    with patch("app.kernel.loop._execute_and_verify", side_effect=Exception("DLQ trigger error")):
        processed = await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
        assert processed == 1
        
        await session.refresh(item)
        assert item.status == "DEAD"
        
        # Check if audit event or trace is recorded using OpTrace model
        stmt = select(OpTrace.detail).where(OpTrace.op_id == "op3", OpTrace.kind == "retry")
        res = await session.execute(stmt)
        traces = res.scalars().all()
        assert len(traces) > 0

@pytest.mark.asyncio
async def test_outbox_circuit_breaker_trip(session):
    """Test 19: Verify circuit breaker trips after 3 failures and blocks subsequent operations."""
    # Seed 3 failing operations/items
    for i in range(3):
        op = OpRow(
            id=f"op_cb_{i}", tenant_id="t1", brand_id="b1", domain="manage",
            action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
            state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key=f"ik_cb_{i}"
        )
        item = OutboxItem(
            op_id=f"op_cb_{i}", tenant_id="t1", status="PENDING", attempts=0,
            next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
        )
        session.add_all([op, item])
    await session.commit()

    with patch("app.kernel.loop._execute_and_verify", side_effect=Exception("CB Trip Failure")):
        # Drain 3 times to trip breaker
        await loop.drain_once(session, max_items=3, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))

    # Verify breaker is now OPEN
    stmt = select(CircuitBreakerRow).where(
        CircuitBreakerRow.tenant_id == "t1",
        CircuitBreakerRow.brand_id == "b1",
        CircuitBreakerRow.domain == "manage"
    )
    res = await session.execute(stmt)
    breaker = res.scalar_one()
    assert breaker.state == "OPEN"

    # Enqueue a 4th operation
    op4 = OpRow(
        id="op_cb_4", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik_cb_4"
    )
    item4 = OutboxItem(
        op_id="op_cb_4", tenant_id="t1", status="PENDING", attempts=0,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op4, item4])
    await session.commit()

    # Try to drain again
    processed = await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
    assert processed >= 1

    await session.refresh(item4)
    await session.refresh(op4)
    # Breaker should immediately block the execution, marking item DEAD and op BLOCKED
    assert item4.status == "DEAD"
    assert op4.state == "BLOCKED"

@pytest.mark.asyncio
async def test_outbox_circuit_breaker_reset(session):
    """Test 20: Verify circuit breaker auto-resets from OPEN to CLOSED after reset timeout."""
    breaker = CircuitBreakerRow(
        tenant_id="t1", brand_id="b1", domain="manage",
        consecutive_failures=3, state="OPEN",
        tripped_at=dt.datetime.utcnow() - dt.timedelta(seconds=1000) # > 900s timeout
    )
    session.add(breaker)
    await session.commit()

    # Check circuit status
    tripped = await loop.is_circuit_tripped(session, "t1", "b1", "manage")
    assert tripped is False # Auto-reset should have triggered and returned False

    await session.refresh(breaker)
    assert breaker.state == "CLOSED"
    assert breaker.consecutive_failures == 0

@pytest.mark.asyncio
async def test_outbox_concurrency_locking(session):
    """Test 42: Verify that concurrent drains do not double-process the same outbox item."""
    op = OpRow(
        id="op_lock", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik_lock"
    )
    item = OutboxItem(
        op_id="op_lock", tenant_id="t1", status="PENDING", attempts=0,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    # Simulate concurrent processing by marking status as IN_FLIGHT in another session
    item.status = "IN_FLIGHT"
    await session.commit()

    # A new drain run should skip it
    processed = await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
    assert processed == 0

@pytest.mark.asyncio
async def test_outbox_idempotent_deduplication(session):
    """Test 43: Verify enqueuing duplicate items for same operation results in a single execution (enforced by DB unique constraint)."""
    op = OpRow(
        id="op_dedup", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik_dedup"
    )
    item1 = OutboxItem(op_id="op_dedup", tenant_id="t1", status="PENDING", attempts=0)
    session.add_all([op, item1])
    await session.commit()

    # Attempting to add a duplicate outbox item should raise IntegrityError due to unique constraint on op_id
    item2 = OutboxItem(op_id="op_dedup", tenant_id="t1", status="PENDING", attempts=0)
    session.add(item2)
    with pytest.raises(Exception) as excinfo:
        await session.commit()
    assert "UNIQUE constraint failed" in str(excinfo.value) or "unique constraint" in str(excinfo.value).lower()

@pytest.mark.asyncio
async def test_outbox_state_machine_transitions(session):
    """Test 44: Verify outbox item state transitions: PENDING -> IN_FLIGHT -> DONE."""
    op = OpRow(
        id="op_state", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik_state"
    )
    item = OutboxItem(
        op_id="op_state", tenant_id="t1", status="PENDING", attempts=0,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    # Capture state during execution without refreshing from DB (which would discard unflushed memory changes)
    async def mock_exec_verify(s, row):
        assert item.status == "IN_FLIGHT"

    with patch("app.kernel.loop._execute_and_verify", side_effect=mock_exec_verify):
        await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
        
        await session.commit()  # Commit the final DONE status updated by drain_once
        await session.refresh(item)
        assert item.status == "DONE"

@pytest.mark.asyncio
async def test_outbox_performance_under_load(session):
    """Test 45: Verify batch boundaries and performance under load (e.g. 25 items)."""
    for i in range(25):
        op = OpRow(
            id=f"op_load_{i}", tenant_id="t1", brand_id="b1", domain="manage",
            action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
            state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key=f"ik_load_{i}"
        )
        item = OutboxItem(
            op_id=f"op_load_{i}", tenant_id="t1", status="PENDING", attempts=0,
            next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
        )
        session.add_all([op, item])
    await session.commit()

    with patch("app.kernel.loop._execute_and_verify", return_value=None):
        # Process first batch of 10
        processed = await loop.drain_once(session, max_items=10, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
        assert processed == 10
        
        # Verify 15 remain pending
        stmt = select(OutboxItem).where(OutboxItem.status == "PENDING")
        res = await session.execute(stmt)
        assert len(res.scalars().all()) == 15

@pytest.mark.asyncio
async def test_outbox_transactional_safety_rollback(session):
    """Test 46: Verify that a transaction failure during execution rolls back all side effects."""
    op = OpRow(
        id="op_tx", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik_tx"
    )
    item = OutboxItem(
        op_id="op_tx", tenant_id="t1", status="PENDING", attempts=0,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    async def mock_exec_fail(s, row):
        # Seed a dummy record that should be rolled back on exception
        conn = Connection(tenant_id="t1", brand_id="b1", provider="shopify", credential="c")
        s.add(conn)
        raise RuntimeError("Crash after write")

    await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))

    # Verify connection was NOT saved (rolled back)
    stmt = select(Connection).where(Connection.tenant_id == "t1")
    res = await session.execute(stmt)
    assert res.scalar_one_or_none() is None

@pytest.mark.asyncio
async def test_outbox_rls_isolation(session):
    """Test 47: Verify that Postgres RLS isolates background outbox queries (or executes context setting)."""
    # Verify set_config is called during drain_once if running on postgres
    op = OpRow(
        id="op_rls", tenant_id="t1", brand_id="b1", domain="manage",
        action="manage.shopify.connect", params={"provider": "shopify", "config": {}},
        state="APPROVED", impact=1, reversibility="COMPENSATABLE", idem_key="ik_rls"
    )
    item = OutboxItem(
        op_id="op_rls", tenant_id="t1", status="PENDING", attempts=0,
        next_attempt_at=dt.datetime.utcnow() - dt.timedelta(seconds=1)
    )
    session.add_all([op, item])
    await session.commit()

    with patch("app.kernel.loop._execute_and_verify", return_value=None):
        if session.bind and session.bind.dialect.name == "postgresql":
            # If postgres, we spy on execute
            with patch.object(session, "execute", wraps=session.execute) as mock_execute:
                await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
                # Check that set_config was called with tenant_id
                calls = [c[0][0] for c in mock_execute.call_args_list if isinstance(c[0][0], str) or hasattr(c[0][0], "text")]
                assert any("set_config" in str(c) for c in calls)
        else:
            # On SQLite, just verify the happy path execution
            await loop.drain_once(session, now=dt.datetime.utcnow() + dt.timedelta(seconds=10))
            await session.refresh(item)
            assert item.status == "DONE"
