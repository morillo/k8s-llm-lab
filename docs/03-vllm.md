# 03 — Deploy vLLM with a small open model

vLLM runs as a single-replica Deployment on the inference node, caches weights on a PersistentVolume, and is reachable in-cluster at `http://vllm.llm.svc.cluster.local:8000/v1`. About 30–45 minutes, mostly the first model download.

Model: `Qwen/Qwen2.5-1.5B-Instruct` — Apache-2.0, not gated (no token needed), ~3 GB in bf16, usable speed on CPU. Image: the official `vllm/vllm-openai-cpu` arm64 build, which has the same OpenAI-compatible API, flags, and Prometheus metrics as GPU vLLM.

## 3.1 Mirror the pinned vLLM CPU image into the local registry

[`lab.env`](../lab.env) pins the tag (`v0.30.0-arm64`).

```bash
cd ~/k8s-llm-lab && source lab.env && echo "$VLLM_TAG"

# Confirm the architecture before downloading
docker buildx imagetools inspect vllm/vllm-openai-cpu:$VLLM_TAG --format '{{json .Image}}' | jq '{os, architecture}'
# expected: {"os": "linux", "architecture": "arm64"}

docker pull --platform linux/arm64 vllm/vllm-openai-cpu:$VLLM_TAG
docker tag  vllm/vllm-openai-cpu:$VLLM_TAG localhost:5001/vllm-openai-cpu:$VLLM_TAG
docker push localhost:5001/vllm-openai-cpu:$VLLM_TAG
curl -s localhost:5001/v2/vllm-openai-cpu/tags/list   # {"name":"vllm-openai-cpu","tags":["v0.30.0-arm64"]}
```

The `-arm64` tags are single-platform manifests, so `imagetools inspect` shows no platform list; the architecture lives in the image config, which the `{{json .Image}}` query reads. `docker pull --platform linux/arm64` double-checks it: Docker refuses an image whose platform does not match.

To move to a newer release later, list the stable arm64 tags, then update `lab.env`:

```bash
curl -s "https://hub.docker.com/v2/repositories/vllm/vllm-openai-cpu/tags?page_size=100&name=arm64" \
  | jq -r '.results[].name' | grep -E '^v[0-9.]+-arm64$' | sort -V | tail -3
```

## 3.2 (Recommended) Smoke-test the image outside Kubernetes

If this works and the pod later fails, the problem is the cluster, not the image. It uses the smaller 0.5B model for speed.

```bash
docker run --rm -p 8000:8000 --cap-add SYS_NICE --shm-size=2g \
  -e VLLM_CPU_KVCACHE_SPACE=2 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  vllm/vllm-openai-cpu:$VLLM_TAG Qwen/Qwen2.5-0.5B-Instruct --dtype=bfloat16 --max-model-len=2048
```

In a second terminal, check that the server is up *and* that the model generates (`/v1/models` alone only proves the first):

```bash
curl -s localhost:8000/v1/models | jq -r '.data[0].id'

curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "messages": [{"role": "user", "content": "In one sentence, what is a Kubernetes taint?"}],
  "max_tokens": 60
}' | jq -r '.choices[0].message.content, .usage'

# Same request, timed: completion_tokens / time_total = rough decode speed
curl -s -o /dev/null -w 'total %{time_total}s\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "messages": [{"role": "user", "content": "In one sentence, what is a Kubernetes taint?"}],
  "max_tokens": 60
}'
```

Then Ctrl-C the container (`--rm` removes it). What to read in the server log:

