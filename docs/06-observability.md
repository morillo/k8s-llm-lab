# 06 — Observability

Work from coarse to fine: cluster state with kubectl, live views with k9s and stern, node internals with crictl and journalctl, then time series with Prometheus and Grafana, including a vLLM dashboard kept as code. About 45 minutes.

## 6.1 Cluster and workload state

```bash
kubectl get nodes -L pool                               # Ready? which pool?
kubectl get deploy,rs,pods,svc,endpointslices,pvc -o wide
kubectl get pods -w                                     # live status transitions; Ctrl-C
kubectl events --watch                                  # namespace events as they happen; Ctrl-C
kubectl get events -A --sort-by=.lastTimestamp | tail -30
kubectl describe pod -l app=vllm                        # conditions, probes, last state, events
kubectl top nodes
kubectl top pods --containers
kubectl describe node llm-lab-worker2 | sed -n '/Allocated resources/,/Events/p'   # requests vs allocatable
```

## 6.2 Logs

```bash
kubectl logs deploy/vllm --since=15m | grep -v 'GET /health'      # one pod of a Deployment, probe lines hidden
kubectl logs -l app=chat-ui --prefix -f                             # all pods by label, live; starts from the last 10 lines per pod
kubectl logs -l app=chat-ui --prefix --tail=-1 | grep answered      # with -l, add --tail=-1 before grep (default is 10 lines)
kubectl logs deploy/vllm --previous                                 # the crashed container before the last restart (errors if it never restarted)
stern 'chat-ui|vllm' --since 5m --exclude 'GET /health'             # color-coded multi-pod tail; the regex also drops /healthz
```

The lesson of this step is probe noise. Each pod's readiness and liveness probes produce a request every few seconds, burying the handful of real ones. Filtering at read time (`grep -v`, `stern --exclude`) works; the better fix is to drop probe hits at the source while keeping access logs for real traffic. chat-ui does that since 0.2.0 (`DropProbeLogs` in `app/main.py`); vLLM still logs every `GET /health`. To see which access-log options your vLLM version has: `kubectl exec deploy/vllm -- vllm serve --help=all | grep -i access`.

An empty filtered log only means something once you have seen the same filter show real traffic. Start this in one terminal (`--tail 0` shows only new lines; without it stern replays up to 48 hours of history):

```bash
stern 'chat-ui|vllm' --exclude 'GET /health' --tail 0
```

And this in a second:

```bash
curl -N -s localhost:8080/api/ask -H 'Content-Type: application/json' -d '{"question": "What is a Kubernetes namespace?"}'; echo
```

Expect, interleaved and color-coded: chat-ui's `HTTP Request: POST http://vllm.llm.svc.cluster.local:8000/v1/chat/completions` (logged by the OpenAI SDK's HTTP client), its `POST /api/ask` access line and `answered ttft=...`, vLLM's `POST /v1/chat/completions` from that chat-ui pod's IP, and vLLM's `Engine 000: ...` lines every 10 seconds.

Two things to read in that trace:

- chat-ui sees every browser or curl request as coming from the control-plane node's IP (`172.18.0.4` here). NodePort traffic enters there, and kube-proxy rewrites the source address (SNAT) before forwarding to a pod on another node, so the real client IP is lost. `externalTrafficPolicy: Local` on the Service preserves it, at the cost of only routing to pods on the node that received the traffic.
- While one long answer is generating, `Running: 1 reqs` with `Avg generation throughput: ~11.6 tokens/s` is the true single-request decode speed, because one request filled the whole 10-second logging window.

## 6.3 Terminal UI

`k9s -n llm`, then type `:pods`, `:deploy`, `:svc`, or `:events`. On a pod: `l` logs, `d` describe, `s` shell, `shift-f` port-forward, `ctrl-d` delete.

## 6.4 Node internals

What you would SSH into a real node for:

```bash
docker stats --no-stream                                # per-node CPU/RAM/network/disk: each kind node is a container
docker exec llm-lab-worker2 crictl ps                   # containers running on the inference node
docker exec llm-lab-worker2 crictl pods                 # pod sandboxes: the "pause" container owns each pod's network namespace
docker exec llm-lab-worker2 crictl stats                # per-container CPU, memory, and writable-layer DISK
docker exec llm-lab-worker2 journalctl -u kubelet -n 40 --no-pager   # last 40 kubelet lines; a quiet kubelet can log nothing for hours
kubectl exec deploy/vllm -- sh -c 'du -sh /tmp /root/.cache 2>/dev/null'   # what is filling the container's writable layer
```

