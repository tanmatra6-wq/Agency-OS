import datetime as dt
import os
os.environ["AOS_ENV"] = "test"
import sys
sys.modules["meridian"] = None
import pytest
import subprocess
import tempfile
import shutil
import pathlib
from unittest.mock import patch, MagicMock, AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.models import Base
from httpx import ASGITransport, AsyncClient

original_run = subprocess.run

@pytest.fixture
def run_git():
    def _run(args, **kwargs):
        # Force safe.bareRepository=all for all git commands in tests
        cmd = ["git", "-c", "safe.bareRepository=all"] + args
        return subprocess.run(cmd, **kwargs)
    return _run

@pytest.fixture
def temp_git_remote(run_git):
    """Creates a temporary git repository to act as a remote."""
    temp_dir = tempfile.mkdtemp()
    remote_path = os.path.join(temp_dir, "remote.git")
    
    # Initialize bare repo
    run_git(["init", "--bare", remote_path], check=True, capture_output=True)
    run_git(["-C", remote_path, "symbolic-ref", "HEAD", "refs/heads/main"], check=True, capture_output=True)
    
    # Clone it locally to commit initial file
    clone_path = os.path.join(temp_dir, "clone")
    run_git(["clone", remote_path, clone_path], check=True, capture_output=True)
    
    # Create initial files
    src_dir = os.path.join(clone_path, "src")
    os.makedirs(src_dir, exist_ok=True)
    app_js = os.path.join(src_dir, "App.js")
    with open(app_js, "w") as f:
        f.write("function App() {\n  return <Hero color=\"red\" />;\n}\n")
        
    # package.json
    package_json = os.path.join(clone_path, "package.json")
    with open(package_json, "w") as f:
        f.write('{\n  "name": "brand-site",\n  "version": "1.0.0",\n  "scripts": {\n    "test:smoke": "node run_smoke.js"\n  }\n}\n')
        
    # run_smoke.js
    run_smoke = os.path.join(clone_path, "run_smoke.js")
    with open(run_smoke, "w") as f:
        f.write('const baseUrl = process.env.BASE_URL || "http://localhost:3000";\nconsole.log(`Running smoke tests against ${baseUrl}...`);\nif (baseUrl.includes("fail-smoke")) {\n  console.error("Smoke tests failed: simulated failure");\n  process.exit(1);\n}\nconsole.log("Smoke tests passed");\nprocess.exit(0);\n')
        
    # Commit and push
    run_git(["config", "user.email", "test@test.com"], cwd=clone_path, check=True)
    run_git(["config", "user.name", "Test User"], cwd=clone_path, check=True)
    run_git(["add", "-A"], cwd=clone_path, check=True)
    run_git(["commit", "-m", "initial commit"], cwd=clone_path, check=True)
    run_git(["branch", "-M", "main"], cwd=clone_path, check=True)
    run_git(["push", "origin", "main"], cwd=clone_path, check=True)
    
    yield remote_path
    
    shutil.rmtree(temp_dir)

