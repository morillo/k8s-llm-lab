# 07 — Scaling, rollouts, rollbacks, and autoscaling

Stateless UI pods scale freely; the model server scales only as far as accelerators allow. Feeling that difference is the point of this chapter. About 30 minutes.

## 7.1 Scale the UI

```bash
kubectl scale deploy/chat-ui --replicas=4
kubectl get pods -l app=chat-ui -o wide
for i in {1..200}; do curl -s -D - -o /dev/null localhost:8080/healthz | grep -i x-served-by; done | sort | uniq -c
```

With 200 requests across 4 replicas, expect about 50 ± 10 per pod (measured: 49, 50, 43, 58); with a dozen requests the split is too noisy to read. kube-proxy picks a backend at random per new *connection*. Check its mode, then read the actual rules on a node:

```bash
kubectl -n kube-system get cm kube-proxy -o yaml | grep -E '^\s+mode:'      # iptables here
docker exec llm-lab-control-plane iptables-save -t nat | grep 'llm/chat-ui:http ->'
```

Each backend gets a rule with `--mode random --probability`: 0.25, then 0.333, then 0.5, then a final rule with none. Each rule takes its share of whatever the earlier rules did not, which gives every pod 1-in-4 odds. Because the choice is per connection, a client that keeps one connection open (gRPC, or an LLM client reusing a streaming connection) stays on one pod; inference-aware gateways balance per request instead.

## 7.2 Try to scale vLLM — and hit the "GPU" wall

```bash
kubectl scale deploy/vllm --replicas=2
kubectl get pods -l app=vllm                     # second pod: Pending
kubectl get pods -l app=vllm --field-selector=status.phase=Pending -o jsonpath='{.items[*].status.conditions[?(@.type=="PodScheduled")].message}'; echo
kubectl events | grep FailedScheduling           # Insufficient example.com/gpu
kubectl scale deploy/vllm --replicas=1
```

Measured message: `0/3 nodes are available: 1 Insufficient example.com/gpu, 1 node(s) didn't match Pod's node affinity/selector, 1 node(s) had untolerated taint(s). preemption: 0/3 nodes are available: 1 No preemption victims found for incoming pod, 2 Preemption is not helpful for scheduling.`

Every node is accounted for: the inference node is out of "GPUs", the apps node fails the selector, the control plane has a taint. The preemption half is the scheduler asking whether evicting pods would help: on two nodes it would not (no eviction fixes a selector or taint mismatch), and on the inference node there is no lower-priority pod to evict. A `PriorityClass` is how a production inference pod gets to displace a batch job for scarce accelerators. The event repeats about every 5 minutes because the scheduler periodically retries unschedulable pods.

The real-world answers to this message: add nodes (a cluster autoscaler on a GPU node group), share an accelerator (partitioning or time-slicing), or use fewer accelerators per replica (smaller model, quantization).

## 7.3 Rolling update, history, and rollback

The cluster runs chat-ui 0.2.0, which filters probe hits out of the access log. To practice a rollout and a rollback with a *visible* difference, build the previous version, 0.1.0, from Git history and roll the UI to it.

```bash
cd ~/k8s-llm-lab
REV=$(git log --format=%H -1 --grep='^chat-ui 0.2.0')   # the commit that introduced 0.2.0
SRC=$(mktemp -d)
git archive "$REV^" app | tar -x -C "$SRC"              # app/ as it was just before: 0.1.0
grep -c DropProbeLogs "$SRC/app/main.py"                # 0 = no probe-log filter
docker build -t localhost:5001/chat-ui:0.1.0 "$SRC/app" && docker push localhost:5001/chat-ui:0.1.0
rm -rf "$SRC"
```

In another terminal, keep this running for the whole section; every line should stay `200`, because readiness probes plus surge keep serving pods in the Service throughout:

```bash
while true; do curl -s -o /dev/null -w '%{http_code}\n' localhost:8080/healthz; sleep 0.2; done
```

Roll out, inspect, roll back:

```bash
kubectl set image deploy/chat-ui chat-ui=localhost:5001/chat-ui:0.1.0
kubectl annotate deploy/chat-ui kubernetes.io/change-cause="0.1.0: previous build, probe hits in access log" --overwrite
kubectl rollout status deploy/chat-ui
kubectl logs -l app=chat-ui --prefix --tail=20 | grep -c healthz   # > 0: probe lines are back
kubectl rollout history deploy/chat-ui

kubectl rollout undo deploy/chat-ui                                 # back to the previous revision: 0.2.0
kubectl rollout status deploy/chat-ui
kubectl get deploy chat-ui -o jsonpath='{.spec.template.spec.containers[0].image}'; echo   # localhost:5001/chat-ui:0.2.0
kubectl rollout history deploy/chat-ui                              # the restored revision gets a new number
```