How to read it:

- In `docker stats`, `CPU %` is per core (100% = one full core). The inference node's `NET I/O` received total (about 4 GB here) is the 1.3 GB image pull plus the 2.9 GB model download.
- `crictl stats` reports decimal GB while `docker stats` uses GiB, so 10.79 GB and 10.05 GiB are the same memory. Its `DISK` column is the container's own writable layer, not the PVC: anything a process writes outside a mounted volume counts as ephemeral storage and can get pods evicted when the node runs short of disk.
- A kubelet doing nothing logs nothing, so `--since "15 min ago"` often prints `-- No entries --`; `-n 40` always shows the most recent activity.

Reading the kubelet journal after a `rollout restart`: the old container is removed, its volumes (the `dshm` emptyDir, the PVC, the service-account token) are unmounted, the new pod is `ADDED`, the same volumes are attached, and `Observed pod startup duration ... podStartE2EDuration` records time to *running* (about 1.4 s here, no image pull); the pod's later `MODIFIED` update about 50 s on is when it became Ready. The PVC appears as `kubernetes.io/host-path` because the local-path provisioner backs each volume with a directory on that node, which ties the volume, and any pod using it, to `llm-lab-worker2`. Not every `E` line is a problem: `NotFound` right after `RemoveContainer` is a harmless cleanup race, `Error on socket receive ... 10250` appears when a `logs -f`, `exec`, or `port-forward` stream closes, and `superfluous response.WriteHeader` is known kubelet noise. Judge log lines by timing and symptoms, not by severity letter.

**Attributing the writable layer.** `crictl stats`'s `DISK` column is the authoritative number. `du` inside the container sees the merged view (image layers + writable layer + any volume you name), so compare against a fresh container from the same image, which has written nothing yet:

```bash
source lab.env
docker run --rm --entrypoint sh vllm/vllm-openai-cpu:$VLLM_TAG -c 'du -sh /tmp /root /usr /opt 2>/dev/null'
```

Measured, fresh container → running pod: `/tmp` 4 KB → 159 MB, `/opt` 1,845 MB → 1,979 MB (+134 MB, from `du -sm /opt`; `du -sh` rounds too coarsely), `/root` and `/usr` unchanged. The writable layer is `/tmp` (compiler scratch files) plus the `/opt` growth (likely Python bytecode compiled on first import): 293 MiB, about 307 MB, nearly all of `crictl`'s 311 MB. Also note that the image is 1.3 GB compressed but about 3.9 GB unpacked on the node; disk planning uses the unpacked size. Optional hardening: mount an `emptyDir` with a `sizeLimit` at `/tmp` and set `resources.requests.ephemeral-storage`, so scratch space is explicit, bounded, and counted by the scheduler.

## 6.5 Prometheus and Grafana (kube-prometheus-stack)

The two `NilUsesHelmValues=false` flags make Prometheus pick up ServiceMonitors from any namespace without a special label.

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update
helm install kps prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false
kubectl -n monitoring get pods -w                       # wait until all Running; Ctrl-C
kubectl -n monitoring get svc                           # kps-grafana, kps-kube-prometheus-stack-prometheus, ...
```

Tested with chart `kube-prometheus-stack-91.9.0`; add `--version 91.9.0` to reproduce exactly.

[`manifests/30-vllm-servicemonitor.yaml`](../manifests/30-vllm-servicemonitor.yaml) tells Prometheus to scrape the `http` port of any Service labelled `app: vllm`, at `/metrics`, every 15 seconds.

```bash
kubectl get crd | grep monitoring.coreos.com          # the Prometheus Operator's CRDs, incl. servicemonitors
kubectl apply -f manifests/30-vllm-servicemonitor.yaml
kubectl get servicemonitor -n llm

# Port-forwards in the background of this terminal; `jobs` lists them, `kill %1 %2` stops them
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null &
kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null &

# Target health from the CLI (Prometheus HTTP API); give it ~30 s after applying the ServiceMonitor
curl -s 'localhost:9090/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | [.labels.job, .health, .lastError] | @tsv' | sort

# A PromQL query from the CLI
curl -s localhost:9090/api/v1/query --data-urlencode 'query=vllm:num_requests_running' \
  | jq -r '.data.result[] | "\(.metric.pod)  \(.value[1])"'

kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d; echo
open http://localhost:9090/targets                      # llm/vllm should be UP
open http://localhost:3000                              # Grafana: user admin, password printed above
```

What the install created: Helm installed the Prometheus Operator plus CRDs, and the operator turned a `Prometheus` and an `Alertmanager` custom resource into StatefulSets (`prometheus-...-0`, `alertmanager-...-0`) with headless Services (`prometheus-operated`, `alertmanager-operated`). That operator pattern — declare a custom resource, a controller builds the workload — is also how GPU operators manage drivers and device plugins. `node-exporter` is a DaemonSet that tolerates every taint, so it runs on the inference node too, as GPU metrics exporters do on GPU nodes. Grafana runs three containers: Grafana plus two sidecars that load dashboards and data sources from ConfigMaps. By default Prometheus stores metrics in an `emptyDir`, so history is lost when its pod restarts; production sets `prometheus.prometheusSpec.storageSpec` to a PVC.

**Expected target list:** `vllm` up; `kubelet` up nine times (`/metrics`, `/metrics/cadvisor`, `/metrics/probes` on three nodes); two each for CoreDNS, Prometheus, and Alertmanager; and these **down** with `connection refused`: `kube-controller-manager` (:10257), `kube-scheduler` (:10259), `kube-etcd` (:2381), and `kube-proxy` (:10249, once per node). kubeadm, which kind uses, binds those metrics endpoints to 127.0.0.1, so Prometheus cannot reach them on the node IP. That is expected on kind.

### Optional: fix two of those targets live (static pods)

The control-plane components are static pods: editing their manifest on the node makes the kubelet recreate them. kube-proxy reads a ConfigMap.

```bash
# Scheduler: a static pod. Edit its manifest on the node; the kubelet notices and recreates it
docker exec llm-lab-control-plane sed -i 's/--bind-address=127.0.0.1/--bind-address=0.0.0.0/' \
  /etc/kubernetes/manifests/kube-scheduler.yaml

# kube-proxy: a DaemonSet configured by a ConfigMap
kubectl -n kube-system get cm kube-proxy -o yaml \
  | sed 's/metricsBindAddress: .*/metricsBindAddress: 0.0.0.0:10249/' | kubectl apply -f -
kubectl -n kube-system rollout restart ds kube-proxy

# ~1 min later: both jobs should report up
curl -s 'localhost:9090/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | [.labels.job, .health] | @tsv' | grep -E 'scheduler|proxy'
```

These edits live only on this cluster and disappear on rebuild; the durable version is a `kubeadmConfigPatches` entry in `kind-config.yaml`. Exposing metrics on `0.0.0.0` is for the lab only. Expect a one-time `last-applied-configuration` warning on the kube-proxy ConfigMap (kubeadm created it imperatively). The scheduler pod in `kubectl -n kube-system get pods` is a *mirror pod*, a read-only reflection of the static pod; `kubectl delete` on it does not stop the scheduler, the file does. After the edit its AGE resets while RESTARTS stays 0: a changed manifest produces a new pod, not a restarted container. The same `sed` fixes `kube-controller-manager.yaml`. etcd uses `--listen-metrics-urls=http://127.0.0.1:2381` instead, and restarting the cluster's only etcd member makes the API server unavailable for a few seconds — a small taste of why production runs three or five etcd members.

## 6.6 The inference queries to know

Paste into Prometheus → Graph. Metric names shift slightly between vLLM versions; confirm against the `# HELP vllm:` list from chapter 03.

| Question | PromQL |
| --- | --- |
| Generation throughput (tokens/s) | `sum(rate(vllm:generation_tokens_total[1m]))` |
| p95 time to first token | `histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[5m])))` |
| Requests running vs queued | `{__name__=~"vllm:num_requests_(running\|waiting)"}` (both series on one graph) |
| KV-cache fill | `vllm:kv_cache_usage_perc` (older versions: `vllm:gpu_cache_usage_perc`) |
| CPU the model server uses | `sum(rate(container_cpu_usage_seconds_total{namespace="llm",container="vllm"}[1m]))` |

To graph two metrics together, add a second query or match both names with a regex on `__name__`, as in the running-versus-queued row. Do not join them with `and`: in PromQL `and` is a set operator that returns only the left-hand series where a matching right-hand series exists, so you would see one line, not two. `vllm:kv_cache_usage_perc` is a 0–1 fraction despite its name; during the 32-prompt benchmark below expect only about 0.017, since 8 requests × ~320 tokens is about 2,560 of the 149,760-token cache.

