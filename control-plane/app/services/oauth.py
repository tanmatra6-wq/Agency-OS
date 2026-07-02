import os
import re
import json
import hmac
import hashlib
import base64
import datetime as dt
import urllib.parse as urlparse
import httpx
import logging
from typing import Optional, Dict, Any

from app.services.secrets import SecretManagerClient

logger = logging.getLogger(__name__)

_DEFAULT_STATE_SECRET = "default-aos-state-secret-key"
_SECRET_KEY_STR = os.getenv("SECRET_KEY", _DEFAULT_STATE_SECRET)
# The OAuth state HMAC key is the CSRF/tamper protection binding tenant_id/brand_id/
# redirect_uri into the flow. Running on the public source-code default in production
# lets anyone forge a valid state, so fail closed at boot rather than silently degrade.
if os.getenv("ENV") == "production" and _SECRET_KEY_STR == _DEFAULT_STATE_SECRET:
    raise RuntimeError(
        "PRODUCTION BOOT ERROR: SECRET_KEY must be set to a strong random value "
        "(provision the aos-oauth-state-secret secret) — the built-in default is forbidden"
    )
SECRET_KEY = _SECRET_KEY_STR.encode("utf-8")

def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

def _base64url_decode(data: str) -> bytes:
    padding = "=" * (4 - (len(data) % 4))
    return base64.urlsafe_b64decode(data + padding)

def normalize_shopify_domain(shop: str) -> str:
    """Return the canonical '<handle>.myshopify.com' host for a Shopify shop.

    Accepts a bare handle ('ableys'), a full myshopify domain
    ('ableys.myshopify.com'), or a URL ('https://ableys.myshopify.com/...') and
    normalizes to the host form.
    """
    shop = (shop or "").strip().lower()
    if "://" in shop:
        shop = urlparse.urlparse(shop).hostname or shop
    shop = shop.strip("/")
    if shop.endswith(".myshopify.com"):
        return shop
    return f"{shop}.myshopify.com"

