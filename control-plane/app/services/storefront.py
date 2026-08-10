import os
from abc import ABC, abstractmethod
from typing import Optional

class IStorefrontAdapter(ABC):
    """Unified interface for all storefront channels (Shopify, WooCommerce, etc.) in Agency OS."""
    
    @abstractmethod
    async def run_catalog_audit(self) -> dict:
        """Audits the product catalog for SKU completeness, barcodes (GTINs), and metadata quality."""
        pass

    @abstractmethod
    async def run_sales_analysis(self) -> dict:
        """Analyzes recent orders to calculate AOV, total sales, and customer geographic hubs."""
        pass

    @abstractmethod
    async def register_poas_webhooks(self, gateway_url: str) -> dict:
        """Registers purchase/fulfillment webhooks pointing to the sGTM tracking gateway."""
        pass

    @abstractmethod
    async def get_products_with_costs(self) -> list[dict]:
        """Retrieves products list with SKU, price, and supply cost."""
        pass

    @abstractmethod
    async def update_product_price(self, sku: str, new_price: int) -> bool:
        """Updates the price of a variant by SKU."""
        pass


def get_storefront_client(provider: str = "shopify", shop_url: str = "", token: Optional[str] = None) -> IStorefrontAdapter:
    """Factory resolving a storefront adapter for a provider.

    Mirrors services.marketing.get_marketing_client / services.gtm.get_gtm_client:
    - In the test environment a mock-backed client is returned.
    - In real environments a token is required. ShopifyStorefront self-selects mock mode
      for placeholder/empty tokens, so audits stay safe when misconfigured.
    """
    if provider in ("shopify", "mock"):
        from app.services.shopify import ShopifyStorefront
        env = os.getenv("AOS_ENV", "development")
        if env == "test":
            return ShopifyStorefront(shop_url=shop_url or "mock-shop", token=token or "mock-token")
        if not token:
            raise ValueError(f"Credentials (token) are required for provider: {provider}")
        return ShopifyStorefront(shop_url=shop_url, token=token)
    raise ValueError(f"Unsupported storefront provider: {provider}")
