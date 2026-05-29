import json
import logging
import os
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import httpx
from fastapi import FastAPI, Request


# -----------------------------------------------------------------------------
# App setup
# -----------------------------------------------------------------------------

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("aegf-deal-analytics")

app = FastAPI(title="AEGF Deal Analytics")

SERVICE_NAME = os.getenv("SERVICE_NAME", "aegf-deal-analytics")
ANALYTICS_VERSION = os.getenv("ANALYTICS_VERSION", "2026-05-29-contact-fallback-v4")

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
CONTACT_ID_FIELD = os.getenv("CONTACT_ID_FIELD", "").strip()
GHL_LOCATION_ID = os.getenv("GHL_LOCATION_ID", "").strip()
GHL_PIPELINE_ID = os.getenv("GHL_PIPELINE_ID", "").strip()


# -----------------------------------------------------------------------------
# Field map
# -----------------------------------------------------------------------------
# These are the GHL opportunity custom-field keys visible in Settings > Custom Fields.
# FIELD_MAP in Railway may override or extend this. FIELD_MAP values can be:
#   "opportunity.actual_sale_price"
#   "{{opportunity.actual_sale_price}}"
#   "a-real-ghl-custom-field-id"
#
# This v2 file does NOT send opportunity.* keys directly to the opportunity update.
# It resolves the matching location custom field first, then sends the real GHL ID.

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

FIELD_NAME_OVERRIDES: Dict[str, str] = {
    "actual_cashoncash_return": "Actual Cash-on-Cash Return",
    "actual_roi": "Actual ROI %",
    "projected_cashoncash_return": "Projected Cash-on-Cash Return",
    "projected_roi": "Projected ROI %",
    "last_calculation_runtime_ms": "Last Calculation Runtime (ms)",
    "timeline_variance_days": "Timeline Variance Days",
    "proj_arv": "Proj ARV",
    "dscr": "DSCR",
    "mao": "MAO",
}

FIELD_MAP_LOAD_ERROR: Optional[str] = None
FIELD_MAP_SOURCE = "default"


def normalize_field_ref(value: Any) -> str:
    """Normalize GHL merge tags into plain field refs."""
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


def humanize_logical_key(logical_key: str) -> str:
    if logical_key in FIELD_NAME_OVERRIDES:
        return FIELD_NAME_OVERRIDES[logical_key]
    return logical_key.replace("_", " ").title()


def norm_lookup_key(value: Any) -> str:
    """Normalize keys enough to match payload keys, merge tags, field names, and GHL field keys."""
    text = normalize_field_ref(value).strip().lower()
    text = text.replace("{{", "").replace("}}", "")
    text = text.replace("%", " percent ")
    text = text.replace("&", " and ")
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
        has_custom_field_value = any(k in obj for k in ("field_value", "fieldValue", "value", "values"))

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
        humanize_logical_key(logical_key),
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


def looks_like_custom_field_id(ref: str) -> bool:
    """GHL field IDs are opaque strings; field keys usually contain a dot or readable words."""
    if not ref:
        return False
    if ref.startswith("opportunity.") or ref.startswith("contact."):
        return False
    if "{{" in ref or "}}" in ref:
        return False
    if "." in ref:
        return False
    # Existing GHL ids in your response looked like 20-char mixed-case ids.
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{12,80}", ref))


def definition_identities(field_def: Dict[str, Any]) -> List[str]:
    identities: List[str] = []

    for key in (
        "id",
        "name",
        "key",
        "fieldKey",
        "field_key",
        "placeholder",
        "label",
        "displayName",
        "display_name",
        "objectKey",
        "object_key",
        "parentId",
    ):
        value = field_def.get(key)
        if not is_blank(value):
            identities.append(str(value))

    # Some custom-field API responses nest additional searchable data.
    for nested_key in ("field", "customField", "schema"):
        nested = field_def.get(nested_key)
        if isinstance(nested, dict):
            identities.extend(definition_identities(nested))

    return identities