def generate_oauth_state(tenant_id: str, brand_id: str, redirect_uri: str, provider: Optional[str] = None, shop: Optional[str] = None) -> str:
    """Generates a cryptographically signed, short-lived state token for OAuth flow."""
    expires_at = int((dt.datetime.utcnow() + dt.timedelta(minutes=15)).timestamp())
    payload = {
        "tenant_id": tenant_id,
        "brand_id": brand_id,
        "redirect_uri": redirect_uri,
        "expires_at": expires_at
    }
    if provider:
        payload["provider"] = provider
    if shop:
        payload["shop"] = shop
    payload_json = json.dumps(payload, sort_keys=True).encode("utf-8")
    payload_b64 = _base64url_encode(payload_json)
    
    # Sign the payload
    signature = hmac.new(SECRET_KEY, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    signature_b64 = _base64url_encode(signature)
    
    return f"{payload_b64}.{signature_b64}"


def verify_oauth_state(state: str) -> Dict[str, Any]:
    """Verifies the state token signature and expiration, returning the decoded payload."""
    if not state or "." not in state:
        raise ValueError("Invalid state format")
        
    parts = state.split(".")
    if len(parts) != 2:
        raise ValueError("Invalid state format")
        
    payload_b64, signature_b64 = parts
    
    # Recalculate signature
    expected_signature = hmac.new(SECRET_KEY, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    expected_signature_b64 = _base64url_encode(expected_signature)
    
    if not hmac.compare_digest(signature_b64, expected_signature_b64):
        raise ValueError("Invalid state signature")
        
    try:
        payload_json = _base64url_decode(payload_b64)
        payload = json.loads(payload_json)
    except Exception as e:
        raise ValueError(f"Failed to decode state payload: {e}")
        
    # Check expiration
    now = dt.datetime.utcnow().timestamp()
    if now > payload.get("expires_at", 0):
        raise ValueError("State token expired")
        
    return payload

def validate_redirect_uri(redirect_uri: str) -> bool:
    """Validates that the redirect URI is allowed to prevent open redirect vulnerabilities."""
    try:
        parsed = urlparse.urlparse(redirect_uri)
        if parsed.scheme not in ("http", "https"):
            return False
            
        hostname = parsed.hostname
        if not hostname:
            return False
            
        # Strictly validate hostname characters
        if not re.match(r"^[a-zA-Z0-9.-]+$", hostname):
            return False
            
        # Allow localhost or *.agencyos.com
        if hostname == "localhost":
            return True
        if hostname == "agencyos.com" or hostname.endswith(".agencyos.com"):
            return True
            
        return False
    except Exception:
        return False

def _parse_secret_id(secret_ref: str) -> str:
    match = re.search(r"/secrets/([^/]+)", secret_ref)
    if match:
        return match.group(1)
    return secret_ref

class OauthService:
    """Handles OAuth token exchanges, refreshes, and secret storage."""

    def __init__(self):
        self.secrets_client = SecretManagerClient()

    async def refresh_token(
        self, 
        tenant_id: str, 
        brand_id: str, 
        provider: str, 
        refresh_token_ref: str
    ) -> Dict[str, Any]:
        """Refreshes an OAuth token for a given provider and updates Secret Manager."""
        logger.info(f"Refreshing OAuth token for tenant={tenant_id}, provider={provider}")
        
        # Read the old refresh token
        old_refresh = await self.secrets_client.read_secret(refresh_token_ref)
        
        # Determine URL
        if provider == "shopify":
            url = f"https://{brand_id}.myshopify.com/admin/oauth/access_token"
        else:
            url = "https://oauth2.googleapis.com/token"
            
        # Setup payload
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": old_refresh,
            "client_id": os.getenv("GOOGLE_CLIENT_ID", "mock-client-id"),
            "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", "mock-client-secret")
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, data=payload)
                if resp.status_code != 200:
                    raise Exception(f"Token exchange failed: {resp.status_code} - {resp.text}")
                    
                data = resp.json()
            except Exception as e:
                logger.error(f"Error during OAuth token exchange: {e}")
                raise
                
        new_access = data.get("access_token")
        new_refresh = data.get("refresh_token", old_refresh)  # Fallback to old if not rotated
        expires_in = data.get("expires_in", 3600)
        
        if not new_access:
            raise Exception("No access_token returned by provider")
            
        # Write new tokens to Secret Manager
        secret_id = _parse_secret_id(refresh_token_ref)
        
        # Write refresh token back (call 1)
        new_refresh_ref = await self.secrets_client.write_secret(secret_id, new_refresh)
        
        # Write access token (call 2)
        new_access_ref = await self.secrets_client.write_secret(f"{secret_id}_access", new_access)
        
        return {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": expires_in,
            "access_token_ref": new_access_ref,
            "refresh_token_ref": new_refresh_ref
        }

    async def prune_old_versions(self, new_version_ref: str):
        """Prunes the immediately preceding version of a secret to save storage and maintain hygiene."""
        match = re.match(r"^(projects/[^/]+/secrets/[^/]+/versions/)(\d+)$", new_version_ref)
        if match:
            prefix, version_str = match.groups()
            version = int(version_str)
            if version > 1:
                old_version_ref = f"{prefix}{version - 1}"
                try:
                    logger.info(f"Pruning old secret version: {old_version_ref}")
                    await self.secrets_client.delete_secret(old_version_ref)
                except Exception as e:
                    logger.error(f"Failed to prune old secret version {old_version_ref}: {e}")

    async def exchange_code_for_token(
        self, 
        tenant_id: str, 
        brand_id: str, 
        provider: str,
        code: str,
        shop: Optional[str] = None,
        redirect_uri: Optional[str] = None
    ) -> Dict[str, Any]:
        """Exchanges the authorization code for access and refresh tokens, writing them to Secret Manager."""
        logger.info(f"Exchanging OAuth authorization code for tenant={tenant_id}, provider={provider}...")
        
        if provider == "shopify":
            # Shopify OAuth runs against the store's own admin domain. Prefer the
            # explicit shop carried in the OAuth state; fall back to brand_id.
            shop_domain = normalize_shopify_domain(shop or brand_id)
            url = f"https://{shop_domain}/admin/oauth/access_token"
            payload = {
                "client_id": os.getenv("SHOPIFY_CLIENT_ID", "mock-shopify-client-id"),
                "client_secret": os.getenv("SHOPIFY_CLIENT_SECRET", "mock-shopify-client-secret"),
                "code": code
            }
        else:
            from app.services.oauth_registry import OauthProviderRegistry
            env_prefix = provider.replace("-", "_").upper()
            # The redirect_uri MUST match the one used in the authorize request; it is
            # carried in the signed OAuth state. Fall back to env/default for back-compat.
            resolved_redirect = redirect_uri or os.getenv(f"{env_prefix}_REDIRECT_URI", "http://localhost/callback")
            url, payload = OauthProviderRegistry.get_exchange_payload(provider, code, redirect_uri=resolved_redirect)
            
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, data=payload)
                if resp.status_code != 200:
                    try:
                        err_data = resp.json()
                        err_msg = err_data.get("error_description") or err_data.get("error") or resp.text
                    except Exception:
                        err_msg = resp.text
                    raise Exception(f"Token exchange HTTP failed: {resp.status_code} - {err_msg}")
                data = resp.json()
            except Exception as e:
                logger.error(f"OAuth token exchange request failed: {e}")
                raise
            
        access_token = data.get("access_token")
        # Google Ads returns refresh_token on authorization_code grant. Shopify returns permanent access_token.
        refresh_token = data.get("refresh_token", access_token) 
        expires_in = data.get("expires_in", 3600)
        
        if not access_token:
            raise ValueError("No access_token returned by provider during exchange")
        
        # Write securely to tenant-isolated Secret Manager
        secret_id_refresh = f"{tenant_id}-{brand_id}-{provider}-secret"
        refresh_token_ref = await self.secrets_client.write_secret(secret_id_refresh, refresh_token)
        access_token_ref = await self.secrets_client.write_secret(f"{secret_id_refresh}_access", access_token)
        
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": expires_in,
            "access_token_ref": access_token_ref,
            "refresh_token_ref": refresh_token_ref,
            "scope": data.get("scope")
        }
