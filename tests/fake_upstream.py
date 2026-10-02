"""A slow fake OpenAI-compatible server: streams one token every 0.2s."""
import asyncio, json
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
app = FastAPI()
@app.get("/health")
async def health(): return {"ok": True}
@app.post("/v1/chat/completions")
async def chat(req: Request):
    b = await req.json(); n = b.get("max_tokens", 50)
    if not b.get("stream"):
        await asyncio.sleep(0.2 * n); return JSONResponse({"choices": [{"message": {"content": "x" * n}}]})
    async def gen():
        for i in range(n):
            await asyncio.sleep(0.2); yield f"data: {json.dumps({'i': i})}\n\n"
        yield "data: [DONE]\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream")