In the Prometheus UI, use the **Graph** tab with a time range that covers your load runs (for example 1h); the **Table** tab shows only the current value, which is 0 for every queue metric when the server is idle.

## 6.7 Generate load and watch the queue form

```bash
hey -z 60s -c 6 -m POST -H 'Content-Type: application/json' \
  -d '{"question": "Write a haiku about GPUs."}' http://localhost:8080/api/ask
```

Then vLLM's own benchmark (check flags with `vllm bench serve --help`):

```bash
kubectl exec deploy/vllm -- vllm bench serve --base-url http://localhost:8000 \
  --model qwen2.5-1.5b --tokenizer Qwen/Qwen2.5-1.5B-Instruct \
  --dataset-name random --random-input-len 256 --random-output-len 64 --num-prompts 32
```

It reports throughput, TTFT, and inter-token latency percentiles — the vocabulary used to compare inference hardware. Running the benchmark inside the vLLM pod makes the client compete with the server for CPU; for cleaner numbers, run it from a separate pod or machine.

Measured results (M4 Max, 16-vCPU / 48 GB Docker VM):

| Run | Result | What it shows |
| --- | --- | --- |
| `hey`, 6 concurrent, 60 s | 143 requests, all 200; p50 2.58 s, p99 3.75 s; `resp wait` 7 ms | 6 is below `--max-num-seqs=8`, so every request ran immediately. `resp wait` is time to response *headers*, which a streaming response sends before the first token, so `hey` cannot see TTFT; use the `answered ttft=` log or Prometheus. |
| `vllm bench serve`, 32 prompts, 256 in / 64 out | 35.8 output tok/s (96 peak); mean TTFT 29 s, p99 51 s; mean TPOT 122 ms; p99 ITL 1.27 s | 32 arrive together but only 8 run at a time: 4 waves of about 14 s. TTFT is almost entirely queueing — wave 1 waits ~0 s, wave 4 ~42 s, plus prefill. |
| Same run, per token | TPOT 122 ms vs ~83 ms for a single request (12 tok/s) | Batching 8 requests made each about 1.5× slower per token but raised total output about 5×: per-user latency versus total throughput. |
| Same run, ITL tail | median 93 ms, p99 1,271 ms | When a new wave's prompts (8 × 256 tokens) are prefilled, decoding of running requests stalls: prefill/decode interference. |

Confirm the queue in Prometheus, then confirm the probes survived the CPU saturation:

```bash
curl -s localhost:9090/api/v1/query --data-urlencode 'query=max_over_time(vllm:num_requests_waiting[30m])' | jq -r '.data.result[].value[1]'   # predicted ~24 (32 - 8); measured 23
curl -s localhost:9090/api/v1/query --data-urlencode 'query=max_over_time(vllm:num_requests_running[30m])' | jq -r '.data.result[].value[1]'   # predicted 8 (--max-num-seqs); measured 6
kubectl get pod -l app=vllm      # RESTARTS should still be 0: the 5 s probe timeouts held under load

# A second view of the same gauges: vLLM's own engine log line, every 10 s
kubectl logs deploy/vllm --since=2h | grep -o 'Running: [0-9]* reqs, Waiting: [0-9]* reqs' | sort | uniq -c | sort -rn | head
```

Prometheus scrapes every 15 s, so `max_over_time` only sees the values at those instants and can miss a short peak. Measured: Prometheus reported a peak of 6 running, but the engine log (every 10 s on its own clock) shows `Running: 8 reqs, Waiting: 24 reqs`, `Running: 8 reqs, Waiting: 8 reqs`, and `Running: 7 reqs, Waiting: 16 reqs` — `--max-num-seqs=8` is honored and the gap was sampling. Each wave starts with lines like `Running: 1 reqs, Waiting: 23 reqs`, because the scheduler admits waiting requests a few per step as it fits their prompt tokens into its per-step token budget. Know how often a metric is sampled before building a capacity plan on it.

**Benchmarking pitfall.** Run the same `vllm bench serve` command twice and the second run can look much faster. Measured: duration 57.2 s → 40.4 s, output throughput 35.8 → 50.6 tok/s, mean TTFT 29.0 → 19.0 s, p99 ITL 1,271 → 733 ms. The random dataset uses `--seed 0` by default, so both runs send identical prompts, and with prefix caching on (the default) the second run reuses KV blocks the first computed. Test it rather than assume it:

