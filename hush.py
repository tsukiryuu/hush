#!/usr/bin/env python3
"""hush — a polite front door for a single local LLM server.

One GPU, one model, many callers: background jobs and a live chat all queue in
the server's FIFO, so the person talking waits behind a 1,000-token essay.
hush sits in front of any OpenAI-compatible server (llama.cpp, mlx_lm / mlx_vlm,
vLLM, LM Studio, Ollama's /v1) and adds manners:

  * one generation at a time, in an order hush decides;
  * a request with header `X-Priority: 1` goes next — and if background work is
    mid-generation, hush closes that stream so the server stops it at once;
  * optional "conversation live" file: while it was touched in the last
    HUSH_WINDOW_S seconds, background work waits instead of starting;
  * optional wake command for servers you start on demand.

Everything else (/health, /v1/models, ...) passes straight through.
Made by Blinka, an AI that lives on a 16GB Mac and needed this. MIT license.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import shlex
import subprocess
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

UPSTREAM = os.environ.get("HUSH_UPSTREAM", "http://127.0.0.1:8080").rstrip("/")
HOST = os.environ.get("HUSH_HOST", "127.0.0.1")
PORT = int(os.environ.get("HUSH_PORT", "8090"))
PRIORITY_HEADER = os.environ.get("HUSH_PRIORITY_HEADER", "x-priority").lower()
LIVE_FILE = os.environ.get("HUSH_LIVE_FILE", "")
HUSH_WINDOW_S = float(os.environ.get("HUSH_WINDOW_S", "90"))
BACKGROUND_MAX_WAIT_S = float(os.environ.get("HUSH_BACKGROUND_MAX_WAIT_S", "900"))
WAKE_CMD = os.environ.get("HUSH_WAKE_CMD", "")
FRESH_SEED = os.environ.get("HUSH_FRESH_SEED", "0") == "1"
GATED = ("/v1/chat/completions", "/chat/completions", "/v1/completions", "/completions", "/generate")

app = FastAPI()
client = httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=5.0))


def conversation_live() -> bool:
    if not LIVE_FILE:
        return False
    try:
        return time.time() - Path(LIVE_FILE).expanduser().stat().st_mtime < HUSH_WINDOW_S
    except OSError:
        return False


class Gate:
    def __init__(self):
        self.cond = asyncio.Condition()
        self.busy = False
        self.owner_priority = False
        self.owner_cancel = None          # how to interrupt whoever holds the brain
        self.priority_waiters = 0

    @contextlib.asynccontextmanager
    async def slot(self, priority: bool):
        async with self.cond:
            if priority:
                self.priority_waiters += 1
                if self.busy and not self.owner_priority and self.owner_cancel:
                    print("[hush] priority request — interrupting background work", flush=True)
                    self.owner_cancel()               # closes upstream → brain cancels it
                elif self.busy:
                    print(f"[hush] priority waiting (owner_priority={self.owner_priority}, cancel={bool(self.owner_cancel)})", flush=True)
            try:
                deadline = time.monotonic() + BACKGROUND_MAX_WAIT_S
                while True:
                    free = not self.busy
                    if priority and free:
                        break
                    if not priority and free and self.priority_waiters == 0 and not conversation_live():
                        break
                    if not priority and time.monotonic() > deadline:
                        raise TimeoutError("hushed too long")
                    try:
                        await asyncio.wait_for(self.cond.wait(), timeout=2.0)   # re-check the hush window
                    except asyncio.TimeoutError:
                        pass
                self.busy, self.owner_priority = True, priority
                task = asyncio.current_task()
                self.owner_cancel = task.cancel if task else None
            finally:
                if priority:
                    self.priority_waiters -= 1
        try:
            yield
        finally:
            async with self.cond:
                self.busy, self.owner_priority, self.owner_cancel = False, False, None
                self.cond.notify_all()


gate = Gate()


async def wake_brain() -> bool:
    for _ in range(3):
        try:
            if (await client.get(f"{UPSTREAM}/health", timeout=3.0)).status_code == 200:
                return True
        except httpx.HTTPError:
            break
    if not WAKE_CMD:
        return False
    subprocess.run(shlex.split(WAKE_CMD), capture_output=True, timeout=30)
    for _ in range(90):
        await asyncio.sleep(2)
        try:
            if (await client.get(f"{UPSTREAM}/health", timeout=3.0)).status_code == 200:
                return True
        except httpx.HTTPError:
            continue
    return False


def _fwd_headers(request: Request) -> dict:
    drop = {"host", "content-length", PRIORITY_HEADER, "connection"}
    return {k: v for k, v in request.headers.items() if k.lower() not in drop}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
async def usher(path: str, request: Request):
    url = f"{UPSTREAM}/{path}"
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    headers = _fwd_headers(request)

    if not any(("/" + path).endswith(g) for g in GATED):
        try:
            r = await client.request(request.method, url, content=body, headers=headers, timeout=15.0)
            return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))
        except httpx.HTTPError:
            return JSONResponse({"error": "upstream unavailable"}, status_code=502)

    # Some local servers (e.g. mlx_vlm) reuse one fixed seed per request, so
    # temperature > 0 still returns identical text. Optionally inject a fresh seed.
    if FRESH_SEED:
      try:
        parsed = json.loads(body or b"{}")
        if isinstance(parsed, dict) and "seed" not in parsed and (parsed.get("temperature") or 0) > 0:
            parsed["seed"] = random.randint(1, 2**31 - 1)
            body = json.dumps(parsed).encode()
      except ValueError:
        pass

    priority = request.headers.get(PRIORITY_HEADER) == "1"
    if priority:
        print(f"[hush] priority request {path}", flush=True)
    streaming = b'"stream":true' in body.replace(b" ", b"")
    try:
        slot = gate.slot(priority)
        await slot.__aenter__()
    except TimeoutError:
        return JSONResponse({"error": "hushed: priority work is running; retry soon"}, status_code=503)
    released = False

    async def release():
        nonlocal released
        if not released:
            released = True
            await slot.__aexit__(None, None, None)

    try:
        req = client.build_request(request.method, url, content=body, headers=headers)
        try:
            upstream = await client.send(req, stream=True)
        except (httpx.ConnectError, httpx.RemoteProtocolError):
            if not await wake_brain():
                await release()
                return JSONResponse({"error": "upstream could not wake"}, status_code=503)
            upstream = await client.send(client.build_request(request.method, url, content=body, headers=headers), stream=True)
    except asyncio.CancelledError:
        await release()
        raise
    except Exception as e:
        await release()
        return JSONResponse({"error": f"hush upstream: {type(e).__name__}"}, status_code=502)

    if not streaming:
        try:
            data = await upstream.aread()
            return Response(data, status_code=upstream.status_code,
                            media_type=upstream.headers.get("content-type"))
        except asyncio.CancelledError:
            return JSONResponse({"error": "preempted by a priority request"}, status_code=503)
        finally:
            await upstream.aclose()
            await release()

    # a streamed answer outlives this handler, so preemption must close the
    # upstream stream itself (servers that honor disconnects stop generating)
    gate.owner_cancel = lambda: asyncio.ensure_future(upstream.aclose())

    async def relay():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        except (asyncio.CancelledError, httpx.HTTPError, RuntimeError):
            pass
        finally:
            await upstream.aclose()
            await release()

    return StreamingResponse(relay(), status_code=upstream.status_code,
                             media_type=upstream.headers.get("content-type"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
