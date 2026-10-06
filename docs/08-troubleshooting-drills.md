# 08 — Break-fix troubleshooting drills

Break the working stack on purpose, diagnose it using only the ladder below, then fix it. Do each drill twice: once reading the table, once with the Fix column covered. About 60–90 minutes.

Run these drills **before** [chapter 09](09-helm.md). They change objects with kubectl and reset them from `manifests/`; once Helm owns the objects, recover with `helm rollback` instead (see the note at the end).

## The diagnostic ladder

1. `kubectl get` — what state? `Pending`, `ContainerCreating`, `CrashLoopBackOff`, `Running 0/1`?
2. `kubectl describe` — Events and Last State explain most failures.
3. `kubectl logs` — add `--previous` after a crash.
4. Follow the traffic — Service → EndpointSlices → pod readiness → DNS.
5. Go to the node — `kubectl describe node`, `kubectl top`, `crictl`, kubelet journal.
6. Isolate layers — run the same image with plain `docker run` ([chapter 03, step 3.2](03-vllm.md#32-recommended-smoke-test-the-image-outside-kubernetes)).

## The drills

Run `source lab.env` first; drill 5's fix uses `$VLLM_TAG`.

| # | Break it | What you see | Find it with | Fix |
| --- | --- | --- | --- | --- |
| 1 | `kubectl set image deploy/chat-ui chat-ui=localhost:5001/chat-ui:9.9.9` | New pod `ErrImagePull` → `ImagePullBackOff`; old pods keep serving | `kubectl describe pod <new-pod>` → Events: image not found | `kubectl rollout undo deploy/chat-ui` |
| 2 | `kubectl patch svc vllm -p '{"spec":{"selector":{"app":"vllm-typo"}}}'` | UI returns 502 "LLM unreachable"; vLLM pod is healthy | `kubectl get endpointslices -l kubernetes.io/service-name=vllm` → no endpoints; `curl localhost:8080/api/backend` | `kubectl patch svc vllm -p '{"spec":{"selector":{"app":"vllm"}}}'` |
| 3 | `kubectl patch configmap chat-ui-config --type merge -p '{"data":{"LLM_MODEL":"llama-70b"}}'` then `kubectl rollout restart deploy/chat-ui` | 502 "LLM returned HTTP 404" | `kubectl logs -l app=chat-ui --prefix --tail=-1 \| grep ERROR`; `curl localhost:8000/v1/models` via port-forward | Patch `LLM_MODEL` back to `qwen2.5-1.5b`, then `kubectl rollout restart deploy/chat-ui` |
| 4 | `kubectl set resources deploy/vllm --requests=memory=1Gi --limits=memory=2Gi` | Old pod stops (Recreate), new pod `CrashLoopBackOff`, restarts climb | `kubectl describe pod -l app=vllm` → Last State: `OOMKilled`, exit code 137 | `kubectl set resources deploy/vllm --requests=memory=12Gi --limits=memory=16Gi` |
| 5 | `kubectl patch deploy vllm --type=json -p='[{"op":"remove","path":"/spec/template/spec/tolerations"}]'` | Outage: old pod gone, new pod `Pending` | `kubectl describe pod -l app=vllm` → untolerated taint `dedicated: inference` | Re-apply: `sed "s\|__VLLM_TAG__\|${VLLM_TAG}\|" manifests/10-vllm.yaml \| kubectl apply -f -` |
| 6 | `kubectl set resources deploy/vllm --requests=cpu=64` | New pod `Pending` | `kubectl describe pod -l app=vllm` → `Insufficient cpu`; compare with `kubectl describe node llm-lab-worker2` Allocatable | `kubectl set resources deploy/vllm --requests=cpu=6` |
| 7 | `kubectl patch deploy vllm -p '{"spec":{"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}}'` then `kubectl set env deploy/vllm DEMO=1` | Rollout hangs: new pod `Pending`, old pod still serving; after 10 min `ProgressDeadlineExceeded` | `kubectl rollout status deploy/vllm`; `kubectl get rs`; describe the new pod → `Insufficient example.com/gpu` | `kubectl patch deploy vllm -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}'`, then `kubectl set env deploy/vllm DEMO-` |
| 8 | `kubectl patch deploy chat-ui --type=json -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/nope"}]'` | New pod `Running 0/1`; rollout stalls; old pods keep serving | `kubectl describe pod` → Readiness probe failed: HTTP 404; EndpointSlice shows it not ready | `kubectl rollout undo deploy/chat-ui` |
| 9 | `kubectl -n kube-system scale deploy/coredns --replicas=0` | New connections fail with 502 "LLM unreachable"; pooled connections may keep working for a while | `kubectl run netshoot --rm -it --image=nicolaka/netshoot -- dig vllm.llm.svc.cluster.local` | `kubectl -n kube-system scale deploy/coredns --replicas=2` |
| 10 | `docker stop llm-lab-worker` (a whole node dies) | Node `NotReady` within about a minute; UI unreachable; after ~5 min pods are evicted and replacements stay `Pending` | `kubectl get nodes`; `kubectl describe node llm-lab-worker` (Conditions); `kubectl get pods -o wide -w` | `docker start llm-lab-worker` |

In the table, `\|` is Markdown escaping for a pipe; type a plain `|`. `<new-pod>` means "substitute the pod name" — do not type the angle brackets (zsh reads `<` as input redirection). Get names with `kubectl get pods -o name`.

### Drill 7, step by step

```bash
# 1) Break: switch to a surge-based rollout, then change the pod template to trigger one
kubectl patch deploy vllm -p '{"spec":{"strategy":{"type":"RollingUpdate","rollingUpdate":{"maxSurge":1,"maxUnavailable":0}}}}'
kubectl set env deploy/vllm DEMO=1

# 2) Observe the deadlock: the new pod needs the only "GPU", which the old pod still holds
kubectl get rs -l app=vllm                      # new ReplicaSet: 1 desired, 0 ready; old: 1 ready
kubectl get pods -l app=vllm                    # new pod Pending, old pod Running (users still served)
kubectl rollout status deploy/vllm --timeout=30s   # times out: waiting for 1 new replica to be available
kubectl get pods -l app=vllm --field-selector=status.phase=Pending \
  -o jsonpath='{.items[*].status.conditions[?(@.type=="PodScheduled")].message}'; echo   # Insufficient example.com/gpu

# 3) Fix: allow one pod to be unavailable and no surge, so the old pod stops first and frees the GPU
kubectl patch deploy vllm -p '{"spec":{"strategy":{"rollingUpdate":{"maxSurge":0,"maxUnavailable":1}}}}'
kubectl get pods -l app=vllm -w                 # old pod Terminating, new pod scheduled, Running 0/1, then 1/1 (~1 min); Ctrl-C

# 4) Clean up: remove DEMO (one more restart), then run the reset block below to restore Recreate from Git
kubectl set env deploy/vllm DEMO-
```

The fix trades availability for progress, exactly like `Recreate`: there is a short gap with no vLLM pod while the new one loads. The alternative is spare capacity (a second accelerator) so a surge pod can start before the old one stops.

### Drill 8, step by step

```bash
# 1) Break: point the readiness probe at a path that returns 404
kubectl patch deploy chat-ui --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/readinessProbe/httpGet/path","value":"/nope"}]'

# 2) Observe
kubectl get pods -l app=chat-ui                    # one new pod Running 0/1; the two old pods still 1/1
NEW=$(kubectl get pods -l app=chat-ui --no-headers | awk '$2=="0/1"{print $1}' | head -1); echo "$NEW"
kubectl describe pod "$NEW" | grep 'Readiness:'           # http-get http://:http/nope
kubectl describe pod "$NEW" | sed -n '/Events/,$p'        # Readiness probe failed: HTTP probe failed with statuscode: 404
kubectl get endpointslices -l kubernetes.io/service-name=chat-ui \
  -o jsonpath='{range .items[*].endpoints[*]}{.targetRef.name}{"  ready="}{.conditions.ready}{"\n"}{end}'
                                                   # the new pod listed with ready=false, so kube-proxy sends it nothing
kubectl rollout status deploy/chat-ui --timeout=30s   # stalls: the new pod never becomes available

# 3) Fix
kubectl rollout undo deploy/chat-ui
```

Why exactly one new pod: chat-ui uses the default rolling update (25% surge, 25% unavailable). With 2 replicas, surge rounds up to 1 and unavailable rounds down to 0, so Kubernetes adds one new pod and removes no old pod until it is ready. Users never notice; the rollout just waits.

## Reset to the Git baseline

After each drill, confirm recovery with `curl -s localhost:8080/api/backend` and a question in the browser. If you get lost, reset:

```bash
cd ~/k8s-llm-lab && source lab.env
git status --short                       # uncommitted file edits? `git restore <file>` returns a file to the last commit
sed "s|__VLLM_TAG__|${VLLM_TAG:?run 'source lab.env' first}|" manifests/10-vllm.yaml | kubectl apply -f -
kubectl apply -f manifests/20-chat-ui.yaml -f manifests/30-vllm-servicemonitor.yaml
kubectl -n kube-system scale deploy/coredns --replicas=2     # drill 9 changes something outside manifests/
kubectl diff -f manifests/20-chat-ui.yaml                    # no output = chat-ui matches Git
kubectl get pods                                             # all Running and READY before the next drill
```

`10-vllm.yaml` always goes through `sed` because it holds the `__VLLM_TAG__` placeholder. These commands restore everything the drills touch except a stopped node (drill 10: `docker start llm-lab-worker`). One blind spot: client-side `kubectl apply` and `kubectl diff` leave alone fields that were added imperatively and never appeared in a manifest, such as an env var from `kubectl set env` (which is why drill 7 removes `DEMO` explicitly). Check those directly: `kubectl get deploy vllm -o jsonpath='{.spec.template.spec.containers[0].env[*].name}'; echo` should list only `HF_HOME VLLM_CACHE_ROOT VLLM_CPU_KVCACHE_SPACE VLLM_CPU_OMP_THREADS_BIND HF_TOKEN`.

**After chapter 09** (Helm owns the objects): do not run this reset block. Break things the same way if you like, then recover with `helm rollback llm-lab <revision> -n llm` or `helm upgrade` (see chapter 09).

## What each drill teaches

- **1 and 8:** rolling updates protect you — a bad image or bad probe never takes down the old pods.
- **2:** "pods healthy, Service dead" is almost always selectors, ports, or readiness. EndpointSlices are the truth.
- **3:** ConfigMaps consumed as env vars are read at pod start; a config change is a deploy.
- **4:** exit code 137 = SIGKILL, usually the OOM killer. Model servers need memory sized for weights + KV cache + runtime overhead.
- **5, 6, 7:** scheduling failures are always explained in the pod's Events. Drill 7 is the classic accelerator-cluster trap: with one accelerator, a surge-based rollout can never start the new pod. Use `maxSurge: 0` or `Recreate`, and plan spare capacity for upgrades.
- **9:** DNS is the first suspect when only *new* connections fail.
- **10:** pods tolerate an unreachable node for 300 s by default before eviction, and replacements need somewhere to go. Production spreads replicas across nodes and zones.

Next: [09 — Helm](09-helm.md)
