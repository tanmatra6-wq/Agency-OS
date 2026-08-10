import datetime as dt
import hashlib
import hmac
import json
import logging
import os
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, text

from app.database import get_db, get_worker_db, get_worker_session_maker, tenant_context
from app.models import Connection, Tenant, ProcessedWebhookMessage
from app.whatsapp import process_whatsapp_webhook_payload
from app.kernel import loop
from app.kernel.plugins import get_plugin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
WHATSAPP_APP_SECRET = os.getenv("WHATSAPP_APP_SECRET")


async def resolve_whatsapp_secret() -> str | None:
    """Resolves the WhatsApp App Secret from Secret Manager. Literal secrets are not allowed."""
    if not WHATSAPP_APP_SECRET:
        return None
    if not WHATSAPP_APP_SECRET.startswith("projects/"):
        raise ValueError("Literal secret references cannot be used as cryptographic keys.")
    from app.services.secrets import SecretManagerClient
    try:
        secrets_client = SecretManagerClient()
        return await secrets_client.read_secret(WHATSAPP_APP_SECRET)
    except Exception as e:
        logger.error(f"Failed to resolve WHATSAPP_APP_SECRET from Secret Manager reference {WHATSAPP_APP_SECRET}: {e}")
        raise RuntimeError(f"Failed to resolve WhatsApp secret from Secret Manager: {e}")


async def verify_whatsapp_signature(payload: bytes, signature: str) -> bool:
    """Verifies SHA256 signature using HMAC and Meta app secret."""
    secret_value = await resolve_whatsapp_secret()
    if not secret_value:
        logger.warning("WHATSAPP_APP_SECRET not configured. Signature check bypassed.")
        return True
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected_sig = hmac.new(
        secret_value.encode("utf-8"),
        payload,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected_sig)


@router.get("/webhooks/whatsapp")
async def verify_whatsapp(
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
):
    """Webhook verification endpoint for Meta WhatsApp Cloud API."""
    if (hub_mode == "subscribe" and hub_verify_token and WHATSAPP_VERIFY_TOKEN
            and hmac.compare_digest(hub_verify_token, WHATSAPP_VERIFY_TOKEN)):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(403, "Verification failed")


@router.post("/webhooks/whatsapp")
async def whatsapp_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
    worker_session_maker = Depends(get_worker_session_maker)
):
    """Webhook event receiver endpoint for Meta WhatsApp Cloud API."""
    raw_body = await request.body()

    # Verify signature if secret is configured
    if WHATSAPP_APP_SECRET:
        if not x_hub_signature_256:
            logger.warning("Rejecting WhatsApp webhook: Missing X-Hub-Signature-256 header")
            raise HTTPException(401, "Signature missing")

        if not await verify_whatsapp_signature(raw_body, x_hub_signature_256):
            logger.warning("Rejecting WhatsApp webhook: Signature mismatch")
            raise HTTPException(401, "Invalid signature")

    body = json.loads(raw_body)
    logger.info(f"WhatsApp webhook received: [REDACTED]")

    # Simple validation that it is a whatsapp event
    if body.get("object") != "whatsapp_business_account":
        raise HTTPException(400, "Invalid object type")

    background_tasks.add_task(process_whatsapp_webhook_payload, body, worker_session_maker)
    return {"status": "accepted"}


async def _find_connection(s: AsyncSession, provider: str, identifier: str) -> Connection | None:
    stmt = select(Connection).where(Connection.provider == provider)
    res = await s.execute(stmt)
    conns = res.scalars().all()
    for conn in conns:
        if provider == "shopify" and conn.config.get("shop_url") == identifier:
            return conn
    return None


