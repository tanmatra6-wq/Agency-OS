import logging
import json
from httpx import AsyncClient
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import BrandProperty
from app.services.oauth import normalize_shopify_domain
from app.services.llm import VertexAIClient

logger = logging.getLogger(__name__)

async def bootstrap_brand_identity(db_session: AsyncSession, tenant_id: str, brand_id: str, shopify_token: str, shop: str = None):
    """Scans Shopify catalog, calls Gemini, and seeds the Brand RAG Profile."""
    logger.info(f"Starting autonomous RAG bootstrapping for brand {brand_id}...")

    # A. Fetch Shop and Product metadata from Shopify
    shop_domain = normalize_shopify_domain(shop or brand_id)
    shop_url = f"https://{shop_domain}/admin/api/2024-01/products.json?limit=5"
    headers = {
        "X-Shopify-Access-Token": shopify_token,
        "Content-Type": "application/json"
    }
    
    async with AsyncClient() as client:
        try:
            resp = await client.get(shop_url, headers=headers, timeout=10.0)
            if resp.status_code == 200:
                products_data = resp.json().get("products", [])
            else:
                logger.error(f"Shopify catalog fetch failed with status: {resp.status_code}")
                products_data = []
        except Exception as e:
            logger.error(f"Failed to scan Shopify catalog during RAG bootstrap: {e}")
            return
            
    # B. Compile catalog context
    catalog_summary = []
    for p in products_data:
        catalog_summary.append(
            f"Title: {p.get('title')}\n"
            f"Type: {p.get('product_type')}\n"
            f"Description: {p.get('body_html', '')[:200]}"
        )
    catalog_context = "\n---\n".join(catalog_summary)
    
    # C. Call Gemini to synthesize Tone of Voice, Target Persona, and Guidelines
    llm_client = VertexAIClient()
    
    prompt = (
        f"Analyze this e-commerce product catalog and synthesize a highly accurate Brand Identity profile. "
        f"Determine:\n"
        f"1. Tone of Voice (TOV) (e.g. empathetic, sensory-friendly, highly professional, playful).\n"
        f"2. Target Audience Personas (e.g. ADHD parents, outdoor enthusiasts).\n"
        f"3. Key copy guidelines (what terms to focus on, what to avoid).\n\n"
        f"Catalog Context:\n{catalog_context}"
    )
    
    try:
        identity_data = await llm_client.generate_personalized_content(
            tenant_id=tenant_id,
            brand_id=brand_id,
            prompt=prompt,
            session=None,
            system_instruction=(
                "You are a senior brand consultant. Respond ONLY with a valid JSON object containing: "
                "'tone_of_voice', 'target_persona', and 'past_experience' keys."
            )
        )
    except Exception as e:
        logger.error(f"Failed to synthesize brand identity via LLM: {e}")
        identity_data = {
            "tone_of_voice": "Friendly, modern, and direct",
            "target_persona": "General e-commerce consumers",
            "past_experience": "No performance logs recorded yet."
        }

    if isinstance(identity_data, str):
        try:
            identity_data = json.loads(identity_data)
        except Exception as e:
            logger.error(f"Failed to parse identity JSON: {e}")
            identity_data = {
                "tone_of_voice": "Friendly, modern, and direct",
                "target_persona": "General e-commerce consumers",
                "past_experience": "No performance logs recorded yet."
            }

    # D. Write the RAG profile directly to the database
    # Clean up any existing brand_identity entries
    stmt_cleanup = delete(BrandProperty).where(
        BrandProperty.tenant_id == tenant_id,
        BrandProperty.brand_id == brand_id,
        BrandProperty.type == "brand_identity"
    )
    await db_session.execute(stmt_cleanup)
        
    rag_prop = BrandProperty(
        tenant_id=tenant_id,
        brand_id=brand_id,
        type="brand_identity",
        provider="internal",
        status="active",
        findings=identity_data
    )
    db_session.add(rag_prop)
    logger.info(f"Autonomous RAG profile successfully bootstrapped for brand {brand_id}! TOV: {identity_data.get('tone_of_voice')}")

async def bootstrap_brand_identity_task(tenant_id: str, brand_id: str, shopify_token: str, session_maker, shop: str = None):
    """Background task wrapper that creates its own database session."""
    async with session_maker() as db_session:
        await bootstrap_brand_identity(db_session, tenant_id, brand_id, shopify_token, shop)
        await db_session.commit()
