import logging
import json
import os
from typing import Protocol, Any, Optional

logger = logging.getLogger(__name__)

class MarketingClient(Protocol):
    """Universal interface for marketing channel integrations (§5)."""
    provider: str

    async def create_campaign(self, campaign_id: str, name: str, budget_minor: int, bid_minor: int) -> bool:
        ...

    async def update_campaign(
        self, 
        campaign_id: str, 
        budget_minor: Optional[int] = None, 
        bid_minor: Optional[int] = None, 
        status: Optional[str] = None
    ) -> bool:
        ...

    async def delete_campaign(self, campaign_id: str) -> bool:
        ...

    async def get_campaign(self, campaign_id: str) -> Optional[dict]:
        ...

    async def get_performance(self, campaign_id: str) -> Optional[dict]:
        ...

    async def swap_pmax_audience(self, campaign_names: list[str], new_audience_id: str) -> bool:
        ...

    async def clean_search_keywords(self, campaign_name: str, brand_terms: list[str]) -> tuple[bool, list[str]]:
        ...

    async def bootstrap_offline_conversions(self) -> dict:
        ...

    async def create_audience(self, name: str, lookalike_params: dict) -> dict:
        ...

    async def update_keyword_bid_strategy(self, campaign_id: str, strategy_type: str, value: float) -> bool:
        ...

    async def audit_creatives(self, campaign_id: str) -> list[dict]:
        ...


def get_marketing_client(provider: str, token: Optional[str] = None, config: Optional[dict] = None) -> MarketingClient:
    """Factory to resolve the active marketing client for a provider."""
    # 1. Check for test environment or explicit mock provider
    env = os.getenv("AOS_ENV", "development")
    if env == "test" or provider == "mock":
        return MockMarketingClient(provider=provider)

    # 2. If no credentials (token) are provided for real channels, raise ValueError
    if provider in ("google-ads", "meta-ads"):
        if not token:
            raise ValueError(f"Credentials (token) are required for provider: {provider}")
        
        if provider == "google-ads":
            from app.services.google_ads import GoogleAdsClient
            return GoogleAdsClient(token=token, config=config)
            
        if provider == "meta-ads":
            from app.services.meta_ads import MetaAdsClient
            return MetaAdsClient(token=token, config=config)

    # 3. For any unknown providers, raise ValueError
    raise ValueError(f"Unsupported marketing provider: {provider}")


def _get_campaigns_file() -> str:
    return os.getenv("AOS_MOCK_CAMPAIGNS_FILE") or os.path.join(os.path.dirname(__file__), "../../scratch/mock_marketing_campaigns.json")