| Log line | Meaning |
| --- | --- |
| `Triton not installed` / `Model Runner V2 requires Triton; using the V1 model runner` | Expected on CPU: Triton compiles GPU kernels |
| `core ids=[0 … 14]`, `reserved_cpus=[15]` | `auto` binding pins one OpenMP thread per vCPU and keeps one free for the server process. The pod does the same, because each kind node sees all VM vCPUs. |
| `CPU KV cache size: 174,720 tokens` | Qwen2.5-0.5B is 24 layers × 2 KV heads × head dim 64 = 12 KiB per token in bf16; 2 GiB ÷ 12 KiB = 174,762, rounded down to whole KV blocks (formula in 3.6) |
| `Maximum concurrency for 2,048 tokens per request: 85.31x` | 174,720 ÷ 2,048: how many full-length requests fit in the cache at once |
| `saved AOT compiled function to /root/.cache/vllm/...` | The warm-up compile is cached; the pod points `VLLM_CACHE_ROOT` at the PVC to keep it |
| `Default vLLM sampling parameters have been overridden by the model's generation_config.json` | Qwen ships its own temperature, top_p, top_k, and repetition penalty, and the chat UI inherits them |
| `Avg generation throughput: 2.9 tokens/s` | Not real speed: an average over the 10-second logging window. Time a request instead. |

Measured (M4 Max, 16-vCPU / 48 GB Docker VM): about 26 tok/s end to end for the 0.5B model. The prompt counts about 40 tokens for a 10-word question because the chat template adds role markers and a default system prompt. The 0.5B model also gets technical details wrong (a taint goes on a node, not a pod), which is why the cluster runs 1.5B.

## 3.3 The manifest

[`manifests/10-vllm.yaml`](../manifests/10-vllm.yaml) holds a PVC, the Deployment, and the Service. The non-obvious lines:

| Setting | Why |
| --- | --- |
| `strategy: Recreate` | One accelerator and a ReadWriteOnce volume: stop the old pod before starting the new one |
| `enableServiceLinks: false` | **Required.** See below |
| `nodeSelector: {pool: inference}` + toleration for `dedicated=inference:NoSchedule` | Go to the inference pool, and be allowed past its taint |
| `image: localhost:5001/vllm-openai-cpu:__VLLM_TAG__` | Placeholder replaced with `sed` from `lab.env` at apply time |
| `--served-model-name=qwen2.5-1.5b` | The name clients send in `"model"` |
| `--max-model-len=4096`, `--max-num-seqs=8` | Context length and maximum concurrent sequences per batch |
| `HF_HOME=/models/hf`, `VLLM_CACHE_ROOT=/models/vllm` | Weights and compile cache land on the PVC and survive pod restarts |
| `VLLM_CPU_KVCACHE_SPACE=4` | GiB reserved for the KV cache (sizing in 3.6) |
| `HF_TOKEN` from Secret `hf-token`, `optional: true` | Only needed for gated models; the pod starts without it |
| requests `cpu: 6, memory: 12Gi`; limits `memory: 16Gi, example.com/gpu: 1`; no CPU limit | CFS throttling from a CPU limit hurts token latency; the fake GPU comes from chapter 02 |
| `SYS_NICE` capability | Lets vLLM set NUMA memory policy (per the vLLM CPU docs) |
| startup probe: `failureThreshold: 120` × `periodSeconds: 10` | First start = download + load; allow up to 20 minutes |
| readiness/liveness `timeoutSeconds: 5` | The default is 1 s; a CPU-saturated vLLM can answer more slowly |
| `dshm` emptyDir (Memory, 2Gi) at `/dev/shm` | Pods get only 64 MiB of `/dev/shm` by default |