@pytest.fixture(autouse=True)
def mock_terraform_cli():
    import json
    def mock_run(cmd, cwd=None, **kwargs):
        if cmd[0] != "terraform":
            return original_run(cmd, cwd=cwd, **kwargs)
        subcomm = cmd[1]
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stderr = ""

        if subcomm == "init":
            mock_res.stdout = "Success! Terraform has been initialized."
        elif subcomm in ("plan", "apply", "output"):
            tfvars_path = os.path.join(cwd, "terraform.tfvars.json") if cwd else None
            vars_dict = {}
            if tfvars_path and os.path.exists(tfvars_path):
                with open(tfvars_path, "r") as f:
                    vars_dict = json.load(f)

            # Determine recipe
            if "db_tier" in vars_dict:
                recipe = "wp-serverless-mysql"
            elif "shop_url" in vars_dict:
                recipe = "shopify-storefront"
            elif "db_connection_name" in vars_dict:
                recipe = "n8n"
            elif "gtm_container_config" in vars_dict:
                recipe = "sgtm-capi"
            elif "dkim_record" in vars_dict:
                recipe = "email-dns"
            elif "bucket_name" in vars_dict:
                recipe = "static-host"
            elif "domain" in vars_dict or "custom_domain" in vars_dict:
                recipe = "web-host"
            elif "project_id" in vars_dict:
                recipe = "webapp-postgres"
            elif "brand_id" in vars_dict:
                recipe = "brand-baseline"
            elif "db_name" in vars_dict:
                recipe = "postgres-db"
            else:
                recipe = "unknown"

            if subcomm == "plan":
                # Write a mock plan file if -out is requested
                out_arg = next((arg for arg in cmd if arg.startswith("-out=")), None)
                if out_arg and cwd:
                    plan_filename = out_arg.split("=")[1]
                    with open(os.path.join(cwd, plan_filename), "w") as f:
                        f.write("mock_tfplan_content")

                if os.environ.get("SIMULATE_DRIFT") == "1":
                    mock_res.returncode = 2
                    mock_res.stdout = "Note: Objects have changed outside Terraform.\n~ resource \"cloud_dns\" \"zone\" {\n    TTL = 300 -> 3600 (drifted)\n  }\nPlan: 0 to add, 1 to change, 0 to destroy."
                else:
                    if recipe == "brand-baseline":
                        brand = vars_dict.get("brand_id", "example-brand")
                        mock_res.stdout = f"Plan: 3 to add, 0 to change, 0 to destroy.\n+ project {brand}\n+ database db-{brand}"
                    elif recipe == "webapp-postgres":
                        mock_res.stdout = "Plan: 8 to add, 0 to change, 0 to destroy.\n+ sql_instance postgres\n+ secret db_url\n+ artifact_registry repo"
                    elif recipe == "web-host":
                        domain = vars_dict.get("domain") or vars_dict.get("custom_domain", "example.in")
                        mock_res.stdout = f"Plan: 5 to add, 0 to change, 0 to destroy.\n+ cloud_dns zone {domain}\n"
                    elif recipe == "sgtm-capi":
                        mock_res.stdout = "Plan: 5 to add, 0 to change, 0 to destroy.\n+ google_cloud_run_service sgtm\n+ google_secret_manager_secret capi_token\n+ google_secret_manager_secret capi_pixel"
                    elif recipe == "email-dns":
                        mock_res.stdout = "Plan: 3 to add, 0 to change, 0 to destroy.\n+ google_dns_record_set mx\n+ google_dns_record_set spf\n+ google_dns_record_set dkim"
                    elif recipe == "static-host":
                        mock_res.stdout = "Plan: 2 to add, 0 to change, 0 to destroy.\n+ google_storage_bucket static_bucket\n+ google_storage_bucket_iam_member public_read"
                    elif recipe == "n8n":
                        mock_res.stdout = "Plan: 2 to add, 0 to change, 0 to destroy.\n+ cloud_run n8n-service\n"
                    elif recipe == "postgres-db":
                        db_name = vars_dict.get("db_name", "brand-db")
                        mock_res.stdout = f"Plan: 1 to add, 0 to change, 0 to destroy.\n+ neon_database {db_name}\n"
                    elif recipe == "wp-serverless-mysql":
                        mock_res.stdout = "Plan: 3 to add, 0 to change, 0 to destroy.\n+ google_sql_database_instance mysql\n+ google_cloud_run_service wordpress\n+ google_storage_bucket uploads"
                    else:
                        mock_res.stdout = "Plan: 0 to add"
            elif subcomm == "apply":
                domain = vars_dict.get("domain") or vars_dict.get("custom_domain")
                if domain == "fail.in" or vars_dict.get("project_id") == "fail-project":
                    mock_res.returncode = 1
                    mock_res.stderr = "Terraform apply failed: simulated error"
                    mock_res.stdout = "Apply failed!"
                else:
                    mock_res.stdout = "Apply complete! Resources: added, 0 changed, 0 destroyed."
            elif subcomm == "output":
                if recipe == "brand-baseline":
                    brand = vars_dict.get("brand_id", "example-brand")
                    tier = vars_dict.get("tier", "shared")
                    outputs = {
                        "project_id": {"type": "string", "value": f"aos-brand-{brand}" if tier == "dedicated" else "aos-shared-tier"},
                        "service_account_email": {"type": "string", "value": f"aos-deployer-{brand}@aos-brand-{brand}.iam.gserviceaccount.com" if tier == "dedicated" else "shared-sa@aos-shared-tier.iam.gserviceaccount.com"},
                        "db_connection_name": {"type": "string", "value": "" if tier == "dedicated" else "aos-shared-tier:asia-south1:aos-shared-postgres"}
                    }
                elif recipe == "web-host":
                    domain = vars_dict.get("domain") or vars_dict.get("custom_domain", "example.in")
                    outputs = {
                        "service_url": {"type": "string", "value": f"https://web-{domain}"},
                        "lb_ip": {"type": "string", "value": "34.120.15.22"}
                    }
                elif recipe == "sgtm-capi":
                    outputs = {
                        "sgtm_url": {"type": "string", "value": "https://sgtm-container-123.run.app"},
                        "dns_verified": {"type": "bool", "value": True}
                    }
                elif recipe == "webapp-postgres":
                    outputs = {
                        "frontend_url": {"type": "string", "value": "https://tanmatra-mock-url.run.app"},
                        "api_url": {"type": "string", "value": "https://wellness-foods-mock-url.run.app"},
                        "db_connection_name": {"type": "string", "value": "aos-brand-b1:asia-south2:brand-b1-db"}
                    }
                elif recipe == "email-dns":
                    outputs = {
                        "dns_verified": {"type": "bool", "value": True}
                    }
                elif recipe == "static-host":
                    bucket = vars_dict.get("bucket_name", "brand-bucket")
                    domain = vars_dict.get("domain", "brand.in")
                    outputs = {
                        "bucket_url": {"type": "string", "value": f"https://storage.googleapis.com/{bucket}"},
                        "cdn_url": {"type": "string", "value": f"https://static-{domain}"}
                    }
                elif recipe == "n8n":
                    outputs = {
                        "service_url": {"type": "string", "value": "https://n8n-service-123.run.app"}
                    }
                elif recipe == "postgres-db":
                    db_name = vars_dict.get("db_name", "brand-db")
                    outputs = {
                        "connection_uri": {"type": "string", "value": f"postgresql://aos-user:mock-pass@neon-host.in/{db_name}"},
                        "db_host": {"type": "string", "value": "neon-host.in"}
                    }
                elif recipe == "wp-serverless-mysql":
                    outputs = {
                        "service_url": {"type": "string", "value": "https://wordpress-app.run.app"},
                        "db_instance_name": {"type": "string", "value": "wp-mysql-instance"},
                        "uploads_bucket": {"type": "string", "value": "gs://wp-uploads-bucket"}
                    }
                elif recipe == "shopify-storefront":
                    shop_url = vars_dict.get("shop_url", "default.myshopify.com")
                    gcp_project = vars_dict.get("gcp_project", "aos-brand-b1")
                    outputs = {
                        "service_url": {"type": "string", "value": f"https://{shop_url}"},
                        "mcp_server_url": {"type": "string", "value": f"https://mcp-shopify.{gcp_project}.run.app/rpc"}
                    }
                else:
                    outputs = {}
                mock_res.stdout = json.dumps(outputs)
        elif subcomm == "destroy":
            mock_res.stdout = "Destroy complete! Resources: 0 added, 0 changed, 5 destroyed."
        else:
            mock_res.stdout = ""
        return mock_res

    with patch("app.adapters.provision.subprocess.run", side_effect=mock_run) as mock:
        yield mock


