# Command cheat sheet

The commands from this lab worth knowing by heart, roughly in the order you reach for them.

Angle-bracket placeholders such as `<pod>` mean "substitute a name". Do not type the brackets: zsh reads `<` as input redirection. Get names with `kubectl get pods -o name`, or use the `deploy/<name>` form where kubectl accepts it (`logs`, `exec`, `port-forward`). In the tables, `\|` is a Markdown-escaped `|`.

## kubectl

| Command | Use it to |
| --- | --- |
| `kubectl config get-contexts` / `kubectl config use-context kind-llm-lab` | See and switch clusters |
| `kubectl config set-context --current --namespace=llm` | Set a default namespace |
| `kubectl get nodes -o wide -L pool` | Node health, IPs, pool labels |
| `kubectl get all,endpointslices,pvc -o wide` | One-screen view of a namespace |
| `kubectl get pods -w` | Watch status transitions live |
| `kubectl describe pod <pod>` | Events, probe failures, Last State, scheduling reasons |
| `kubectl events --watch` / `kubectl get events -A --sort-by=.lastTimestamp` | What just happened, cluster-wide |
| `kubectl logs <pod> -c <container> --previous` | Logs of the crashed container |
| `kubectl logs -l app=chat-ui --prefix --tail=-1` | Logs from all pods with a label (`-l` defaults to 10 lines per pod) |
| `kubectl exec -it <pod> -- sh` | Shell into a running container |
| `kubectl debug -it <pod> --image=nicolaka/netshoot --target=<container>` | Ephemeral debug container sharing the pod's process namespace |
| `kubectl run netshoot --rm -it --image=nicolaka/netshoot -- bash` | Throwaway pod for DNS and HTTP tests |
| `kubectl port-forward svc/vllm 8000:8000` | Reach a ClusterIP Service from the Mac |
| `kubectl top nodes` / `kubectl top pods --containers` | Live CPU and memory (metrics-server) |
| `kubectl describe node llm-lab-worker2` | Taints, capacity vs allocatable, allocated requests |
| `kubectl taint nodes <node> key=value:NoSchedule` (append `-` to remove) | Fence off a node pool |
| `kubectl cordon <node>` / `kubectl drain <node> --ignore-daemonsets` / `kubectl uncordon <node>` | Node maintenance |
| `kubectl scale deploy/chat-ui --replicas=N` | Manual scaling |
| `kubectl set image` / `set resources` / `set env` | Imperative spec changes (each triggers a rollout) |
| `kubectl rollout status` / `history` / `undo` / `restart` `deploy/<name>` | Manage rollouts |
| `kubectl autoscale deploy/chat-ui --cpu=60% --min=2 --max=6` | Create an HPA |
| `kubectl get <kind> <name> -o yaml` | Full live object, including status |
| `kubectl get <kind> <name> --show-managed-fields -o yaml` | Who owns which fields (server-side apply) |
| `kubectl explain deployment.spec.strategy --recursive` | Built-in API reference |
| `kubectl diff -f manifests/20-chat-ui.yaml` | Preview what an apply would change |
| `kubectl apply --dry-run=server -f <file>` | Validate with the API server without persisting |
| `kubectl auth can-i <verb> <resource>` | RBAC checks |

## Lab-specific

| Command | Use it to |
| --- | --- |
| `source lab.env` | Load `VLLM_TAG` in a new terminal |
| `sed "s\|__VLLM_TAG__\|${VLLM_TAG:?run 'source lab.env' first}\|" manifests/10-vllm.yaml \| kubectl apply -f -` | Apply the vLLM manifest (pre-Helm) |
| `./cluster/up.sh` | Create the cluster, or repair taint / fake GPU / metrics-server on an existing one |
| `curl -s localhost:8080/api/backend \| jq` | Is the UI able to reach vLLM? |
| `curl -s localhost:5001/v2/_catalog` | What is in the local registry |

## Nodes, containers, kind

| Command | Use it to |
| --- | --- |
| `docker exec llm-lab-worker2 crictl ps` / `crictl images` / `crictl stats` | Container runtime view on a node |
| `docker exec llm-lab-worker2 journalctl -u kubelet -n 40 --no-pager` | Kubelet logs on a node |
| `docker stats --no-stream` | Per-node resource use (each kind node is a container) |
| `kind get clusters` / `kind delete cluster --name llm-lab` | Manage kind clusters |

## Helm

| Command | Use it to |
| --- | --- |
| `helm lint charts/llm-lab --set vllm.image.tag=$VLLM_TAG` | Check the chart |
| `helm template llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG \| kubectl diff -f -` | Compare the rendered chart with the live cluster |
| `helm upgrade llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG --force-conflicts` | Upgrade (force needed for fields shared since adoption) |
| `helm history llm-lab -n llm` / `helm rollback llm-lab <revision> -n llm` | Release history and rollback |
| `helm list -A` / `helm get values llm-lab -n llm --all` | Inspect releases and their values |

## Observability

| Command | Use it to |
| --- | --- |
| `stern 'chat-ui\|vllm' --exclude 'GET /health' --tail 0` | Live, color-coded multi-pod tail without probe noise |
| `k9s -n llm` | Terminal UI |
| `kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null &` | Prometheus on `localhost:9090` |
| `kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null &` | Grafana on `localhost:3000` |
| `curl -s localhost:9090/api/v1/query --data-urlencode 'query=<promql>' \| jq` | PromQL from the CLI |