**Why `enableServiceLinks: false` is required.** For every Service in a namespace, Kubernetes injects legacy Docker-link environment variables into each pod that starts afterwards. A Service named `vllm` produces `VLLM_SERVICE_HOST`, `VLLM_PORT=tcp://<cluster-ip>:8000`, and similar. vLLM reads `VLLM_*` variables as its own configuration, so its worker fails with `ValueError: VLLM_PORT 'tcp://...' appears to be a URI` and the pod lands in `CrashLoopBackOff`. The alternative is not to name the Service `vllm`; turning off service links keeps the name (and every DNS reference to it) and removes the whole class of collision. See [lessons learned](11-lessons-learned.md#1-the-vllm_port-collision).

## 3.4 Apply and watch it come up

Expected sequence: PVC `Pending` (WaitForFirstConsumer) → pod scheduled → PVC `Bound` → `ContainerCreating` (pull from the local registry) → `Running 0/1` (download + load; startup probe failures are normal) → `Running 1/1`.

```bash
cd ~/k8s-llm-lab && source lab.env

# 1) Validate against the API server (schema + admission) without creating anything
sed "s|__VLLM_TAG__|${VLLM_TAG:?run 'source lab.env' first}|" manifests/10-vllm.yaml \
  | kubectl apply --dry-run=server -f -

# 2) Apply for real
sed "s|__VLLM_TAG__|${VLLM_TAG:?run 'source lab.env' first}|" manifests/10-vllm.yaml \
  | kubectl apply -f -

kubectl get pvc,pods -o wide                        # one snapshot of both (--watch accepts only one resource type)
kubectl events --watch                              # PVC provisioning + scheduling/pull/probe events; Ctrl-C
kubectl get pods -o wide -w                         # status transitions; Ctrl-C when 1/1
kubectl describe pod -l app=vllm | tail -20         # Events: Scheduled, Pulling, Started, Unhealthy (startup)
kubectl logs -f deploy/vllm                         # download progress, then the API server start-up lines; Ctrl-C
kubectl rollout status deploy/vllm --timeout=20m
```

`${VLLM_TAG:?...}` stops the shell with that message if the variable is empty; without it, a fresh terminal silently produces the image `localhost:5001/vllm-openai-cpu:` and the pod fails with `InvalidImageName`. `--dry-run=server` runs the API server's validation and admission without persisting anything, catching errors a client-side check misses.

If the pod restarts instead of becoming ready, Events only show the symptom (`Startup probe failed: connection refused`, `BackOff`); the cause is in the container log, usually the last `ERROR` or `ValueError` line:

```bash
kubectl logs deploy/vllm --previous | grep -E 'ERROR|Error' | tail -5
```

`kubectl rollout status` gives up after the Deployment's `progressDeadlineSeconds` (600 s by default) even with `--timeout=20m`; the next `apply` that changes the pod template starts a fresh rollout.

**A healthy start (measured):** `READY 1/1` after about 50 s with 0 restarts (model already on the PVC); `kubectl exec deploy/vllm -- env | grep '^VLLM_'` shows only the three variables you set; the log shows `CPU KV cache size: 149,760 tokens` and `36.56x` concurrency at 4,096 tokens, exactly the 3.6 prediction; and `saved AOT compiled function to /models/vllm/...` confirms the compile cache lives on the PVC. After that the log fills with `GET /health` lines from `10.244.1.1`, the node's own address on the pod network — the kubelet running the probes. Hide them with `kubectl logs deploy/vllm | grep -v 'GET /health'`.

## 3.5 Talk to the model directly

```bash
kubectl port-forward svc/vllm 8000:8000             # leave running; use a second terminal

curl -s localhost:8000/v1/models | jq -r '.data[0].id, .data[0].root'   # served name, and the HF model behind it

# Ask a question; print the answer and token usage
curl -s localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "qwen2.5-1.5b",
  "messages": [{"role": "user", "content": "In one sentence, what is a Kubernetes taint?"}],
  "max_tokens": 80
}' | jq -r '.choices[0].message.content, .usage'

# Timed, greedy (temperature 0 = same answer every run): completion tokens on stdout,
# total seconds on stderr, so tokens / seconds = rough decode speed
curl -s -w '%{stderr}total %{time_total}s\n' localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' -d '{
  "model": "qwen2.5-1.5b",
  "messages": [{"role": "user", "content": "In one sentence, what is a Kubernetes taint?"}],
  "max_tokens": 80, "temperature": 0
}' | jq '.usage.completion_tokens'

# Metrics: the catalogue, then the exact names chapter 06 queries
curl -s localhost:8000/metrics | grep '^# HELP vllm:' | head -30
curl -s localhost:8000/metrics | grep -E '^# HELP vllm:(generation_tokens_total|time_to_first_token_seconds|inter_token_latency_seconds|kv_cache_usage_perc|num_preemptions_total) '
```

Measured: about **12 tok/s** end to end for the 1.5B model (28 tokens in 2.28 s) — better than a naive 3× slowdown from the 0.5B model, because per-request overhead does not grow with model size.

Answers change between runs because Qwen's `generation_config.json` sets temperature 0.7, so compare with `"temperature": 0`. In the metrics, `vllm:num_preemptions_total` is the one to watch under load: it climbs when the KV cache runs out and vLLM evicts running requests.

If `kubectl port-forward` prints only `Forwarding from [::1]:8000` (no `127.0.0.1:8000` line), another process already owns `127.0.0.1:8000`. Requests still work because macOS resolves `localhost` to `::1` first, but once the port-forward stops, `curl localhost:8000` silently reaches that other process. Find it with `lsof -nP -iTCP:8000 -sTCP:LISTEN`, then stop it or forward a different local port (`kubectl port-forward svc/vllm 18000:8000`).

## 3.6 KV-cache sizing: why 4 GiB is plenty

KV-cache bytes per token = 2 × layers × KV heads × head dimension × bytes per value.

Qwen2.5-1.5B has 28 layers, 2 KV heads (grouped-query attention), and head dimension 128 (1536 ÷ 12 heads), so in bf16: 2 × 28 × 2 × 128 × 2 = 28,672 bytes (28 KiB) per token. 4 GiB ÷ 28,672 = 149,796 tokens, rounded down to whole KV blocks = the **149,760 tokens** vLLM reports — far more than 8 sequences × 4,096 tokens = 32,768. Check the inputs yourself:

```bash
curl -sL https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct/resolve/main/config.json \
  | jq '{num_hidden_layers, num_attention_heads, num_key_value_heads, hidden_size}'
```

## 3.7 (Optional) A Hugging Face token as a Secret

Not needed for Qwen, but it removes the "unauthenticated requests to the HF Hub" warning and is required for gated models. Create a read-only token in your Hugging Face account settings, then:

```bash
read -rs HF_TOKEN && echo "read ${#HF_TOKEN} characters"   # paste the token; nothing echoes, and it stays out of shell history
kubectl create secret generic hf-token --from-literal=HF_TOKEN="$HF_TOKEN"
unset HF_TOKEN
kubectl rollout restart deploy/vllm                          # env vars are read only at container start
kubectl rollout status deploy/vllm --timeout=10m
kubectl logs deploy/vllm | grep -E 'unauthenticated|init engine'   # warning gone
```

Two checks: `kubectl exec deploy/vllm -- sh -c 'echo ${#HF_TOKEN}'` prints the token's length, never its value; and `kubectl get secret hf-token -o jsonpath='{.data.HF_TOKEN}' | head -c 12; echo` shows the stored value is only base64-encoded. Secrets are not encrypted unless the cluster enables encryption at rest, so RBAC on `get secret` is the real access control. The Secret is deliberately not in Git or `up.sh`.

The restart also tests the compile cache on the PVC. Measured: `init engine` went from 26.5 s to 23.4 s. The gain is small because on vLLM 0.30 CPU the cached artifact fails to load (`Compiling model again due to a load failure ... 'finalize_loading'`) and vLLM recompiles; check with `kubectl logs deploy/vllm | grep -E 'load failure|saved AOT|init engine'`. Keep `VLLM_CACHE_ROOT` anyway: it costs nothing, and on GPUs, where compilation and graph capture take minutes, a working cache is a major cold-start win.

## Key takeaways

- Test the image with plain `docker run` first; it separates image problems from cluster problems.
- Name collisions between Service env vars and application env vars are real; `enableServiceLinks: false` removes the whole class.
- KV-cache size is arithmetic you can do before deploying, and vLLM's log confirms it to the token.

Next: [04 — Chat UI](04-chat-ui.md)