@pytest.fixture()
async def db_file():
    temp_dir = tempfile.mkdtemp()
    db_path = pathlib.Path(temp_dir) / "test.db"
    yield f"sqlite+aiosqlite:///{db_path}"
    shutil.rmtree(temp_dir)


@pytest.fixture()
async def db_engine(db_file):
    from migrate import migrate
    from sqlalchemy import event
    engine = create_async_engine(db_file)
    
    # Enable SQLite foreign key constraint enforcement
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()
        
    await migrate(engine)
    
    # Register a dynamic before_flush listener on all Session instances
    # to automatically seed parent tenants and brands on-the-fly whenever a child row is inserted
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    from app.models import Tenant, Brand
    import datetime as dt
    
    @event.listens_for(Session, "before_flush")
    def auto_seed_missing_tenants_and_brands(session, flush_context, instances):
        now_val = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        
        # Pass 1: Auto-seed missing Tenants
        for obj in session.new:
            if isinstance(obj, Tenant):
                continue
            tenant_id = getattr(obj, "tenant_id", None)
            if tenant_id:
                tenant_in_session = False
                for local_obj in session.new:
                    if isinstance(local_obj, Tenant) and local_obj.id == tenant_id:
                        tenant_in_session = True
                        break
                if not tenant_in_session:
                    identity = (Tenant, (tenant_id,))
                    if identity in session.identity_map:
                        tenant_in_session = True
                if not tenant_in_session:
                    conn = session.connection()
                    res = conn.execute(text("SELECT 1 FROM tenants WHERE id = :id"), {"id": tenant_id})
                    if not res.scalar():
                        conn.execute(
                            text("INSERT INTO tenants (id, name, hosting_tier, is_active, created_at) VALUES (:id, :name, 'shared', 1, :created_at)"),
                            {"id": tenant_id, "name": f"Auto Seeded {tenant_id}", "created_at": now_val}
                        )
                        
        # Pass 2: Auto-seed missing Brands
        for obj in session.new:
            if isinstance(obj, Brand) or isinstance(obj, Tenant):
                continue
            brand_id = getattr(obj, "brand_id", None)
            tenant_id = getattr(obj, "tenant_id", None)
            if brand_id and tenant_id:
                brand_in_session = False
                for local_obj in session.new:
                    if isinstance(local_obj, Brand) and local_obj.id == brand_id:
                        brand_in_session = True
                        break
                if not brand_in_session:
                    identity = (Brand, (brand_id,))
                    if identity in session.identity_map:
                        brand_in_session = True
                if not brand_in_session:
                    conn = session.connection()
                    res = conn.execute(text("SELECT 1 FROM brands WHERE id = :id"), {"id": brand_id})
                    if not res.scalar():
                        conn.execute(
                            text("INSERT INTO brands (id, tenant_id, name, created_at) VALUES (:id, :tenant_id, :name, :created_at)"),
                            {"id": brand_id, "tenant_id": tenant_id, "name": f"Auto Seeded {brand_id}", "created_at": now_val}
                        )
                        
    yield engine
    await engine.dispose()


