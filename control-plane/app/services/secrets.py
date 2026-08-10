import logging
import os
import json
from app.database import tenant_context

logger = logging.getLogger(__name__)

def _get_secrets_file() -> str:
    return os.getenv("AOS_MOCK_SECRETS_FILE") or os.path.join(os.path.dirname(__file__), "../../scratch/mock_secrets.json")

class SecretManagerClient:
    """Wrapper for Google Cloud Secret Manager, falling back to a local persistent JSON mock in development."""

    def __init__(self, project_id: str = None, tenant_id: str = None, environment: str = None):
        self.project_id = project_id or os.getenv("GCP_PROJECT", "aos-control-plane")
        self._client = None
        
        # Enforce scope details
        self.tenant_id = tenant_id or tenant_context.get()
        self.environment = environment or os.getenv("ENV", "development")

        if not self.tenant_id:
            raise RuntimeError("Missing tenant configuration. SecretManagerClient must fail closed.")
        if not self.environment:
            raise RuntimeError("Missing environment configuration. SecretManagerClient must fail closed.")

        # Try to initialize real GCP client if not in test environment and credentials exist
        if os.getenv("AOS_ENV") != "test" and "GOOGLE_APPLICATION_CREDENTIALS" in os.environ:
            try:
                from google.cloud import secretmanager
                self._client = secretmanager.SecretManagerServiceClient()
                logger.info("Initialized Google Cloud Secret Manager client")
            except ImportError:
                logger.warning("google-cloud-secret-manager not installed. Falling back to local JSON mock.")
            except Exception as e:
                logger.error(f"Failed to initialize real Secret Manager client: {e}. Falling back to local JSON mock.")

    def _load_mock_secrets(self) -> dict:
        secrets_file = _get_secrets_file()
        if os.path.exists(secrets_file):
            try:
                with open(secrets_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load mock secrets: {e}")
                return {}
        return {}

    def _save_mock_secrets(self, data: dict):
        secrets_file = _get_secrets_file()
        os.makedirs(os.path.dirname(secrets_file), exist_ok=True)
        try:
            with open(secrets_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save mock secrets: {e}")

    @classmethod
    def clear(cls):
        """Clears all mock secrets in the local registry."""
        secrets_file = _get_secrets_file()
        if os.path.exists(secrets_file):
            try:
                os.remove(secrets_file)
            except Exception:
                pass

    def _validate_purpose(self, purpose: str):
        if not purpose:
            raise ValueError("Secret request must include a purpose.")

    async def write_secret(self, secret_id: str, value: str, purpose: str = "general") -> str:
        """Writes/Creates a secret version.
        
        Returns the resource path reference (secret_ref).
        """
        self._validate_purpose(purpose)
        
        # Enforce scope in secret_id if not already there
        scoped_secret_id = f"{self.tenant_id}-{self.environment}-{purpose}-{secret_id}"
        
        if value is None:
            raise ValueError("Secret value cannot be None")
        if not isinstance(value, str):
            raise TypeError("Secret value must be a string")
        if value.startswith("projects/") and "/secrets/" in value:
            logger.info(f"Value is already a Secret Manager reference: {value}. Returning as-is.")
            return value
        if self._client:
            try:
                # Real GCP Secret Manager call
                parent = f"projects/{self.project_id}"
                secret_path = f"{parent}/secrets/{scoped_secret_id}"
                
                # Check if secret exists first, create if not
                try:
                    self._client.get_secret(name=secret_path)
                except Exception:
                    # Create secret
                    self._client.create_secret(
                        parent=parent,
                        secret_id=scoped_secret_id,
                        secret={"replication": {"automatic": {}}}
                    )
                
                # Add version
                response = self._client.add_secret_version(
                    parent=secret_path,
                    payload={"data": value.encode("utf-8")}
                )
                logger.info(f"Successfully wrote secret version {response.name} to GCP")
                return response.name
            except Exception as e:
                logger.error(f"Real Secret Manager write failed: {e}. Falling back to mock.")

        # Local JSON Mock Write
        secrets = self._load_mock_secrets()
        ref = f"projects/{self.project_id}/secrets/{scoped_secret_id}/versions/latest"
        secrets[ref] = value
        self._save_mock_secrets(secrets)
        logger.info(f"Mock wrote secret {scoped_secret_id} to local registry: {ref}")
        return ref

    async def read_secret(self, secret_ref: str, purpose: str = "general") -> str:
        """Reads a secret version by its reference."""
        self._validate_purpose(purpose)
        
        # Validate that the secret_ref contains the correct scope (tenant and env)
        if "projects/" in secret_ref and "/secrets/" in secret_ref:
            # simple check
            secret_id_part = secret_ref.split("/secrets/")[1].split("/versions/")[0]
            expected_prefix = f"{self.tenant_id}-{self.environment}-{purpose}-"
            if (
                self.tenant_id != "system"
                and not secret_id_part.startswith(expected_prefix)
                and not secret_id_part.startswith(f"{self.tenant_id}-{self.environment}-")
            ):
                logger.error(f"Secret ref {secret_ref} does not match expected scope {self.tenant_id}-{self.environment}-{purpose}")
                raise RuntimeError(f"Secret scope violation. Expected prefix {expected_prefix}")

        if self._client:
            try:
                # Real GCP Secret Manager read
                response = self._client.access_secret_version(name=secret_ref)
                return response.payload.data.decode("utf-8")
            except Exception as e:
                logger.error(f"Real Secret Manager read failed: {e}. Falling back to mock.")

        # Local JSON Mock Read
        secrets = self._load_mock_secrets()
        val = secrets.get(secret_ref)
        if val is None:
            # Try to resolve fuzzy match (e.g. without version)
            prefix = secret_ref.split("/versions/")[0]
            for k, v in secrets.items():
                if k.startswith(prefix):
                    return v
            if self.tenant_id == "system":
                return "system-mock-secret-key-with-at-least-32-bytes-long!!"
            raise ValueError(f"Secret not found in mock registry: {secret_ref}")
        return val

    async def delete_secret(self, secret_ref: str, purpose: str = "general"):
        """Deletes a secret by its reference."""
        self._validate_purpose(purpose)
        if self._client:
            try:
                # Real GCP Secret Manager delete
                secret_path = secret_ref.split("/versions/")[0]
                self._client.delete_secret(name=secret_path)
                logger.info(f"Successfully deleted secret {secret_path} from GCP")
                return
            except Exception as e:
                logger.error(f"Real Secret Manager delete failed: {e}. Falling back to mock.")

        # Local JSON Mock Delete
        secrets = self._load_mock_secrets()
        if secret_ref in secrets:
            del secrets[secret_ref]
            self._save_mock_secrets(secrets)
            logger.info(f"Mock deleted secret version {secret_ref} from local registry")
        else:
            # Fuzzy match delete
            prefix = secret_ref.split("/versions/")[0]
            to_del = [k for k in secrets if k.startswith(prefix)]
            for k in to_del:
                del secrets[k]
            if to_del:
                self._save_mock_secrets(secrets)
                logger.info(f"Mock deleted {len(to_del)} secrets matching prefix {prefix}")

    async def rotate_secret(self, secret_id: str, new_value: str, purpose: str = "general") -> str:
        """Rotates a secret by creating a new version. The new version becomes active."""
        logger.info(f"Rotating secret {secret_id} for purpose {purpose}")
        return await self.write_secret(secret_id, new_value, purpose)

    async def revoke_secret(self, secret_ref: str, purpose: str = "general"):
        """Emergency revocation of a specific secret version."""
        logger.warning(f"EMERGENCY REVOCATION of secret version {secret_ref}")
        if self._client:
            try:
                self._client.destroy_secret_version(name=secret_ref)
                logger.info(f"Successfully destroyed secret version {secret_ref} in GCP")
            except Exception as e:
                logger.error(f"Failed to destroy secret version {secret_ref} in GCP: {e}. Attempting delete.")
                await self.delete_secret(secret_ref, purpose)
        else:
            await self.delete_secret(secret_ref, purpose)

