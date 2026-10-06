# 05 — Expose and test end to end

Open `http://localhost:8080`, ask a question, then prove each hop of the request path from the command line. About 10 minutes.

## 5.1 Browser and curl

```bash
open http://localhost:8080

# Same request from the terminal; -N disables buffering so you see tokens stream
curl -N -s localhost:8080/api/ask -H 'Content-Type: application/json' \
  -d '{"question": "Explain a Kubernetes Service in two sentences."}'; echo

curl -s localhost:8080/api/backend | jq          # {"llm_base_url": "...", "reachable": true}
```

## 5.2 Watch the Service load-balance across the two UI pods

```bash
for i in {1..8}; do curl -s -D - -o /dev/null localhost:8080/healthz | grep -i x-served-by; done
```

## 5.3 Prove each hop

```bash
# Hop 1: host port 8080 -> control-plane container port 30080
docker port llm-lab-control-plane

# Hop 2: NodePort -> kube-proxy -> a ready chat-ui pod. EndpointSlices = the pods behind a Service
kubectl get svc chat-ui vllm
kubectl get endpointslices -l kubernetes.io/service-name=chat-ui -o wide

# Hop 3: chat-ui -> DNS name vllm.llm.svc.cluster.local -> ClusterIP -> vllm pod
kubectl get endpointslices -l kubernetes.io/service-name=vllm -o wide
kubectl run netshoot --rm -it --image=nicolaka/netshoot -- bash
#   inside the debug pod:
#   dig +short vllm.llm.svc.cluster.local
#   curl -s vllm:8000/v1/models | head -c 300; echo
#   exit
```

## 5.4 The app's own view of each request

```bash
kubectl logs -l app=chat-ui --prefix --tail=-1 --since=30m | grep answered   # TTFT and total time per request
```

`--tail=-1` matters. With a label selector (`-l`), `kubectl logs` shows only the **last 10 lines per pod** by default. Any steady log traffic — probe access lines in a build without the 0.2.0 filter, the SDK's `HTTP Request:` lines, other requests — pushes the `answered` line out of that window quickly. Without a selector (`kubectl logs <pod>`) the default is the whole log. See [lessons learned](11-lessons-learned.md#3-kubectl-logs--l-shows-only-10-lines-per-pod).

## Checkpoint

If the browser answers and every command above succeeds, the whole stack is wired correctly. If you have local edits, commit them now so the drills in chapter 08 always have a known-good baseline to return to. Before pushing a repository anywhere, scan it for secrets:

```bash
git grep -nE 'hf_[A-Za-z0-9]{20,}|pass[w]ord|BEGIN [A-Z ]*PRIVATE KEY' || echo "no secrets found"
```

## Key takeaways

- A request crosses three hops: host port mapping, NodePort/kube-proxy, and in-cluster DNS to a ClusterIP. Each can be checked on its own.
- EndpointSlices are the truth about which pods a Service will send traffic to.
- With `kubectl logs -l ... | grep`, always add `--tail=-1`.

Next: [06 — Observability](06-observability.md)