`kubectl rollout undo deploy/chat-ui --to-revision=<N>` jumps to any revision listed by `history`.

Expect a warning on `rollout undo`: `resource deployments/chat-ui was previously managed with 'kubectl apply'. Rolling back will not update the kubectl.kubernetes.io/last-applied-configuration annotation...`. It points at the real lesson: `scale`, `set image`, and `rollout undo` are imperative changes, so the live cluster drifts from Git. Check it:

```bash
kubectl diff -f manifests/20-chat-ui.yaml      # replicas 4 -> 2 (from 7.1); the image matches again after the undo
```

The next `kubectl apply -f` would quietly revert the replica count. In production, a GitOps controller such as Argo CD or Flux runs that comparison continuously and either alerts on drift or reverts it. The end of this chapter brings Git and the cluster back in sync.

## 7.4 Config changes need a restart

Pods read environment variables only at start.

```bash
kubectl patch configmap chat-ui-config --type merge -p '{"data":{"MAX_TOKENS":"128"}}'
kubectl rollout restart deploy/chat-ui
kubectl rollout status deploy/chat-ui

# Verify the new value reached the pods, then see its effect
kubectl exec deploy/chat-ui -- printenv MAX_TOKENS          # 128
curl -N -s localhost:8080/api/ask -H 'Content-Type: application/json' \
  -d '{"question": "What is a Kubernetes namespace?"}'; echo   # long answers now stop mid-sentence
kubectl logs -l app=chat-ui --prefix --tail=-1 | grep answered | sort -k2 | tail -1   # newest across all pods (sort by timestamp); total ~11 s
```

A long answer is now cut off at 128 tokens and its total time drops accordingly (measured: about 11 s at ~12 tok/s, versus 18 s for a ~200-token answer). The ConfigMap change alone did nothing until the restart. (ConfigMaps mounted as *files* do update in place after a short delay, but the application still has to re-read them.) The Helm chart in [chapter 09](09-helm.md) automates this restart.

## 7.5 Horizontal Pod Autoscaler on the UI

```bash
kubectl autoscale deploy/chat-ui --cpu=60% --min=2 --max=6   # --cpu-percent is deprecated
hey -z 120s -c 100 http://localhost:8080/healthz &           # CPU load on the UI itself
kubectl get hpa chat-ui -w                                   # replicas climb, then fall ~5 min after load stops; Ctrl-C
kubectl describe hpa chat-ui | sed -n '/Events/,$p'          # each scaling decision with its reason
```

Measured: CPU went 2% → 304% → 501% of the 100m request, so each pod hit its 500m *limit* (500% is the ceiling with a limit five times the request), and replicas went 4 → 6 (the max) within about 75 s. The formula is `desired = ceil(current × currentUtilization / target)` = ceil(4 × 304 / 60) = 21, capped at 6. With 6 pods sharing the load, CPU settled near 333%. After `hey` stopped, CPU fell to 2–3% within a minute, but replicas stayed at 6 for about 5 more minutes (the default scale-down stabilization window) and then dropped 6 → 2 in one step. `hey` reported about 11,000 requests/s with p50 4 ms; note that it records at most 1,000,000 results, so a long run's statistics cover only the first million requests.

`kubectl get -w` prints a line only when the object changes. When the measured CPU holds steady nothing changes, so the watch goes quiet; that is by design, not a hang. `kubectl get hpa chat-ui` shows the current state at any time.

Notice what does *not* trigger it: LLM questions. The UI pod spends that time waiting on vLLM, so its CPU stays low while users wait. Model servers scale on their queue (`vllm:num_requests_waiting`) or KV-cache fill, typically through KEDA or the Prometheus Adapter.

## 7.6 Clean up: make Git and the cluster agree

```bash
kubectl delete hpa chat-ui && kubectl scale deploy/chat-ui --replicas=2
kubectl diff -f manifests/20-chat-ui.yaml      # expect only MAX_TOKENS: 128 live (7.4) vs 256 in the file
kubectl apply -f manifests/20-chat-ui.yaml && kubectl rollout restart deploy/chat-ui   # restart: env vars are read at start
kubectl diff -f manifests/20-chat-ui.yaml      # no output = in sync
```

While an HPA manages a Deployment, leave `replicas` out of its manifest, or every `kubectl apply` resets the replica count the HPA chose.

## Key takeaways

- Stateless tiers scale with a command; accelerator-bound tiers scale only as far as the hardware does, and the scheduler's message says exactly why.
- kube-proxy balances per connection, not per request.
- Rollouts with readiness probes are zero-downtime; `rollout undo` is fast — and both are imperative changes that create drift from Git.
- A ConfigMap change consumed as env vars is a deploy.
- Autoscale on the signal that reflects user pain: CPU for the UI, queue depth or KV-cache use for the model server.

Next: [08 — Troubleshooting drills](08-troubleshooting-drills.md)