@pytest.fixture()
async def session(db_engine):
    async_session = async_sessionmaker(db_engine, expire_on_commit=False)
    async with async_session() as s:
        yield s


@pytest.fixture()
async def client(db_engine):
    import app.main as mainmod
    from app.database import get_db, get_worker_db, get_worker_session_maker

    async_session = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with async_session() as s:
            await s.begin()
            try:
                yield s
                if s.in_transaction():
                    await s.commit()
            except Exception:
                if s.in_transaction():
                    await s.rollback()
                raise

    async def override_get_worker_session_maker():
        return async_session

    from app.services.secrets import SecretManagerClient
    from app.services.storage import GcsClient
    SecretManagerClient.clear()
    GcsClient.clear()
    print("DEBUG APP ID IN CONFTEST:", id(mainmod.app))
    mainmod.app.dependency_overrides[get_db] = override_get_db
    mainmod.app.dependency_overrides[get_worker_db] = override_get_db
    mainmod.app.dependency_overrides[get_worker_session_maker] = override_get_worker_session_maker
    mainmod.app.dependency_overrides[mainmod.verify_operator_auth] = lambda: None
    mainmod.app.dependency_overrides[mainmod.resolved_operator_role] = lambda: "OPERATOR_AUTHENTICATED"
    mainmod.app.state.db_session_maker = async_session
    mainmod.app.state.bypass_tenant_validation = True
    async with AsyncClient(transport=ASGITransport(app=mainmod.app), base_url="http://test") as ac:
        yield ac
    mainmod.app.state.db_session_maker = mainmod.AsyncSessionLocal
    mainmod.app.state.bypass_tenant_validation = False
    mainmod.app.dependency_overrides.clear()
    SecretManagerClient.clear()
    GcsClient.clear()


@pytest.fixture(autouse=True)
def reset_rate_limiter():
    """Resets the in-memory rate limiter token buckets before each test runs."""
    from app.middleware import active_rate_limiter
    if active_rate_limiter is not None:
        active_rate_limiter.buckets.clear()


