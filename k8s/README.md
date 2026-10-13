# Cabal on Kubernetes

Plain-YAML manifests + a thin Kustomize base for deploying Cabal Core,
the LiteLLM proxy, and a Litestream sidecar in a single namespace.

## Prerequisites

- A Kubernetes cluster (k3s, kind, AKS, GKE, EKS, k0s — anything 1.27+).
- A storage class that supports `ReadWriteOnce` PersistentVolumeClaims.
- The Cabal Core image published to a registry your cluster can pull from
  (`ghcr.io/tenishai/cabal:latest` by default — change in `cabal-core.yaml`
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
# Once you are logged in, set CABAL_BOOTSTRAP_CODE=off (or "file") on the
# deployment so later starts stop writing a code to the log.

# 5. Port-forward so the cbl CLI can reach the cluster.
kubectl -n cabal port-forward svc/cabal-core 7400:7400 &
CABAL_URL=http://127.0.0.1:7400 cbl login --code <CODE>
CABAL_URL=http://127.0.0.1:7400 cbl chat archivist
```

## Through Gojira

`gojira.yaml` at the repo root is the deploy contract for
[Gojira](https://github.com/tenishai/gojira). Connecting the repo there
generates the Deployment, Service, CI workflow and per-environment overlays
under `k8s/base` and `k8s/overlays/<env>`; `k8s/base/pvc.yaml` is the one
object kept as app-authored. The flat manifests in this directory stay the
standalone path. On a Gojira cluster, LiteLLM runs as its own app and
`identity.toml` is a ConfigMap (`cabal-config`) made per environment, so
`[litellm] api_base` points at that app's Service and the model list matches
its config — the same pairing `tests/test_config_models_are_served.py` pins
for the flat manifests.

## Upgrading

`kubectl apply -k k8s/` on its own does **not** roll the pods, and a running
pod never sees a changed `identity.toml`:

- `configmap.yaml` is a plain `resources:` entry, not a `configMapGenerator`,
  so the ConfigMap name is stable and applying it leaves both pod templates
  unchanged. No rollout is triggered.
- `cabal-core.yaml` mounts `identity.toml` with `subPath`, and Kubernetes does
  not propagate ConfigMap updates into a `subPath` mount.
- The image is `:latest` with `imagePullPolicy: IfNotPresent`, so a recreated
  pod can start a cached older image.

So after every apply, restart both Deployments and wait for them:

```bash
kubectl apply -k k8s/
kubectl -n cabal rollout restart deploy/cabal-core deploy/cabal-litellm
kubectl -n cabal rollout status  deploy/cabal-core
```

**The ConfigMap and the image must land together whenever persona vocabulary
changes** (a persona rename, or adding or removing one). If they drift, the
next unrelated pod recreation (node drain, eviction, OOM) starts one version of
the code against the other version's `identity.toml`. Personas the running code
does not know keep their bulletin rows but get no handler: startup logs
`personas.no_handler_registered` at info level and carries on, and they fail
only when something dispatches to them (cabal#114).

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
