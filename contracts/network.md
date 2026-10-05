# Simulated card-network file contract (version 1)

This is a teaching contract, not Visa or Mastercard's proprietary production specification. Both sides speak real SFTP (SSH File Transfer Protocol). SSH host keys are pinned; credentials are supplied outside version control.

Four send slots per day: 00:00, 06:00, 12:00, 18:00 UTC. A slot may contain many bounded files, split by network and logical partition. Exactly four files per day is not required: a file-count cap would prevent bounded files at arbitrarily high volume.

Final path: `/incoming/{batch_uuid}.csv`. Temporary path: `/incoming/.{batch_uuid}.{attempt_uuid}.tmp`. Standard SFTP rename MUST fail if the destination exists, and MUST atomically publish the final file. Final files are immutable and retained. Temporary files are never ingested. The receiver keeps a durable receipt keyed by batch ID, rejects changed bytes for that identity, and enforces unique chargeback IDs across all accepted files.

CSV is UTF-8, RFC 4180 quoting, CRLF row endings, with this exact header:

```
chargeback_id,transaction_id,network,currency,amount_minor,reason
```

IDs are canonical UUIDs; network is `visa` or `mastercard`; currency is three uppercase ASCII letters; amount_minor is a positive base-10 integer, not a decimal amount. Reason is a fixed API reason code. Rows sort by chargeback UUID; at most 10,000 rows per file, normally 1,000. Every row has the same network. There are no card numbers or customer names in the file.

Acknowledgement path: `/acks/{batch_uuid}.json`, atomically published only after the receiver's durable acceptance or rejection. JSON shape:

```json
{"version":1,"batch_id":"uuid","sha256":"64 lowercase hex characters","row_count":1000,"status":"accepted","reason":null}
```

`status` is `accepted` or `rejected`. A rejected file is terminal and still retains its membership; there is no automatic re-batching. Acknowledgement `sha256` covers exact CSV bytes. Nexo verifies every field against its persisted artifact. Missing acknowledgements do not imply rejection or success.

The sender records publication intent before rename. After intent, a crash or timeout becomes unknown. Reconciliation reads a matching durable acknowledgement first, then reads and hashes the final remote file. A matching final file means submitted, not acknowledged. If both are absent, the sender cannot prove whether the receiver consumed it and does not republish. This conservative rule favors financial integrity over automatic availability; operations investigates. A transfer failure before publication intent is safe to retry with a new temporary path.

The simulator uses SQLite transactions and persistent volume storage for receipts and chargeback uniqueness. It supports mode.json with `mode`: `normal`, `timeout`, `partial_upload`, `disconnect_after_rename`, `late_ack`, `missing_ack`, or `reject`. `late_ack` also accepts `delay_seconds` (default 30). Conflicting duplicate batch IDs and chargeback IDs are rejected. This control stays in the separate simulator namespace, never the customer API.
