# RLS coverage and documentation safety tests
import os
import pytest
import re
import hashlib
import datetime as dt
from sqlalchemy import text, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.pool import NullPool

from app.models import Base
from migrate import migrate as run_migrate

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/agency_os",
)


def _pg_reachable(url: str) -> bool:
    import asyncio
    async def check():
        engine = create_async_engine(url, connect_args={"timeout": 1.0}, poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()
    try:
        return asyncio.run(check())
    except Exception:
        return False


APP_ROLE, APP_PW = "aos_rls_coverage_role", "aos_rls_coverage_password"
WORKER_ROLE, WORKER_PW = "aos_rls_coverage_worker", "aos_rls_coverage_worker_password"


@pytest.fixture(scope="module")
async def setup_postgres_schema():
    """Initializes the postgres database schema, creates the RLS role, and grants permissions."""
    admin_engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    
    # Reset database schema.
    async with admin_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))

    # Run migrations to create tables & enable RLS
    await run_migrate(admin_engine)

    # Create restricted RLS app role.
    async with admin_engine.begin() as conn:
        await conn.execute(text(
            f"DO $$ BEGIN CREATE ROLE {APP_ROLE} LOGIN PASSWORD '{APP_PW}'; "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"))
        await conn.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}"))
        await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}"))

    # Create the NON-superuser worker role and configure it exactly like prod.
    async with admin_engine.begin() as conn:
        await conn.execute(text(
            f"DO $$ BEGIN CREATE ROLE {WORKER_ROLE} LOGIN PASSWORD '{WORKER_PW}'; "
            f"EXCEPTION WHEN duplicate_object THEN NULL; END $$"))
        await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {WORKER_ROLE}"))
        await conn.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {WORKER_ROLE}"))
        await conn.execute(text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {WORKER_ROLE}"))
        await conn.execute(text(
            "DO $$ DECLARE r record; BEGIN "
            "FOR r IN SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='r' AND c.relrowsecurity LOOP "
            "EXECUTE format('DROP POLICY IF EXISTS worker_bypass ON public.%I', r.relname); "
            "EXECUTE format('CREATE POLICY worker_bypass ON public.%I AS PERMISSIVE FOR ALL TO "
            f"{WORKER_ROLE} USING (true) WITH CHECK (true)', r.relname); "
            "END LOOP; END $$"))

    yield admin_engine

    # Cleanup RLS role and tables.
    async with admin_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        for role in (APP_ROLE, WORKER_ROLE):
            # Revoke all privileges and drop owned objects to avoid DependentObjectsStillExistError
            await conn.execute(text(f"DROP OWNED BY {role}"))
            await conn.execute(text(f"DROP ROLE IF EXISTS {role}"))


def _get_tables_with_tenant_id():
    """Find all SQLAlchemy tables that have a tenant_id column."""
    tables = set()
    for mapper in Base.registry.mappers:
        model_class = mapper.class_
        if hasattr(model_class, '__table__'):
            table = model_class.__table__
            if 'tenant_id' in table.columns:
                tables.add(table.name)
    return tables


def test_architecture_rls_documentation():
    """Verify that ARCHITECTURE.md §3 accurately lists all RLS-enabled tables."""
    arch_path = os.path.join(os.path.dirname(__file__), '../../ARCHITECTURE.md')
    with open(arch_path, 'r', encoding='utf-8') as f:
        content = f.read()
        
    # Find the RLS table list using regex
    match = re.search(r'The following tables are subject to RLS policy:\s*([^\n]+)', content)
    assert match, "Could not find the RLS table list in ARCHITECTURE.md"
    
    # Extract backtick-wrapped names
    declared_tables = set(re.findall(r'`([^`]+)`', match.group(1)))
    
    db_tables = _get_tables_with_tenant_id()
    
    missing_in_doc = db_tables - declared_tables
    extra_in_doc = declared_tables - db_tables
    
    assert not missing_in_doc, f"Tables with tenant_id missing from ARCHITECTURE.md RLS list: {missing_in_doc}"
    assert not extra_in_doc, f"Tables listed in ARCHITECTURE.md but don't have tenant_id column: {extra_in_doc}"


@pytest.mark.asyncio
@pytest.mark.skipif(not _pg_reachable(DATABASE_URL), reason="Postgres not reachable")
async def test_postgres_rls_coverage(setup_postgres_schema):
    """Verify that every table with tenant_id (plus tenants) has RLS enabled, forced, and carries both isolation policies."""
    db_tables = _get_tables_with_tenant_id()
    
    # The 'tenants' table itself must also be RLS protected (uses 'id' instead of 'tenant_id')
    all_target_tables = db_tables.union({'tenants'})
    
    engine = create_async_engine(DATABASE_URL, poolclass=NullPool)
    async with engine.connect() as conn:
        # 1. Query pg_class for RLS enabled (relrowsecurity) and RLS forced (relforcerowsecurity)
        stmt_rls = text("""
            SELECT relname, relrowsecurity, relforcerowsecurity 
            FROM pg_class 
            WHERE relname = ANY(:tables) AND relkind = 'r' AND relnamespace = 'public'::regnamespace;
        """)
        res_rls = await conn.execute(stmt_rls, {"tables": list(all_target_tables)})
        rls_info = {row.relname: (row.relrowsecurity, row.relforcerowsecurity) for row in res_rls}

        # 2. Query pg_policies to get all active policies on these tables
        stmt_pol = text("""
            SELECT tablename, policyname 
            FROM pg_policies 
            WHERE schemaname = 'public' AND tablename = ANY(:tables);
        """)
        res_pol = await conn.execute(stmt_pol, {"tables": list(all_target_tables)})
        
        policies_info = {}
        for row in res_pol:
            policies_info.setdefault(row.tablename, set()).add(row.policyname)
            
    # 3. Assert invariants for each table
    for table in all_target_tables:
        # Assert table exists in DB
        assert table in rls_info, f"Table '{table}' not found in public schema in the database"
        
        row_security, force_security = rls_info[table]
        
        # Assert RLS is enabled
        assert row_security, f"Row-Level Security (RLS) is NOT enabled on table '{table}'"
        
        # Assert RLS is forced (so owner/worker bypass is explicit, not accidental)
        assert force_security, f"FORCE Row-Level Security is NOT enabled on table '{table}'"
        
        # Assert policies exist
        table_policies = policies_info.get(table, set())
        
        # Assert 'tenant_isolation' policy exists
        assert 'tenant_isolation' in table_policies, f"Table '{table}' is missing the 'tenant_isolation' policy"
        
        # Assert 'worker_bypass' in table_policies, f"Table '{table}' is missing the 'worker_bypass' policy"
        assert 'worker_bypass' in table_policies, f"Table '{table}' is missing the 'worker_bypass' policy"


# -----------------------------------------------------------------------------
# NEGATIVE TESTS (Asserting that the checks actually fail on violations)
# -----------------------------------------------------------------------------

def test_architecture_rls_documentation_negative():
    """Verify that test_architecture_rls_documentation fails if there is a mismatch."""
    from unittest.mock import patch
    # Mock _get_tables_with_tenant_id to return an extra table not in ARCHITECTURE.md
    with patch('tests.test_rls_coverage._get_tables_with_tenant_id', return_value={'brands', 'non_existent_table'}):
        with pytest.raises(AssertionError) as exc_info:
            test_architecture_rls_documentation()
        assert "missing from ARCHITECTURE.md" in str(exc_info.value)


@pytest.mark.asyncio
async def test_postgres_rls_coverage_rls_disabled_negative():
    """Verify that test_postgres_rls_coverage fails if RLS is disabled on a table."""
    from unittest.mock import AsyncMock, MagicMock, patch
    
    mock_conn = AsyncMock()
    
    # Simulate 'brands' having RLS disabled (relrowsecurity = False)
    mock_result_rls = MagicMock()
    mock_result_rls.__iter__.return_value = [
        MagicMock(relname='brands', relrowsecurity=False, relforcerowsecurity=True),
        MagicMock(relname='tenants', relrowsecurity=True, relforcerowsecurity=True),
    ]
    
    mock_result_pol = MagicMock()
    mock_result_pol.__iter__.return_value = [
        MagicMock(tablename='brands', policyname='tenant_isolation'),
        MagicMock(tablename='brands', policyname='worker_bypass'),
        MagicMock(tablename='tenants', policyname='tenant_isolation'),
        MagicMock(tablename='tenants', policyname='worker_bypass'),
    ]
    
    async def mock_execute(stmt, params=None):
        if "pg_class" in str(stmt):
            return mock_result_rls
        elif "pg_policies" in str(stmt):
            return mock_result_pol
        return MagicMock()
        
    mock_conn.execute = AsyncMock(side_effect=mock_execute)
    
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    
    with patch('tests.test_rls_coverage.create_async_engine', return_value=mock_engine), \
         patch('tests.test_rls_coverage._get_tables_with_tenant_id', return_value={'brands'}):
         
        with pytest.raises(AssertionError) as exc_info:
            await test_postgres_rls_coverage(None)
        assert "Row-Level Security (RLS) is NOT enabled on table 'brands'" in str(exc_info.value)


@pytest.mark.asyncio
async def test_postgres_rls_coverage_missing_policy_negative():
    """Verify that test_postgres_rls_coverage fails if a policy is missing."""
    from unittest.mock import AsyncMock, MagicMock, patch
    
    mock_conn = AsyncMock()
    
    mock_result_rls = MagicMock()
    mock_result_rls.__iter__.return_value = [
        MagicMock(relname='brands', relrowsecurity=True, relforcerowsecurity=True),
        MagicMock(relname='tenants', relrowsecurity=True, relforcerowsecurity=True),
    ]
    
    # Mock 'brands' to be missing 'worker_bypass' policy
    mock_result_pol = MagicMock()
    mock_result_pol.__iter__.return_value = [
        MagicMock(tablename='brands', policyname='tenant_isolation'),
        MagicMock(tablename='tenants', policyname='tenant_isolation'),
        MagicMock(tablename='tenants', policyname='worker_bypass'),
    ]
    
    async def mock_execute(stmt, params=None):
        if "pg_class" in str(stmt):
            return mock_result_rls
        elif "pg_policies" in str(stmt):
            return mock_result_pol
        return MagicMock()
        
    mock_conn.execute = AsyncMock(side_effect=mock_execute)
    
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn
    
    with patch('tests.test_rls_coverage.create_async_engine', return_value=mock_engine), \
         patch('tests.test_rls_coverage._get_tables_with_tenant_id', return_value={'brands'}):
         
        with pytest.raises(AssertionError) as exc_info:
            await test_postgres_rls_coverage(None)
        assert "Table 'brands' is missing the 'worker_bypass' policy" in str(exc_info.value)


def test_rls_bypass_import_guardrails():
    """Verify that only explicitly permitted modules are allowed to import RLS-bypass database sessions."""
    import ast
    import pathlib

    allowed_files = {
        "app/database.py",
        "app/main.py",
        "app/routers/webhooks.py",
        "app/routers/oauth.py",
        "app/routers/tenants.py",
        "app/routers/tasks.py",
    }
    
    app_root = pathlib.Path(__file__).parent.parent / "app"
    violations = []
    
    for path in app_root.rglob("*.py"):
        rel_path = path.relative_to(app_root.parent).as_posix()
        
        if rel_path.startswith("app/tasks/"):
            continue
            
        if rel_path in allowed_files:
            continue
            
        with open(path, "r", encoding="utf-8") as f:
            try:
                tree = ast.parse(f.read(), filename=rel_path)
            except SyntaxError:
                continue
                
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module in ("app.database", "database"):
                    for alias in node.names:
                        if alias.name in ("get_worker_db", "WorkerAsyncSessionLocal"):
                            violations.append(f"{rel_path}:{node.lineno} -> imports {alias.name}")
            elif isinstance(node, ast.Attribute):
                if node.attr in ("get_worker_db", "WorkerAsyncSessionLocal"):
                    violations.append(f"{rel_path}:{node.lineno} -> references attribute {node.attr}")
                        
    assert not violations, f"Modules violating RLS-bypass import guardrails:\n" + "\n".join(violations)
