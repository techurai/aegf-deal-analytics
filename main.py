from fastapi import FastAPI, Request

app = FastAPI()

@app.get("/")
def root():
    return {"status": "running"}

@app.post("/webhook/ghl")
async def ghl_webhook(request: Request):
    data = await request.json()
    print("Webhook received:", data)
    return {"received": True}