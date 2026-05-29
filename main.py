import json
import logging
import os
import re
import time
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request


# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("aegf-deal-analytics")

app = FastAPI(title="AEGF Deal Analytics")

SERVICE_NAME = os.getenv("SERVICE_NAME", "aegf-deal-analytics")
ANALYTICS_VERSION = os.getenv("ANALYTICS_VERSION", "2026-05-29-key-map-v1")

GHL_API_BASE = os.getenv("GHL_API_BASE", "https://services.leadconnectorhq.com").rstrip("/")
GHL_API_VERSION = os.getenv("GHL_API_VERSION", "2023-02-21")
GHL_API_KEY = os.getenv("GHL_API_KEY", "").strip()

# Keep this false in production. Set true temporarily if you want to verify
# calculations without writing back to HighLevel.
DRY_RUN = os.getenv("DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "y"}
HTTP_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT_SECONDS", "25"))

# Optional: if your GHL webhook uses a nonstandard payload key for the opportunity ID,
# set OPPORTUNITY_ID_FIELD to that key name in Railway.
OPPORTUNITY_ID_FIELD = os.getenv("OPPORTUNITY_ID_FIELD", "").strip()


# -----------------------------------------------------------------------------
# Field map
# -----------------------------------------------------------------------------
# These are the GHL opportunity custom-field keys visible in Settings > Custom Fields.
# FIELD_MAP in Railway may override or extend this. FIELD_MAP values can be either:
#   "opportunity.actual_sale_price"
#   "{{opportunity.actual_sale_price}}"
#   "a-real-ghl-custom-field-id"

FIELD_KEYS = [
    "acquisition_channel",
    "actual_cash_invested",
    "actual_cashoncash_return",
    "actual_close_date",
    "actual_closing_costs",
    "actual_gross_profit",
    "actual_holding_costs",
    "actual_interest_rate",
    "actual_loan_amount",
    "actual_monthly_payment",
    "actual_monthly_rent",
    "actual_net_profit",
    "actual_purchase_price",
    "actual_refi_amount",
    "actual_refi_date",
    "actual_rehab_completion",
    "actual_rehab_cost",
    "actual_roi",
    "actual_sale_date",
    "actual_sale_price",
    "analytics_version",
    "assigned_underwriter",
    "contract_date",
    "days_in_rehab",
    "days_to_close",
    "deal_outcome",
    "deal_status",
    "deal_type",
    "deal_wonlost_reason",
    "dscr",
    "equity_created",
    "est_close_date",
    "est_refi_date",
    "est_rehab_completion",
    "est_sale_date",
    "exit_strategy",
    "high_priority",
    "is_behind_schedule",
    "is_profitable",
    "last_analytics_update",
    "last_calculation_runtime_ms",
    "last_sync_error",
    "last_sync_status",
    "last_webhook_received",
    "lead_received_date",
    "mao",
    "meets_buy_box",
    "offer_submitted_date",
    "profit_variance",
    "proj_arv",
    "proj_cash_invested",
    "proj_closing_costs",
    "proj_holding_costs",
    "proj_interest_rate",
    "proj_loan_amount",
    "proj_monthly_payment",
    "proj_monthly_rent",
    "proj_purchase_price",
    "proj_refi_amount",
    "proj_rehab_budget",
    "proj_sale_price",
    "projected_cashoncash_return",
    "projected_gross_profit",
    "projected_net_profit",
    "projected_roi",
    "property_address",
    "purchase_variance",
    "rehab_variance",
    "timeline_variance_days",
    "total_deal_duration",
]

DEFAULT_FIELD_MAP: Dict[str, str] = {key: f"opportunity.{key}" for key in FIELD_KEYS}

FIELD_MAP_LOAD_ERROR: Optional[str] = None
FIELD_MAP_SOURCE = "default"


def normalize_field_ref(value: Any) -> str:
    """Normalize GHL merge tags into API-ready field keys."""
    if value is None:
        return ""

    text = str(value).strip()

    # Convert {{opportunity.foo}} to opportunity.foo
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2].strip()

    return text