@router.post("/webhooks/plugins/{provider}")
async def plugin_webhook(
    provider: str,
    request: Request,
    x_shopify_hmac_sha256: str | None = Header(default=None, alias="X-Shopify-Hmac-Sha256"),
    x_shopify_topic: str | None = Header(default=None, alias="X-Shopify-Topic"),
    s: AsyncSession = Depends(get_worker_db)
):
    try:
        plugin = get_plugin(provider)
        if not plugin:
            raise HTTPException(404, f"No plugin registered for provider: {provider}")

        headers = dict(request.headers)
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            payload = {}

        # Deduplicate webhook using ProcessedWebhookMessage
        from sqlalchemy.exc import IntegrityError
        webhook_id = headers.get("x-shopify-webhook-id") or headers.get("x-webhook-id") or headers.get("x-request-id")
        if webhook_id:
            try:
                async with s.begin_nested():
                    s.add(ProcessedWebhookMessage(message_id=webhook_id))
            except IntegrityError:
                logger.info(f"Duplicate plugin webhook message ID ignored: {webhook_id}")
                return {"status": "ignored", "detail": "duplicate webhook"}

        # 1. Resolve identifier
        identifier = await plugin.resolve_connection_identifier(headers, payload)
        if not identifier:
            raise HTTPException(400, "Unable to resolve connection identifier from webhook headers/payload")

        # 2. Find Connection (RLS is bypassed in get_worker_db session)
        conn = await _find_connection(s, provider, identifier)
        if not conn:
            raise HTTPException(404, f"Unknown brand connection for identifier: {identifier}")
        if conn.status == "revoked":
            raise HTTPException(401, f"Revoked brand connection for identifier: {identifier}")

        # Retrieve tenant to determine dedicated GCP project ID for secret isolation
        stmt_tenant = select(Tenant).where(Tenant.id == conn.tenant_id)
        res_tenant = await s.execute(stmt_tenant)
        tenant = res_tenant.scalar_one_or_none()
        gcp_project = tenant.gcp_project if tenant else None

        # 3. Retrieve signature and secret key
        signature = None
        if provider == "shopify":
            signature = x_shopify_hmac_sha256
        if not signature:
            signature = headers.get("x-signature")

        if not signature:
            raise HTTPException(401, "Webhook signature header missing")

        # Resolve actual secret key from Secret Manager
        from app.services.secrets import SecretManagerClient
        try:
            secrets_client = SecretManagerClient(tenant_id=conn.tenant_id, project_id=gcp_project)
            secret_key = await secrets_client.read_secret(conn.credential)
        except ValueError as e:
            logger.error(f"Failed to resolve webhook credential secret from Secret Manager: {e}")
            raise HTTPException(401, "Webhook credential secret is unconfigured or invalid")
        except Exception as e:
            logger.error(f"Failed to read webhook secret from Secret Manager: {e}")
            raise HTTPException(500, "Internal secret resolution error")

        # 4. Verify signature
        if not await plugin.verify_signature(raw_body, signature, secret_key):
            raise HTTPException(401, "Webhook signature mismatch")

        # 5. Translate webhook payload to OpSpecs
        event_type = x_shopify_topic or headers.get("x-event-type", "")
        specs = await plugin.translate_webhook(event_type, payload, conn.tenant_id, conn.brand_id)

        proposed_ops = []
        # 6. Propose and gate each Op under the connection's tenant_id context
        token = tenant_context.set(conn.tenant_id)
        try:
            # Set app.current_tenant_id at the DB connection level for local RLS checks
            if s.bind.dialect.name == "postgresql":
                await s.execute(
                    text("SELECT set_config('app.current_tenant_id', :tenant_id, true)"),
                    {"tenant_id": conn.tenant_id},
                )

            for spec in specs:
                row = await loop.propose(s, spec, actor=f"webhook.{provider}")
                
                # Resolve trust snapshot to find tier
                from app.kernel.services import resolve_brand_tier
                tier = await resolve_brand_tier(s, tenant_id=conn.tenant_id, brand_id=conn.brand_id, domain=spec.domain)

                await loop.preview_and_gate(s, row, tier=tier, actor=f"webhook.{provider}")
                proposed_ops.append(row.id)
                
            await s.commit()
        except Exception:
            await s.rollback()
            raise
        finally:
            tenant_context.reset(token)

        return {"status": "accepted", "proposed_ops": proposed_ops}
    except Exception as e:
        logger.exception("WEBHOOK EXCEPTION ENCOUNTERED:")
        raise