def build_definition_lookup(custom_field_defs: List[Dict[str, Any]]) -> Dict[str, str]:
    """Build a normalized identity -> GHL custom field ID lookup."""
    lookup: Dict[str, str] = {}

    for field_def in custom_field_defs:
        if not isinstance(field_def, dict):
            continue

        field_id = field_def.get("id") or field_def.get("fieldId") or field_def.get("customFieldId")
        if is_blank(field_id):
            continue

        field_id = str(field_id).strip()
        for identity in definition_identities(field_def):
            normalized = norm_lookup_key(identity)
            if normalized:
                lookup[normalized] = field_id

            # Helpful extra normalization: opportunity.foo should also match foo.
            cleaned = normalize_field_ref(identity)
            if cleaned.startswith("opportunity."):
                lookup[norm_lookup_key(cleaned.replace("opportunity.", "", 1))] = field_id

    return lookup


def resolve_field_ids(custom_field_defs: List[Dict[str, Any]]) -> Tuple[Dict[str, str], List[str]]:
    """Resolve logical analytics keys to actual GHL custom field IDs."""
    definition_lookup = build_definition_lookup(custom_field_defs)
    resolved: Dict[str, str] = {}
    missing: List[str] = []

    for logical_key, raw_ref in FIELD_MAP.items():
        ref = normalize_field_ref(raw_ref)

        # If FIELD_MAP already contains a real field ID, keep it.
        if looks_like_custom_field_id(ref):
            resolved[logical_key] = ref
            continue

        matched_id = None
        for candidate in field_candidates(logical_key):
            normalized = norm_lookup_key(candidate)
            if normalized in definition_lookup:
                matched_id = definition_lookup[normalized]
                break

        if matched_id:
            resolved[logical_key] = matched_id
        else:
            missing.append(logical_key)

    return resolved, missing


