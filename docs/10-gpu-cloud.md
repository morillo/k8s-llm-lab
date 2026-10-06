# 10 — From laptop to GPU cloud

> **Status: not yet run.** Everything in this chapter is a plan derived from the Mac lab, not a measured result. Commands and values here have not been executed on a GPU cluster, and the GPU values file below is **not** in the repository.

The application manifests barely change; the platform underneath them changes completely. The useful skill is being able to say exactly which layers change.

Docker Desktop runs containers inside a Linux VM, and the Mac's GPU is not exposed to Linux containers, so the lab runs vLLM on its CPU backend. Every Kubernetes mechanism in chapters 01–09 is real; the accelerator driver layer is what this chapter adds.

## What changes, layer by layer

| Layer | This Mac lab | On a GPU cloud |
| --- | --- | --- |
| Nodes | kind containers inside one VM | GPU servers; some providers (for example CoreWeave) run Kubernetes directly on bare metal |
| Accelerator exposure | Fake `example.com/gpu` patched onto node status | NVIDIA GPU Operator: driver, container toolkit, device plugin (`nvidia.com/gpu`), GPU Feature Discovery labels, DCGM exporter |
| Pod spec | `limits: {example.com/gpu: "1"}` | `limits: {nvidia.com/gpu: "1"}` plus a toleration for the GPU taint |
| vLLM image | `vllm/vllm-openai-cpu` (arm64) | `vllm/vllm-openai` (CUDA); arm64 builds exist for Grace-based GH200/GB200 hosts |
| Model loading | PVC on local disk, download on first start | Shared filesystems or object storage, plus fast loaders such as [Tensorizer](https://docs.vllm.ai/en/stable/models/extensions/tensorizer/) (built into vLLM) |
| Multi-GPU | None | `--tensor-parallel-size` over NVLink; multi-node over InfiniBand/RDMA with NCCL |
| Metrics | vLLM + kubelet | Add DCGM: GPU utilization, memory, power, XID errors |
| Batch and training | Not covered | Slurm-on-Kubernetes offerings and managed Kubernetes/Slurm from GPU clouds (for example CoreWeave SUNK, Lambda Managed Kubernetes and Managed Slurm) |

## The vLLM pod diff

```yaml
          image: vllm/vllm-openai:<version>          # CUDA image instead of the CPU one
          args:
            - meta-llama/Llama-3.1-8B-Instruct       # gated: needs the hf-token Secret (chapter 03, step 3.7)
            - --served-model-name=llama-3.1-8b
            - --max-model-len=8192
            - --gpu-memory-utilization=0.90
          resources:
            limits: {nvidia.com/gpu: "1", memory: 32Gi}
      tolerations:
        - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
      # remove: VLLM_CPU_* env vars and the SYS_NICE capability
```

## One chart, two environments

With the chart from [chapter 09](09-helm.md), only a values file changes. A proposed `values-gpu.yaml` (not in the repository):

```yaml
vllm:
  image:
    repository: vllm/vllm-openai            # CUDA image; pass its tag with --set vllm.image.tag=...
  model: meta-llama/Llama-3.1-8B-Instruct   # gated: create the hf-token Secret first
  servedModelName: llama-3.1-8b
  maxModelLen: 8192
  extraArgs: ["--gpu-memory-utilization=0.90"]
  env: null                                 # drop the CPU-only variables
  resources:
    requests: {cpu: "4", memory: 24Gi}
    limits: {memory: 32Gi, nvidia.com/gpu: "1", example.com/gpu: null}
  nodeSelector: {pool: null}
  tolerations:
    - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
  capabilities: []
chatUi:
  image:
    repository: <your-registry>/chat-ui     # a registry the GPU cluster can pull from; build multi-arch
```

Intended install: `helm upgrade --install llm-lab charts/llm-lab -n llm -f values-gpu.yaml --set vllm.image.tag=<cuda-tag>`. Helm deep-merges maps, so a key from `values.yaml` survives unless you set it to `null`; that is why `example.com/gpu: null` and `pool: null` appear. Lists such as `tolerations` are replaced whole.

## GPU sizing with the KV-cache formula

Using the formula from [chapter 03, step 3.6](03-vllm.md#36-kv-cache-sizing-why-4-gib-is-plenty): Llama-3.1-8B has 32 layers, 8 KV heads, head dimension 128, so the bf16 KV cache costs 2 × 32 × 8 × 128 × 2 = 131,072 bytes (128 KiB) per token. On a 24 GB GPU, ~16 GB goes to weights, leaving roughly 4–5 GB for KV cache: about 35,000 tokens, or only ~8 concurrent 4k-token conversations. That arithmetic is why memory capacity, FP8, and prefix caching dominate GPU choice for inference.

## Low-cost ways to run it

Prices change; check the provider's page before launching.

| Option | What you practice | Cost notes |
| --- | --- | --- |
| AWS: one GPU EC2 instance + k3s | Real `nvidia.com/gpu` scheduling on 1× L4 24 GB (g6.xlarge) or 1× A10G 24 GB (g5.xlarge); the Deep Learning AMI ships the NVIDIA driver | Hourly; Spot is cheaper; no control-plane fee; terminate the same day |
| AWS: EKS + GPU node group | Managed control plane, node groups or Karpenter, IAM | Adds a per-cluster hourly fee plus the GPU node |
| Lambda on-demand GPU instance + k3s | The same lab on another GPU cloud, with drivers preinstalled | Hourly, per-instance pricing |

## The next layer: llm-d (optional)

llm-d is a Kubernetes-native distributed inference stack built on vLLM. It adds what a single vLLM Deployment lacks at scale:

- **Inference-aware routing.** A gateway (Gateway API + the Inference Extension) asks an endpoint picker (EPP) which vLLM replica should take each request, using live queue depth and KV-cache/prefix-cache state instead of random choice. That is the gap from [chapter 07](07-scaling-rollouts-hpa.md#71-scale-the-ui): a plain Service cannot see that one replica is saturated.
- **Prefill/decode disaggregation.** Separate pools for prompt processing and token generation, with KV cache transferred between them — the interference measured in [chapter 06](06-observability.md#67-generate-load-and-watch-the-queue-form).
- **Wide expert parallelism** for large mixture-of-experts models across many GPUs.

llm-d publishes a lightweight vLLM simulator (`ghcr.io/llm-d/llm-d-inference-sim`) that speaks the OpenAI API, emits `vllm:*` metrics, and fakes TTFT and inter-token latency without a GPU; its "simulated accelerators" guide deploys the gateway, EPP, and simulator with Helm. Before trying it locally, confirm the images have an arm64 variant:

```bash
docker buildx imagetools inspect ghcr.io/llm-d/llm-d-inference-sim:<tag> | grep -E 'Platform|linux/'
```

If arm64 is listed, run the guide in a separate kind cluster (`kind create cluster --name llm-d-sim`) so it does not disturb this lab; otherwise run it on the GPU host. What to observe: the EPP's routing decisions in its logs, and how requests sharing a long prompt prefix stick to the same replica.

## References for this chapter

- [vLLM — Loading models with Tensorizer](https://docs.vllm.ai/en/stable/models/extensions/tensorizer/)
- [llm-d inference simulator](https://github.com/llm-d/llm-d-inference-sim)
- [llm-d — Simulated accelerators guide](https://llm-d.ai/docs/guide/Installation/simulated-accelerators)
- [Lambda — Managed Kubernetes on 1-Click Clusters](https://docs.lambda.ai/managed-kubernetes/managed-kubernetes-legacy/)

Next: [11 — Lessons learned](11-lessons-learned.md)
