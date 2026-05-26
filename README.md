# AEGF Deal Analytics Engine

FastAPI webhook service for Railway. Receives a GHL workflow webhook, fetches the Opportunity, calculates deal metrics, and writes computed custom fields back to GHL.

## Railway Variables

Required:

- `GHL_API_KEY` — LeadConnector / GHL private integration access token
- `FIELD_MAP` — JSON object mapping logical names to GHL opportunity custom field IDs

Recommended:

- `WEBHOOK_SECRET` — simple shared secret sent from the GHL workflow webhook as `x-webhook-secret`
- `GHL_API_VERSION` — defaults to `2023-02-21`

## FIELD_MAP Example

```json
{
  "projected_purchase_price": "FIELD_ID_HERE",
  "projected_rehab_budget": "FIELD_ID_HERE",
  "projected_sale_price": "FIELD_ID_HERE",
  "actual_purchase_price": "FIELD_ID_HERE",
  "actual_rehab_cost": "FIELD_ID_HERE",
  "actual_sale_price": "FIELD_ID_HERE",
  "projected_gross_profit": "FIELD_ID_HERE",
  "actual_gross_profit": "FIELD_ID_HERE",
  "projected_roi_percent": "FIELD_ID_HERE",
  "actual_roi_percent": "FIELD_ID_HERE",
  "last_calculated_at": "FIELD_ID_HERE"
}
```

## Endpoints

- `GET /health` — confirms Railway service is up and environment variables are loaded
- `POST /debug/payload` — temporary endpoint to inspect the GHL webhook payload
- `POST /webhook/ghl` — production webhook endpoint

## Recommended GHL Workflow Setup

1. Trigger: Opportunity Changed.
2. Filter: only trigger when one of the input fields changes, not when computed fields change.
3. Action: Custom Webhook.
4. Method: POST.
5. URL: `https://YOUR-RAILWAY-DOMAIN.up.railway.app/webhook/ghl`.
6. Header: `x-webhook-secret: YOUR_SECRET` if using `WEBHOOK_SECRET`.
7. Body: include at least the Opportunity ID. Example:

```json
{
  "opportunityId": "{{ opportunity.id }}"
}
```

## Current Formula

Projected Gross Profit = Projected Sale Price - Projected Purchase Price - Projected Rehab Budget

Actual Gross Profit = Actual Sale Price - Actual Purchase Price - Actual Rehab Cost

Projected ROI % = Projected Gross Profit / (Projected Purchase Price + Projected Rehab Budget) * 100

Actual ROI % = Actual Gross Profit / (Actual Purchase Price + Actual Rehab Cost) * 100
```