def load_field_map() -> Dict[str, str]:
    global FIELD_MAP_LOAD_ERROR, FIELD_MAP_SOURCE

    field_map = dict(DEFAULT_FIELD_MAP)
    raw = os.getenv("FIELD_MAP", "").strip()

    if not raw:
        FIELD_MAP_SOURCE = "default"
        return field_map

    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("FIELD_MAP must be a JSON object")

        for logical_key, ghl_ref in parsed.items():
            field_map[str(logical_key).strip()] = normalize_field_ref(ghl_ref)

        FIELD_MAP_SOURCE = "env+default"
        return field_map
    except Exception as exc:
        FIELD_MAP_LOAD_ERROR = str(exc)
        FIELD_MAP_SOURCE = "default_after_env_error"
        logger.exception("Failed to parse FIELD_MAP. Falling back to DEFAULT_FIELD_MAP.")
        return field_map


FIELD_MAP = load_field_map()


# Common aliases from earlier versions of this project and possible webhook payloads.
ALIASES: Dict[str, List[str]] = {
    "proj_purchase_price": ["projected_purchase_price", "purchase_price", "purchase"],
    "proj_rehab_budget": ["projected_rehab_budget", "rehab_budget", "rehab_cost"],
    "proj_sale_price": ["projected_sale_price", "sale_price", "arv", "proj_arv"],
    "proj_cash_invested": ["projected_cash_invested", "cash_invested"],
    "projected_roi": ["projected_roi_percent", "proj_roi", "roi"],
    "actual_purchase_price": ["purchase_price_actual"],
    "actual_rehab_cost": ["rehab_cost_actual"],
    "actual_sale_price": ["sale_price_actual"],
    "actual_cash_invested": ["cash_invested_actual"],
    "actual_roi": ["actual_roi_percent"],
    "property_address": ["address", "full_address", "opportunity_name"],
}


# -----------------------------------------------------------------------------
# General helpers
# -----------------------------------------------------------------------------


def now_date_string() -> str:
    # GHL Date Picker fields are safest as YYYY-MM-DD.
    return datetime.now(timezone.utc).date().isoformat()


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def norm_lookup_key(value: Any) -> str:
    """Normalize keys enough to match payload keys, merge tags, and GHL field keys."""
    text = normalize_field_ref(value).strip().lower()
    text = text.replace("{{", "").replace("}}", "")
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def flatten_payload(data: Any, prefix: str = "") -> Dict[str, Any]:
    """Flatten nested dictionaries for forgiving webhook lookup."""
    out: Dict[str, Any] = {}

    if isinstance(data, dict):
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            out[path] = value
            out[norm_lookup_key(path)] = value
            out[norm_lookup_key(key)] = value
            if isinstance(value, dict):
                out.update(flatten_payload(value, path))
    return out


