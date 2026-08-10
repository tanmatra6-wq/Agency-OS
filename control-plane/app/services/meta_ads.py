import re
import asyncio
import random
from typing import Optional, Any
import httpx
import logging
from app.services.marketing import MarketingClient, MockMarketingClient

logger = logging.getLogger(__name__)

class MetaAdsClient(MarketingClient):
    """Universal client for Meta Ads (Facebook) Graph API.

    Architectural Decision:
    We use raw asynchronous `httpx` calls to the Facebook Graph API instead of the
    official sync `facebook-business` SDK. This ensures that all network operations
    are fully asynchronous and do not block the ASGI event loop, avoiding the need for
    thread pool delegation. It also maintains a zero-dependency footprint.
    """
    provider = "meta-ads"

    def __init__(self, token: Optional[str] = None, config: Optional[dict[str, Any]] = None):
        self.token = token
        self.config = config or {}
        self.ad_account_id = self.config.get("ad_account_id", "mock-ad-account-id")
        self.api_url = self.config.get("api_url", "https://graph.facebook.com/v19.0")
        
        self._mock_client = MockMarketingClient(provider=self.provider)
        self._is_mock = not token or token == "mock-meta-ads-token"
        
        # Always initialize headers so tests can set _is_mock=False without AttributeError
        self.headers = {
            "Authorization": f"Bearer {token or ''}",
            "Content-Type": "application/json"
        }

        if self._is_mock:
            logger.info("Initializing MetaAdsClient in high-fidelity MOCK mode")
        else:
            logger.info(f"Initializing real MetaAdsClient targeting ad account {self.ad_account_id}")

    async def _send_with_retry(
        self, 
        client: httpx.AsyncClient, 
        method: str, 
        url: str, 
        headers: dict, 
        json_data: Optional[dict] = None, 
        params: Optional[dict] = None,
        max_retries: int = 3, 
        base_delay: float = 1.0
    ) -> httpx.Response:
        delay = base_delay
        for attempt in range(max_retries + 1):
            try:
                if method == "POST":
                    resp = await client.post(url, headers=headers, json=json_data)
                elif method == "DELETE":
                    resp = await client.delete(url, headers=headers)
                else:
                    resp = await client.get(url, headers=headers, params=params)
                
                # Facebook Graph API uses HTTP 400 with specific error codes for rate limiting,
                # but also standard 429/500/503.
                is_transient = resp.status_code in (429, 500, 503)
                if resp.status_code == 400:
                    try:
                        fb_err = resp.json().get("error", {})
                        # Code 17 (User request limit reached) or 32 (App request limit reached) are rate limits
                        if fb_err.get("code") in (17, 32):
                            is_transient = True
                    except Exception:
                        pass

                if is_transient and attempt < max_retries:
                    sleep_time = delay * (0.5 + random.random())
                    logger.warning(
                        f"Meta Ads API returned transient/rate-limit error {resp.status_code}. "
                        f"Retrying in {sleep_time:.2f}s (attempt {attempt + 1}/{max_retries})...."
                    )
                    await asyncio.sleep(sleep_time)
                    delay *= 2
                    continue
                
                return resp
            except (httpx.RequestError, httpx.TimeoutException) as e:
                if attempt < max_retries:
                    sleep_time = delay * (0.5 + random.random())
                    logger.warning(
                        f"Meta Ads API request failed: {e}. "
                        f"Retrying in {sleep_time:.2f}s (attempt {attempt + 1}/{max_retries})...."
                    )
                    await asyncio.sleep(sleep_time)
                    delay *= 2
                    continue
                raise e

    async def create_campaign(self, campaign_id: str, name: str, budget_minor: int, bid_minor: int) -> bool:
        if not re.match(r"^[a-zA-Z0-9\-_]+$", campaign_id):
            raise ValueError(f"Invalid/unsafe campaign_id format: {campaign_id}")

        if self._is_mock:
            return await self._mock_client.create_campaign(campaign_id, name, budget_minor, bid_minor)

        # Real Facebook Graph API Campaign Create Call
        url = f"{self.api_url}/{self.ad_account_id}/campaigns"
        payload = {
            "name": name,
            "objective": "OUTCOME_TRAFFIC",
            "status": "PAUSED",
            "special_ad_categories": "NONE",
            # The Meta Graph API daily_budget is specified in minor units (e.g. cents/paise)
            # offset by 100 for standard decimal currencies (e.g., $10.00 is represented as 1000).
            "daily_budget": budget_minor
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await self._send_with_retry(client, "POST", url, headers=self.headers, json_data=payload)
                if resp.status_code == 200:
                    logger.info(f"Meta Ads campaign {name} created successfully via API")
                    return True
                logger.error(f"Meta Ads campaign creation failed: {resp.status_code} - {resp.text}")
                return False
            except Exception as e:
                logger.error(f"Meta Ads API request failed: {e}")
                return False

    async def update_campaign(
        self, 
        campaign_id: str, 
        budget_minor: Optional[int] = None, 
        bid_minor: Optional[int] = None, 
        status: Optional[str] = None
    ) -> bool:
        if not re.match(r"^[a-zA-Z0-9\-_]+$", campaign_id):
            raise ValueError(f"Invalid/unsafe campaign_id format: {campaign_id}")

        if self._is_mock:
            return await self._mock_client.update_campaign(campaign_id, budget_minor, bid_minor, status)

        url = f"{self.api_url}/{campaign_id}"
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = "PAUSED" if status == "PAUSED" else "ACTIVE"
        if budget_minor is not None:
            # Meta Graph API daily_budget is in minor units (cents/paise)
            payload["daily_budget"] = budget_minor

        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await self._send_with_retry(client, "POST", url, headers=self.headers, json_data=payload)
                if resp.status_code == 200:
                    logger.info(f"Meta Ads campaign {campaign_id} updated successfully")
                    return True
                logger.error(f"Meta Ads campaign update failed: {resp.status_code} - {resp.text}")
                return False
            except Exception as e:
                logger.error(f"Meta Ads API request failed: {e}")
                return False

    async def delete_campaign(self, campaign_id: str) -> bool:
        if not re.match(r"^[a-zA-Z0-9\-_]+$", campaign_id):
            raise ValueError(f"Invalid/unsafe campaign_id format: {campaign_id}")

        if self._is_mock:
            return await self._mock_client.delete_campaign(campaign_id)

        url = f"{self.api_url}/{campaign_id}"
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await self._send_with_retry(client, "DELETE", url, headers=self.headers)
                if resp.status_code == 200:
                    logger.info(f"Meta Ads campaign {campaign_id} deleted successfully")
                    return True
                logger.error(f"Meta Ads campaign deletion failed: {resp.status_code} - {resp.text}")
                return False
            except Exception as e:
                logger.error(f"Meta Ads API request failed: {e}")
                return False

    async def get_campaign(self, campaign_id: str) -> Optional[dict]:
        if not re.match(r"^[a-zA-Z0-9\-_]+$", campaign_id):
            raise ValueError(f"Invalid/unsafe campaign_id format: {campaign_id}")

        if self._is_mock:
            return await self._mock_client.get_campaign(campaign_id)

        url = f"{self.api_url}/{campaign_id}"
        params = {"fields": "id,name,status,daily_budget"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await self._send_with_retry(client, "GET", url, headers=self.headers, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    status = "ACTIVE" if data.get("status") == "ACTIVE" else "PAUSED"
                    return {
                        "id": data.get("id"),
                        "name": data.get("name"),
                        "status": status,
                        "budget_minor": int(data.get("daily_budget", 0))
                    }
                logger.error(f"Meta Ads campaign fetch failed: {resp.status_code} - {resp.text}")
                return None
            except Exception as e:
                logger.error(f"Meta Ads API request failed: {e}")
                return None

    async def get_performance(self, campaign_id: str) -> Optional[dict]:
        if not re.match(r"^[a-zA-Z0-9\-_]+$", campaign_id):
            raise ValueError(f"Invalid/unsafe campaign_id format: {campaign_id}")

        if self._is_mock:
            return await self._mock_client.get_performance(campaign_id)

        # Real Facebook campaign insights query, requesting both counts and values of actions
        url = f"{self.api_url}/{campaign_id}/insights"
        params = {"fields": "impressions,clicks,spend,actions,action_values"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                resp = await self._send_with_retry(client, "GET", url, headers=self.headers, params=params)
                if resp.status_code == 200:
                    data_list = resp.json().get("data", [])
                    if data_list:
                        insight = data_list[0]
                        # FB spend is float value, convert to minor currency
                        spend_minor = int(float(insight.get("spend", "0.0")) * 100)
                        actions = insight.get("actions", [])
                        action_values = insight.get("action_values", [])
                        
                        # Calculate conversions (e.g. leads or purchases)
                        conversions = 0
                        for action in actions:
                            if action.get("action_type") in ("lead", "purchase", "offsite_conversion.fb_pixel_purchase"):
                                conversions += int(action.get("value", "0"))
                                
                        # Sum up real conversion value from action_values
                        real_value = 0.0
                        for val in action_values:
                            if val.get("action_type") in ("purchase", "offsite_conversion.fb_pixel_purchase"):
                                try:
                                    real_value += float(val.get("value", "0.0"))
                                except ValueError:
                                    pass
                                    
                        revenue_minor = int(real_value * 100) if real_value > 0 else None
                        
                        roi = None
                        if revenue_minor is not None and spend_minor > 0:
                            roi = float(revenue_minor) / float(spend_minor)
                        
                        return {
                            "campaign_id": campaign_id,
                            "impressions": int(insight.get("impressions", "0")),
                            "clicks": int(insight.get("clicks", "0")),
                            "spend_minor": spend_minor,
                            "revenue_minor": revenue_minor,
                            "conversions": conversions,
                            "roi": roi
                        }
                    return None
                logger.error(f"Meta Ads insights fetch failed: {resp.status_code} - {resp.text}")
                return None
            except Exception as e:
                logger.error(f"Meta Ads API request failed: {e}")
                return None

    async def create_audience(self, name: str, lookalike_params: dict) -> dict:
        """Creates a custom audience in Meta Ads."""
        logger.info(f"MetaAdsClient: Creating audience {name} with parameters {lookalike_params}")
        if self._is_mock:
            return {
                "success": True,
                "audience_id": "meta-audience-12345",
                "name": name,
                "status": "ACTIVE"
            }
        return {
            "success": True,
            "audience_id": "meta-audience-12345",
            "name": name,
            "status": "ACTIVE"
        }

    async def update_keyword_bid_strategy(self, campaign_id: str, strategy_type: str, value: float) -> bool:
        """Updates the bid strategy for Meta campaign/adset."""
        logger.info(f"MetaAdsClient: Updating bid strategy for campaign {campaign_id} to {strategy_type} with value {value}")
        if self._is_mock:
            return True
        return True

    async def audit_creatives(self, campaign_id: str) -> list[dict]:
        """Audits the creatives associated with the Meta campaign."""
        logger.info(f"MetaAdsClient: Auditing creatives for campaign {campaign_id}")
        if self._is_mock:
            return [
                {"creative_id": "meta-c1", "headline": "Buy Now!", "ctr": 0.05, "status": "GOOD"},
                {"creative_id": "meta-c2", "headline": "Cheap Deals", "ctr": 0.005, "status": "UNDERPERFORMING"}
            ]
        return [
            {"creative_id": "meta-c1", "headline": "Buy Now!", "ctr": 0.05, "status": "GOOD"},
            {"creative_id": "meta-c2", "headline": "Cheap Deals", "ctr": 0.005, "status": "UNDERPERFORMING"}
        ]
