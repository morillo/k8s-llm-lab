# 02 — Node pools and a simulated GPU

Create the app namespace, fence off the inference node the way GPU clouds do, give it one fake "GPU", and install metrics-server. About 10 minutes.

`./cluster/up.sh` already did all of this in its section 6. This chapter explains each command and verifies the result. Every command below is safe to run again by hand.

## 2.1 Namespace and default namespace

```bash
kubectl create namespace llm --dry-run=client -o yaml | kubectl apply -f -
kubectl config set-context --current --namespace=llm   # later commands can omit -n llm
```

The `set-context` line is in `up.sh` because `kind delete cluster` removes the context; a rebuilt cluster would otherwise default to the `default` namespace.

## 2.2 Taint the inference node

Only pods that tolerate the taint may land there. GPU clouds do the same so CPU-only pods never occupy a GPU host.

```bash
INF_NODE=$(kubectl get nodes -l pool=inference -o jsonpath='{.items[0].metadata.name}')
echo "$INF_NODE"                                                    # llm-lab-worker2
kubectl taint nodes "$INF_NODE" dedicated=inference:NoSchedule --overwrite
kubectl describe node "$INF_NODE" | grep -A2 Taints
```

## 2.3 Advertise one fake GPU

On a real GPU node, the vendor's device plugin advertises the accelerators as an *extended resource* (see [chapter 10](10-gpu-cloud.md)). You can advertise one by hand on the node's status, and the scheduler treats it identically: integer-only, never overcommitted.

```bash
kubectl patch node "$INF_NODE" --subresource=status --type=json \
  -p='[{"op":"add","path":"/status/capacity/example.com~1gpu","value":"1"}]'

# ~10 s later the kubelet reflects it in Allocatable
kubectl describe node "$INF_NODE" | grep -E 'example.com/gpu'
```

`~1` is JSON-Patch escaping for `/`. The patch lives in node status, not in any config file, so it must be re-applied after every cluster rebuild — which is why it is in `up.sh`.

## 2.4 metrics-server

metrics-server feeds `kubectl top` and the Horizontal Pod Autoscaler. kind's kubelets use self-signed serving certificates, so the lab adds `--kubelet-insecure-tls`; never do that in production.

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl -n kube-system rollout status deployment/metrics-server
kubectl top nodes                                   # works after ~60 s
```

Running the `apply` + `patch` pair again is safe: `apply` resets the container args to the manifest, then the patch re-adds the flag, so it always ends up exactly once. Optional: to pin metrics-server, read the version you got with `kubectl -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].image}'` and change `releases/latest/download/` to `releases/download/vX.Y.Z/` in `up.sh`.

## 2.5 Observe where things landed and why

```bash
kubectl get pods -A -o wide --sort-by=.spec.nodeName
kubectl describe node llm-lab-control-plane | grep -A2 Taints   # control-plane NoSchedule taint
```

Nothing new lands on the control plane (its own taint) or on `llm-lab-worker2` (your taint), so regular pods all go to `llm-lab-worker`. That is node-pool design in miniature.

## 2.6 `up.sh` as a repair tool

Run `./cluster/up.sh` against the live cluster. Expected output and what it means:

| Output line | Meaning |
| --- | --- |
| `Cluster llm-lab already exists; skipping create` | The section 2 guard worked |
| `namespace/llm unchanged` | Idempotent. If the namespace was ever created imperatively, the first `apply` prints a one-time warning about the missing `last-applied-configuration` annotation and `configured`; `apply` adds the annotation it needs for its three-way merge. |
| `node/llm-lab-worker2 modified` | `--overwrite` always rewrites the taint, with the same value |
| `node/llm-lab-worker2 patched (no change)` | The fake GPU was already there; JSON-Patch `add` replaced it with itself |
| `deployment.apps/metrics-server configured`, then `patched` | The args reset and the flag re-added; a brief metrics-server restart is the only side effect |

Verify nothing regressed:

```bash
kubectl describe node llm-lab-worker2 | grep -E 'Taints|example.com/gpu'
kubectl -n kube-system get deploy metrics-server -o jsonpath='{.spec.template.spec.containers[0].args}'; echo
kubectl top nodes
```

If `example.com/gpu` ever disappears (for example after Docker Desktop restarts), the vLLM pod goes `Pending` with `Insufficient example.com/gpu`. Re-run `./cluster/up.sh`. On a real GPU node, a device-plugin restart behaves the same way: the resource reads zero until the plugin re-registers it.

## Key takeaways

- Labels choose a pool; taints keep everything else out; tolerations let the right workloads in.
- Extended resources are integers and never overcommitted, so one fake `example.com/gpu` reproduces accelerator scarcity faithfully.
- Node *status* is not configuration: anything patched there must be re-applied by automation.

Next: [03 — vLLM](03-vllm.md)
