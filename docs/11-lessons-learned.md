# 11 — Lessons learned

The problems actually hit while building this lab, how each was diagnosed, and how it was fixed. The fixes are already in the repository; this page explains why they are there.

## 1. The `VLLM_PORT` collision

**Symptom.** The vLLM pod went into `CrashLoopBackOff`. Events only said `Startup probe failed: connection refused` and `BackOff`.

**Diagnosis.** The cause was in the previous container's log, not in Events:

```bash
kubectl logs deploy/vllm --previous | grep -E 'ERROR|Error' | tail -5
# ValueError: VLLM_PORT 'tcp://<cluster-ip>:8000' appears to be a URI ...
```

For every Service in a namespace, Kubernetes injects legacy Docker-link environment variables into pods that start afterwards. A Service named `vllm` produces `VLLM_SERVICE_HOST`, `VLLM_PORT=tcp://<cluster-ip>:8000`, and others. vLLM reads `VLLM_*` variables as its own configuration, and `VLLM_PORT` must be a port number.

**Fix.** `enableServiceLinks: false` in the vLLM pod spec ([`manifests/10-vllm.yaml`](../manifests/10-vllm.yaml), [`charts/llm-lab/templates/vllm.yaml`](../charts/llm-lab/templates/vllm.yaml)). Renaming the Service would also work, but turning off service links keeps the name and every DNS reference to it and removes the whole class of collision. Check: `kubectl exec deploy/vllm -- env | grep '^VLLM_'` lists only the variables you set.

**Lesson.** Events show symptoms; the container log shows causes. Any app that reads env vars with a prefix matching a Service name is exposed to this.

## 2. Probe timeouts under CPU saturation

**Problem.** Kubernetes probes default to a 1-second timeout. vLLM's CPU backend uses every vCPU it can get (there is deliberately no CPU limit), so under a batch of requests the `/health` endpoint competes with inference for CPU. With a 1 s timeout, a slow-but-healthy answer counts as a failure: readiness failures pull the only model server out of the Service, and liveness failures restart it mid-load, discarding the loaded model.

**Fix.** `timeoutSeconds: 5` on the startup, readiness, and liveness probes, and a liveness `failureThreshold: 4` (about a minute of consecutive failures before a restart). The startup probe gets its own large budget (`failureThreshold: 120` × 10 s) for the first model download.

