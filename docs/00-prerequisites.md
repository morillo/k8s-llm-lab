# 00 — Prerequisites

Install the CLI tools, size the Docker Desktop VM, and confirm the VM exposes BF16 to Linux. About 15 minutes.

## 0.1 Install the tools

`kubernetes-cli` provides `kubectl`; `hey` is an HTTP load generator; `stern` tails logs across pods; `k9s` is a terminal UI.

```bash
brew update
brew install kubernetes-cli kind helm k9s stern jq hey

# Docker Desktop also ships a kubectl. Make sure Homebrew's comes first (/opt/homebrew/bin):
which -a kubectl
kubectl version --client
kind version
helm version --short
docker version --format 'Docker Engine {{.Server.Version}}'
```

Tested with kubectl v1.37, kind v0.33 (Kubernetes v1.37 nodes), and Helm v4.3. Chapter 09 relies on Helm 4 behaviour (server-side apply); Helm 3 will not behave the same way.

## 0.2 Let zsh accept `#` comments

The command blocks in these docs use trailing `# comments`. zsh, the macOS default shell, does **not** treat `#` as a comment in an interactive session, so a pasted comment becomes extra arguments (for example `helm history` then fails with `requires 1 argument`). Enable comments once:

```bash
echo 'setopt interactivecomments' >> ~/.zshrc && source ~/.zshrc
```

## 0.3 Size the Docker Desktop VM

Every kind node is a container inside Docker Desktop's Linux VM, so the VM is your "data center". Check what it has now:

```bash
docker info --format '{{.NCPU}} CPUs'
docker info --format '{{.MemTotal}}' | awk '{printf "%.0f GiB RAM\n", $1/1024/1024/1024}'
```

Target **12–16 CPUs, 48 GB RAM, 150 GB disk**. Docker Desktop exposes these limits only in the app: Settings → Resources → Advanced → CPU limit, Memory limit, Disk usage limit → Apply & restart. Re-run the two commands to confirm.

Do not skip this. Docker Desktop's default memory can be as low as 8 GiB, and at that size the vLLM pod stays `Pending` with `Insufficient memory`. If Docker Desktop already shows all of your cores, keep that; it does not reserve them away from macOS.

All measured numbers in these docs come from an Apple M4 Max with a 16-vCPU / 48 GB Docker VM.

## 0.4 Check BF16 support inside the VM

vLLM's CPU backend runs the model in bfloat16 only if the CPU exposes it; Apple M2 and later do.

```bash
docker run --rm alpine grep -o -m1 -w bf16 /proc/cpuinfo
```

If it prints `bf16`, keep `--dtype=bfloat16` (the default in `manifests/10-vllm.yaml` and `charts/llm-lab/values.yaml`). If it prints nothing, use `--dtype=float32` and double the vLLM memory request and limit.

## 0.5 Get the repository

```bash
git clone <repository-url> ~/k8s-llm-lab     # the HTTPS or SSH URL of this repository
cd ~/k8s-llm-lab
cat lab.env                                  # export VLLM_TAG=v0.30.0-arm64
```

All later chapters assume you are in `~/k8s-llm-lab`. `lab.env` pins the vLLM CPU image tag so every terminal and every rebuild uses the same version; run `source lab.env` in each new terminal before commands that use `$VLLM_TAG`.

Next: [01 — Cluster and registry](01-cluster-and-registry.md)
