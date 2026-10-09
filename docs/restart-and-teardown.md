# Restart, rebuild, and teardown

## Pause and resume (shutdown or reboot)

You do not need to delete anything before shutting down the Mac. Afterwards, bring the lab back like this:

```bash
docker desktop start            # or open the Docker Desktop app; wait until `docker info` answers
docker start kind-registry llm-lab-control-plane llm-lab-worker llm-lab-worker2   # no-op for any already running
kubectl get nodes -w            # all three Ready, usually within 1–2 minutes; Ctrl-C
cd ~/k8s-llm-lab && ./cluster/up.sh   # repair mode: re-applies the taint and the fake GPU, which node status can lose on restart
kubectl get pods -A | grep -v Running   # only the header line once everything is up; vLLM needs about a minute to load

# Port-forwards do not survive a reboot; start the ones you need again
kubectl -n monitoring port-forward svc/kps-kube-prometheus-stack-prometheus 9090:9090 >/dev/null &
kubectl -n monitoring port-forward svc/kps-grafana 3000:80 >/dev/null &
curl -s localhost:8080/api/backend | jq -r .reachable    # true
open http://localhost:3000/d/vllm-lab                    # the vLLM lab dashboard, reloaded from its ConfigMap
```

The model on the PVC, the images in the registry, and the Helm release survive a restart. The vLLM lab Grafana dashboard comes back automatically, but only because the chart ships it as a ConfigMap labeled `grafana_dashboard: "1"` ([chapter 09, step 9.8](09-helm.md#98-ship-the-grafana-dashboard-in-the-chart)): Grafana's own database is lost when its pod is recreated, and a sidecar reloads the dashboard from the ConfigMap. A copy created through the HTTP API or the web UI ([chapter 06, step 6.8](06-observability.md#68-grafana-built-in-dashboards-plus-a-vllm-dashboard-as-code)) does not survive. If the nodes are not `Ready` after a few minutes, or the vLLM pod stays `Pending` after `up.sh`, check `kubectl describe pod -l app=vllm` and `kubectl get nodes`. The last resort is a rebuild (below), which also re-downloads the model because the PVC goes with the cluster.

## Delete the cluster

Deleting the cluster takes seconds. Keep the registry container so rebuilds skip the vLLM image pull.

```bash
kind delete cluster --name llm-lab          # nodes, pods, PVCs (including the downloaded model) are gone
docker ps --filter name=kind-registry       # registry and your pushed images survive
```

## Rebuild from scratch

```bash
cd ~/k8s-llm-lab && source lab.env
./cluster/up.sh                             # chapters 01–02: cluster, registry wiring, taint, fake GPU, metrics-server
curl -s localhost:5001/v2/_catalog          # vllm-openai-cpu and chat-ui are still there; if not, repeat 03 step 3.1 and 04 step 4.2
```

Then:

1. Install the monitoring stack: [chapter 06, step 6.5](06-observability.md#65-prometheus-and-grafana-kube-prometheus-stack) (the `helm install kps ...` command). The chart's ServiceMonitor needs its CRDs.
2. Install the lab with Helm. On a fresh cluster there is nothing to adopt. The install also recreates the ServiceMonitor and the Grafana dashboard ConfigMap ([chapter 09, step 9.8](09-helm.md#98-ship-the-grafana-dashboard-in-the-chart)); there is no separate dashboard step:

   ```bash
   helm upgrade --install llm-lab charts/llm-lab -n llm --set vllm.image.tag=$VLLM_TAG
   kubectl rollout status deploy/vllm --timeout=20m      # first start downloads the model again
   ```

   With the Grafana port-forward from 6.5 running, the dashboard is at `http://localhost:3000/d/vllm-lab` within about a minute.

To follow chapters 03–08 again instead, apply the manifests (`sed ... manifests/10-vllm.yaml | kubectl apply -f -`, then `kubectl apply -f manifests/20-chat-ui.yaml -f manifests/30-vllm-servicemonitor.yaml`) and adopt them with Helm later as in chapter 09.

Not restored automatically:

- The optional `hf-token` Secret ([chapter 03, step 3.7](03-vllm.md#37-optional-a-hugging-face-token-as-a-secret)) is deliberately not in Git or `up.sh`. The vLLM pod starts fine without it (`optional: true`).
- The monitoring stack (step 1 above). The dashboard needs no step of its own: the chart install in step 2 provisions it.

A useful drill: time the rebuild. Under 15 minutes to a working chat page, excluding the model download, is a good target.

To keep the downloaded model across cluster rebuilds, mount a Mac folder into the inference node with kind's `extraMounts` (`hostPath`: an absolute path to a folder on the Mac; `containerPath: /hf-cache`) and point the vLLM pod at it with a `hostPath` volume instead of the PVC. This is not implemented in the repository.

## Full teardown

```bash
kind delete cluster --name llm-lab
docker rm -f kind-registry                  # the registry and its images
docker image prune -f

# Free the VM's RAM when you are done for the day:
docker desktop stop
```
