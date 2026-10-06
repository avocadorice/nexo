# Nexo requirements and decisions

Nexo is a chargeback intake and file-delivery system, not a payment processor. The supplied community designs are references, not specifications. The deployed receiver is explicitly a Visa/Mastercard simulator; no real network integration or certification is claimed.

<!-- BEGIN GENERATED REQUIREMENTS -->

Generated from [explorer/mapping.json](../explorer/mapping.json). Edit that mapping and run `.venv/bin/python scripts/build_explorer.py` to update this section.

## Functional behavior

### FR-1 · File a chargeback and check its status.

A provisioned customer uses the real customer page to see imported transactions, dispute an owned transaction and read its status. Customer identity, network and currency come from server-side records. The same customer, idempotency key and request return the same chargeback; changed content returns 409, and different keys cannot dispute the same transaction twice.

Components: Customer / operations UI, Chargeback API, PostgreSQL + work queue.

Limit: Transactions are synthetic imports. One chargeback per transaction is an initial scope limit. Acknowledgement confirms the receiver’s answer, not settlement or a refund.

### FR-2 · Send CSVs to each network in four daily slots.

At 00:00, 06:00, 12:00 and 18:00 UTC, a Kubernetes CronJob launches bounded batch work. Missed slots can be replayed. A slot claims committed, eligible, unassigned requests received before its cutoff; late commits wait for a later slot. Batch work writes comma-separated values (CSV) files split by network and stable logical partition, with a configurable row cap. It stores immutable objects through S3-compatible storage and delivers them over SFTP (SSH File Transfer Protocol). A matching durable acknowledgement sets acknowledged or rejected. Without it, a published batch stays submitted or unknown; a mismatched file identity, hash or row count becomes an observable conflict.

Components: Four daily slots, Claim and build CSV, PostgreSQL + work queue, CSV artifact storage, Delivery worker, Visa / Mastercard simulator.

Limit: Four daily submission windows can contain multiple bounded files. The user approved SFTP and the simulator contract on 2026-10-05; this is not a proprietary Visa/Mastercard file format or a live network integration.

### FR-3 · Never put a chargeback in two submitted files.

PostgreSQL transactions, uniqueness constraints and immutable-membership triggers assign each chargeback permanently to at most one batch, including under concurrency. Each batch has one deterministic CSV and final filename. The worker uploads to a unique temporary name, verifies its bytes, commits publication intent, then renames without replacing an existing final file. A retry checks the acknowledgement and original final file first; reconciliation never creates a new batch. The simulator keeps durable, unique file and chargeback receipts.

Components: PostgreSQL + work queue, Claim and build CSV, Delivery worker, Visa / Mastercard simulator.

Limit: SFTP alone does not provide exactly-once delivery. Once publication may have occurred, a missing final file does not prove failure: without receiver evidence, Nexo keeps unknown and does not publish again.

### FR-4 · Inspect batches, files and delivery attempts.

The operations page calls the real service and requires an operations role. It shows lifecycle counts, queue age, batches, immutable file digests, delivery attempts and audit events, so accepted work can be followed through its recorded outcome.

Components: Customer / operations UI, Chargeback API, PostgreSQL + work queue.

Limit: This is a small operations view with manual refresh and bounded recent lists, not a full observability stack.

## Non-functional behavior

### NFR-1 · Preserve exact amounts and identities.

Amounts are positive integer minor units expressed as decimal strings at the HTTP (Hypertext Transfer Protocol) boundary and stored as PostgreSQL bigint; they never pass through floating point. An amount cannot exceed the original transaction. Server-side ownership checks and database constraints preserve transaction and chargeback identities. Batch membership and the stored file manifest remain fixed, and generated rows preserve the recorded amounts and identifiers.

Components: Chargeback API, PostgreSQL + work queue, Claim and build CSV.

Limit: Nexo submits disputes; it does not move money, convert currencies, settle refunds or implement a general payment ledger.

### NFR-2 · Keep accepted work durable and auditable.