**Verification.** After the 32-prompt benchmark that saturated the node ([chapter 06](06-observability.md#67-generate-load-and-watch-the-queue-form)), `kubectl get pod -l app=vllm` still showed 0 restarts.

**Lesson.** Probe timeouts are part of capacity planning. Size them for the server under full load, not idle.

## 3. `kubectl logs -l` shows only 10 lines per pod

**Symptom.** `kubectl logs -l app=chat-ui --prefix | grep answered` returned nothing, although requests had clearly been answered.

**Diagnosis.** With a label selector, `kubectl logs` defaults to `--tail=10` per pod (without a selector, it returns the whole log). Probe access lines arrived every few seconds and pushed the `answered` line out of that 10-line window within a minute.

**Fix.** Add `--tail=-1` whenever `-l` output is piped to `grep`. The deeper fix was to stop logging probe hits at the source: chat-ui 0.2.0's `DropProbeLogs` filter on the `uvicorn.access` logger.

## 4. zsh does not treat `#` as a comment

**Symptom.** Pasting a command with a trailing `# comment` failed in odd ways, for example `helm history` complaining `requires 1 argument`.

**Diagnosis.** Interactive zsh (the macOS default) passes `# comment` words as arguments unless `interactivecomments` is set.

**Fix.** `echo 'setopt interactivecomments' >> ~/.zshrc && source ~/.zshrc` ([chapter 00](00-prerequisites.md)). A related trap: placeholders like `<pod>` must not be typed literally, because zsh reads `<` as input redirection (`zsh: no such file or directory: pod`).

## 5. The prefix-cache benchmark pitfall (seed 0 vs seed 1)

**Symptom.** Running the identical `vllm bench serve` command twice made the server look ~40% faster the second time: 35.8 → 50.6 output tok/s, mean TTFT 29.0 → 19.0 s.

**Hypothesis.** The random dataset uses `--seed 0` by default, so both runs sent identical prompts, and vLLM's prefix caching (on by default) reused KV blocks from the first run.

**Test.** The Prometheus ratio `increase(vllm:prefix_cache_hits_total[10m]) / increase(vllm:prefix_cache_queries_total[10m])` showed 0.32 — about a third of prompt tokens served from cache. A re-run with `--seed 1` (new prompts) returned to the first run's numbers: 37.0 tok/s, mean TTFT 28.0 s.

**Lesson.** Vary prompts between benchmark runs, discard a warm-up run, and state whether prefix caching was on. Test an explanation before accepting it.

## 6. Prometheus 15 s sampling vs vLLM's 10 s log

**Symptom.** `max_over_time(vllm:num_requests_running[30m])` reported a peak of **6** running requests, although `--max-num-seqs=8` and 32 queued requests should have filled the batch.

**Diagnosis.** Prometheus scrapes the gauge every 15 s and only sees the value at those instants. vLLM's own engine log, on its own 10 s clock, showed `Running: 8 reqs, Waiting: 24 reqs` and other full-batch moments. Full batches are brief because the scheduler admits waiting requests a few per step as their prompt tokens fit its per-step token budget, so each wave ramps up to 8 and drains again.

**Lesson.** Know how often a metric is sampled before building a capacity plan on its peaks. Cross-check gauges with a second source.

## 7. Configuration drift after imperative changes

**Symptom.** After `kubectl scale`, `kubectl set image`, and `kubectl rollout undo`, `kubectl rollout undo` warned that the `last-applied-configuration` annotation would not be updated, and `kubectl diff -f manifests/20-chat-ui.yaml` showed differences between the live Deployment and Git.

**Diagnosis.** Imperative commands change the live object without changing the manifest, so the next `kubectl apply` quietly reverts them. A second, subtler case: client-side `apply` and `diff` never show fields that were added imperatively and never appeared in any manifest (for example an env var from `kubectl set env`). Similarly, the namespace created imperatively by `kubectl create namespace` and the kube-proxy ConfigMap created by kubeadm both produced a one-time `last-applied-configuration` warning on their first `apply`.

**Fix.** After each experiment, bring the file and the cluster back into agreement (update the manifest and commit, or re-apply it) and confirm with `kubectl diff` returning nothing. Check imperatively added fields directly with `jsonpath`. Later, move ownership to one tool (Helm, chapter 09).

**Lesson.** One source of truth per object. In production, a GitOps controller does this comparison continuously.

## 8. Helm 4 server-side apply and adopted objects

**Context.** Chapter 09 adopts objects that were created and modified with kubectl (label `app.kubernetes.io/managed-by=Helm` plus the `meta.helm.sh/release-name` and `release-namespace` annotations), so the PVC with the downloaded model survives.

**Symptom.** The install worked, but the first config upgrade (`--set chatUi.maxTokens=128`) failed:

```
Upgrade "llm-lab" failed: conflict occurred while applying object llm/chat-ui-config /v1, Kind=ConfigMap:
Apply failed with 1 conflict: conflict with "kubectl-client-side-apply" using v1: .data.MAX_TOKENS
```

**Diagnosis.** Helm 4 applies with server-side apply, which tracks an owner (field manager) for every field. Where the chart's value equalled the live value at adoption time, ownership stayed *shared* with the earlier kubectl manager. Changing a shared field is a conflict. Clearing `managedFields` on the objects did **not** help: the API server then attributes pre-existing fields to a synthetic manager called `before-first-apply`, and the retried upgrade failed with `conflict with "before-first-apply"` on `.data.MAX_TOKENS` and on chat-ui's `.spec.template.metadata.annotations.checksum/config`.

**Fix.** Pass `--force-conflicts` on the install and on upgrades that change shared fields; it hands the changed fields to Helm alone. After that, `helm rollback` needs no force because Helm owns those fields. Objects Helm creates itself never have the problem. The final command sequence is in [chapter 09, steps 9.5–9.6](09-helm.md#95-preview-then-install); it contains no `managedFields` step.

A related adoption detail: drill 7 left `spec.strategy.rollingUpdate` owned by `kubectl-patch`. Server-side apply changes only fields Helm declares, so Helm could set `type: Recreate` while leaving `rollingUpdate` in place, a combination the API server can reject. The fix was to remove the leftover with a JSON patch before installing.

## 9. A partial Helm upgrade failure

**Symptom.** After the failed upgrade above, `helm history llm-lab -n llm` showed the revision as `failed`, yet chat-ui pods had restarted.

**Diagnosis.** Helm had applied the chat-ui Deployment (with the new `checksum/config`) before the ConfigMap apply failed. The pods rolled, but read the *old* ConfigMap, so `MAX_TOKENS` was still 256 — a release that matched neither the old nor the new revision.

**Fix.** `helm rollback llm-lab 1 -n llm` restored a consistent state (it becomes a new revision; the broken one stays marked `failed`). Then the upgrade was repeated with `--force-conflicts`, and a final rollback confirmed the round trip (`MAX_TOKENS` 128, then 256, with chat-ui rolling automatically each time). Helm 4's `--rollback-on-failure` (Helm 3: `--atomic`) automates the rollback.

## 10. The custom Grafana dashboard disappeared after a reboot

**Symptom.** After a reboot, everything in the lab came back (the model on the PVC, the Helm release, kube-prometheus-stack's built-in dashboards), but the vLLM lab dashboard from [chapter 06](06-observability.md#68-grafana-built-in-dashboards-plus-a-vllm-dashboard-as-code) was gone.

**Diagnosis.** That dashboard had been created through Grafana's HTTP API. Dashboards created through the API or the web UI are stored in Grafana's internal database, which kube-prometheus-stack keeps in a temporary volume, so they are lost whenever the Grafana pod is recreated. The built-in dashboards survived because they are not in that database at all: they are ConfigMaps labeled `grafana_dashboard: "1"`, and a sidecar container in the Grafana pod loads them on every start (`kubectl -n monitoring get configmap -l grafana_dashboard=1`).

**Fix.** First, provision the dashboard the same way: the JSON from `dashboards/vllm-lab.json`, unwrapped and given the fixed UID `vllm-lab`, in a ConfigMap carrying that label. Grafana then lists it as provisioned and reloads it on every start. Second, since a hand-made ConfigMap is one more object that no release describes, move it into the Helm chart ([`templates/grafana-dashboard.yaml`](../charts/llm-lab/templates/grafana-dashboard.yaml), [chapter 09, step 9.8](09-helm.md#98-ship-the-grafana-dashboard-in-the-chart)), so the lab has one owner. A `helm upgrade` that only adds this object needs no `--force-conflicts` and restarts no pods; a rebuild needs no separate dashboard step; and `helm uninstall` removes the dashboard with the rest of the lab.

**Lesson.** Anything you create only through an application's API or UI lives as long as that application's storage. If it must survive a restart, declare it as a Kubernetes object, and give that object one owner.

## Smaller lessons

| Problem or trap | Diagnosis and fix |
| --- | --- |
| A cluster-exists check that can take the wrong branch | `kind get clusters \| grep -qx` under `set -o pipefail`: `grep -q` can exit on the first match while `kind` is still writing, `kind` gets SIGPIPE, and the pipeline fails. `up.sh` uses `grep -x ... >/dev/null`. |
| Port 5000 already in use on macOS | AirPlay Receiver owns it; the registry uses 5001. |
| `InvalidImageName` on the vLLM pod | A fresh terminal without `source lab.env` produced `localhost:5001/vllm-openai-cpu:`. `${VLLM_TAG:?run 'source lab.env' first}` makes the shell stop instead. |
| `curl localhost:8000` returned answers from the wrong server | Another process owned `127.0.0.1:8000`; `kubectl port-forward` printed only `[::1]:8000`. Find it with `lsof -nP -iTCP:8000 -sTCP:LISTEN`, or forward a different local port. |
| `kubectl rollout status --timeout=20m` gave up after 10 minutes | The Deployment's `progressDeadlineSeconds` (600 s) ends the rollout first. |
| The compile cache on the PVC barely sped up restarts (26.5 s → 23.4 s `init engine`) | On vLLM 0.30 CPU the cached artifact fails to load (`Compiling model again due to a load failure`). Kept anyway: it is correct configuration and matters on GPUs. |
| `hey` showed a 7 ms `resp wait` for multi-second answers | A streaming response sends headers before the first token, so `hey` cannot measure TTFT. Use the app's `answered ttft=` log or Prometheus. |
| A PromQL query with `and` showed one series instead of two | `and` is a set operator. Use a `__name__` regex to graph `num_requests_running` and `num_requests_waiting` together. |
| The fake GPU can vanish (for example after a Docker Desktop restart), leaving vLLM `Pending` with `Insufficient example.com/gpu` | It lives in node status. Re-run `./cluster/up.sh`, which re-applies it. |

## Key takeaways

- **Accelerator scarcity is a scheduling problem.** Taints and tolerations fence accelerator pools; extended resources are integers and never overcommitted; a surge-based rollout deadlocks when every accelerator is in use, so inference Deployments use `maxSurge: 0` or `Recreate` and keep spare capacity for upgrades.
- **Inference SLOs are measurable.** TTFT, inter-token latency, tokens/s, queue depth, and KV-cache fill come from vLLM's `/metrics` and `vllm bench serve`, and each has a PromQL query.
- **Capacity math fits on a whiteboard.** KV bytes per token = 2 × layers × KV heads × head dim × bytes per value. It predicted vLLM's reported cache size to the token.
- **Autoscale on the right signal.** CPU-based HPA misses LLM load entirely; scale model servers on queue depth or KV-cache use, and count model-load time as the real cost of scaling up from zero.
- **Cold starts are a distribution problem.** Registry mirrors, image size, pre-pulled images, persistent model caches, and fast loaders all shorten them.
- **Failure domains.** Pods tolerate an unreachable node for 300 s before eviction, and replacements need somewhere to go; spread across nodes and zones.
- **Health checks reflect the pod's own ability to serve,** not its dependencies, or one backend restart cascades into a front-end outage.
- **Troubleshooting is a method:** get → describe → logs → follow the traffic → the node → isolate layers.