```bash
# 1) Did the second run hit the prefix cache? (fraction of prompt tokens served from cache, last 10 min)
curl -s localhost:9090/api/v1/query --data-urlencode \
  'query=increase(vllm:prefix_cache_hits_total[10m]) / increase(vllm:prefix_cache_queries_total[10m])' \
  | jq -r '.data.result[].value[1]'

# 2) Controlled re-run with new prompts: if results return to the first run's numbers, the cache explains the gap
kubectl exec deploy/vllm -- vllm bench serve --base-url http://localhost:8000 \
  --model qwen2.5-1.5b --tokenizer Qwen/Qwen2.5-1.5B-Instruct \
  --dataset-name random --random-input-len 256 --random-output-len 64 --num-prompts 32 --seed 1
```

Measured: a prefix-cache hit fraction of 0.32, and the `--seed 1` run came back to the first run's numbers (55.4 s, 37.0 tok/s, mean TTFT 28.0 s, p99 ITL 1,281 ms). So the cache, not warm-up, explained the faster second run. For benchmarks: vary prompts between runs, discard a warm-up run, and state whether prefix caching was on.

## 6.8 Grafana: built-in dashboards, plus a vLLM dashboard as code

kube-prometheus-stack installed Grafana (`kps-grafana`), connected it to Prometheus, and loaded about 25 dashboards. With the 6.5 port-forward running, open `http://localhost:3000` and log in as `admin`.

1. ☰ menu → **Dashboards**, search `Compute Resources`, open **Kubernetes / Compute Resources / Namespace (Pods)**.
2. Set the `namespace` dropdown to `llm`.
3. In the time picker choose **Last 24 hours**. Hover to see the pod; drag across a spike to zoom.
4. Open **Kubernetes / Compute Resources / Pod** (namespace `llm`, pod `vllm-…`): CPU rises far above the 6-core request during load because there is no CPU limit, while memory stays flat near 10 GiB because the KV cache is allocated up front. **Node (Pods)** with `llm-lab-worker2` shows the whole inference node.

`No data` means the time range or namespace does not cover any activity; Prometheus has data only from its installation onward.

Then load the lab's own dashboard through Grafana's HTTP API. [`dashboards/vllm-lab.json`](../dashboards/vllm-lab.json) defines five panels — requests running vs waiting, generation throughput, p95 TTFT, KV-cache usage, and vLLM CPU — using the queries from 6.6. It lives outside `manifests/` so `kubectl apply -f manifests/` only ever sees Kubernetes objects, and it contains the placeholder `DS_UID` rather than any credential.

```bash
cd ~/k8s-llm-lab
lsof -nP -iTCP:3000 -sTCP:LISTEN          # no output? run: kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null &
GF_PASS=$(kubectl -n monitoring get secret kps-grafana -o jsonpath='{.data.admin-password}' | base64 -d)
DS_UID=$(curl -s -u "admin:$GF_PASS" localhost:3000/api/datasources | jq -r '[.[] | select(.type=="prometheus")][0].uid')
echo "Prometheus data source UID: $DS_UID"  # expected: prometheus

URL=$(sed "s/DS_UID/$DS_UID/g" dashboards/vllm-lab.json \
  | curl -s -u "admin:$GF_PASS" -H 'Content-Type: application/json' \
         -X POST localhost:3000/api/dashboards/db -d @- \
  | tee /dev/stderr | jq -r .url)
open "http://localhost:3000$URL"
```

Expected reply: `"status":"success"` and a `"url":"/d/<uid>/vllm-lab"`. `"overwrite": true` in the file makes re-running safe: edit the JSON and run the `URL=...` command again to update the dashboard, or after a cluster rebuild to recreate it. The chart's own dashboards are ConfigMaps a Grafana sidecar loads automatically; packaging this JSON the same way would make it fully declarative.

## Key takeaways

- Work coarse to fine: object state → events → logs → node → time series.
- Probe noise is a logging design problem; fix it at the source.
- Know the sampling interval of every metric you reason about (Prometheus 15 s here, vLLM's log 10 s).
- Benchmarks with repeated synthetic prompts overstate performance when prefix caching is on.
- Dashboards belong in Git like any other configuration.

Next: [07 — Scaling, rollouts, HPA](07-scaling-rollouts-hpa.md)
