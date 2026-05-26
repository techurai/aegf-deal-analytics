import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

app = FastAPI(title="AEGF Deal Analytics Engine")

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("aegf-deal-analytics")

GHL_API_KEY = os.getenv("GHL_API_KEY")
GHL_API_VERSION = os.getenv("GHL_API_VERSION", "2023-02-21")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# Railway environment variable. Example:
# {
#   "projected_purchase_price": "abc123",
#   "projected_rehab_budget": "def456",
#   "projected_sale_price": "ghi789",
#   "actual_purchase_price": "jkl012",
#   "actual_rehab_cost": "mno345",
#   "actual_sale_price": "pqr678",
#   "projected_gross_profit": "stu901",
#   "actual_gross_profit": "vwx234",
#   "projected_roi_percent": "yz567",
#   "actual_roi_percent": "aaa111"
# }
FIELD_MAP: Dict[str, str] = json.loads(os.getenv("FIELD_MAP", "{}"))

GHL_BASE_URL = "https://services.leadconnectorhq.com"


def require_config() -> None:
    if not GHL_API_KEY:
        raise HTTPException(status_code=500, detail="Missing GHL_API_KEY environment variable")


def ghl_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Version": GHL_API_VERSION,
    }


def extract_opportunity_id(payload: Dict[str, Any]) -> Optional[str]:
    """Handle the most common GHL workflow webhook payload shapes."""
    candidates = [
        payload.get("opportunityId"),
        payload.get("opportunity_id"),
        payload.get("id"),
        payload.get("customData", {}).get("opportunityId"),
        payload.get("customData", {}).get("opportunity_id"),
        payload.get("opportunity", {}).get("id"),
    ]
    return next((str(value) for value in candidates if value), None)


def normalize_money(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def get_custom_field_value(opportunity: Dict[str, Any], logical_name: str) -> Any:
    field_id = FIELD_MAP.get(logical_name)
    if not field_id:
        return None

    custom_fields = opportunity.get("customFields") or []
    for field in custom_fields:
        if field.get("id") == field_id or field.get("fieldId") == field_id:
            return field.get("value")
    return None


def build_custom_field_update(logical_name: str, value: Any) -> Optional[Dict[str, Any]]:
    field_id = FIELD_MAP.get(logical_name)
    if not field_id:
        logger.warning("No field mapping found for %s", logical_name)
        return None
    return {"id": field_id, "value": value}


def calculate_deal_metrics(opportunity: Dict[str, Any]) -> Dict[str, Any]:
    projected_purchase = normalize_money(get_custom_field_value(opportunity, "projected_purchase_price"))
    projected_rehab = normalize_money(get_custom_field_value(opportunity, "projected_rehab_budget"))
    projected_sale = normalize_money(get_custom_field_value(opportunity, "projected_sale_price"))

    actual_purchase = normalize_money(get_custom_field_value(opportunity, "actual_purchase_price"))
    actual_rehab = normalize_money(get_custom_field_value(opportunity, "actual_rehab_cost"))
    actual_sale = normalize_money(get_custom_field_value(opportunity, "actual_sale_price"))

    projected_basis = projected_purchase + projected_rehab
    actual_basis = actual_purchase + actual_rehab

    projected_gross_profit = projected_sale - projected_basis if projected_sale else 0
    actual_gross_profit = actual_sale - actual_basis if actual_sale else 0

    projected_roi = (projected_gross_profit / projected_basis * 100) if projected_basis else 0
    actual_roi = (actual_gross_profit / actual_basis * 100) if actual_basis else 0

    return {
        "projected_gross_profit": round(projected_gross_profit, 2),
        "actual_gross_profit": round(actual_gross_profit, 2),
        "projected_roi_percent": round(projected_roi, 2),
        "actual_roi_percent": round(actual_roi, 2),
        "last_calculated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }


async def get_opportunity(opportunity_id: str) -> Dict[str, Any]:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(
            f"{GHL_BASE_URL}/opportunities/{opportunity_id}",
            headers=ghl_headers(),
        )
    logger.info("GHL GET opportunity status=%s", response.status_code)
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json().get("opportunity", response.json())


async def update_opportunity(opportunity_id: str, computed: Dict[str, Any]) -> Dict[str, Any]:
    custom_fields = [
        item for item in (
            build_custom_field_update("projected_gross_profit", computed["projected_gross_profit"]),
            build_custom_field_update("actual_gross_profit", computed["actual_gross_profit"]),
            build_custom_field_update("projected_roi_percent", computed["projected_roi_percent"]),
            build_custom_field_update("actual_roi_percent", computed["actual_roi_percent"]),
            build_custom_field_update("last_calculated_at", computed["last_calculated_at"]),
        )
        if item is not None
    ]

    if not custom_fields:
        raise HTTPException(status_code=500, detail="No output custom fields mapped in FIELD_MAP")

    body = {"customFields": custom_fields}

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.put(
            f"{GHL_BASE_URL}/opportunities/{opportunity_id}",
            headers=ghl_headers(),
            json=body,
        )
    logger.info("GHL PUT opportunity status=%s body=%s", response.status_code, response.text[:500])
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=response.text)
    return response.json()


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": "aegf-deal-analytics",
        "field_map_loaded": bool(FIELD_MAP),
        "ghl_api_key_loaded": bool(GHL_API_KEY),
    }


@app.post("/webhook/ghl")
async def ghl_webhook(request: Request, x_webhook_secret: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    require_config()
    payload = await request.json()
    logger.info("Webhook received: %s", json.dumps(payload)[:1000])

    opportunity_id = extract_opportunity_id(payload)
    if not opportunity_id:
        raise HTTPException(status_code=400, detail="Could not find opportunity ID in webhook payload")

    opportunity = await get_opportunity(opportunity_id)
    computed = calculate_deal_metrics(opportunity)
    ghl_response = await update_opportunity(opportunity_id, computed)

    return {
        "ok": True,
        "opportunity_id": opportunity_id,
        "computed": computed,
        "ghl_update_response": ghl_response,
    }


@app.post("/debug/payload")
async def debug_payload(request: Request) -> Dict[str, Any]:
    """Temporary endpoint for inspecting the exact GHL workflow webhook payload."""
    payload = await request.json()
    return {
        "ok": True,
        "detected_opportunity_id": extract_opportunity_id(payload),
        "payload": payload,
    }
