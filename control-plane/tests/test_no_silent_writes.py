import ast
import os
import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient

# -----------------------------------------------------------------------------
# 1. CONSTANTS & DEFINITIONS
# -----------------------------------------------------------------------------

STATE_MODELS = {
    'OpRow', 'Connection', 'Campaign', 'BrandProperty', 'SpendFact', 
    'Touchpoint', 'Approval', 'AuditEvent', 'Order', 'OrderLine', 
    'Refund', 'CircuitBreakerRow', 'OutboxItem'
}

# Directories/files where direct writes are allowed
ALLOWED_PATHS = [
    'app/adapters',
    'app/kernel/loop.py',
    'migrations',
]

# -----------------------------------------------------------------------------
# 2. STATIC CHECK (AST VISITOR)
# -----------------------------------------------------------------------------

class SilentWriteVisitor(ast.NodeVisitor):
    def __init__(self, filename, allowed_models=None):
        self.filename = filename
        self.violations = []
        self.local_vars = {}
        self.allowed_models = allowed_models or set()

    def visit_FunctionDef(self, node):
        old_vars = self.local_vars.copy()
        self.generic_visit(node)
        self.local_vars = old_vars

    def visit_AsyncFunctionDef(self, node):
        old_vars = self.local_vars.copy()
        self.generic_visit(node)
        self.local_vars = old_vars

    def visit_Assign(self, node):
        # Track local variable assignments to class instantiations or DB gets
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            var_name = node.targets[0].id
            
            # Case 1: direct instantiation: var = Class(...)
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name):
                class_name = node.value.func.id
                self.local_vars[var_name] = class_name
                
            # Case 2: await session.get(Class, ...) or session.get(Class, ...)
            else:
                call_node = None
                if isinstance(node.value, ast.Await):
                    call_node = node.value.value
                elif isinstance(node.value, ast.Call):
                    call_node = node.value
                    
                if call_node and isinstance(call_node, ast.Call) and isinstance(call_node.func, ast.Attribute):
                    if call_node.func.attr == 'get' and self._get_receiver_name(call_node.func.value) in ('session', 's', 'db', 'db_session'):
                        if call_node.args and isinstance(call_node.args[0], ast.Name):
                            class_name = call_node.args[0].id
                            self.local_vars[var_name] = class_name
        self.generic_visit(node)

    def visit_Call(self, node):
        # Detect obj.add(...) or obj.delete(...) on session-like objects
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in ('add', 'delete'):
                receiver_name = self._get_receiver_name(node.func.value)
                if receiver_name in ('session', 's', 'db', 'db_session'):
                    for arg in node.args:
                        # If we can prove it is an allowed non-state model, we skip it
                        if self._is_allowed_non_state_model(arg):
                            continue
                        # Otherwise, if it's a known state model OR if we can't prove it's allowed, we flag it
                        self.violations.append((
                            node.lineno, 
                            f"Potential direct write via {receiver_name}.{node.func.attr}() in {self.filename}:{node.lineno}"
                        ))
                else:
                    # If receiver is not named session, still flag if the argument is explicitly a StateModel
                    for arg in node.args:
                        if self._is_state_model(arg):
                            self.violations.append((
                                node.lineno, 
                                f"Direct write of state model {self._get_model_name(arg)} in {self.filename}:{node.lineno}"
                            ))

            # Detect query.update(...) or query.delete(...)
            elif node.func.attr in ('update', 'delete'):
                if self._references_state_model(node.func.value):
                    self.violations.append((
                        node.lineno, 
                        f"Direct query .{node.func.attr}() against state model in {self.filename}:{node.lineno}"
                    ))

        # Detect direct update(Model) or delete(Model) calls (SQLAlchemy 2.0 style)
        elif isinstance(node.func, ast.Name):
            if node.func.id in ('update', 'delete'):
                if node.args and isinstance(node.args[0], ast.Name) and node.args[0].id in STATE_MODELS and node.args[0].id not in self.allowed_models:
                    self.violations.append((
                        node.lineno, 
                        f"Direct SQLAlchemy {node.func.id}({node.args[0].id}) call in {self.filename}:{node.lineno}"
                    ))

        self.generic_visit(node)

    def _get_receiver_name(self, node):
        if isinstance(node, ast.Name):
            return node.id
        elif isinstance(node, ast.Attribute):
            return node.attr
        return None

    def _is_state_model(self, node):
        model_name = self._get_model_name(node)
        if model_name in STATE_MODELS and model_name not in self.allowed_models:
            return True
        return False

    def _get_model_name(self, node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id
        elif isinstance(node, ast.Name):
            return self.local_vars.get(node.id, node.id)
        return str(node)

    def _is_allowed_non_state_model(self, node):
        allowed_non_state_models = {
            'Tenant', 'Brand', 'PolicyVersion', 'ProcessedWebhookMessage', 
            'OpTrace', 'TrustEvent', 'TrustSnapshot', 'CostEntry', 'ConsentBasis',
            'ShadowDecision', 'BrandObjective'
        }.union(self.allowed_models)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id in allowed_non_state_models
        elif isinstance(node, ast.Name):
            class_name = self.local_vars.get(node.id)
            return class_name in allowed_non_state_models
        return False

    def _references_state_model(self, node):
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id in STATE_MODELS and child.id not in self.allowed_models:
                return True
        return False


def check_file_for_silent_writes(filepath, allowed_models=None):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError as e:
        return [(-1, f"Syntax error: {e}")]
    
    visitor = SilentWriteVisitor(filepath, allowed_models=allowed_models)
    visitor.visit(tree)
    return visitor.violations

# -----------------------------------------------------------------------------
# 3. PYTEST TEST CASES
# -----------------------------------------------------------------------------

def test_static_no_silent_writes():
    """Verify that no file outside allowed paths contains direct DB writes to state models."""
    app_dir = os.path.join(os.path.dirname(__file__), '../app')
    violations = []

    for root, _, files in os.walk(app_dir):
        for file in files:
            if file.endswith('.py'):
                full_path = os.path.normpath(os.path.join(root, file))
                
                # Check if path is allowed
                allowed = False
                for ap in ALLOWED_PATHS:
                    normalized_ap = os.path.normpath(ap)
                    relative_path = os.path.relpath(full_path, os.path.join(os.path.dirname(__file__), '..'))
                    if relative_path.startswith(normalized_ap):
                        allowed = True
                        break
                
                if allowed:
                    continue

                # File-specific allowed models override
                allowed_models = set()
                relative_path_key = os.path.relpath(full_path, os.path.join(os.path.dirname(__file__), '..'))
                if relative_path_key == os.path.normpath('app/kernel/services.py'):
                    allowed_models = {'AuditEvent'}
                elif relative_path_key == os.path.normpath('app/services/attribution.py'):
                    # Legacy exception: Meridian calibration writes results directly.
                    # TODO: Refactor to a governed Op (manage.attribution.calibrate) in ManageAdapter.
                    allowed_models = {'BrandProperty'}
                elif relative_path_key == os.path.normpath('app/main.py'):
                    # Operator-level admin routes in main.py manage Tenant records directly.
                    allowed_models = {'Tenant'}
                elif relative_path_key == os.path.normpath('app/routers/onboarding.py'):
                    # Onboarding router seeds Connection directly.
                    # TODO: route through governed Ops once self-serve onboarding is built.
                    allowed_models = {'Connection'}
                elif relative_path_key == os.path.normpath('app/services/brand_identity.py'):
                    # RAG bootstrapping writes BrandProperty directly.
                    # TODO: route through governed Ops once self-serve onboarding is built.
                    allowed_models = {'BrandProperty'}

                file_violations = check_file_for_silent_writes(full_path, allowed_models=allowed_models)
                violations.extend(file_violations)

    # We expect some known violations for now, or we fail if there are any.
    # If we have violations in app/services/attribution.py, we should flag them.
    assert not violations, f"Found silent write violations:\n" + "\n".join([v[1] for v in violations])


def test_static_ast_visitor_negative():
    """Verify that the AST visitor successfully catches synthetic violations (Negative Test)."""
    synthetic_code = """
def bad_function(session):
    # Violation 1: Direct add of a state model
    conn = Connection(tenant_id="t1", provider="shopify")
    session.add(conn)

    # Violation 2: Direct query update
    session.query(Campaign).filter(Campaign.id == "c1").update({"status": "inactive"})

    # Violation 3: Direct SQLAlchemy 2.0 update
    session.execute(update(BrandProperty).values(status="active"))
    
    # Allowed: Direct add of a non-state model
    trace = OpTrace(op_id="op1", tenant_id="t1", kind="test")
    session.add(trace)
    
    # Allowed: dictionary update
    my_dict = {}
    my_dict.update({"a": 1})
"""
    try:
        tree = ast.parse(synthetic_code)
    except SyntaxError as e:
        pytest.fail(f"Failed to parse synthetic code: {e}")

    visitor = SilentWriteVisitor("synthetic_test.py")
    visitor.visit(tree)
    
    # We expect exactly 3 violations
    assert len(visitor.violations) == 3, f"Expected 3 violations, got {len(visitor.violations)}:\n" + "\n".join([v[1] for v in visitor.violations])
    
    messages = [v[1] for v in visitor.violations]
    assert any("Potential direct write via session.add()" in m for m in messages)
    assert any("Direct query .update() against state model" in m for m in messages)
    assert any("Direct SQLAlchemy update(BrandProperty) call" in m for m in messages)
    assert not any("OpTrace" in m for m in messages)
    assert not any("my_dict" in m for m in messages)

# -----------------------------------------------------------------------------
# 4. RUNTIME GUARD (SQLALCHEMY LISTENER)
# -----------------------------------------------------------------------------

class RuntimeSilentWriteViolation(Exception):
    pass

active_request_context = False

@pytest.fixture(autouse=True)
def setup_runtime_silent_write_guard():
    """Register a SQLAlchemy listener that blocks direct state model writes during HTTP requests."""
    
    def before_flush_listener(session, flush_context, instances):
        global active_request_context
        # Only enforce during active HTTP request context
        if not active_request_context:
            return

        # Check if any state model is being mutated
        for obj in session.new.union(session.dirty).union(session.deleted):
            model_name = obj.__class__.__name__
            if model_name in STATE_MODELS:
                # Exception: We allow OpRow, AuditEvent, OutboxItem to be written during request 
                # IF they are part of a governed proposal (which we can check or just allow these specific models
                # since they are the metadata of the proposal itself).
                # Actually, the route handler is allowed to write OpRow, AuditEvent, OutboxItem, OpTrace.
                # It is NOT allowed to write Connection, Campaign, BrandProperty, SpendFact, Touchpoint, Approval, Order, OrderLine, Refund, CircuitBreakerRow.
                allowed_request_models = {'OpRow', 'AuditEvent', 'OutboxItem', 'OpTrace', 'ProcessedWebhookMessage'}
                if model_name not in allowed_request_models:
                    raise RuntimeSilentWriteViolation(
                        f"Silent write detected! Direct mutation of {model_name} is prohibited during HTTP requests. "
                        f"All state changes must flow through a governed Op and execute asynchronously."
                    )

    # Register listener on Session class
    event.listen(Session, 'before_flush', before_flush_listener)
    yield
    # Unregister listener after test
    event.remove(Session, 'before_flush', before_flush_listener)


# -----------------------------------------------------------------------------
# 5. RUNTIME NEGATIVE TEST
# -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runtime_guard_negative(session):
    """Verify that the runtime guard blocks direct writes during mock HTTP requests (Negative Test)."""
    global active_request_context
    
    from app.models import Connection, Tenant
    
    # 1. Verify writes are allowed OUTSIDE request context (e.g. in tests or background workers)
    active_request_context = False
    conn = Connection(tenant_id="t1", brand_id="b1", provider="shopify", credential="ref", status="active")
    session.add(conn)
    await session.flush() # Should succeed
    await session.delete(conn)
    await session.flush()
    
    # 2. Verify writes to non-state models (like Tenant) are allowed INSIDE request context
    active_request_context = True
    tenant = Tenant(name="New Tenant")
    session.add(tenant)
    await session.flush() # Should succeed
    await session.delete(tenant)
    await session.flush()
    
    # 3. Verify writes to state models (like Connection) are BLOCKED INSIDE request context
    active_request_context = True
    conn2 = Connection(tenant_id="t1", brand_id="b1", provider="shopify", credential="ref", status="active")
    session.add(conn2)
    
    with pytest.raises(RuntimeSilentWriteViolation) as excinfo:
        await session.flush()
        
    assert "Silent write detected!" in str(excinfo.value)
    assert "Connection" in str(excinfo.value)
    
    # Clean up session
    await session.rollback()
    active_request_context = False