No accepted write is acknowledged before its database commit. Each accepted chargeback links to an append-only audit event, each delivery attempt has a durable record, and stored CSV artifacts and receiver receipts preserve evidence across process restarts. PostgreSQL currently holds the durable work queue. Database backups and restore exercises are part of deployment readiness.

Components: Chargeback API, PostgreSQL + work queue, CSV artifact storage, Delivery worker, Visa / Mastercard simulator.

Limit: The local restore drill covers a small PostgreSQL snapshot, not object storage, receiver files or cloud recovery. Cross-store disaster recovery is manual. Kafka is not implemented; its hosting choice is still pending.

### NFR-3 · Keep work bounded as volume grows.

Batch row caps, bounded database reads, worker concurrency, finite connection pools, request body limits and network timeouts bound work and memory. Logical partitions allow independent claimers. The initial targets are 100 accepted requests/second and 100,000 chargebacks drained within six hours. The recorded local run accepted 1,000 requests at 506.7/second in a short burst and drained 100,000 already accepted fixtures through matching acknowledgements in 48.93 seconds.

Components: Chargeback API, PostgreSQL + work queue, Claim and build CSV, Delivery worker.

Limit: The intake sample lasted about two seconds; the 100,000-row backlog was seeded separately. These measurements are not sustained throughput, Kubernetes workload sizing, cloud capacity or bank-scale validation. Physical database sharding is not implemented.

### NFR-4 · Restrict access and protect customer data.

Revocable opaque bearer tokens identify customers and operations roles; the database stores token hashes. Requests enforce ownership, strict field validation, parameterized queries and safe errors. Logs exclude tokens and customer payloads, and Nexo never receives a card number. Credentials and Secure Shell (SSH) host keys are generated outside version control; the worker verifies the receiver’s host key. Production transport must be encrypted.

Components: Chargeback API, PostgreSQL + work queue, Delivery worker, Visa / Mastercard simulator.

Limit: Identity-provider integration is not implemented. Local loopback HTTP and unencrypted local database connections are documented exceptions. Local network policies are not enforced by default kind networking; cloud enforcement still needs verification. No bank security certification is claimed.

### NFR-5 · Run the same workloads locally and in cloud.

Status: **partial**.

The service and delivery worker use Kubernetes Deployments; the scheduler launches bounded CronJob Jobs, and migrations use a Job. Shared base manifests support local kind and cloud overlays, with the simulator in a separate namespace. Local Kubernetes runs these workloads. The user approved DigitalOcean managed Kubernetes, managed PostgreSQL and Spaces on 2026-10-05.

Components: Chargeback API, Four daily slots, Claim and build CSV, Delivery worker, Visa / Mastercard simulator.

Limit: Cloud resources have not been provisioned. Provisioning still requires user sign-in and approval of the concrete resource plan, topology and cost. This is a single-region learning system, without multi-region failover.

<!-- END GENERATED REQUIREMENTS -->

## Explorer and scope

The user approved a static HTML explorer with linked SVG views, one JSON traceability source and build-time symbol extraction on 2026-10-05. Browser code uses TypeScript, compiled to JavaScript, as required by the workspace language guidance. Go runs the API and delivery worker; Python handles batching, the simulator and operational tooling. Language-neutral contracts define their shared data. The explorer explains real flows without changing runtime behavior. One architecture diagram and a sequence per use case share that source; symbol references resolve to exact local VS Code lines. Glossary tooltips explain terms.

Simplifications: synthetic imported transactions, provisioned tokens rather than an identity provider, no card-network proprietary format, no settlement/refunds, no HSM (hardware security module), no PCI (Payment Card Industry) certification, one cloud region, no physical database sharding, and a deliberately small receiver simulator. Production readiness is a checklist supported by actual execution evidence, not a label on this document.

## Language boundaries

Go implements the HTTP API and concurrent delivery worker with pgx, the AWS S3 SDK and pkg/sftp. Python implements the batch/file processor, card-network simulator, migrations and load tools with psycopg, boto3 and Paramiko. Browser code is TypeScript compiled to static JavaScript. OpenAPI and the CSV/JSON acknowledgement contract define the boundaries.
