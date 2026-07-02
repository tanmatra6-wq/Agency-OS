"""Shared operator authentication + production boot guards.

Lives in its own module (no app imports) so both app.main and the routers can
depend on it without the circular import that previously forced the onboarding
router to ship unauthenticated.
"""
import hmac
import os

from fastapi import Header, HTTPException

OPERATOR_TOKEN = os.getenv("OPERATOR_TOKEN", "default-dev-token")
if os.getenv("ENV") == "production" and OPERATOR_TOKEN == "default-dev-token":
    raise RuntimeError(
        "PRODUCTION BOOT ERROR: OPERATOR_TOKEN must be explicitly set — default is forbidden"
    )


async def verify_operator_auth(authorization: str | None = Header(default=None)):
    """Verifies the request carries a valid Operator Bearer Token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[7:]
    if not hmac.compare_digest(token, OPERATOR_TOKEN):
        raise HTTPException(403, "Forbidden: Invalid operator token")


async def resolved_operator_role(authorization: str | None = Header(default=None)) -> str | None:
    """Resolves the operator's role if authenticated, else returns None."""
    if not authorization:
        return None
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid Authorization header")
    token = authorization[7:]
    if not hmac.compare_digest(token, OPERATOR_TOKEN):
        raise HTTPException(403, "Forbidden: Invalid operator token")
    return "OPERATOR_AUTHENTICATED"


def require_non_production():
    """Dependency that 404s in production so debug/introspection routes cannot be
    reached against a live deployment. Registered as a route dependency rather than
    conditionally omitting the routes so the surface is identical across envs."""
    if os.getenv("ENV") == "production":
        raise HTTPException(404, "Not Found")
