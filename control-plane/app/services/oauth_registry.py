# app/services/oauth_registry.py
import os
import urllib.parse
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class OauthProviderRegistry:
    """Unified registry defining the OAuth configurations for all supported business platforms."""
    
    CONFIGS: Dict[str, Dict[str, Any]] = {
        "google-ads": {
            "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
            "token_url": "https://oauth2.googleapis.com/token",
            "scopes": ["https://www.googleapis.com/auth/adwords"],
            "extra_params": {"access_type": "offline", "prompt": "consent", "response_type": "code"}
        },
        "meta-ads": {
            "auth_url": "https://www.facebook.com/v18.0/dialog/oauth",
            "token_url": "https://graph.facebook.com/v18.0/oauth/access_token",
            "scopes": ["ads_management", "ads_read", "business_management"],
            "extra_params": {"response_type": "code"}
        },
        "tiktok-ads": {
            "auth_url": "https://business-api.tiktok.com/portal/v1.3/oauth",
            "token_url": "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/",
            "scopes": ["ads:read", "ads:write"],
            "extra_params": {}
        },
        "hubspot": {
            "auth_url": "https://app.hubspot.com/oauth/authorize",
            "token_url": "https://api.hubapi.com/oauth/v1/token",
            "scopes": ["crm.objects.contacts.read", "crm.objects.contacts.write"],
            "extra_params": {}
        },
        "salesforce": {
            "auth_url": "/services/oauth2/authorize",
            "token_url": "/services/oauth2/token",
            "scopes": ["api", "refresh_token", "offline_access"],
            "extra_params": {"response_type": "code"}
        }
    }

    @classmethod
    def get_authorize_url(cls, provider: str, state: str, redirect_uri: str, custom_domain: Optional[str] = None) -> str:
        """Dynamically compiles the correct authorization redirect URL for any platform."""
        cfg = cls.CONFIGS.get(provider)
        if not cfg:
            raise ValueError(f"OAuth Provider '{provider}' not supported in registry.")
            
        if provider == "salesforce" and not custom_domain:
            custom_domain = "login.salesforce.com"

        base_auth_url = cfg["auth_url"]
        if custom_domain:
            if not custom_domain.startswith("https://"):
                custom_domain = f"https://{custom_domain}"
            base_auth_url = f"{custom_domain.rstrip('/')}{cfg['auth_url']}"
        elif not base_auth_url.startswith("http"):
            # Providers like Salesforce use a per-tenant instance host; a relative
            # auth_url without custom_domain would produce a broken same-host redirect.
            raise ValueError(
                f"OAuth provider '{provider}' requires an instance domain (custom_domain) but none was supplied."
            )

        env_prefix = provider.replace("-", "_").upper()
        client_id = os.getenv(f"{env_prefix}_CLIENT_ID", f"mock-{provider}-client-id")

        params = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(cfg["scopes"]),
            **cfg["extra_params"]
        }
        
        return f"{base_auth_url}?{urllib.parse.urlencode(params)}"

    @classmethod
    def get_exchange_payload(cls, provider: str, code: str, redirect_uri: str, custom_domain: Optional[str] = None) -> tuple[str, dict]:
        """Dynamically compiles the token exchange URL and POST payload for any platform."""
        cfg = cls.CONFIGS.get(provider)
        if not cfg:
            raise ValueError(f"OAuth Provider '{provider}' not supported in registry.")
            
        if provider == "salesforce" and not custom_domain:
            custom_domain = "login.salesforce.com"

        base_token_url = cfg["token_url"]
        if custom_domain:
            if not custom_domain.startswith("https://"):
                custom_domain = f"https://{custom_domain}"
            base_token_url = f"{custom_domain.rstrip('/')}{cfg['token_url']}"
        elif not base_token_url.startswith("http"):
            raise ValueError(
                f"OAuth provider '{provider}' requires an instance domain (custom_domain) but none was supplied."
            )

        env_prefix = provider.replace("-", "_").upper()
        client_id = os.getenv(f"{env_prefix}_CLIENT_ID", f"mock-{provider}-client-id")
        client_secret = os.getenv(f"{env_prefix}_CLIENT_SECRET", f"mock-{provider}-client-secret")
        
        payload = {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code"
        }
        
        return base_token_url, payload
