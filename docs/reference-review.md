# Reference review

The workspace `AGENTS.md` is the product brief. The two community diagrams are alternative proposals for the same problem; neither is a specification. The payment-system PDFs provide design practices, not additional payment-processing scope.

## What transfers

| Source | Useful contribution | Qualification for Nexo |
| --- | --- | --- |
| `excalidraw-chargeback-community-solution-batch-export.svg` | Durable assignment table keyed by chargeback; chunked generation; archived immutable files; reconciliation | Its 100 million requests/day is an example scale assumption, not measured capacity. Kafka, change data capture and a separate finalizer are optional mechanisms, not requirements. |
| `excalidraw-chargeback-community-solution-durable-delivery.svg` | Bounded atomic claims; immutable files; explicit delivery attempts; temporary upload then rename; distinguish submission from acknowledgement | Its internal-client/Mastercard-only scope is narrower than the user request. “At most four files” differs from four daily submission windows. Multiple diagrams use inconsistent state names. |
| `Design a Payment System  Stripe style  · Ironclad Academy.pdf` | Persist intent before external calls; compare idempotency request content; preserve integer minor units; reconcile unknown outcomes | Ledger, payouts, authorization/capture and refunds belong to a payment processor. Nexo does not move money or claim those capabilities. |
| `Hello Interview guide design payment system.pdf` | Explicit requirements; audit transitions in the same transaction; durable external attempts; timeout is uncertainty; bounded capacity planning | Kafka consumer groups alone do not guarantee exactly-once external effects. Its throughput estimates and descriptions of external companies are reference claims, not Nexo evidence. |
| `nubank-interview-reports.md` | Two chargeback anecdotes agree on durable intake and CSV delivery four times daily; one mentions database read pressure | Unverified anecdotes. Coding-competition and compensation reports add no requirements. |

The PDF references were inspected from their embedded page images because the files contain no extractable text. The relevant sections cover requirements, idempotency, auditability, reconciliation and capacity. Root reference files remain unchanged.

## Reconciled design implications

**Assignment is a database invariant.** A chargeback receives at most one immutable batch identifier. An atomic bounded claim and a database constraint or trigger must survive concurrent workers, replayed schedules and process crashes. A retry resumes the original batch; it never clears membership or creates replacement membership. Customer/idempotency-key uniqueness is separate from transaction uniqueness and batch membership.

**Scheduling is a durable business operation.** Identify each slot by its intended UTC cutoff, not the time a pod happens to start. Persist slot completion and permit safe replay. Kubernetes explicitly documents that a CronJob can create two Jobs or miss one, so scheduler concurrency settings alone cannot enforce uniqueness. [Kubernetes CronJob documentation](https://kubernetes.io/docs/concepts/workloads/controllers/cron-jobs/)

**Bound each unit of work.** Cap rows per file and per database claim, use indexed eligibility scans and stable ordering, and stream file bytes. A logical partition identifies independent work; it does not mean the database is physically sharded. PostgreSQL supports `SKIP LOCKED` for competing queue consumers, but skipped rows mean an empty claim is not proof that no eligible rows exist. Do not advance a global cursor past locked or uncommitted rows. [PostgreSQL SELECT documentation](https://www.postgresql.org/docs/current/sql-select.html)

**Freeze file identity before delivery.** Persist batch identifier, membership, row order, byte count and SHA-256 digest. Check generated row count and exact amounts against the durable membership. Never create a new final filename in response to a timeout. Keep totals separated by currency if totals are exposed.

**A transfer timeout does not prove failure.** Persist an attempt before network activity and persist publication intent before the final rename. Upload to an attempt-specific temporary path, verify its bytes, then publish without overwriting a final path. Paramiko distinguishes standard `rename`, whose destination must not exist, from `posix_rename`, which overwrites an existing destination. The latter is inappropriate for an immutable final file. [Paramiko SFTP documentation](https://docs.paramiko.org/en/3.3/api/sftp.html)

After an ambiguous publication, inspect durable acknowledgement evidence and the final remote file. An exact matching file proves submission; a different hash is a conflict. A missing file cannot prove non-submission because a receiver may already have consumed or moved it. Without durable receiver evidence, retain `unknown` and escalate; do not republish. Nexo's simulator must expose this failure and preserve evidence, not silently turn it into success.

**Acknowledgement is a separate boundary.** Successful SFTP (SSH File Transfer Protocol) upload only says bytes reached the receiver. Acknowledgement must bind network, batch/file identity, hash and row count to an accepted or rejected result. Missing or delayed acknowledgement leaves the batch submitted and pending; duplicates must be harmless; conflicting acknowledgements must be visible. Acknowledgement of a file is not a final decision about the customer's dispute or proof that money moved.

## Scope and evidence

The implementation requirements define the agreed choices: customer UI, Visa and Mastercard routing, four UTC windows, split files bounded by row count, integer amounts, SFTP and a documented simulator format. There is no claim to implement a proprietary card-network contract. Database-backed durable work avoids adding a broker that the first deployment does not need.

A reference's 100 million/day corresponds to about 1,157 accepted requests/second on average, before bursts and write amplification. A file-per-six-hours interpretation would hold roughly 25 million rows per file before network splitting. That is a useful sizing exercise, not proof of scalability. Report the tested rate, dataset size, resource limits, file sizes, failure scenarios and elapsed drain time. Keep unknown outcomes, oldest pending age, missing acknowledgements and conflicts visible in operations.

The most useful failure tests are: concurrent idempotent intake; competing claims; replayed slots; crash after assignment; interrupted object creation; partial temporary upload; disconnect after final rename; database failure after remote publication; missing/late/conflicting acknowledgement; and retry after the receiver removes a final file.
