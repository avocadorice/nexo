# Nexo requirements and decisions

Nexo is a chargeback intake and file-delivery system, not a payment processor. The supplied community designs are references, not specifications. The deployed receiver is explicitly a Visa/Mastercard simulator; no real network integration or certification is claimed.

## Functional behavior

- A provisioned customer authenticates with a revocable opaque bearer token, sees only their imported transactions, files one dispute against an owned transaction, and reads its status. Customer identity, card network and currency come from server-side records. Nexo never receives a card number.
- Amounts are positive integer minor units expressed as decimal strings at the HTTP boundary, stored as PostgreSQL bigint, and never converted through floating point. An amount cannot exceed the original transaction. One chargeback per transaction is a deliberate initial scope limit.
- The same customer and idempotency key with the same request returns the same chargeback; changed content returns 409. Different keys cannot dispute the same transaction twice.
- At 00:00, 06:00, 12:00 and 18:00 UTC a Kubernetes CronJob launches bounded batch work. Missed slots can be replayed. A slot includes committed, eligible, unassigned requests received before its cutoff; late commits are picked up by a later slot.
- Each chargeback belongs permanently to at most one batch. Each batch has one deterministic CSV and final filename. Files are split by network and stable logical partition, with a configurable maximum row count.
- Upload immutable objects to S3-compatible storage, then transmit through SFTP (SSH File Transfer Protocol). SFTP and the documented simulator contract were approved by the user on 2026-10-05.
- Upload to a unique temporary filename, verify content, durably record publication intent, then rename without overwriting a final filename. A retry checks the acknowledgement and final file first. Once publication may have occurred, absence is ambiguous and does not authorize a new publication. Reconciliation never assigns a new batch.
- A matching durable acknowledgement marks a batch acknowledged or rejected. Hash, file identity or count mismatches become an observable conflict. A missing acknowledgement remains pending, not successful.
- A minimal customer UI calls the real API. An authorized operations UI shows lifecycle counts, queue age, batches, immutable file digests, delivery attempts and audit events.

## Non-functional behavior

- PostgreSQL transactions, uniqueness constraints and immutable-membership triggers enforce ownership and assignment under concurrency. PostgreSQL is also the durable work queue; there is no unimplemented broker box.
- No accepted write is acknowledged before its database commit. Every accepted chargeback links to an append-only audit event; every delivery attempt has a durable record. Database backups and restore exercises are part of deployment readiness.
- Work and memory are bounded: batch row cap, bounded SQL reads, worker concurrency, finite connection pools, request body limits and network timeouts. Logical partitions permit independent claimers; physical database sharding is deferred and labeled.
- Initial measurable capacity target: 100 accepted requests/second and 100,000 chargebacks drained within a six-hour slot on the small deployment. These are targets until a repeatable load test records evidence; no claim of bank-scale validation.
- Requests have explicit customer/operations authorization, safe error messages, parameterized SQL, strict field validation and no token/customer payload logging. Credentials and SSH host keys are generated outside version control. Production transport must be encrypted; local loopback HTTP is a documented exception.
- API and delivery workers use Kubernetes Deployments; bounded slot processing uses CronJob Jobs; migrations use a Job. Same base manifests support local kind and cloud overlays. Simulator is isolated in another namespace.
- Cloud resources are not provisioned until provider, topology and cost are approved. Current proposal: DigitalOcean managed Kubernetes, managed PostgreSQL and Spaces; approved by the user on 2026-10-05. Provisioning awaits a concrete resource plan and user sign-in.

## Explorer and scope

The user approved a static HTML explorer with linked SVG views, one JSON traceability source and build-time symbol extraction on 2026-10-05. Browser code uses TypeScript, compiled to JavaScript, as required by the workspace language guidance. Go runs the API and delivery worker; Python handles batching, the simulator and operational tooling. Language-neutral contracts define their shared data. The explorer explains real flows without changing runtime behavior. One architecture diagram and a sequence per use case share that source; symbol references resolve to exact local VS Code lines. Glossary tooltips explain terms.

Simplifications: synthetic imported transactions, provisioned tokens rather than an identity provider, no card-network proprietary format, no settlement/refunds, no HSM (hardware security module), no PCI (Payment Card Industry) certification, one cloud region, no physical database sharding, and a deliberately small receiver simulator. Production readiness is a checklist supported by actual execution evidence, not a label on this document.

## Language boundaries

Go implements the HTTP API and concurrent delivery worker with pgx, the AWS S3 SDK and pkg/sftp. Python implements the batch/file processor, card-network simulator, migrations and load tools with psycopg, boto3 and Paramiko. Browser code is TypeScript compiled to static JavaScript. OpenAPI and the CSV/JSON acknowledgement contract define the boundaries.
