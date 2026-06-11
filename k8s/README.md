# Cabal on Kubernetes

Plain-YAML manifests + a thin Kustomize base for deploying Cabal Core,
the LiteLLM proxy, and a Litestream sidecar in a single namespace.

## Prerequisites

- A Kubernetes cluster (k3s, kind, AKS, GKE, EKS, k0s — anything 1.27+).
- A storage class that supports `ReadWriteOnce` PersistentVolumeClaims.
- The Cabal Core image published to a registry your cluster can pull from
  (`ghcr.io/dimartin/cabal:latest` by default — change in `cabal-core.yaml`
  if you publish elsewhere).
- The pre-public PyPI gate (see top-level `STATUS.md` checklist) is still
  open. Until techne is on PyPI, your image build needs `additional_contexts:
  techne=../techne` set on the build (see `docker-compose.yml`).

## First-time setup

```bash
# 1. Make a real secret. NEVER commit it.
cp k8s/secret.example.yaml k8s/secret.yaml
# Fill CABAL_MASTER_KEY (Fernet key), LITELLM_MASTER_KEY, ANTHROPIC_API_KEY.

# 2. Apply everything.
kubectl apply -k k8s/

# 3. Wait for cabal-core to start.
kubectl -n cabal rollout status deploy/cabal-core

# 4. Grab the bootstrap code from the logs and trade it for an operator JWT.
kubectl -n cabal logs deploy/cabal-core | grep bootstrap_code

# 5. Port-forward so the cbl CLI can reach the cluster.
kubectl -n cabal port-forward svc/cabal-core 7400:7400 &
CABAL_URL=http://127.0.0.1:7400 cbl login --code <CODE>
CABAL_URL=http://127.0.0.1:7400 cbl chat archivist
```

## Files

| File | Purpose |
|---|---|
| `namespace.yaml` | The `cabal` namespace. |
| `secret.example.yaml` | Template for `secret.yaml`. Replace placeholders, never commit. |
| `configmap.yaml` | Three ConfigMaps: identity.toml, litellm/config.yaml, litestream.yml. |
| `pvc.yaml` | `cabal-data` (DB + keys) and `cabal-litestream-replica` (sidecar target). |
| `litellm.yaml` | LiteLLM proxy Deployment + Service on port 4000. |
| `cabal-core.yaml` | Cabal Core Deployment with Litestream sidecar + Service on port 7400. |
| `ingress.example.yaml` | Generic nginx-ingress with SSE-friendly buffering disabled. |
| `kustomization.yaml` | Base Kustomize entrypoint. |

## Production hardening — TODO

- [ ] Replace inline secrets with `external-secrets` / `sealed-secrets` / `sops`.
- [ ] Replace the `file://` Litestream replica with `s3://` / `gcs://` / `azure://`.
- [ ] Set `imagePullPolicy: Always` + digest pinning once you publish tagged images.
- [ ] Add a NetworkPolicy restricting cabal-core ingress to your ingress controller pods.
- [ ] Add resource quotas at the namespace.
- [ ] Consider a HorizontalPodAutoscaler on the LiteLLM proxy if upstream call volume grows.

## Multi-cluster / multi-environment

The `configmap.yaml` is tuned for solo + single-cluster. For multiple
envs, layer a Kustomize overlay:

```
k8s/
├── base/                  # this directory
└── overlays/
    ├── staging/
    └── production/
```

and have each overlay patch `configmap.yaml` (different model lists,
different LiteLLM URLs, etc.).
