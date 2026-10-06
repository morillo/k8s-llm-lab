# 09 — Package the lab as a Helm chart

One chart replaces the three manifests and the `sed` workaround, rolls pods automatically when configuration changes, and keeps its own release history for upgrades and rollbacks. From this chapter on, Helm is the only tool that changes these objects. About 60 minutes.

Requires Helm 4 (tested with v4.3). Helm 4 installs with server-side apply, which matters in 9.5.

## 9.1 Chart skeleton

[`charts/llm-lab/Chart.yaml`](../charts/llm-lab/Chart.yaml): `name: llm-lab`, chart `version: 0.1.0` (the chart's own version), `appVersion: "0.2.0"` (the chat-ui version it ships).

## 9.2 Values: everything that differs between environments

[`charts/llm-lab/values.yaml`](../charts/llm-lab/values.yaml); the defaults are this Mac lab. The main keys:

| Key | Default |
| --- | --- |
| `vllm.image.repository` / `vllm.image.tag` | `localhost:5001/vllm-openai-cpu` / `""` — empty on purpose and **required**: pass `--set vllm.image.tag=$VLLM_TAG` from `lab.env` |
| `vllm.model`, `vllm.servedModelName` | `Qwen/Qwen2.5-1.5B-Instruct`, `qwen2.5-1.5b` |
| `vllm.dtype`, `vllm.maxModelLen`, `vllm.maxNumSeqs`, `vllm.extraArgs` | `bfloat16`, `4096`, `8`, `[]` |
| `vllm.env` | `VLLM_CPU_KVCACHE_SPACE: "4"`, `VLLM_CPU_OMP_THREADS_BIND: auto` |
| `vllm.resources` | requests `cpu: "6", memory: 12Gi`; limits `memory: 16Gi, example.com/gpu: "1"` |
| `vllm.nodeSelector`, `vllm.tolerations`, `vllm.capabilities` | `{pool: inference}`, the `dedicated=inference:NoSchedule` toleration, `[SYS_NICE]` |
| `vllm.cache.size`, `vllm.cache.storageClassName` | `20Gi`, `standard` |
| `chatUi.image`, `chatUi.replicas`, `chatUi.maxTokens`, `chatUi.nodePort` | `localhost:5001/chat-ui:0.2.0`, `2`, `"256"`, `30080` |
| `monitoring.serviceMonitor` | `true` |

## 9.3 Templates, lint, and a preview against the live cluster

| Template | Contents |
| --- | --- |
| [`templates/vllm.yaml`](../charts/llm-lab/templates/vllm.yaml) | PVC `hf-cache` (annotated `helm.sh/resource-policy: keep`, so `helm uninstall` leaves the downloaded model in place), Deployment and Service `vllm` |
| [`templates/chat-ui-configmap.yaml`](../charts/llm-lab/templates/chat-ui-configmap.yaml) | ConfigMap `chat-ui-config`; `LLM_BASE_URL` is built from `.Release.Namespace`, `LLM_MODEL` from `vllm.servedModelName` |
| [`templates/chat-ui.yaml`](../charts/llm-lab/templates/chat-ui.yaml) | Deployment and Service `chat-ui`, with a `checksum/config` pod annotation |
| [`templates/servicemonitor.yaml`](../charts/llm-lab/templates/servicemonitor.yaml) | ServiceMonitor `vllm`, rendered only if `monitoring.serviceMonitor` is true |

The templates produce the same objects as `manifests/`, with the same names (`vllm`, `chat-ui`, `chat-ui-config`, `hf-cache`), so Helm can adopt the running objects in 9.4 instead of recreating them.

The `checksum/config` annotation is Helm's answer to [chapter 07, step 7.4](07-scaling-rollouts-hpa.md#74-config-changes-need-a-restart): when the ConfigMap's content changes, its checksum changes, which changes the pod template, so the upgrade rolls the pods by itself.

Check that the chart renders and lints cleanly, then preview it against the live objects:

```bash
cd ~/k8s-llm-lab && source lab.env
helm lint charts/llm-lab --set vllm.image.tag=$VLLM_TAG     # "[INFO] Chart.yaml: icon is recommended" is harmless
helm template llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG | less   # exactly what Helm will send

# Read-only preview against the live objects, before changing anything in 9.4
helm template llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG | kubectl diff -f -
```

Expected differences: the PVC's `helm.sh/resource-policy` annotation and chat-ui's new `checksum/config` annotation (plus `generation` bumps). Leftover drift from chapters 07–08 also shows up here, for example a vLLM `strategy` of `RollingUpdate` from drill 7; step 2 of 9.5 removes it. Any change *inside* the vLLM pod template means the chart renders it differently from `manifests/10-vllm.yaml`, and vLLM would restart once on install.

`kubectl diff` cannot show fields that were added imperatively and never appeared in any manifest (for example an env var from `kubectl set env`). Check those directly:

```bash
kubectl get deploy vllm -o jsonpath='{.spec.template.spec.containers[0].env[*].name}'; echo
# expected: HF_HOME VLLM_CACHE_ROOT VLLM_CPU_KVCACHE_SPACE VLLM_CPU_OMP_THREADS_BIND HF_TOKEN
```

> **Fresh cluster instead?** If nothing from chapters 03–08 is deployed, skip 9.4 and run the install in 9.5 step 3 directly; `--force-conflicts` is harmless there. The chart's ServiceMonitor needs the Prometheus Operator CRDs from [chapter 06](06-observability.md#65-prometheus-and-grafana-kube-prometheus-stack); without them, add `--set monitoring.serviceMonitor=false`.

## 9.4 Adopt the running objects instead of recreating them

`helm install` refuses to touch objects it did not create, and deleting them would delete the PVC and the downloaded model with it. Mark them as belonging to the release instead; Helm checks this label and these two annotations before taking ownership:

```bash
for r in pvc/hf-cache deploy/vllm svc/vllm configmap/chat-ui-config deploy/chat-ui svc/chat-ui servicemonitor/vllm; do
  kubectl label "$r" app.kubernetes.io/managed-by=Helm --overwrite
  kubectl annotate "$r" meta.helm.sh/release-name=llm-lab meta.helm.sh/release-namespace=llm --overwrite
done
```

## 9.5 Preview, then install

Helm 4 installs with server-side apply, which records an owner ("field manager") for every field. Because these objects already existed, Helm cannot become their only owner: wherever the chart's value equals the live value, ownership stays shared with an earlier manager — one of the kubectl managers from chapters 03–08, or `before-first-apply`, which the API server creates for fields that existed before an object's first server-side apply. So the install uses `--force-conflicts` to take over the fields that differ, and every later upgrade that changes a shared field needs it too (9.6). Objects that Helm creates itself do not have this problem. Background: [lessons learned](11-lessons-learned.md#8-helm-4-server-side-apply-and-adopted-objects).

```bash
# 1) Who owns the fields today: kubectl-client-side-apply, kubectl-patch, kubectl-set, kubectl-rollout ...
kubectl get deploy chat-ui --show-managed-fields \
  -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"  "}{.operation}{"\n"}{end}'

# 2) Only if the 9.3 preview showed vLLM's strategy as RollingUpdate (drift from drill 7): restore Recreate first
kubectl patch deploy vllm --type=json \
  -p='[{"op":"remove","path":"/spec/strategy/rollingUpdate"},{"op":"replace","path":"/spec/strategy/type","value":"Recreate"}]'

# 3) Install
helm upgrade --install llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG --force-conflicts
helm list -n llm
kubectl get pods -w      # chat-ui rolls once (new checksum annotation); vLLM keeps its age. Ctrl-C when chat-ui is 1/1

# 4) Verify: chart and cluster in sync, Helm now owns the fields
kubectl get deploy vllm -o jsonpath='{.spec.strategy.type}'; echo        # Recreate
helm template llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG | kubectl diff -f - && echo "in sync"
kubectl get deploy chat-ui --show-managed-fields \
  -o jsonpath='{range .metadata.managedFields[*]}{.manager}{"  "}{.operation}{"\n"}{end}'   # now includes helm
```

Why step 2: server-side apply only changes the fields Helm declares. Drill 7's `rollingUpdate` settings are owned by `kubectl-patch`, so Helm would set `type: Recreate` but could leave them in place, and the API server can reject that combination (`spec.strategy.rollingUpdate: Forbidden: may not be specified when strategy type is 'Recreate'`). Removing them with the tool that added them avoids the problem; it changes no pod template, so vLLM does not restart. Skip step 2 if there is no drift — the `remove` operation fails when the path does not exist.

Expected install output: `Release "llm-lab" does not exist. Installing it now.`, then `STATUS: deployed` and `REVISION: 1`. If the install reports a conflict, or an ownership error naming one of the seven objects, a label or annotation from 9.4 did not take.

## 9.6 Practice the Helm workflow

```bash
# A config change now rolls the pods automatically (step 7.4 needed a manual restart).
# --force-conflicts: MAX_TOKENS existed before Helm adopted the ConfigMap, so its ownership is shared (see 9.5);
# forcing hands the changed fields to Helm alone
helm upgrade llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG --set chatUi.maxTokens=128 --force-conflicts
kubectl get pods -l app=chat-ui -w                    # chat-ui rolls by itself; Ctrl-C when both pods are 1/1
kubectl exec deploy/chat-ui -- printenv MAX_TOKENS    # 128

helm history llm-lab -n llm                           # one line per revision, newest last
helm rollback llm-lab 1 -n llm                        # back to revision 1's values (256); no force needed, Helm now owns those fields
kubectl get pods -l app=chat-ui -w                    # rolls again; Ctrl-C when both pods are 1/1
kubectl exec deploy/chat-ui -- printenv MAX_TOKENS    # 256
helm get values llm-lab -n llm --all | head -20       # the values the release runs with
```

`helm upgrade` uses `values.yaml` plus only the flags given on that command; earlier `--set` flags are not remembered unless you add `--reuse-values`. That is why every command repeats `--set vllm.image.tag=$VLLM_TAG`.

If an upgrade fails partway, Helm marks that revision `failed`, and objects applied before the failure stay changed. `helm rollback` restores a consistent state (it becomes a new revision; `helm history` shows `failed` for the broken one). Helm 4 can do this automatically with `--rollback-on-failure` (the Helm 3 name was `--atomic`). See [lessons learned](11-lessons-learned.md#9-a-partial-helm-upgrade-failure).

## 9.7 From here on, Helm owns these objects

Do not also run `kubectl apply -f manifests/` or the chapter 08 reset block against them: two tools changing the same objects is drift by design. Change things with `helm upgrade` and recover with `helm rollback`; `manifests/` stays in the repo as the record of chapters 03–08. `helm uninstall llm-lab -n llm` removes everything except the PVC, thanks to `resource-policy: keep`.

The payoff — one chart, two environments, with only a values file changing — is sketched in [chapter 10](10-gpu-cloud.md#one-chart-two-environments).

## Key takeaways

- Helm can adopt live objects (label + two annotations) without recreating them, which matters when a PVC holds gigabytes of model weights.
- A config checksum in the pod template turns "config change" into "rollout" automatically.
- With Helm 4's server-side apply, adopted objects keep shared field ownership; `--force-conflicts` is the deliberate way to take a field over.
- A failed upgrade can leave a partial state; `helm rollback` (or `--rollback-on-failure`) returns to a consistent revision.
- One tool owns an object. Mixing kubectl and Helm on the same objects is drift by design.

Next: [10 — From laptop to GPU cloud](10-gpu-cloud.md)
