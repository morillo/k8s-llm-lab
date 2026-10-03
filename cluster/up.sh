#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
CLUSTER=llm-lab
REG_NAME=kind-registry
REG_PORT=5001

# 1) Registry container: localhost:5001 on the Mac -> port 5000 in the container
if [ "$(docker inspect -f '{{.State.Running}}' "$REG_NAME" 2>/dev/null || true)" != 'true' ]; then
  docker run -d --restart=always -p "127.0.0.1:${REG_PORT}:5000" \
    --network bridge --name "$REG_NAME" registry:3
fi

# 2) Cluster (skipped if it already exists, so the script can be re-run as a repair)
if kind get clusters 2>/dev/null | grep -x "$CLUSTER" >/dev/null; then
  echo "Cluster $CLUSTER already exists; skipping create"
  kubectl config use-context "kind-$CLUSTER"
else
  kind create cluster --config kind-config.yaml
fi

# 3) Tell each node's containerd that "localhost:5001" means the registry container
for node in $(kind get nodes --name "$CLUSTER"); do
  docker exec "$node" mkdir -p "/etc/containerd/certs.d/localhost:${REG_PORT}"
  printf '[host."http://%s:5000"]\n' "$REG_NAME" | \
    docker exec -i "$node" cp /dev/stdin "/etc/containerd/certs.d/localhost:${REG_PORT}/hosts.toml"
done

# 4) Attach the registry to the "kind" Docker network so nodes can reach it by name
if [ "$(docker inspect -f '{{json .NetworkSettings.Networks.kind}}' "$REG_NAME")" = 'null' ]; then
  docker network connect kind "$REG_NAME"
fi

# 5) Advertise the registry to tools (KEP-1755 convention)
kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: local-registry-hosting
  namespace: kube-public
data:
  localRegistryHosting.v1: |
    host: "localhost:${REG_PORT}"
    help: "https://kind.sigs.k8s.io/docs/user/local-registry/"
EOF

# 6) Namespace, inference-pool taint, fake GPU, metrics-server
kubectl create namespace llm --dry-run=client -o yaml | kubectl apply -f -
kubectl config set-context --current --namespace=llm
INF_NODE=$(kubectl get nodes -l pool=inference -o jsonpath='{.items[0].metadata.name}')
kubectl taint nodes "$INF_NODE" dedicated=inference:NoSchedule --overwrite
kubectl patch node "$INF_NODE" --subresource=status --type=json \
  -p='[{"op":"add","path":"/status/capacity/example.com~1gpu","value":"1"}]'
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl -n kube-system rollout status deployment/metrics-server
echo "Cluster $CLUSTER is up. Context: kind-$CLUSTER"
