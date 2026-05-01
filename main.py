from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def root():
    return {"status": "running"}

@app.post("/webhook/ghl")
async def ghl_webhook(request: Request):
    payload = await request.json()

    opportunity_id = (
        payload.get("customData", {}).get("opportunityId")
        or payload.get("id")
    )

    pipeline_id = (
        payload.get("customData", {}).get("pipelineId")
        or payload.get("pipeline_id")
    )

    opportunity_name = (
        payload.get("customData", {}).get("name")
        or payload.get("opportunity_name")
        or payload.get("full_name")
    )

    print("Webhook received")
    print("Opportunity ID:", opportunity_id)
    print("Pipeline ID:", pipeline_id)
    print("Opportunity Name:", opportunity_name)

    return {
        "received": True,
        "opportunityId": opportunity_id,
        "pipelineId": pipeline_id,
        "name": opportunity_name
    }