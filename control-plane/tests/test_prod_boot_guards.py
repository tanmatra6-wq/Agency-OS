import os
import sys
import importlib
import pytest

_ORIGINAL_SYS_MODULES = None

@pytest.fixture(scope="module", autouse=True)
def snapshot_sys_modules():
    global _ORIGINAL_SYS_MODULES
    _ORIGINAL_SYS_MODULES = sys.modules.copy()
    yield

def clean_imports():
    """Clear sys.modules to force python to re-execute module-level code on import."""
    for mod in list(sys.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]

@pytest.fixture(autouse=True)
def restore_sys_modules_after_test():
    """Ensure we restore the original sys.modules after each test to prevent polluting other tests."""
    yield
    # 1. Clear any current app modules in sys.modules to avoid mixing
    for mod in list(sys.modules.keys()):
        if mod == "app" or mod.startswith("app."):
            del sys.modules[mod]
    # 2. Restore the original module instances from the snapshot
    for mod, val in _ORIGINAL_SYS_MODULES.items():
        if mod == "app" or mod.startswith("app."):
            sys.modules[mod] = val

def setup_valid_prod_env(monkeypatch):
    """Sets up a baseline environment with valid production settings."""
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("OPERATOR_TOKEN", "secure-prod-operator-token-xyz")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@10.0.0.1:5432/agency_os")
    monkeypatch.setenv("WORKER_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@10.0.0.2:5432/agency_os")
    
    for var in [
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
        "WHATSAPP_TOKEN", "WHATSAPP_VERIFY_TOKEN", "WHATSAPP_APP_SECRET", "WHATSAPP_PHONE_NUMBER_ID",
        "SECRET_KEY"
    ]:
        monkeypatch.setenv(var, "secure-prod-value-xyz")

    # Explicitly clear mock settings to prevent test env leaks
    for var in ["AOS_MOCK_CAMPAIGNS_FILE", "AOS_MOCK_SECRETS_FILE", "AOS_MOCK_STORAGE_FILE", "MOCK_PLAYWRIGHT"]:
        monkeypatch.delenv(var, raising=False)



def test_prod_boot_guards_operator_token(monkeypatch):
    # Case 1: ENV=production + OPERATOR_TOKEN=default-dev-token -> RuntimeError
    setup_valid_prod_env(monkeypatch)
    monkeypatch.setenv("OPERATOR_TOKEN", "default-dev-token")
    clean_imports()

    with pytest.raises(RuntimeError) as exc_info:
        import app.main
    assert "OPERATOR_TOKEN" in str(exc_info.value)



def test_prod_boot_guards_secret_key(monkeypatch):
    # ENV=production + SECRET_KEY left at the built-in default -> RuntimeError
    monkeypatch.setenv("ENV", "production")
    monkeypatch.setenv("OPERATOR_TOKEN", "super-secret-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@10.0.0.1:5432/agency_os")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "mock-whatsapp-secret")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    clean_imports()

    with pytest.raises(RuntimeError) as exc_info:
        import app.services.oauth
    assert "SECRET_KEY must be set" in str(exc_info.value)

def test_prod_boot_guards_database_url(monkeypatch):
    # Case 2: ENV=production + DATABASE_URL contains localhost -> RuntimeError
    setup_valid_prod_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/agency_os")
    clean_imports()
    
    with pytest.raises(RuntimeError) as exc_info:
        import app.database
    assert "DATABASE_URL still points at localhost" in str(exc_info.value)


def test_prod_boot_guards_database_url_sqlite(monkeypatch):
    # Case 2b: ENV=production + DATABASE_URL starts with sqlite -> RuntimeError
    setup_valid_prod_env(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///agencyos.db")
    clean_imports()
    
    with pytest.raises(RuntimeError) as exc_info:
        import app.database
    assert "DATABASE_URL cannot use SQLite" in str(exc_info.value)


def test_prod_boot_guards_valid_prod(monkeypatch):
    # Case 3: ENV=production + valid baseline -> boots fine
    setup_valid_prod_env(monkeypatch)
    clean_imports()

    try:
        import app.database
        import app.main
    except RuntimeError as e:
        pytest.fail(f"Boots failed in valid production configuration: {e}")


def test_prod_boot_guards_dev_mode(monkeypatch):
    # Case 4: ENV=dev/empty + default tokens -> boots fine
    monkeypatch.delenv("ENV", raising=False)
    monkeypatch.setenv("OPERATOR_TOKEN", "default-dev-token")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/agency_os")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    clean_imports()
    
    try:
        import app.database
        import app.main
    except RuntimeError as e:
        pytest.fail(f"Boots failed in dev mode: {e}")




def test_prod_boot_guards_mock_secret_fails(monkeypatch):
    # Case 5: ENV=production + mock GOOGLE_CLIENT_ID -> RuntimeError
    setup_valid_prod_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "mock-google-id")
    clean_imports()
    with pytest.raises(RuntimeError) as exc_info:
        import app.main
    assert "GOOGLE_CLIENT_ID" in str(exc_info.value)
    assert "cannot be configured to a development or mock value" in str(exc_info.value)


def test_prod_boot_guards_mock_file_enabled_fails(monkeypatch):
    # Case 6: ENV=production + mock files configured -> RuntimeError
    setup_valid_prod_env(monkeypatch)
    monkeypatch.setenv("AOS_MOCK_CAMPAIGNS_FILE", "campaigns.json")
    clean_imports()
    with pytest.raises(RuntimeError) as exc_info:
        import app.main
    assert "AOS_MOCK_CAMPAIGNS_FILE" in str(exc_info.value)
    assert "must be disabled or unset" in str(exc_info.value)


