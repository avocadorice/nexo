# Run and inspect Nexo locally

Nexo runs in a real kind Kubernetes cluster. PostgreSQL persists accepted chargebacks and batch assignment, SeaweedFS serves the S3-compatible object API, and the separate `card-network` namespace runs the real SFTP (SSH File Transfer Protocol) simulator. The same application manifests run in DigitalOcean with managed PostgreSQL and Spaces replacing the two local infrastructure pods.

## Start

Install Docker, kind, kubectl, Python 3, and OpenSSH's `ssh-keygen`. Start the Docker engine and select its context. On this workspace, the prepared engine is the `colima-nexo` Docker context. Project-local tools can live in ignored `.local/bin`; the scripts add it to PATH.

```sh
cd nexo
./scripts/local-up.sh
KUBECONFIG="$PWD/.local/kubeconfig" kubectl -n nexo port-forward service/api 8080:80
```

Open `http://localhost:8080`. The startup script builds the Go API and delivery worker, installs the locked Python dependencies, builds the TypeScript interface/explorer, and loads their shared image into kind. It then creates runtime secrets, waits for PostgreSQL and object storage, migrates the schema, starts services, and seeds synthetic customers/transactions. First startup may take several minutes while images download.

Tokens are written only to `.local/seed.json` with private file permissions. Use the customer or operations token in the UI. All seed records are synthetic. Local runtime passwords, SFTP host key, and kubeconfig are under ignored `.local/`; none belongs in a commit. The worker trusts the exact generated simulator host key instead of accepting arbitrary hosts.

The scripts use the isolated `.local/kubeconfig` by default. For the remaining commands, set:

```sh
export KUBECONFIG="$PWD/.local/kubeconfig"
```

## Follow a chargeback

Create a chargeback in the customer UI. The operations UI shows durable batch and delivery states. Scheduled export slots are 00:00, 06:00, 12:00, and 18:00 UTC. To exercise the scheduler immediately using its real Kubernetes Job template:

```sh
kubectl -n nexo create job --from=cronjob/schedule "schedule-manual-$(date +%s)"
kubectl -n nexo get jobs,pods
kubectl -n nexo logs deployment/worker --tail=100
```

A manual invocation follows the same slot cutoff and idempotency rules. It does not make a chargeback eligible for a past cutoff. Inspect the CLI's explicit `schedule --help` options when testing a future slot; never change a production schedule merely to accelerate a demo.

```sh
kubectl -n nexo logs deployment/api --tail=100
kubectl -n nexo get cronjob schedule
kubectl -n card-network logs deployment/simulator --tail=100
kubectl -n nexo describe job migrate
```

Logs should contain correlation identifiers and state transitions, not authentication tokens or full customer payloads. `/healthz` reports API liveness; `/readyz` checks its database connection. A failed migration is a deployment failure: inspect that Job before retrying application traffic.

## Restart and failure exercises

```sh
kubectl -n nexo rollout restart deployment/worker
kubectl -n card-network rollout restart deployment/simulator
kubectl -n nexo rollout status deployment/worker
```

The database and object store survive pod replacement through PersistentVolumeClaims; the simulator's remote files also survive replacement. The worker reconciles ambiguous outcomes using the existing durable filename, so a restarted attempt must not generate another file for the same chargeback. See the simulator's supported mode contract and the automated integration tests for timeout, partial-upload, and acknowledgement scenarios. A process restart test is not a database restore test.

## Scaling knobs and local limits

The shared ConfigMap sets `PARTITION_COUNT`, `BATCH_MAX_ROWS`, `SCHEDULE_MAX_BATCHES`, database connection limits, and worker polling interval. The partition count forms part of durable data identity; do not change it on an existing dataset without a migration. Jobs cap work per run and can resume through database state. Increase worker replicas only when the database and recipient can handle the concurrent requests.

The local cluster uses one node and single instances of PostgreSQL/SeaweedFS. Its persistent volumes live inside the kind node, so deleting the cluster destroys them. Local PostgreSQL uses one role and unencrypted cluster-local connections; cloud connections use verified TLS and separate migration/runtime roles. The local S3 credentials have administrative scope within the single local server; cloud keys are scoped to one bucket.

NetworkPolicies express the intended ingress boundaries, including worker-only SFTP access. Default kind networking does not enforce those policies; DigitalOcean's network plugin must enforce them and should be verified during cloud acceptance. Kubernetes secrets are access-controlled objects, not an external secret manager. Image tags for public base runtimes follow supported minor releases; record pulled image digests when publishing a deployment.

## Stop

```sh
./scripts/local-down.sh
```

This removes only the `nexo` kind cluster and all its local test data. It does not stop the Docker engine or touch DigitalOcean. Local credentials remain in `.local/` for the next startup. [Cloud deployment and teardown](../infra/digitalocean/README.md) use separate commands and kubeconfig.
