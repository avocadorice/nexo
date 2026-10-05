# Nexo

Nexo accepts customer chargebacks, permanently assigns them to bounded CSV files, and delivers those files to a simulated Visa/Mastercard receiver over real SFTP. It uses real PostgreSQL transactions, S3-compatible object storage and Kubernetes. The receiver is a simulator, not a card-network certification or live banking integration.

The Go API and delivery worker run beside a Python batch processor and receiver simulator. A small TypeScript customer UI, operations dashboard and read-only explorer connect the design to the code. [Requirements](docs/requirements.md), [OpenAPI](contracts/openapi.json), [network file contract](contracts/network.md), and [reference comparison](docs/reference-review.md) define the scope.

## Run locally

Install Docker (or start Colima), kind, kubectl, Python 3 and ssh-keygen, then:

```sh
./scripts/local-up.sh
KUBECONFIG="$PWD/.local/kubeconfig" kubectl --context kind-nexo -n nexo port-forward service/api 8080:80
```

Open http://localhost:8080. The local script generates synthetic customers and private credentials in `.local/seed.json`. Enter a customer's token on the customer page, or `ops_token` on the operations page. Tokens stay in browser memory and expire after seven days. No card numbers are accepted.

Follow [local operations](docs/local-operations.md) for configuration and teardown. The API accepts new requests immediately; file windows occur at 00:00, 06:00, 12:00 and 18:00 UTC. A request filed after a cutoff waits for the next slot. The scheduler can be manually replayed as a Kubernetes Job; the same slot and immutable membership protect against duplicates.

## Guarantees and limits

A unique transaction constraint prevents duplicate disputes; a customer-scoped idempotency key handles retries. PostgreSQL makes batch assignment permanent. Each batch has one immutable manifest, S3 object and remote filename. The sender records publication intent before renaming the temporary upload. After an ambiguous outcome, it checks the receiver's receipt or exact final bytes; absence alone never authorizes republishing. SFTP itself does not provide exactly-once delivery.

The simulator durably deduplicates both file identities and chargeback IDs. It can trigger partial uploads, timeouts, lost rename replies, rejections and delayed or missing acknowledgements. Its CSV contract is public and deliberately small; actual Visa/Mastercard contracts are not supplied.

The current infrastructure supports a single-region learning deployment. It is not certified for production bank data: identity-provider integration, compliance controls, multi-region recovery and physical database sharding are documented gaps. Cloud deployment status and performance claims must be read from [verification](docs/verification.md), not inferred from the presence of Terraform files.

## Verify

```sh
uv sync --frozen
npm ci --prefix explorer
python3 scripts/build_explorer.py
uv run ruff check src tests scripts
uv run pytest
GOCACHE=/tmp/nexo-go-cache go test ./...
```

Set `TEST_DATABASE_URL` to an isolated PostgreSQL database to enable database concurrency tests. Each test uses a separate temporary schema. Delivery integration also uses the local Python environment to run a real SFTP receiver; real S3 tests additionally take `TEST_S3_ENDPOINT` and `TEST_S3_CREDENTIALS` pointing to the generated local credentials file. Tests never use a production database.

## Cloud

The user selected DigitalOcean managed Kubernetes, managed PostgreSQL and Spaces. [Version-controlled infrastructure](infra/digitalocean/) and [cloud options](docs/cloud-options.md) show the components and cost. The small private deployment is approximately $90/month, with one database node and no public load balancer. User sign-in and an account-specific resource and cost review precede billable provisioning. Secrets and Terraform state stay outside Git.

Measured locally: 1,000 accepted HTTP requests at 506.7/second in a short burst, and 100,000 chargebacks archived and acknowledged through real SFTP in 48.93 seconds. See [performance evidence and limits](docs/performance.md) and the [restore procedure](docs/recovery.md). These measurements do not establish cloud capacity or sustained bank-scale throughput.
