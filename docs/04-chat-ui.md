# 04 — Build and deploy the chat UI

A ~140-line FastAPI service serves one HTML page, forwards the question to vLLM with the OpenAI SDK, and streams tokens back as they are generated. About 20 minutes.

## 4.1 The app

[`app/main.py`](../app/main.py), step by step:

1. **Config from env vars.** `LLM_BASE_URL`, `LLM_MODEL`, `MAX_TOKENS`, and `SYSTEM_PROMPT` come from the environment. In Kubernetes the `chat-ui-config` ConfigMap injects them, so the same image runs against this cluster, a GPU cluster, or a hosted API.
2. **`AsyncOpenAI(base_url=...)`.** vLLM speaks the OpenAI API, so the official SDK works unchanged. `api_key` is a placeholder because vLLM runs without `--api-key`.
3. **`DropProbeLogs`** filters kubelet probe hits (`/healthz`) out of uvicorn's access log while keeping real traffic. It was added in version 0.2.0; [chapter 07](07-scaling-rollouts-hpa.md) rolls between versions with and without it.
4. **The middleware** stamps every response with `X-Served-By: <pod name>`, which makes Service load balancing visible.
5. **`/healthz` vs `/api/backend`.** Probes hit `/healthz`, which only says "this process works". Tying readiness to vLLM would mark every UI pod unready whenever vLLM restarts, turning a backend blip into a front-end outage. `/api/backend` exists for humans debugging.
6. **`/api/ask`** calls vLLM with `stream=True`. Connection failures and HTTP errors from vLLM become a clear `502` JSON error instead of a stack trace; the drills in [chapter 08](08-troubleshooting-drills.md) rely on these messages.
7. **`tokens()`** is an async generator: each chunk's `delta.content` goes to the browser the moment vLLM produces it, and the server logs time to first token (TTFT) and total time per request (`answered ttft=... total=...`).
8. **The page's JavaScript** reads the response body as a stream, appends text as it arrives, then shows TTFT, total time, and which pod answered.

[`app/Dockerfile`](../app/Dockerfile) builds on `python:3.12-slim` and runs as the numeric UID 10001, so the pod can enforce `runAsNonRoot`. Dependencies are in [`app/requirements.txt`](../app/requirements.txt).

## 4.2 Build and push

Build natively for arm64 and push to the lab registry. The tag must match the image in `manifests/20-chat-ui.yaml` (`0.2.0`), and should always map to exact, committed source.

```bash
cd ~/k8s-llm-lab
git status --short app/                             # no output = the build matches the committed source
docker build -t localhost:5001/chat-ui:0.2.0 app/
docker push localhost:5001/chat-ui:0.2.0
curl -s localhost:5001/v2/chat-ui/tags/list         # {"name":"chat-ui","tags":["0.2.0"]}
```

For x86 GPU hosts you would build multi-arch instead: `docker buildx build --platform linux/amd64,linux/arm64 ... --push`.

## 4.3 Deploy

[`manifests/20-chat-ui.yaml`](../manifests/20-chat-ui.yaml) holds:

- ConfigMap `chat-ui-config`: `LLM_BASE_URL: http://vllm.llm.svc.cluster.local:8000/v1` (`<service>.<namespace>.svc.cluster.local`), `LLM_MODEL: qwen2.5-1.5b` (must match `--served-model-name`), `MAX_TOKENS: "256"`.
- Deployment `chat-ui`: 2 replicas, `envFrom` the ConfigMap, requests `100m`/`128Mi`, limits `500m`/`256Mi`, readiness and liveness on `/healthz`, `runAsNonRoot`, no privilege escalation.
- Service `chat-ui`: `NodePort` 30080, which `kind-config.yaml` maps to `localhost:8080`.

```bash
kubectl apply -f manifests/20-chat-ui.yaml
kubectl rollout status deploy/chat-ui
kubectl get pods -o wide -l app=chat-ui            # both on llm-lab-worker: the only untainted node
```

No `nodeSelector` is needed: the two taints from chapter 02 already steer the UI pods to the apps node.

## Key takeaways

- Configuration belongs in the environment, not the image.
- Liveness and readiness should reflect the pod's own health, not its dependencies'.
- Turn upstream failures into explicit, logged errors; troubleshooting later depends on them.

Next: [05 — End to end](05-end-to-end.md)