class MockMarketingClient:

    @classmethod
    def _load(cls) -> dict:
        campaigns_file = _get_campaigns_file()
        if os.path.exists(campaigns_file):
            try:
                with open(campaigns_file, "r") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load mock campaigns: {e}")
                return {}
        return {}

    @classmethod
    def _save(cls, data: dict):
        campaigns_file = _get_campaigns_file()
        os.makedirs(os.path.dirname(campaigns_file), exist_ok=True)
        try:
            with open(campaigns_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save mock campaigns: {e}")

    @classmethod
    def clear(cls):
        campaigns_file = _get_campaigns_file()
        if os.path.exists(campaigns_file):
            try:
                os.remove(campaigns_file)
            except Exception:
                pass

    def __init__(self, provider: str = "google-ads"):
        self.provider = provider

    async def create_campaign(self, campaign_id: str, name: str, budget_minor: int, bid_minor: int) -> bool:
        campaigns = self._load()
        campaigns[campaign_id] = {
            "id": campaign_id,
            "name": name,
            "budget_minor": budget_minor,
            "bid_minor": bid_minor,
            "status": "ACTIVE",
            "impressions": 0,
            "clicks": 0,
            "spend_minor": 0,
            "conversions": 0
        }
        self._save(campaigns)
        logger.info(f"Mock created campaign {campaign_id} ({name}) via {self.provider} with budget {budget_minor/100:.2f}")
        return True

    async def update_campaign(self, campaign_id: str, budget_minor: int = None, bid_minor: int = None, status: str = None) -> bool:
        campaigns = self._load()
        if campaign_id not in campaigns:
            logger.error(f"Campaign {campaign_id} not found")
            return False
        if budget_minor is not None:
            campaigns[campaign_id]["budget_minor"] = budget_minor
        if bid_minor is not None:
            campaigns[campaign_id]["bid_minor"] = bid_minor
        if status is not None:
            campaigns[campaign_id]["status"] = status
        self._save(campaigns)
        logger.info(f"Mock updated campaign {campaign_id}: budget={budget_minor}, bid={bid_minor}, status={status}")
        return True

    async def delete_campaign(self, campaign_id: str) -> bool:
        campaigns = self._load()
        if campaign_id in campaigns:
            del campaigns[campaign_id]
            self._save(campaigns)
            logger.info(f"Mock deleted campaign {campaign_id}")
            return True
        return False

    async def get_campaign(self, campaign_id: str) -> dict | None:
        campaigns = self._load()
        return campaigns.get(campaign_id)

    async def get_performance(self, campaign_id: str) -> dict | None:
        campaigns = self._load()
        camp = campaigns.get(campaign_id)
        if not camp:
            return None
            
        budget = camp["budget_minor"]
        roi = 1.5
        if "fail" in camp["name"].lower():
            roi = 0.5
            
        spend = int(budget * 0.8)
        revenue = int(spend * roi)
        conversions = int(spend / 1000)
        
        return {
            "campaign_id": campaign_id,
            "impressions": spend * 10,
            "clicks": spend // 2,
            "spend_minor": spend,
            "revenue_minor": revenue,
            "conversions": conversions,
            "roi": roi
        }

    async def swap_pmax_audience(self, campaign_names: list[str], new_audience_id: str) -> bool:
        campaigns = self._load()
        for name in campaign_names:
            match = next((c for c in campaigns.values() if c["name"] == name), None)
            if match:
                match["pmax_audience_id"] = new_audience_id
        self._save(campaigns)
        logger.info(f"[MOCK] Swapped PMax audience for campaigns {campaign_names} to {new_audience_id}")
        return True

    async def clean_search_keywords(self, campaign_name: str, brand_terms: list[str]) -> tuple[bool, list[str]]:
        logger.info(f"[MOCK] Generic keyword audit completed for search campaign '{campaign_name}'. Paused 2 generic keywords.")
        return True, ["customers/123/adGroupCriteria/12~34", "customers/123/adGroupCriteria/12~56"]

    async def bootstrap_offline_conversions(self) -> dict:
        logger.info(f"[MOCK] Bootstrapped offline UPLOAD_CLICKS conversion action via {self.provider}")
        return {
            "success": True,
            "conversion_action_id": "mock-conversion-12345",
            "name": "AgencyOS CRM Lead Conversion",
            "status": "CREATED_MOCK",
        }

    async def create_audience(self, name: str, lookalike_params: dict) -> dict:
        logger.info(f"[MOCK] Created audience {name} with parameters {lookalike_params}")
        return {
            "success": True,
            "audience_id": "mock-audience-99999",
            "name": name,
            "status": "ACTIVE"
        }

    async def update_keyword_bid_strategy(self, campaign_id: str, strategy_type: str, value: float) -> bool:
        logger.info(f"[MOCK] Updated keyword bid strategy on campaign {campaign_id} to {strategy_type} with value {value}")
        return True

    async def audit_creatives(self, campaign_id: str) -> list[dict]:
        logger.info(f"[MOCK] Auditing creatives for campaign {campaign_id}")
        return [
            {"creative_id": "c1", "headline": "Buy Now!", "ctr": 0.05, "status": "GOOD"},
            {"creative_id": "c2", "headline": "Cheap Deals", "ctr": 0.005, "status": "UNDERPERFORMING"}
        ]