@pytest.fixture(autouse=True)
def sandbox_mock_files(tmp_path, monkeypatch):
    """Isolates mock secrets, marketing campaigns, storage, and terraform plans for each test to prevent state pollution."""
    secrets_file = tmp_path / "mock_secrets.json"
    campaigns_file = tmp_path / "mock_marketing_campaigns.json"
    storage_file = tmp_path / "mock_storage.json"
    tfplans_dir = tmp_path / "aos-tfplans"
    tfplans_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("AOS_MOCK_SECRETS_FILE", str(secrets_file))
    monkeypatch.setenv("AOS_MOCK_CAMPAIGNS_FILE", str(campaigns_file))
    monkeypatch.setenv("AOS_MOCK_STORAGE_FILE", str(storage_file))
    monkeypatch.setenv("TFPLAN_DIR", str(tfplans_dir))
    
    import sys
    if "app.adapters.provision" in sys.modules:
        monkeypatch.setattr("app.adapters.provision.TFPLAN_DIR", str(tfplans_dir))
    if "app.services.storage" in sys.modules:
        monkeypatch.setattr("app.services.storage.MOCK_STORAGE_FILE", str(storage_file))


@pytest.fixture(autouse=True)
def mock_urlopen_globally():
    """Globally mocks urllib.request.urlopen for staging URL HTTP checks in tests."""
    from unittest.mock import patch, MagicMock
    
    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.getcode.return_value = 200
    mock_response.__enter__.return_value = mock_response
    
    with patch("urllib.request.urlopen", return_value=mock_response) as mock_patch:
        yield mock_patch


@pytest.fixture
def mock_secrets_client():
    import unittest.mock
    mock = MagicMock()
    # In-memory storage for secrets
    secrets_store = {}
    
    async def default_write(secret_id, value, *args, **kwargs):
        if mock.write_secret._mock_return_value is not unittest.mock.DEFAULT:
            return mock.write_secret._mock_return_value
        ref = f"projects/test-project/secrets/{secret_id}/versions/1"
        secrets_store[ref] = value
        return ref

    async def default_read(secret_ref, *args, **kwargs):
        if mock.read_secret._mock_return_value is not unittest.mock.DEFAULT:
            return mock.read_secret._mock_return_value
        if secret_ref not in secrets_store:
            if "oauth-state" in secret_ref or "aos-oauth-state-secret" in secret_ref:
                return "system-mock-secret-key-with-at-least-32-bytes-long!!"
            raise ValueError(f"Secret not found: {secret_ref}")
        return secrets_store[secret_ref]

    async def default_delete(secret_ref, *args, **kwargs):
        if mock.delete_secret._mock_return_value is not unittest.mock.DEFAULT:
            return mock.delete_secret._mock_return_value
        if secret_ref in secrets_store:
            del secrets_store[secret_ref]

    mock.write_secret = AsyncMock(side_effect=default_write)
    mock.read_secret = AsyncMock(side_effect=default_read)
    mock.delete_secret = AsyncMock(side_effect=default_delete)
    mock.store = secrets_store  # Allow test inspection

    with patch("app.services.secrets.SecretManagerClient.write_secret", side_effect=mock.write_secret),\
         patch("app.services.secrets.SecretManagerClient.read_secret", side_effect=mock.read_secret),\
         patch("app.services.secrets.SecretManagerClient.delete_secret", side_effect=mock.delete_secret):
        yield mock


@pytest.fixture
def mock_mcp_client():
    import unittest.mock
    mock = MagicMock()
    
    async def default_call_tool(tool_name, arguments):
        if mock.call_tool._mock_return_value is not unittest.mock.DEFAULT:
            return mock.call_tool._mock_return_value
        # Default mock returns
        if tool_name == "shopify_get_shop_info":
            return {
                "content": [
                    {
                        "type": "text",
                        "text": '{"shop_name": "Mock Ableys Shop", "domain": "ableys.myshopify.com", "currency": "INR", "status": "active"}'
                    }
                ]
            }
        return {"content": [{"type": "text", "text": "{}"}]}

    mock.call_tool = AsyncMock(side_effect=default_call_tool)
    mock.list_tools = AsyncMock(return_value=[])
    
    with patch("app.services.mcp.McpClient.call_tool", side_effect=mock.call_tool),\
         patch("app.services.mcp.McpClient.list_tools", side_effect=mock.list_tools):
        yield mock


@pytest.fixture
def mock_oidc_verification():
    with patch("google.oauth2.id_token.verify_oauth2_token") as mock:
        yield mock

