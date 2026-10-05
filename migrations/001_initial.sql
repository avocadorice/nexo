CREATE TABLE customers (
    id uuid PRIMARY KEY,
    label text NOT NULL
);

CREATE TABLE tokens (
    hash text PRIMARY KEY,
    customer_id uuid REFERENCES customers(id),
    role text NOT NULL CHECK (role IN ('customer', 'ops')),
    expires_at timestamptz NOT NULL,
    revoked boolean NOT NULL DEFAULT false,
    CHECK ((role = 'customer' AND customer_id IS NOT NULL) OR
           (role = 'ops' AND customer_id IS NULL))
);

CREATE TABLE transactions (
    id uuid PRIMARY KEY,
    customer_id uuid NOT NULL REFERENCES customers(id),
    network text NOT NULL CHECK (network IN ('visa', 'mastercard')),
    currency text NOT NULL CHECK (currency ~ '^[A-Z]{3}$'),
    amount_minor bigint NOT NULL CHECK (amount_minor > 0)
);
CREATE INDEX transactions_customer ON transactions(customer_id, id);

CREATE TABLE slots (
    cutoff timestamptz PRIMARY KEY,
    completed_at timestamptz,
    CHECK (date_part('minute', cutoff AT TIME ZONE 'UTC') = 0 AND
           date_part('second', cutoff AT TIME ZONE 'UTC') = 0 AND
           mod(date_part('hour', cutoff AT TIME ZONE 'UTC')::integer, 6) = 0)
);

CREATE TABLE batches (
    id uuid PRIMARY KEY,
    slot timestamptz NOT NULL REFERENCES slots(cutoff),
    network text NOT NULL CHECK (network IN ('visa', 'mastercard')),
    partition integer NOT NULL CHECK (partition >= 0),
    state text NOT NULL DEFAULT 'building' CHECK (state IN
        ('building','ready','unknown','submitted','acknowledged','rejected','conflict')),
    row_count integer NOT NULL DEFAULT 0 CHECK (row_count BETWEEN 0 AND 10000),
    object_key text,
    sha256 text CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    byte_count bigint CHECK (byte_count IS NULL OR byte_count >= 0),
    publication_started boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error text,
    ack jsonb,
    CHECK (state = 'building' OR
        (row_count > 0 AND object_key IS NOT NULL AND sha256 IS NOT NULL AND byte_count > 0))
);
CREATE INDEX batches_due ON batches(next_attempt_at, created_at)
    WHERE state IN ('ready','unknown','submitted');
CREATE INDEX batches_slot ON batches(slot, network, partition);

CREATE TABLE chargebacks (
    id uuid PRIMARY KEY,
    customer_id uuid NOT NULL REFERENCES customers(id),
    transaction_id uuid NOT NULL UNIQUE REFERENCES transactions(id),
    idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 128),
    request_hash text NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    amount_minor bigint NOT NULL CHECK (amount_minor > 0),
    reason text NOT NULL CHECK (reason IN ('FRAUD','DUPLICATE','NOT_RECEIVED','OTHER')),
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    partition integer NOT NULL CHECK (partition >= 0),
    batch_id uuid REFERENCES batches(id),
    UNIQUE (customer_id, idempotency_key)
);
CREATE INDEX chargebacks_eligible ON chargebacks(partition, received_at, id)
    WHERE batch_id IS NULL;
CREATE INDEX chargebacks_batch ON chargebacks(batch_id, id);
CREATE INDEX chargebacks_customer ON chargebacks(customer_id, received_at DESC, id);

CREATE TABLE delivery_attempts (
    id uuid PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES batches(id),
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz,
    outcome text NOT NULL DEFAULT 'in_flight',
    detail text
);
CREATE INDEX attempts_batch ON delivery_attempts(batch_id, started_at DESC);

CREATE TABLE audit_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    chargeback_id uuid REFERENCES chargebacks(id),
    batch_id uuid REFERENCES batches(id),
    event text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX audit_batch ON audit_events(batch_id, id);
CREATE INDEX audit_chargeback ON audit_events(chargeback_id, id);

CREATE FUNCTION protect_transaction() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'imported transactions are immutable' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER immutable_transaction BEFORE UPDATE OR DELETE ON transactions
    FOR EACH ROW EXECUTE FUNCTION protect_transaction();

