import logging
import os
import time

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from openai import APIConnectionError, APIStatusError, AsyncOpenAI
from pydantic import BaseModel

# --- Configuration comes from the environment (a ConfigMap in Kubernetes) ---
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "http://vllm.llm.svc.cluster.local:8000/v1")
LLM_MODEL = os.getenv("LLM_MODEL", "qwen2.5-1.5b")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "256"))
SYSTEM_PROMPT = os.getenv("SYSTEM_PROMPT", "You are a concise, helpful assistant.")
POD = os.getenv("HOSTNAME", "local")          # Kubernetes sets HOSTNAME to the pod name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("chat-ui")

# vLLM ignores the API key, but the SDK requires a non-empty string
client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="not-needed", timeout=120.0)
app = FastAPI(title="chat-ui")


class Ask(BaseModel):
    question: str


@app.middleware("http")
async def served_by(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Served-By"] = POD      # shows load balancing across replicas
    return response


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}                    # liveness/readiness: this process can serve


@app.get("/api/backend")
async def backend():
    """Is vLLM reachable? Used for troubleshooting, NOT as a readiness probe."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as h:
            r = await h.get(LLM_BASE_URL.removesuffix("/v1") + "/health")
        ok = r.status_code == 200
    except httpx.HTTPError as e:
        log.warning("backend check failed: %s", e)
        ok = False
    return JSONResponse({"llm_base_url": LLM_BASE_URL, "reachable": ok},
                        status_code=200 if ok else 503)


@app.post("/api/ask")
async def ask(body: Ask):
    started = time.perf_counter()
    try:
        stream = await client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": body.question}],
            max_tokens=MAX_TOKENS,
            stream=True,
        )
    except APIConnectionError as e:
        log.error("LLM unreachable at %s: %s", LLM_BASE_URL, e)
        return JSONResponse({"error": f"LLM unreachable at {LLM_BASE_URL}"}, status_code=502)
    except APIStatusError as e:
        log.error("LLM returned HTTP %s: %s", e.status_code, e.message)
        return JSONResponse({"error": f"LLM returned HTTP {e.status_code}"}, status_code=502)

    async def tokens():
        first = None
        async for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                if first is None:
                    first = time.perf_counter() - started
                yield delta
        log.info("answered ttft=%.2fs total=%.2fs", first or -1.0, time.perf_counter() - started)

    return StreamingResponse(tokens(), media_type="text/plain; charset=utf-8")


@app.get("/", response_class=HTMLResponse)
async def index():
    return PAGE


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K8s LLM Lab</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:760px;margin:40px auto;padding:0 16px}
 textarea{width:100%;height:90px;font:inherit}
 #out{white-space:pre-wrap;border:1px solid #ccc;border-radius:8px;padding:12px;min-height:80px;margin-top:12px}
 small{color:#666}
</style></head><body>
<h1>Ask the in-cluster LLM</h1>
<textarea id="q" placeholder="Ask a question..."></textarea>
<button id="go">Ask</button> <small id="meta"></small>
<div id="out"></div>
<script>
const go = document.getElementById('go'), out = document.getElementById('out'),
      meta = document.getElementById('meta');
go.onclick = async () => {
  out.textContent = ''; meta.textContent = 'thinking...'; go.disabled = true;
  const t0 = performance.now(); let first = null;
  try {
    const r = await fetch('/api/ask', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: document.getElementById('q').value})});
    const pod = r.headers.get('X-Served-By');
    if (!r.ok) { out.textContent = 'Error ' + r.status + ': ' + await r.text(); meta.textContent = ''; return; }
    const reader = r.body.getReader(), dec = new TextDecoder();
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      if (first === null) first = performance.now() - t0;
      out.textContent += dec.decode(value, {stream: true});
    }
    meta.textContent = 'first token ' + (first / 1000).toFixed(2) + 's | total ' +
      ((performance.now() - t0) / 1000).toFixed(2) + 's | pod ' + pod;
  } finally { go.disabled = false; }
};
</script></body></html>"""
