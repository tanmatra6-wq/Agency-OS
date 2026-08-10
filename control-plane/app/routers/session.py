from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import hmac

from app.auth import OPERATOR_TOKEN, sign_jwt

router = APIRouter(tags=["session"])


class BootstrapRequest(BaseModel):
    operator_token: str


@router.post("/session/bootstrap")
async def session_bootstrap(body: BootstrapRequest):
    if not hmac.compare_digest(body.operator_token, OPERATOR_TOKEN):
        raise HTTPException(403, "Forbidden: Invalid operator token")

    # Generate a signed JWT session token valid for 2 hours (7200 seconds)
    token = sign_jwt({"role": "OPERATOR_AUTHENTICATED"}, OPERATOR_TOKEN, expires_in=7200)
    return {"session_token": token, "expires_in": 7200}
