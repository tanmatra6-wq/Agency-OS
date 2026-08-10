import os
import logging
import hmac
import base64
import json
import hashlib
import time
from fastapi import Header, HTTPException, Request

logger = logging.getLogger(__name__)

OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "default-dev-token")
WORKER_SA = os.getenv("AOS_WORKER_SERVICE_ACCOUNT")
AOS_ENV = os.getenv("AOS_ENV", "development")

if os.getenv("ENV") == "production" and OPERATOR_TOKEN == "default-dev-token":
    raise RuntimeError("PRODUCTION BOOT ERROR: OPERATOR_TOKEN must be explicitly set — default is forbidden")


def base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def base64url_decode(data: str) -> bytes:
    padding = "=" * (4 - (len(data) % 4))
    return base64.urlsafe_b64decode(data + padding)


def sign_jwt(payload: dict, secret: str, expires_in: int = 7200) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    payload_copy = payload.copy()
    payload_copy["exp"] = int(time.time()) + expires_in
    
    header_b64 = base64url_encode(json.dumps(header).encode("utf-8"))
    payload_b64 = base64url_encode(json.dumps(payload_copy).encode("utf-8"))
    
    signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    signature_b64 = base64url_encode(signature)
    
    return f"{header_b64}.{payload_b64}.{signature_b64}"


def verify_jwt(token: str, secret: str) -> dict | None:
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
            
        header_b64, payload_b64, signature_b64 = parts
        
        signing_input = f"{header_b64}.{payload_b64}".encode("utf-8")
        expected_sig = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        actual_sig = base64url_decode(signature_b64)
        
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None
            
        payload = json.loads(base64url_decode(payload_b64).decode("utf-8"))
        if payload.get("exp", 0) < time.time():
            return None
            
        return payload
    except Exception:
        return None


async def resolve_operator_secret() -> str:
    if OPERATOR_TOKEN.startswith("projects/"):
        from app.services.secrets import SecretManagerClient
        try:
            secrets_client = SecretManagerClient()
            return await secrets_client.read_secret(OPERATOR_TOKEN, purpose="auth")
        except Exception as e:
            logger.error(f"Failed to resolve OPERATOR_TOKEN from Secret Manager: {e}")
            raise RuntimeError("Failed to resolve operator token")
    if os.getenv("ENV") == "production":
        raise ValueError("Literal secret references cannot be used as cryptographic keys. OPERATOR_TOKEN must be a Secret Manager reference in production.")
    return OPERATOR_TOKEN

async def verify_operator_auth(authorization: str | None = Header(default=None)):
    """Verifies that the request carries a valid Operator Bearer Token or a valid signed JWT session token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[7:]
    
    secret = await resolve_operator_secret()
    
    # 1. Compare directly against raw OPERATOR_TOKEN (backward compatibility)
    if hmac.compare_digest(token, secret):
        return
        
    # 2. Try to verify as signed JWT session token
    payload = verify_jwt(token, secret)
    if payload and payload.get("role") == "OPERATOR_AUTHENTICATED":
        return
        
    raise HTTPException(403, "Forbidden: Invalid operator token or session expired")

async def resolved_operator_role(authorization: str | None = Header(default=None)) -> str | None:
    """Resolves the operator's role if authenticated, else returns None."""
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[7:]
    
    secret = await resolve_operator_secret()
    
    if hmac.compare_digest(token, secret):
        return "OPERATOR_AUTHENTICATED"
        
    payload = verify_jwt(token, secret)
    if payload and payload.get("role") == "OPERATOR_AUTHENTICATED":
        return "OPERATOR_AUTHENTICATED"
        
    raise HTTPException(403, "Forbidden: Invalid operator token or session expired")


async def verify_worker_auth(request: Request, authorization: str | None = Header(default=None)):
    if AOS_ENV == "test" or not WORKER_SA:
        return

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")

    token = authorization[7:]
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        google_request = google_requests.Request()
        aud_base = f"{request.url.scheme}://{request.url.netloc}{request.url.path}"
        info = id_token.verify_oauth2_token(token, google_request, audience=aud_base)

        if info.get("iss") not in ["accounts.google.com", "https://accounts.google.com"]:
            raise ValueError("Wrong issuer")

        email = info.get("email")
        if email != WORKER_SA:
            raise ValueError(f"Unauthorized service account: {email}")

    except Exception as e:
        logger.error(f"OIDC token verification failed: {e}")
        raise HTTPException(401, f"Unauthorized: {e}")


def validate_id(id_val: str, name: str = "ID") -> str:
    if not id_val:
        raise HTTPException(400, f"{name} is required")
    import re
    if not re.match(r"\A[a-zA-Z0-9_-]+\Z", id_val):
        raise HTTPException(400, f"Invalid characters or path traversal in {name}")
    return id_val


def tenant_id(x_tenant_id: str | None = Header(default=None)) -> str:
    if not x_tenant_id:
        raise HTTPException(401, "X-Tenant-Id header required")
    return validate_id(x_tenant_id, "tenant_id")

