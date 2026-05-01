import os
import requests
from fastapi import FastAPI, Request

app = FastAPI()

GHL_API_KEY = os.getenv("GHL_API_KEY")

@app.post("/webhook/ghl")
async def ghl_webhook(request: Request):
    payload = await request.json()

    opportunity_id = (
        payload.get("customData", {}).get("opportunityId")
        or payload.get("id")
    )

    print("Webhook received")
    print("Opportunity ID:", opportunity_id)

    # 🔽 CALL GHL API
    url = f"https://services.leadconnectorhq.com/opportunities/{opportunity_id}"

    headers = {
        "Authorization": f"Bearer {GHL_API_KEY}",
        "Content-Type": "application/json",
        "Version": "2021-07-28"
    }

    response = requests.get(url, headers=headers)

    print("GHL API Status:", response.status_code)
    print("GHL API Response:", response.text)

    return {"ok": True}