def walk(obj: Any) -> Iterable[Any]:
    """Yield every nested object inside obj."""
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from walk(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from walk(item)


def build_custom_field_lookup(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Extract custom field values from common GHL customFields shapes."""
    lookup: Dict[str, Any] = {}

    for obj in walk(payload):
        if not isinstance(obj, dict):
            continue

        has_custom_field_identity = any(k in obj for k in ("id", "key", "fieldKey", "field_key", "name"))
        has_custom_field_value = any(
            k in obj for k in ("field_value", "fieldValue", "value", "values")
        )

        if not (has_custom_field_identity and has_custom_field_value):
            continue

        value = None
        for value_key in ("field_value", "fieldValue", "value", "values"):
            if value_key in obj:
                value = obj.get(value_key)
                break

        for identity_key in ("id", "key", "fieldKey", "field_key", "name"):
            identity = obj.get(identity_key)
            if not is_blank(identity):
                lookup[norm_lookup_key(identity)] = value

    return lookup


def field_candidates(logical_key: str) -> List[str]:
    ghl_ref = FIELD_MAP.get(logical_key, f"opportunity.{logical_key}")
    normalized_ref = normalize_field_ref(ghl_ref)

    candidates = [
        logical_key,
        normalized_ref,
        f"{{{{{normalized_ref}}}}}",
    ]

    if normalized_ref.startswith("opportunity."):
        candidates.append(normalized_ref.replace("opportunity.", "", 1))

    candidates.extend(ALIASES.get(logical_key, []))
    return [candidate for candidate in candidates if not is_blank(candidate)]


def get_payload_value(payload: Dict[str, Any], logical_key: str) -> Any:
    flat = flatten_payload(payload)
    custom_lookup = build_custom_field_lookup(payload)

    for candidate in field_candidates(logical_key):
        # Exact direct match first.
        if isinstance(payload, dict) and candidate in payload and not is_blank(payload[candidate]):
            return payload[candidate]

        normalized = norm_lookup_key(candidate)
        if normalized in flat and not is_blank(flat[normalized]):
            return flat[normalized]

        if normalized in custom_lookup and not is_blank(custom_lookup[normalized]):
            return custom_lookup[normalized]

    return None


def parse_decimal(value: Any) -> Optional[Decimal]:
    if is_blank(value):
        return None

    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return None

    text = str(value).strip()
    if not text:
        return None

    # Handle accounting-style negatives: ($1,234.50)
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    text = text.replace("$", "").replace(",", "").replace("%", "").strip()

    try:
        number = Decimal(text)
        return -number if negative else number
    except InvalidOperation:
        return None


def parse_date(value: Any) -> Optional[date]:
    if is_blank(value):
        return None

    if isinstance(value, date) and not isinstance(value, datetime):
        return value

    if isinstance(value, datetime):
        return value.date()

    text = str(value).strip()
    if not text:
        return None

    # Try ISO datetime/date first.
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue

    return None


def days_between(start: Optional[date], end: Optional[date]) -> Optional[int]:
    if not start or not end:
        return None
    return (end - start).days


def round_decimal(value: Optional[Decimal], places: str = "0.01") -> Optional[float]:
    if value is None:
        return None
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def percent(numerator: Optional[Decimal], denominator: Optional[Decimal]) -> Optional[Decimal]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return (numerator / denominator) * Decimal("100")


def safe_add(*values: Optional[Decimal]) -> Optional[Decimal]:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present, Decimal("0"))


def safe_subtract(first: Optional[Decimal], *rest: Optional[Decimal]) -> Optional[Decimal]:
    if first is None:
        return None
    total = first
    for value in rest:
        if value is not None:
            total -= value
    return total


def estimate_cash_invested(
    cash_invested: Optional[Decimal],
    purchase_price: Optional[Decimal],
    rehab_cost: Optional[Decimal],
    closing_costs: Optional[Decimal],
    holding_costs: Optional[Decimal],
    loan_amount: Optional[Decimal],
) -> Optional[Decimal]:
    if cash_invested is not None:
        return cash_invested

    total_cost = safe_add(purchase_price, rehab_cost, closing_costs, holding_costs)
    if total_cost is None:
        return None

    if loan_amount is not None:
        estimated = total_cost - loan_amount
        return estimated if estimated > 0 else Decimal("0")

    return total_cost


def set_if_value(target: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    target[key] = value


# -----------------------------------------------------------------------------
# Analytics calculations
# -----------------------------------------------------------------------------


def calculate_deal_fields(payload: Dict[str, Any]) -> Dict[str, Any]:
    calculated: Dict[str, Any] = {}

    today = now_date_string()
    calculated["analytics_version"] = ANALYTICS_VERSION
    calculated["last_webhook_received"] = today
    calculated["last_analytics_update"] = today

    # Projected inputs
    proj_purchase = parse_decimal(get_payload_value(payload, "proj_purchase_price"))
    proj_rehab = parse_decimal(get_payload_value(payload, "proj_rehab_budget"))
    proj_sale = parse_decimal(get_payload_value(payload, "proj_sale_price"))
    proj_arv = parse_decimal(get_payload_value(payload, "proj_arv"))
    proj_closing = parse_decimal(get_payload_value(payload, "proj_closing_costs"))
    proj_holding = parse_decimal(get_payload_value(payload, "proj_holding_costs"))
    proj_loan = parse_decimal(get_payload_value(payload, "proj_loan_amount"))
    proj_cash_input = parse_decimal(get_payload_value(payload, "proj_cash_invested"))
    proj_monthly_rent = parse_decimal(get_payload_value(payload, "proj_monthly_rent"))
    proj_monthly_payment = parse_decimal(get_payload_value(payload, "proj_monthly_payment"))

    if proj_sale is None and proj_arv is not None:
        proj_sale = proj_arv

    projected_gross_profit = None
    projected_net_profit = None
    projected_cash_invested = None

    if proj_sale is not None and proj_purchase is not None:
        projected_gross_profit = safe_subtract(proj_sale, proj_purchase, proj_rehab)
        projected_net_profit = safe_subtract(
            proj_sale,
            proj_purchase,
            proj_rehab,
            proj_closing,
            proj_holding,
        )
        projected_cash_invested = estimate_cash_invested(
            proj_cash_input,
            proj_purchase,
            proj_rehab,
            proj_closing,
            proj_holding,
            proj_loan,
        )

    projected_roi = percent(projected_net_profit, projected_cash_invested)

    projected_cashoncash = None
    if proj_monthly_rent is not None and proj_monthly_payment is not None:
        annual_cash_flow = (proj_monthly_rent - proj_monthly_payment) * Decimal("12")
        projected_cashoncash = percent(annual_cash_flow, projected_cash_invested)

    dscr = None
    if proj_monthly_rent is not None and proj_monthly_payment not in (None, Decimal("0")):
        dscr = proj_monthly_rent / proj_monthly_payment

    mao = None
    mao_basis = proj_arv if proj_arv is not None else proj_sale
    if mao_basis is not None:
        mao = (mao_basis * Decimal("0.70")) - (proj_rehab or Decimal("0"))

    set_if_value(calculated, "projected_gross_profit", round_decimal(projected_gross_profit))
    set_if_value(calculated, "projected_net_profit", round_decimal(projected_net_profit))
    set_if_value(calculated, "proj_cash_invested", round_decimal(projected_cash_invested))
    set_if_value(calculated, "projected_roi", round_decimal(projected_roi))
    set_if_value(calculated, "projected_cashoncash_return", round_decimal(projected_cashoncash))
    set_if_value(calculated, "dscr", round_decimal(dscr))
    set_if_value(calculated, "mao", round_decimal(mao))

    # Actual inputs
    actual_purchase = parse_decimal(get_payload_value(payload, "actual_purchase_price"))
    actual_rehab = parse_decimal(get_payload_value(payload, "actual_rehab_cost"))
    actual_sale = parse_decimal(get_payload_value(payload, "actual_sale_price"))
    actual_closing = parse_decimal(get_payload_value(payload, "actual_closing_costs"))
    actual_holding = parse_decimal(get_payload_value(payload, "actual_holding_costs"))
    actual_loan = parse_decimal(get_payload_value(payload, "actual_loan_amount"))
    actual_cash_input = parse_decimal(get_payload_value(payload, "actual_cash_invested"))
    actual_monthly_rent = parse_decimal(get_payload_value(payload, "actual_monthly_rent"))
    actual_monthly_payment = parse_decimal(get_payload_value(payload, "actual_monthly_payment"))
    actual_refi_amount = parse_decimal(get_payload_value(payload, "actual_refi_amount"))

    actual_gross_profit = None
    actual_net_profit = None
    actual_cash_invested = None

    # For refi/hold deals, use refi amount if there is no sale price yet.
    actual_exit_value = actual_sale if actual_sale is not None else actual_refi_amount

    if actual_exit_value is not None and actual_purchase is not None:
        actual_gross_profit = safe_subtract(actual_exit_value, actual_purchase, actual_rehab)
        actual_net_profit = safe_subtract(
            actual_exit_value,
            actual_purchase,
            actual_rehab,
            actual_closing,
            actual_holding,
        )
        actual_cash_invested = estimate_cash_invested(
            actual_cash_input,
            actual_purchase,
            actual_rehab,
            actual_closing,
            actual_holding,
            actual_loan,
        )

    actual_roi = percent(actual_net_profit, actual_cash_invested)

    actual_cashoncash = None
    if actual_monthly_rent is not None and actual_monthly_payment is not None:
        annual_cash_flow = (actual_monthly_rent - actual_monthly_payment) * Decimal("12")
        actual_cashoncash = percent(annual_cash_flow, actual_cash_invested)

    set_if_value(calculated, "actual_gross_profit", round_decimal(actual_gross_profit))
    set_if_value(calculated, "actual_net_profit", round_decimal(actual_net_profit))
    set_if_value(calculated, "actual_cash_invested", round_decimal(actual_cash_invested))
    set_if_value(calculated, "actual_roi", round_decimal(actual_roi))
    set_if_value(calculated, "actual_cashoncash_return", round_decimal(actual_cashoncash))

    if actual_net_profit is not None:
        calculated["is_profitable"] = actual_net_profit > 0

    # Variances
    set_if_value(
        calculated,
        "profit_variance",
        round_decimal(safe_subtract(actual_net_profit, projected_net_profit)),
    )
    set_if_value(
        calculated,
        "purchase_variance",
        round_decimal(safe_subtract(actual_purchase, proj_purchase)),
    )
    set_if_value(
        calculated,
        "rehab_variance",
        round_decimal(safe_subtract(actual_rehab, proj_rehab)),
    )

    if actual_purchase is not None and proj_arv is not None:
        set_if_value(calculated, "equity_created", round_decimal(proj_arv - actual_purchase - (actual_rehab or Decimal("0"))))

    # Date metrics
    lead_received = parse_date(get_payload_value(payload, "lead_received_date"))
    contract_date = parse_date(get_payload_value(payload, "contract_date"))
    actual_close_date = parse_date(get_payload_value(payload, "actual_close_date"))
    actual_sale_date = parse_date(get_payload_value(payload, "actual_sale_date"))
    actual_refi_date = parse_date(get_payload_value(payload, "actual_refi_date"))
    actual_rehab_completion = parse_date(get_payload_value(payload, "actual_rehab_completion"))
    est_close_date = parse_date(get_payload_value(payload, "est_close_date"))
    est_sale_date = parse_date(get_payload_value(payload, "est_sale_date"))
    est_refi_date = parse_date(get_payload_value(payload, "est_refi_date"))
    est_rehab_completion = parse_date(get_payload_value(payload, "est_rehab_completion"))

    close_start = contract_date or lead_received
    set_if_value(calculated, "days_to_close", days_between(close_start, actual_close_date))
    set_if_value(calculated, "days_in_rehab", days_between(actual_close_date or contract_date, actual_rehab_completion))

    exit_date = actual_sale_date or actual_refi_date or actual_close_date
    start_date = lead_received or contract_date
    set_if_value(calculated, "total_deal_duration", days_between(start_date, exit_date))

    timeline_variance = None
    if actual_sale_date and est_sale_date:
        timeline_variance = days_between(est_sale_date, actual_sale_date)
    elif actual_refi_date and est_refi_date:
        timeline_variance = days_between(est_refi_date, actual_refi_date)
    elif actual_rehab_completion and est_rehab_completion:
        timeline_variance = days_between(est_rehab_completion, actual_rehab_completion)
    elif actual_close_date and est_close_date:
        timeline_variance = days_between(est_close_date, actual_close_date)

    set_if_value(calculated, "timeline_variance_days", timeline_variance)
    if timeline_variance is not None:
        calculated["is_behind_schedule"] = timeline_variance > 0

    return calculated


# -----------------------------------------------------------------------------
# GHL API helpers
# -----------------------------------------------------------------------------


def ghl_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Version": GHL_API_VERSION,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def field_ref_to_custom_field(logical_key: str, value: Any) -> Optional[Dict[str, Any]]:
    if logical_key not in FIELD_MAP:
        return None

    ref = normalize_field_ref(FIELD_MAP[logical_key])
    if not ref:
        return None

    # If it looks like a GHL fieldKey, send it as key. Otherwise assume it is an id.
    if "." in ref or ref.startswith("opportunity_") or ref.startswith("contact_"):
        return {"key": ref, "field_value": value}

    return {"id": ref, "field_value": value}


def build_custom_fields(calculated_fields: Dict[str, Any]) -> List[Dict[str, Any]]:
    custom_fields: List[Dict[str, Any]] = []

    for logical_key, value in calculated_fields.items():
        if is_blank(value):
            continue
        custom_field = field_ref_to_custom_field(logical_key, value)
        if custom_field:
            custom_fields.append(custom_field)

    return custom_fields


async def get_opportunity(opportunity_id: str) -> Dict[str, Any]:
    url = f"{GHL_API_BASE}/opportunities/{opportunity_id}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.get(url, headers=ghl_headers())
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            return data.get("opportunity") if isinstance(data.get("opportunity"), dict) else data
        return {}


def base_update_body_from_opportunity(opportunity: Dict[str, Any]) -> Dict[str, Any]:
    """Preserve required/update-safe standard opportunity fields when available."""
    body: Dict[str, Any] = {}

    for key in (
        "pipelineId",
        "pipelineStageId",
        "name",
        "status",
        "monetaryValue",
        "assignedTo",
    ):
        if key in opportunity and not is_blank(opportunity[key]):
            body[key] = opportunity[key]

    # Some responses use nested pipeline/stage structures or snake_case variants.
    if "pipelineId" not in body:
        pipeline_id = opportunity.get("pipeline_id")
        if not pipeline_id and isinstance(opportunity.get("pipeline"), dict):
            pipeline_id = opportunity["pipeline"].get("id")
        if pipeline_id:
            body["pipelineId"] = pipeline_id

    if "pipelineStageId" not in body:
        stage_id = opportunity.get("pipeline_stage_id") or opportunity.get("stageId")
        if not stage_id and isinstance(opportunity.get("stage"), dict):
            stage_id = opportunity["stage"].get("id")
        if stage_id:
            body["pipelineStageId"] = stage_id

    return body


async def update_opportunity_custom_fields(
    opportunity_id: str,
    custom_fields: List[Dict[str, Any]],
    calculated_fields: Dict[str, Any],
) -> Dict[str, Any]:
    if not GHL_API_KEY:
        return {"ok": False, "skipped": True, "reason": "GHL_API_KEY is not configured"}

    if not custom_fields:
        return {"ok": True, "skipped": True, "reason": "No mapped custom fields to update"}

    if DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "custom_fields_count": len(custom_fields),
            "calculated_fields": calculated_fields,
        }

    url = f"{GHL_API_BASE}/opportunities/{opportunity_id}"

    # Start by trying a focused customFields-only update. If HighLevel requires
    # standard opportunity fields for this account/API version, retry with the
    # current opportunity values included.
    body = {"customFields": custom_fields}

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.put(url, headers=ghl_headers(), json=body)

        if response.status_code in {400, 422}:
            first_error_text = response.text[:1000]
            logger.warning("Custom-fields-only update failed; retrying with opportunity base fields: %s", first_error_text)

            try:
                current_opp = await get_opportunity(opportunity_id)
                retry_body = base_update_body_from_opportunity(current_opp)
                retry_body["customFields"] = custom_fields
                response = await client.put(url, headers=ghl_headers(), json=retry_body)
            except Exception as exc:
                return {
                    "ok": False,
                    "status_code": response.status_code,
                    "error": first_error_text,
                    "retry_error": str(exc),
                }

        if response.status_code >= 400:
            return {
                "ok": False,
                "status_code": response.status_code,
                "error": response.text[:2000],
            }

        try:
            response_json = response.json()
        except Exception:
            response_json = {"raw": response.text[:2000]}

        return {
            "ok": True,
            "status_code": response.status_code,
            "custom_fields_count": len(custom_fields),
            "response": response_json,
        }


async def write_sync_status(opportunity_id: str, status: str, error: str = "") -> None:
    """Best-effort write of sync status fields. Never raises."""
    try:
        fields = {
            "last_sync_status": status,
            "last_sync_error": error[:500] if error else "",
        }
        custom_fields = build_custom_fields(fields)
        if custom_fields and GHL_API_KEY and not DRY_RUN:
            await update_opportunity_custom_fields(opportunity_id, custom_fields, fields)
    except Exception:
        logger.exception("Failed to write sync status")


# -----------------------------------------------------------------------------
# Opportunity ID detection
# -----------------------------------------------------------------------------


def get_nested(obj: Dict[str, Any], dotted_key: str) -> Any:
    current: Any = obj
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def detect_opportunity_id(payload: Dict[str, Any]) -> Optional[str]:
    if OPPORTUNITY_ID_FIELD:
        value = get_nested(payload, OPPORTUNITY_ID_FIELD) or payload.get(OPPORTUNITY_ID_FIELD)
        if not is_blank(value):
            return str(value).strip()

    direct_keys = [
        "opportunity_id",
        "opportunityId",
        "opportunity.id",
        "opportunityIdString",
        "deal_id",
        "dealId",
    ]

    for key in direct_keys:
        value = get_nested(payload, key) if "." in key else payload.get(key)
        if not is_blank(value):
            return str(value).strip()

    opportunity = payload.get("opportunity")
    if isinstance(opportunity, dict):
        for key in ("id", "opportunity_id", "opportunityId"):
            value = opportunity.get(key)
            if not is_blank(value):
                return str(value).strip()

    return None


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@app.get("/")
async def root() -> Dict[str, Any]:
    return health_payload()


@app.get("/health")
async def health() -> Dict[str, Any]:
    return health_payload()


def health_payload() -> Dict[str, Any]:
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "analytics_version": ANALYTICS_VERSION,
        "dry_run": DRY_RUN,
        "field_map_loaded": bool(FIELD_MAP),
        "field_map_source": FIELD_MAP_SOURCE,
        "field_map_count": len(FIELD_MAP),
        "field_map_error": FIELD_MAP_LOAD_ERROR,
        "ghl_api_key_loaded": bool(GHL_API_KEY),
        "ghl_api_version": GHL_API_VERSION,
    }


@app.get("/debug/field-map")
async def debug_field_map() -> Dict[str, Any]:
    return {
        "ok": True,
        "source": FIELD_MAP_SOURCE,
        "count": len(FIELD_MAP),
        "field_map": FIELD_MAP,
    }


@app.post("/webhook/ghl")
async def ghl_webhook(request: Request) -> Dict[str, Any]:
    start = time.perf_counter()

    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            return {"ok": False, "error": "Webhook payload must be a JSON object"}
    except Exception as exc:
        return {"ok": False, "error": f"Invalid JSON payload: {exc}"}

    opportunity_id = detect_opportunity_id(payload)
    calculated_fields = calculate_deal_fields(payload)
    calculated_fields["last_calculation_runtime_ms"] = int((time.perf_counter() - start) * 1000)

    custom_fields = build_custom_fields(calculated_fields)

    if not opportunity_id:
        return {
            "ok": False,
            "reason": "No opportunity ID found in webhook payload. Add opportunity_id to the GHL webhook/custom data, or set OPPORTUNITY_ID_FIELD.",
            "field_map_loaded": bool(FIELD_MAP),
            "ghl_api_key_loaded": bool(GHL_API_KEY),
            "calculated_fields": calculated_fields,
            "custom_fields_count": len(custom_fields),
        }

    result = await update_opportunity_custom_fields(opportunity_id, custom_fields, calculated_fields)

    if result.get("ok"):
        await write_sync_status(opportunity_id, "Success", "")
    else:
        await write_sync_status(opportunity_id, "Error", str(result.get("error") or result.get("reason") or "Unknown error"))

    return {
        "ok": bool(result.get("ok")),
        "service": SERVICE_NAME,
        "detected_opportunity_id": opportunity_id,
        "field_map_loaded": bool(FIELD_MAP),
        "field_map_source": FIELD_MAP_SOURCE,
        "ghl_api_key_loaded": bool(GHL_API_KEY),
        "dry_run": DRY_RUN,
        "calculated_field_keys": sorted(calculated_fields.keys()),
        "custom_fields_count": len(custom_fields),
        "ghl_update": result,
        "payload": payload,
    }