CREATE FUNCTION protect_chargeback() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE original transactions; assigned batches;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'chargebacks are immutable' USING ERRCODE = '23514';
    END IF;
    IF TG_OP = 'INSERT' THEN
        SELECT * INTO STRICT original FROM transactions WHERE id = NEW.transaction_id;
        IF original.customer_id <> NEW.customer_id OR NEW.amount_minor > original.amount_minor THEN
            RAISE EXCEPTION 'invalid transaction ownership or amount' USING ERRCODE = '23514';
        END IF;
    ELSE
        IF (NEW.id, NEW.customer_id, NEW.transaction_id, NEW.idempotency_key, NEW.request_hash,
            NEW.amount_minor, NEW.reason, NEW.received_at, NEW.partition)
            IS DISTINCT FROM
           (OLD.id, OLD.customer_id, OLD.transaction_id, OLD.idempotency_key, OLD.request_hash,
            OLD.amount_minor, OLD.reason, OLD.received_at, OLD.partition) THEN
            RAISE EXCEPTION 'chargeback financial fields are immutable' USING ERRCODE = '23514';
        END IF;
        -- The first claim is permanent. Retries can only resume this same file.
        IF OLD.batch_id IS NOT NULL AND NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
            RAISE EXCEPTION 'batch assignment is permanent' USING ERRCODE = '23514';
        END IF;
    END IF;
    IF NEW.batch_id IS NOT NULL AND (TG_OP = 'INSERT' OR OLD.batch_id IS NULL) THEN
        SELECT * INTO STRICT assigned FROM batches WHERE id = NEW.batch_id FOR SHARE;
        SELECT * INTO STRICT original FROM transactions WHERE id = NEW.transaction_id;
        IF assigned.state <> 'building' OR assigned.network <> original.network OR
           assigned.partition <> NEW.partition OR NEW.received_at >= assigned.slot THEN
            RAISE EXCEPTION 'ineligible batch assignment' USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER chargeback_integrity BEFORE INSERT OR UPDATE OR DELETE ON chargebacks
    FOR EACH ROW EXECUTE FUNCTION protect_chargeback();

CREATE FUNCTION protect_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'batches are permanent' USING ERRCODE = '23514';
    END IF;
    IF (NEW.id, NEW.slot, NEW.network, NEW.partition, NEW.created_at) IS DISTINCT FROM
       (OLD.id, OLD.slot, OLD.network, OLD.partition, OLD.created_at) THEN
        RAISE EXCEPTION 'batch identity is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.state <> 'building' AND
       (NEW.row_count, NEW.object_key, NEW.sha256, NEW.byte_count) IS DISTINCT FROM
       (OLD.row_count, OLD.object_key, OLD.sha256, OLD.byte_count) THEN
        RAISE EXCEPTION 'published artifact is immutable' USING ERRCODE = '23514';
    END IF;
    IF OLD.state <> 'building' AND NEW.state = 'building' THEN
        RAISE EXCEPTION 'cannot reopen batch membership' USING ERRCODE = '23514';
    END IF;
    IF OLD.state IN ('acknowledged','rejected','conflict') AND NEW.state <> OLD.state THEN
        RAISE EXCEPTION 'terminal batch state is permanent' USING ERRCODE = '23514';
    END IF;
    IF OLD.publication_started AND NOT NEW.publication_started THEN
        RAISE EXCEPTION 'publication intent is permanent' USING ERRCODE = '23514';
    END IF;
    NEW.updated_at = clock_timestamp();
    RETURN NEW;
END;
$$;
CREATE TRIGGER batch_integrity BEFORE UPDATE OR DELETE ON batches
    FOR EACH ROW EXECUTE FUNCTION protect_batch();

CREATE FUNCTION audit_chargeback() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO audit_events(chargeback_id,event) VALUES (NEW.id,'chargeback.accepted');
    ELSIF NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
        INSERT INTO audit_events(chargeback_id,batch_id,event)
            VALUES (NEW.id,NEW.batch_id,'chargeback.assigned');
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER chargeback_audit AFTER INSERT OR UPDATE ON chargebacks
    FOR EACH ROW EXECUTE FUNCTION audit_chargeback();

CREATE FUNCTION audit_batch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' OR NEW.state IS DISTINCT FROM OLD.state THEN
        INSERT INTO audit_events(batch_id,event,details)
            VALUES (NEW.id,'batch.' || NEW.state,jsonb_build_object('state',NEW.state));
    END IF;
    -- Publication intent and its audit event commit together before the remote rename.
    IF TG_OP = 'UPDATE' AND NEW.publication_started AND NOT OLD.publication_started THEN
        INSERT INTO audit_events(batch_id,event) VALUES (NEW.id,'publication_intent');
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER batch_audit AFTER INSERT OR UPDATE ON batches
    FOR EACH ROW EXECUTE FUNCTION audit_batch();

CREATE FUNCTION protect_audit() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit events are append only' USING ERRCODE = '23514';
END;
$$;
CREATE TRIGGER append_only_audit BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION protect_audit();