def build_custom_fields_from_ids(
    calculated_fields: Dict[str, Any],
    resolved_field_ids: Dict[str, str],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    custom_fields: List[Dict[str, Any]] = []
    skipped_unresolved: List[str] = []

    for logical_key, value in calculated_fields.items():
        if is_blank(value):
            continue

        field_id = resolved_field_ids.get(logical_key)
        if not field_id:
            skipped_unresolved.append(logical_key)
            continue

        custom_fields.append({"id": field_id, "field_value": value})

    return custom_fields, skipped_unresolved


def extract_custom_fields_from_response(data: Any) -> List[Dict[str, Any]]:
    if not isinstance(data, dict):
        return []

    for key in ("customFields", "custom_fields", "fields"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]

    # Some responses wrap custom fields under location or customFields key.
    for wrapper_key in ("location", "customField", "customFieldFolder", "data"):
        wrapped = data.get(wrapper_key)
        if isinstance(wrapped, dict):
            found = extract_custom_fields_from_response(wrapped)
            if found:
                return found

    return []


def dedupe_custom_field_defs(fields: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate custom-field definitions while preserving order."""
    seen: set = set()
    deduped: List[Dict[str, Any]] = []

    for field in fields:
        if not isinstance(field, dict):
            continue

        field_id = field.get("id") or field.get("fieldId") or field.get("customFieldId")
        identity = str(field_id or field.get("key") or field.get("fieldKey") or field.get("name") or repr(field))
        if identity in seen:
            continue

        seen.add(identity)
        deduped.append(field)

    return deduped


def extract_fields_from_any_response(data: Any) -> List[Dict[str, Any]]:
    fields = extract_custom_fields_from_response(data)

    if not fields and isinstance(data, dict):
        # Last-resort shape support: top-level list-like values.
        for value in data.values():
            if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
                fields = value
                break

    return [item for item in fields if isinstance(item, dict)]


async def get_location_custom_fields_with_debug(location_id: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Fetch GHL custom fields with several variants.

    The plain location custom-fields endpoint can return only contact/default fields in
    some HighLevel accounts. Opportunity fields usually require model=opportunity.
    We try both current and legacy Version headers because HighLevel's custom-field
    docs and behavior vary across custom-field generations.
    """
    if not GHL_API_KEY:
        raise RuntimeError("GHL_API_KEY is not configured")

    attempts: List[Dict[str, Any]] = []
    collected: List[Dict[str, Any]] = []

    request_variants = [
        (GHL_API_VERSION, {"model": "opportunity"}),
        (GHL_API_VERSION, {"model": "all"}),
        (GHL_API_VERSION, None),
        ("2021-07-28", {"model": "opportunity"}),
        ("2021-07-28", {"model": "all"}),
        ("2021-07-28", None),
    ]

    url = f"{GHL_API_BASE}/locations/{location_id}/customFields"

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        for version, params in request_variants:
            headers = ghl_headers()
            headers["Version"] = version

            attempt_info: Dict[str, Any] = {
                "version": version,
                "params": params or {},
                "status_code": None,
                "fields_count": 0,
                "error": None,
            }

            try:
                response = await client.get(url, headers=headers, params=params)
                attempt_info["status_code"] = response.status_code

                if response.status_code >= 400:
                    attempt_info["error"] = response.text[:500]
                    attempts.append(attempt_info)
                    continue

                data = response.json()
                fields = extract_fields_from_any_response(data)
                attempt_info["fields_count"] = len(fields)
                collected.extend(fields)
            except Exception as exc:
                attempt_info["error"] = str(exc)

            attempts.append(attempt_info)

    return dedupe_custom_field_defs(collected), attempts


async def get_location_custom_fields(location_id: str) -> List[Dict[str, Any]]:
    fields, _attempts = await get_location_custom_fields_with_debug(location_id)
    return fields


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


def get_location_id_from_opportunity(opportunity: Dict[str, Any]) -> Optional[str]:
    value = opportunity.get("locationId") or opportunity.get("location_id")
    if not is_blank(value):
        return str(value).strip()

    location = opportunity.get("location")
    if isinstance(location, dict):
        value = location.get("id") or location.get("locationId")
        if not is_blank(value):
            return str(value).strip()

    return None


async def update_opportunity_custom_fields(
    opportunity_id: str,
    calculated_fields: Dict[str, Any],
) -> Dict[str, Any]:
    if not GHL_API_KEY:
        return {"ok": False, "skipped": True, "reason": "GHL_API_KEY is not configured"}

    if DRY_RUN:
        return {
            "ok": True,
            "dry_run": True,
            "calculated_fields": calculated_fields,
        }

    try:
        current_opp = await get_opportunity(opportunity_id)
    except Exception as exc:
        return {"ok": False, "stage": "get_opportunity", "error": str(exc)}

    location_id = get_location_id_from_opportunity(current_opp)
    if not location_id:
        return {
            "ok": False,
            "stage": "get_location_id",
            "reason": "Opportunity response did not include locationId",
            "opportunity_keys": sorted(current_opp.keys()),
        }

    try:
        custom_field_defs = await get_location_custom_fields(location_id)
    except Exception as exc:
        return {"ok": False, "stage": "get_location_custom_fields", "location_id": location_id, "error": str(exc)}

    resolved_field_ids, missing_mapped_fields = resolve_field_ids(custom_field_defs)
    custom_fields, skipped_unresolved_calculated = build_custom_fields_from_ids(calculated_fields, resolved_field_ids)

    if not custom_fields:
        return {
            "ok": False,
            "stage": "build_custom_fields",
            "reason": "No calculated fields resolved to GHL custom field IDs",
            "location_id": location_id,
            "custom_field_definitions_count": len(custom_field_defs),
            "resolved_field_ids_count": len(resolved_field_ids),
            "missing_mapped_fields": missing_mapped_fields,
            "calculated_field_keys": sorted(calculated_fields.keys()),
            "skipped_unresolved_calculated": skipped_unresolved_calculated,
        }

    url = f"{GHL_API_BASE}/opportunities/{opportunity_id}"
    body = base_update_body_from_opportunity(current_opp)
    body["customFields"] = custom_fields

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        response = await client.put(url, headers=ghl_headers(), json=body)

        if response.status_code >= 400:
            return {
                "ok": False,
                "stage": "put_opportunity",
                "status_code": response.status_code,
                "error": response.text[:2000],
                "location_id": location_id,
                "custom_fields_count": len(custom_fields),
                "resolved_field_ids_count": len(resolved_field_ids),
                "resolved_field_ids_used": sorted({k for k in calculated_fields if k in resolved_field_ids}),
                "skipped_unresolved_calculated": skipped_unresolved_calculated,
            }

        try:
            response_json = response.json()
        except Exception:
            response_json = {"raw": response.text[:2000]}

        return {
            "ok": True,
            "status_code": response.status_code,
            "location_id": location_id,
            "custom_field_definitions_count": len(custom_field_defs),
            "resolved_field_ids_count": len(resolved_field_ids),
            "custom_fields_count": len(custom_fields),
            "resolved_field_ids_used": sorted({k for k in calculated_fields if k in resolved_field_ids}),
            "skipped_unresolved_calculated": skipped_unresolved_calculated,
            "missing_mapped_fields_count": len(missing_mapped_fields),
            "missing_mapped_fields_sample": missing_mapped_fields[:15],
            "response": response_json,
        }


async def write_sync_status(opportunity_id: str, status: str, error: str = "") -> None:
    """Best-effort write of sync status fields. Never raises."""
    try:
        fields = {
            "last_sync_status": status,
            "last_sync_error": error[:500] if error else "",
        }
        if GHL_API_KEY and not DRY_RUN:
            await update_opportunity_custom_fields(opportunity_id, fields)
    except Exception:
        logger.exception("Failed to write sync status")



# -----------------------------------------------------------------------------
# Opportunity/contact fallback and payload enrichment
# -----------------------------------------------------------------------------


def summarize_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return a safe, compact summary of webhook payload shape for debugging."""
    summary: Dict[str, Any] = {
        "top_level_keys": sorted(payload.keys()),
    }
    for key in ("customData", "custom_data", "contact", "opportunity"):
        value = payload.get(key)
        if isinstance(value, dict):
            summary[f"{key}_keys"] = sorted(value.keys())
    return summary


def detect_contact_id(payload: Dict[str, Any]) -> Optional[str]:
    if CONTACT_ID_FIELD:
        value = get_nested(payload, CONTACT_ID_FIELD) or payload.get(CONTACT_ID_FIELD)
        if not is_blank(value):
            return str(value).strip()

    direct_keys = [
        "contact_id",
        "contactId",
        "contact.id",
        "Contact.id",
        "contactID",
    ]

    for key in direct_keys:
        value = get_nested(payload, key) if "." in key else payload.get(key)
        if not is_blank(value):
            return str(value).strip()

    contact = payload.get("contact") or payload.get("Contact")
    if isinstance(contact, dict):
        for key in ("id", "contact_id", "contactId"):
            value = contact.get(key)
            if not is_blank(value):
                return str(value).strip()

    return None


def detect_location_id(payload: Dict[str, Any]) -> Optional[str]:
    direct_keys = [
        "location_id",
        "locationId",
        "location.id",
        "Location.id",
        "sub_account_id",
        "subAccountId",
    ]

    for key in direct_keys:
        value = get_nested(payload, key) if "." in key else payload.get(key)
        if not is_blank(value):
            return str(value).strip()

    location = payload.get("location") or payload.get("Location")
    if isinstance(location, dict):
        for key in ("id", "location_id", "locationId"):
            value = location.get(key)
            if not is_blank(value):
                return str(value).strip()

    return GHL_LOCATION_ID or None


def extract_opportunities_from_response(data: Any) -> List[Dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []

    for key in ("opportunities", "opportunity", "data", "items", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = extract_opportunities_from_response(value)
            if nested:
                return nested

    return []


def opportunity_sort_key(opp: Dict[str, Any]) -> str:
    # Lexicographic ISO-ish strings work well enough for choosing a latest record.
    for key in ("updatedAt", "updated_at", "dateUpdated", "createdAt", "created_at", "dateCreated"):
        value = opp.get(key)
        if not is_blank(value):
            return str(value)
    return ""


async def find_opportunity_by_contact(contact_id: str, location_id: str) -> Dict[str, Any]:
    """Fallback lookup when GHL workflow only sends contact context."""
    if not GHL_API_KEY:
        return {"ok": False, "reason": "GHL_API_KEY is not configured"}

    url = f"{GHL_API_BASE}/opportunities/search"
    attempts: List[Dict[str, Any]] = []

    base_params_variants: List[Dict[str, Any]] = [
        {"location_id": location_id, "contact_id": contact_id, "limit": 20},
        {"location_id": location_id, "contactId": contact_id, "limit": 20},
        {"locationId": location_id, "contactId": contact_id, "limit": 20},
        {"locationId": location_id, "contact_id": contact_id, "limit": 20},
    ]

    if GHL_PIPELINE_ID:
        with_pipeline: List[Dict[str, Any]] = []
        for params in base_params_variants:
            p1 = dict(params)
            p1["pipeline_id"] = GHL_PIPELINE_ID
            with_pipeline.append(p1)
            p2 = dict(params)
            p2["pipelineId"] = GHL_PIPELINE_ID
            with_pipeline.append(p2)
        base_params_variants = with_pipeline + base_params_variants

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS) as client:
        for version in (GHL_API_VERSION, "2021-07-28"):
            headers = ghl_headers()
            headers["Version"] = version
            for params in base_params_variants:
                attempt: Dict[str, Any] = {
                    "method": "GET",
                    "version": version,
                    "params": params,
                    "status_code": None,
                    "opportunities_count": 0,
                    "error": None,
                }
                try:
                    response = await client.get(url, headers=headers, params=params)
                    attempt["status_code"] = response.status_code
                    if response.status_code >= 400:
                        attempt["error"] = response.text[:500]
                        attempts.append(attempt)
                        continue
                    data = response.json()
                    opportunities = extract_opportunities_from_response(data)
                    attempt["opportunities_count"] = len(opportunities)
                    attempts.append(attempt)

                    if opportunities:
                        # Prefer open opportunities, then newest-looking record.
                        open_opps = [o for o in opportunities if str(o.get("status", "")).lower() == "open"]
                        candidates = open_opps or opportunities
                        candidates = sorted(candidates, key=opportunity_sort_key, reverse=True)
                        selected = candidates[0]
                        selected_id = selected.get("id") or selected.get("opportunityId") or selected.get("_id")
                        if not is_blank(selected_id):
                            return {
                                "ok": True,
                                "opportunity_id": str(selected_id).strip(),
                                "contact_id": contact_id,
                                "location_id": location_id,
                                "matched_count": len(opportunities),
                                "selected_status": selected.get("status"),
                                "attempts": attempts,
                            }
                except Exception as exc:
                    attempt["error"] = str(exc)
                    attempts.append(attempt)

    return {
        "ok": False,
        "reason": "No opportunity found for contact_id/location_id",
        "contact_id": contact_id,
        "location_id": location_id,
        "attempts": attempts,
    }


def extract_opportunity_custom_field_values(
    opportunity: Dict[str, Any],
    resolved_field_ids: Dict[str, str],
) -> Dict[str, Any]:
    """Convert opportunity custom field IDs back into logical analytics keys."""
    values: Dict[str, Any] = {}
    id_to_logical = {str(field_id): logical for logical, field_id in resolved_field_ids.items()}

    fields = []
    for key in ("customFields", "custom_fields"):
        value = opportunity.get(key)
        if isinstance(value, list):
            fields.extend(item for item in value if isinstance(item, dict))

    for field in fields:
        field_id = field.get("id") or field.get("fieldId") or field.get("customFieldId")
        if is_blank(field_id):
            continue
        logical_key = id_to_logical.get(str(field_id))
        if not logical_key:
            continue
        for value_key in ("field_value", "fieldValue", "value", "values"):
            if value_key in field:
                values[logical_key] = field.get(value_key)
                break

    return values


async def enrich_payload_from_opportunity(payload: Dict[str, Any], opportunity_id: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Fetch current opportunity custom fields so GHL webhook can send only the ID/contact."""
    meta: Dict[str, Any] = {"attempted": True, "ok": False}
    enriched = dict(payload)

    try:
        opportunity = await get_opportunity(opportunity_id)
        enriched["opportunity"] = opportunity
        meta["opportunity_keys"] = sorted(opportunity.keys())
    except Exception as exc:
        meta["error"] = f"get_opportunity failed: {exc}"
        return enriched, meta

    location_id = get_location_id_from_opportunity(opportunity) or detect_location_id(payload)
    if not location_id:
        meta["error"] = "Could not determine locationId for payload enrichment"
        return enriched, meta

    try:
        custom_field_defs = await get_location_custom_fields(location_id)
        resolved_field_ids, missing = resolve_field_ids(custom_field_defs)
        values = extract_opportunity_custom_field_values(opportunity, resolved_field_ids)
        enriched.update(values)
        meta.update({
            "ok": True,
            "location_id": location_id,
            "custom_field_definitions_count": len(custom_field_defs),
            "resolved_field_ids_count": len(resolved_field_ids),
            "enriched_values_count": len(values),
            "enriched_keys": sorted(values.keys()),
            "missing_mapped_fields_count": len(missing),
        })
    except Exception as exc:
        meta["error"] = f"custom field enrichment failed: {exc}"

    return enriched, meta

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
        "uses_resolved_custom_field_ids": True,
    }


@app.get("/debug/field-map")
async def debug_field_map() -> Dict[str, Any]:
    return {
        "ok": True,
        "source": FIELD_MAP_SOURCE,
        "count": len(FIELD_MAP),
        "field_map": FIELD_MAP,
    }


@app.get("/debug/custom-fields/{location_id}")
async def debug_custom_fields(location_id: str) -> Dict[str, Any]:
    try:
        custom_field_defs, fetch_attempts = await get_location_custom_fields_with_debug(location_id)
        resolved_field_ids, missing = resolve_field_ids(custom_field_defs)

        sample = []
        for field_def in custom_field_defs[:50]:
            if isinstance(field_def, dict):
                sample.append(
                    {
                        "id": field_def.get("id") or field_def.get("fieldId") or field_def.get("customFieldId"),
                        "name": field_def.get("name"),
                        "key": field_def.get("key") or field_def.get("fieldKey") or field_def.get("field_key"),
                        "model": field_def.get("model") or field_def.get("objectKey") or field_def.get("object_key"),
                        "type": field_def.get("dataType") or field_def.get("type"),
                    }
                )

        return {
            "ok": True,
            "location_id": location_id,
            "analytics_version": ANALYTICS_VERSION,
            "custom_fields_count": len(custom_field_defs),
            "resolved_field_ids_count": len(resolved_field_ids),
            "missing_mapped_fields_count": len(missing),
            "missing_mapped_fields": missing,
            "resolved_field_ids": resolved_field_ids,
            "fetch_attempts": fetch_attempts,
            "custom_fields_sample": sample,
        }
    except Exception as exc:
        return {"ok": False, "location_id": location_id, "error": str(exc)}


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
    detection_source = "payload"
    opportunity_lookup: Dict[str, Any] = {"attempted": False}

    if not opportunity_id:
        contact_id = detect_contact_id(payload)
        location_id = detect_location_id(payload)
        if contact_id and location_id:
            opportunity_lookup = await find_opportunity_by_contact(contact_id, location_id)
            if opportunity_lookup.get("ok") and opportunity_lookup.get("opportunity_id"):
                opportunity_id = str(opportunity_lookup["opportunity_id"])
                detection_source = "contact_search"
        else:
            opportunity_lookup = {
                "attempted": False,
                "reason": "Missing contact_id or location_id for fallback opportunity lookup",
                "contact_id_found": bool(contact_id),
                "location_id_found": bool(location_id),
            }

    if not opportunity_id:
        calculated_fields = calculate_deal_fields(payload)
        calculated_fields["last_calculation_runtime_ms"] = int((time.perf_counter() - start) * 1000)
        return {
            "ok": False,
            "reason": "No opportunity ID found in webhook payload and contact fallback lookup did not resolve one.",
            "field_map_loaded": bool(FIELD_MAP),
            "ghl_api_key_loaded": bool(GHL_API_KEY),
            "payload_summary": summarize_payload(payload),
            "opportunity_lookup": opportunity_lookup,
            "calculated_fields": calculated_fields,
        }

    enriched_payload, enrichment = await enrich_payload_from_opportunity(payload, opportunity_id)
    calculated_fields = calculate_deal_fields(enriched_payload)
    calculated_fields["last_calculation_runtime_ms"] = int((time.perf_counter() - start) * 1000)

    result = await update_opportunity_custom_fields(opportunity_id, calculated_fields)

    if result.get("ok"):
        await write_sync_status(opportunity_id, "Success", "")
    else:
        await write_sync_status(opportunity_id, "Error", str(result.get("error") or result.get("reason") or "Unknown error"))

    return {
        "ok": bool(result.get("ok")),
        "service": SERVICE_NAME,
        "detected_opportunity_id": opportunity_id,
        "opportunity_detection_source": detection_source,
        "opportunity_lookup": opportunity_lookup,
        "payload_enrichment": enrichment,
        "analytics_version": ANALYTICS_VERSION,
        "field_map_loaded": bool(FIELD_MAP),
        "field_map_source": FIELD_MAP_SOURCE,
        "ghl_api_key_loaded": bool(GHL_API_KEY),
        "dry_run": DRY_RUN,
        "uses_resolved_custom_field_ids": True,
        "calculated_field_keys": sorted(calculated_fields.keys()),
        "ghl_update": result,
        "payload": payload,
    }
