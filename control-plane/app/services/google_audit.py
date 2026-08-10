import os
import re
import asyncio
import random
from typing import Optional, Any
import httpx
import logging

logger = logging.getLogger(__name__)

class GoogleAuditClient:
    """Client for running real Google Search Console & Merchant Center audits via REST APIs.

    Architectural Decision:
    We use raw asynchronous `httpx` calls to the Google APIs instead of the
    official sync `google-api-python-client`. This ensures that all network operations
    are fully asynchronous and do not block the ASGI event loop, avoiding the need for
    thread pool delegation. It also maintains a zero-dependency footprint.
    """

    def __init__(self, token: Optional[str] = None, config: Optional[dict[str, Any]] = None):
        self.token = token
        self.config = config or {}
        self.site_url = self.config.get("site_url", "https://mock-brand-site.com")
        self.merchant_id = self.config.get("merchant_id", "mock-merchant-123")
        
        env = os.getenv("AOS_ENV", "development")
        self._is_mock = not token or token == "mock-google-token"
        
        # Always initialize self.headers so that mock overrides or runtime changes don't cause AttributeError
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }
        
        if self._is_mock:
            logger.info("Initializing GoogleAuditClient in high-fidelity MOCK mode")
        else:
            logger.info(f"Initializing real GoogleAuditClient targeting site {self.site_url}")

    async def _refresh_token(self) -> Optional[str]:
        tenant_id = self.config.get("tenant_id")
        brand_id = self.config.get("brand_id")
        provider = self.config.get("provider")
        credential_ref = self.config.get("credential_ref")
        
        if tenant_id and brand_id and provider and credential_ref:
            try:
                from app.services.oauth import OauthService
                logger.info(f"Attempting token refresh via OauthService for tenant={tenant_id}, provider={provider}")
                service = OauthService(tenant_id=tenant_id)
                res = await service.refresh_token(tenant_id, brand_id, provider, credential_ref)
                new_token = res.get("access_token")
                if new_token:
                    self.token = new_token
                    self.headers["Authorization"] = f"Bearer {new_token}"
                    logger.info("Successfully refreshed Google OAuth token via OauthService")
                    return new_token
            except Exception as e:
                logger.error(f"Failed to refresh Google OAuth token via OauthService: {e}")

        # Fallback to private refresh if credentials are in config
        refresh_token = self.config.get("refresh_token")
        client_id = self.config.get("client_id")
        client_secret = self.config.get("client_secret")
        
        if not (refresh_token and client_id and client_secret):
            logger.warning("Cannot refresh Google OAuth token: missing refresh_token, client_id, or client_secret in config.")
            return None
            
        url = "https://oauth2.googleapis.com/token"
        payload = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token"
        }
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await client.post(url, data=payload)
                if resp.status_code == 200:
                    new_token = resp.json().get("access_token")
                    logger.info("Successfully refreshed Google OAuth token via direct API call")
                    self.token = new_token
                    self.headers["Authorization"] = f"Bearer {new_token}"
                    return new_token
                logger.error(f"Failed to refresh Google OAuth token via direct API call: {resp.status_code} - {resp.text}")
                return None
            except Exception as e:
                logger.error(f"Error refreshing Google OAuth token via direct API call: {e}")
                return None

    async def _send_with_retry(
        self, 
        client: httpx.AsyncClient, 
        method: str, 
        url: str, 
        headers: dict, 
        json_data: Optional[dict] = None, 
        max_retries: int = 3, 
        base_delay: float = 1.0
    ) -> httpx.Response:
        delay = base_delay
        for attempt in range(max_retries + 1):
            try:
                if method == "POST":
                    resp = await client.post(url, headers=headers, json=json_data)
                else:
                    resp = await client.get(url, headers=headers)
                
                # If 401 Unauthorized, try to refresh token and retry immediately
                if resp.status_code == 401:
                    logger.warning("Google API returned 401 Unauthorized. Attempting token refresh...")
                    new_token = await self._refresh_token()
                    if new_token:
                        headers["Authorization"] = f"Bearer {new_token}"
                        # Retry the current attempt immediately with the new token
                        if method == "POST":
                            resp = await client.post(url, headers=headers, json=json_data)
                        else:
                            resp = await client.get(url, headers=headers)
                
                # Retry on transient errors (429, 500, 503)
                if resp.status_code in (429, 500, 503) and attempt < max_retries:
                    sleep_time = delay * (0.5 + random.random())
                    logger.warning(
                        f"Google API returned transient error {resp.status_code}. "
                        f"Retrying in {sleep_time:.2f}s (attempt {attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(sleep_time)
                    delay *= 2
                    continue
                
                return resp
            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt < max_retries:
                    sleep_time = delay * (0.5 + random.random())
                    logger.warning(
                        f"Google API request failed: {e}. "
                        f"Retrying in {sleep_time:.2f}s (attempt {attempt + 1}/{max_retries})..."
                    )
                    await asyncio.sleep(sleep_time)
                    delay *= 2
                    continue
                raise e

    async def run_search_console_audit(self) -> dict:
        """Runs Google Search Console audit to check URL indexing and crawl statuses."""
        if self._is_mock:
            logger.info("[Mock GSC Audit] Running mock crawl analytics...")
            return {
                "status": "degraded",
                "findings": {
                    "crawl_errors": 4,
                    "indexing_status": "partially_indexed",
                    "site_url_indexed": False,
                    "warnings": ["Missing schema.org markup on blog pages"]
                }
            }

        # Real Google Search Console URL Inspection API call
        url = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
        payload = {
            "inspectionUrl": self.site_url,
            "siteUrl": self.site_url,
            "languageCode": "en-US"
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await self._send_with_retry(client, "POST", url, headers=self.headers, json_data=payload)
                if resp.status_code == 200:
                    result = resp.json().get("inspectionResult", {})
                    index_status = result.get("indexStatusResult", {})
                    verdict = index_status.get("verdict", "UNKNOWN")
                    
                    status = "healthy" if verdict == "PASS" else "degraded"
                    indexing_status = "indexed" if verdict == "PASS" else "partially_indexed"
                    
                    # Parse crawl errors
                    crawl_state = index_status.get("crawlState", "UNKNOWN")
                    errors = 0 if crawl_state in ("SUCCESSFUL", "UNKNOWN") else 1
                    
                    warnings = []
                    if verdict != "PASS":
                        warnings.append(f"GSC Inspection Verdict: {verdict}")
                    
                    return {
                        "status": status,
                        "findings": {
                            "crawl_errors": errors,
                            "indexing_status": indexing_status,
                            "site_url_indexed": verdict == "PASS",
                            "warnings": warnings
                        }
                    }
                else:
                    logger.error(f"GSC URL Inspection failed: {resp.status_code} - {resp.text}")
                    raise RuntimeError(f"GSC API failed: {resp.text}")
            except Exception as e:
                logger.error(f"GSC API request failed: {e}")
                raise

    async def run_merchant_center_audit(self, simulate_disapproved_products: int = 0) -> dict:
        """Runs Google Merchant Center audit to verify product feed sync and approvals."""
        if self._is_mock:
            logger.info(f"[Mock GMC Audit] Running mock feed checks with simulated disapprovals: {simulate_disapproved_products}...")
            status = "healthy" if simulate_disapproved_products == 0 else "degraded"
            return {
                "status": status,
                "findings": {
                    "disapproved_products": simulate_disapproved_products,
                    "feed_sync_status": "success" if simulate_disapproved_products == 0 else "failed_mismatches",
                    "active_items": 128
                }
            }

        # Real Google Merchant Center Content API ProductStatuses call
        url = f"https://shoppingcontent.googleapis.com/content/v2.1/{self.merchant_id}/productstatuses"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await self._send_with_retry(client, "GET", url, headers=self.headers)
                if resp.status_code == 200:
                    resources = resp.json().get("resources", [])
                    disapproved = 0
                    active = 0
                    
                    for prod in resources:
                        dest_statuses = prod.get("destinationStatuses", [])
                        is_approved = True
                        for dest in dest_statuses:
                            if dest.get("status") == "disapproved":
                                is_approved = False
                                break
                        if not is_approved:
                            disapproved += 1
                        else:
                            active += 1
                            
                    status = "healthy" if disapproved == 0 else "degraded"
                    feed_status = "success" if disapproved == 0 else "failed_mismatches"
                    
                    return {
                        "status": status,
                        "findings": {
                            "disapproved_products": disapproved,
                            "feed_sync_status": feed_status,
                            "active_items": active
                        }
                    }
                else:
                    logger.error(f"GMC product statuses fetch failed: {resp.status_code} - {resp.text}")
                    raise RuntimeError(f"GMC API failed: {resp.text}")
            except Exception as e:
                logger.error(f"GMC API request failed: {e}")
                raise


class GoogleSearchConsoleAudit:
    """High-level runner for Google Search Console audits, handling token resolution and auto-refresh."""

    def __init__(self, tenant_id: str, brand_id: str, session: Any):
        self.tenant_id = tenant_id
        self.brand_id = brand_id
        self.session = session

    async def run(self) -> dict:
        from app.models import Connection
        from app.services.secrets import SecretManagerClient
        from sqlalchemy import select

        # 1. Query the google-search-console connection
        stmt = select(Connection).where(
            Connection.tenant_id == self.tenant_id,
            Connection.brand_id == self.brand_id,
            Connection.provider == "google-search-console"
        )
        res = await self.session.execute(stmt)
        conn = res.scalar_one_or_none()
        if not conn:
            raise ValueError(f"No active google-search-console connection found for brand {self.brand_id}")

        # 2. Resolve token from Secret Manager
        from app.models import Tenant
        stmt_tenant = select(Tenant).where(Tenant.id == self.tenant_id)
        res_tenant = await self.session.execute(stmt_tenant)
        tenant = res_tenant.scalar_one_or_none()
        gcp_project = tenant.gcp_project if tenant else None

        secrets_client = SecretManagerClient(tenant_id=self.tenant_id, project_id=gcp_project)
        token = await secrets_client.read_secret(conn.credential)

        # 3. Setup client config, including metadata for OauthService refresh
        config = {
            **(conn.config or {}),
            "tenant_id": self.tenant_id,
            "brand_id": self.brand_id,
            "provider": conn.provider,
            "credential_ref": conn.credential
        }

        # 4. Instantiate GoogleAuditClient and run audit
        client = GoogleAuditClient(token=token, config=config)
        result = await client.run_search_console_audit()
        return result

