# DigitalOcean environment

The approved small deployment creates a VPC (virtual private cloud), a non-HA Kubernetes control plane, two 2-vCPU/4-GiB workers, one 2-GiB managed PostgreSQL 17 node, a private versioned Spaces bucket, an application key limited to that bucket, and a Basic container registry. Kubernetes creates a 10-GiB volume for the separate card-network simulator. Expected cost is **about $89.45/month** before tax, traffic/storage overages, and domain costs. This estimate excludes a public load balancer: initial access is an authenticated Kubernetes port-forward. Adding public HTTPS later is approximately $12/month plus a domain. See [pricing and trade-offs](../../docs/cloud-options.md).

The database and control plane are single-instance availability simplifications. PostgreSQL constraints and transactions still protect batch assignment; outages are visible and recoverable. The database receives traffic only from the Kubernetes cluster, using its private hostname and verified TLS (Transport Layer Security). The migration Job has administrative database credentials; running services use a separate user without schema-changing grants.

## Prepare an account and a reviewable plan

The user signs in to DigitalOcean and supplies their own credentials. Agents do not create accounts or enter passwords. Required tools are Terraform 1.6 or newer (below 2.0), `doctl`, `kubectl`, Docker with Buildx, Python 3, and `ssh-keygen`. The checked-in provider is pinned and has a dependency lock file.

Create a DigitalOcean API token that can manage this environment's projects, VPCs, Kubernetes clusters, databases, registry, and Spaces keys. Separately create a Spaces key that can create/manage buckets for Terraform. Put those values in the ignored file `.local/cloud.env`, with mode `600`:

```sh
DIGITALOCEAN_TOKEN=your_api_token
SPACES_ACCESS_KEY_ID=your_terraform_spaces_key
SPACES_SECRET_ACCESS_KEY=your_terraform_spaces_secret
```

Do not put credentials in chat, Terraform variables, or version control. The application gets its own bucket-limited key, not the provisioning key. Terraform state and plan files also contain secrets; the initial local state is ignored and must be protected and backed up separately. Do not run Terraform concurrently against this local state. A shared team should move it to an encrypted remote backend with state locking before collaborating.

From `nexo/`, load the file in your own shell and choose an exact supported Kubernetes release:

```sh
export PATH="$PWD/.local/bin:$PATH"
set -a
source .local/cloud.env
set +a
export DIGITALOCEAN_ACCESS_TOKEN="$DIGITALOCEAN_TOKEN"
doctl kubernetes options versions
cp infra/digitalocean/terraform.tfvars.example infra/digitalocean/terraform.tfvars
```

Edit the ignored `.tfvars` file: set a globally unique `nexo-*` name, the version slug from the command, and your current public IP with `/32` (or a specific trusted network). NYC3 is the proposed region. If the account already has a shared registry, adapt the configuration to reference it without taking ownership; do not import unrelated shared resources into this disposable environment. Verify the VPC subnet does not overlap existing networks.

```sh
terraform -chdir=infra/digitalocean init
terraform -chdir=infra/digitalocean validate
terraform -chdir=infra/digitalocean plan -out=nexo.tfplan
```

Review the plan and cost before running the apply. A provider choice is approved; an actual account-specific plan has not been executed merely by writing these files.

## Provision and deploy

These commands create billable resources and push an image into the user's private registry:

```sh
terraform -chdir=infra/digitalocean apply nexo.tfplan
./infra/digitalocean/deploy.sh
KUBECONFIG="$PWD/.local/cloud-kubeconfig" kubectl -n nexo port-forward service/api 8080:80
```

Open `http://localhost:8080`. The browser connection stays on the local machine; Kubernetes carries it over an authenticated encrypted connection. No public HTTP service is exposed. Synthetic customer and operations tokens are in `.local/cloud/seed.json`; keep them private. The separate kubeconfig avoids changing the user's active local cluster context.

The deployment script builds an amd64 Go/Python/TypeScript image, pushes a unique tag, generates the cloud overlay, installs namespace-scoped pull credentials, mounts the database certificate, runs the migration to completion, then applies workloads and waits for readiness. A failed migration stops the deployment before new workloads start. Schema migrations must remain backward compatible with existing pods during a rolling release; an incompatible change requires a planned maintenance window. Provider-specific addresses come from Terraform outputs into ignored files. It never prints token values. For later code changes, rerun the deployment script. `NEXO_APP_DB_ROLE=nexo` tells the migration to grant table access after applying the schema.

Scaling knobs are deliberately small: `worker_count` in Terraform, API/worker replicas in Kubernetes, database plan, and bounded batch settings in the ConfigMap. No autoscaling is enabled. Increase a measured bottleneck only after checking database connections and recipient throughput.

## Teardown

```sh
./infra/digitalocean/destroy.sh
```

The command explicitly confirms data destruction, removes Kubernetes namespaces while the storage/load-balancer controllers still exist, then presents Terraform's destroy plan. The dedicated Spaces bucket uses `force_destroy=true`, so confirmed destruction includes all retained object versions. Do not run teardown on data that must be retained. Check billing afterward for manually created snapshots or unrelated resources; this script owns only this environment.

## Validation still needed in the real account

`terraform validate` verifies provider schemas and local configuration. It does not prove regional capacity, account quotas, a successful restore, or the application's throughput. After apply, record the real endpoint/topology and demonstrate a synthetic chargeback through a scheduled CSV, SFTP delivery, acknowledgement, restart, and an interrupted-upload recovery. Run and time a database restore exercise before claiming an operational recovery target.

The Python S3 client requests ordinary fixed-length HTTPS uploads. Optional SDK streaming checksums are disabled because [Spaces PutObject requires Content-Length](https://docs.digitalocean.com/reference/api/spaces/); the persisted manifest and delivery worker still verify SHA-256 across the exact file bytes. A test inspects the real SDK prepared request before any network send.
