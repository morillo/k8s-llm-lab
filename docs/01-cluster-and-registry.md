# 01 — Cluster and local registry

One config file and one script create a 3-node kind cluster named `llm-lab` plus a local image registry the nodes can pull from. About 10 minutes.

## 1.1 Cluster config

[`cluster/kind-config.yaml`](../cluster/kind-config.yaml) defines one control plane and two workers. The worker labels are the two "node pools"; the port mapping publishes NodePort 30080 on your Mac as `localhost:8080`.

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
name: llm-lab
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080      # NodePort used by the chat UI Service
        hostPort: 8080            # what you open in the browser
        listenAddress: "127.0.0.1"
        protocol: TCP
  - role: worker
    labels:
      pool: apps
  - role: worker
    labels:
      pool: inference
```

To lock a Kubernetes version, add `image: kindest/node:vX.Y.Z@sha256:…` under each node; kind's release notes list each node image with its digest.

## 1.2 Bootstrap script

[`cluster/up.sh`](../cluster/up.sh) follows the official kind local-registry recipe and then lays the cluster foundations:

| Section | What it does |
| --- | --- |
| 1 | Starts a `registry:3` container named `kind-registry`, published on `127.0.0.1:5001` (port 5000 is taken by macOS AirPlay Receiver) |
| 2 | Creates the cluster from `kind-config.yaml`, or skips creation if `llm-lab` already exists, so the script doubles as a repair tool |
| 3 | Writes `/etc/containerd/certs.d/localhost:5001/hosts.toml` on every node so containerd resolves `localhost:5001` to `http://kind-registry:5000` |
| 4 | Connects the registry to the `kind` Docker network so nodes can reach it by name |
| 5 | Publishes the `local-registry-hosting` ConfigMap in `kube-public` (KEP-1755 convention) |
| 6 | Namespace `llm`, inference-node taint, fake GPU, metrics-server — explained in [chapter 02](02-node-pools-and-fake-gpu.md) |

Run it:

```bash
cd ~/k8s-llm-lab
./cluster/up.sh
```

The cluster-exists guard uses `grep -x ... >/dev/null` rather than `grep -qx` on purpose: under `set -o pipefail`, `grep -q` can exit on the first match while `kind` is still writing, `kind` dies of SIGPIPE, and the `if` takes the wrong branch. Every other section is idempotent (`mkdir -p`, file overwrite, guarded network connect, `kubectl apply`).

## 1.3 Verify

kind switches your kubectl context to `kind-llm-lab`, and `up.sh` sets its default namespace to `llm`.

```bash
kubectl config current-context                 # kind-llm-lab
kubectl get nodes -o wide -L pool              # 3 nodes Ready; POOL column shows apps / inference
kubectl get pods -A                            # CoreDNS, kube-proxy, kindnet, local-path-provisioner, metrics-server
kubectl get storageclass                       # "standard (default)" = local-path, WaitForFirstConsumer
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'   # each node is a container
curl -s localhost:5001/v2/_catalog             # {"repositories":[]} = registry reachable
```

Look one level down, as you would on a real node: each kind node runs containerd, and `crictl` is the node-level CLI for when kubectl is not enough.

```bash
docker exec llm-lab-worker crictl ps            # containers on that node
docker exec llm-lab-worker crictl images        # images cached on that node
docker exec llm-lab-worker systemctl status kubelet --no-pager | head -5
```

## 1.4 Test the registry path end to end

Before trusting the registry with the ~0.8 GB vLLM image, push a tiny image and run it as a pod. This exercises the `hosts.toml` redirect, which the checks above do not.

```bash
printf 'FROM alpine\nCMD ["echo","registry path OK"]\n' | docker build -q -t localhost:5001/hello:test -
docker push localhost:5001/hello:test
curl -s localhost:5001/v2/_catalog                     # {"repositories":["hello"]}
docker exec llm-lab-worker cat /etc/containerd/certs.d/localhost:5001/hosts.toml   # [host."http://kind-registry:5000"]

kubectl run hello --image=localhost:5001/hello:test --restart=Never
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/hello --timeout=60s
kubectl logs hello                                      # registry path OK
kubectl delete pod hello
```

`registry path OK` proves the whole chain: Mac → registry container → `kind` Docker network → node containerd → pod.

## Key takeaways

- A cluster defined in a file and a script is rebuildable in about a minute; nothing depends on GUI settings.
- A local registry mirrors the real build → push → pull path and avoids `kind load` quirks and public-registry rate limits.
- Make bootstrap scripts idempotent so the same script creates and repairs.

Next: [02 — Node pools and a fake GPU](02-node-pools-and-fake-gpu.md)
