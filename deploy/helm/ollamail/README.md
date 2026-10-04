# ollamail Helm chart

Kubernetes deployment of ollamail: `frontend`, `api`, worker groups (one Deployment per set
of queues), a pre-install/pre-upgrade migration job, optional Ingress (cert-manager),
optional Ollama (CPU/GPU) and NetworkPolicies (on by default; need an enforcing CNI).

```sh
kubectl -n ollamail create secret generic ollamail-secrets \
  --from-literal=OLLAMAIL_SECRET_KEY="$(openssl rand -base64 32)" \
  --from-literal=OLLAMAIL_DATABASE_URL='postgresql+asyncpg://user:password@host:5432/ollamail'
helm install ollamail deploy/helm/ollamail -n ollamail --set secrets.existingSecret=ollamail-secrets
helm -n ollamail test ollamail
```

Requirements: PostgreSQL 16 with pgvector, an existing Secret with `OLLAMAIL_SECRET_KEY`.
All values are documented in [`values.yaml`](values.yaml); the operations guide (German) is
[`docs/operations/kubernetes.md`](../../../docs/operations/kubernetes.md).
