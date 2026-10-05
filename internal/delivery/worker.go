package delivery

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/pkg/sftp"
)

type Batch struct {
	ID, State, ObjectKey, SHA256 string
	Rows                         int
	Bytes                        int64
	PublicationStarted           bool
}

type Ack struct {
	Version  int     `json:"version"`
	BatchID  string  `json:"batch_id"`
	SHA256   string  `json:"sha256"`
	RowCount int     `json:"row_count"`
	Status   string  `json:"status"`
	Reason   *string `json:"reason"`
}

type Worker struct {
	Pool   *pgxpool.Pool
	Store  *s3.Client
	Config Config
}

func newID() string {
	b := make([]byte, 16)
	if _, err := rand.Read(b); err != nil {
		panic(err)
	}
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

func ValidateAck(data []byte, batch Batch) (Ack, error) {
	var ack Ack
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&ack); err != nil {
		return ack, fmt.Errorf("invalid_ack")
	}
	var trailing any
	if decoder.Decode(&trailing) != io.EOF {
		return ack, fmt.Errorf("invalid_ack")
	}
	if ack.Version != 1 || ack.BatchID != batch.ID || ack.SHA256 != batch.SHA256 || ack.RowCount != batch.Rows || (ack.Status != "accepted" && ack.Status != "rejected") {
		return ack, fmt.Errorf("ack_integrity_mismatch")
	}
	return ack, nil
}

// Reconcile never treats a missing remote file as proof that publication did not happen.
func Reconcile(remote *sftp.Client, batch Batch) (state string, acknowledgement []byte, err error) {
	data, found, err := readRemote(remote, "/acks/"+batch.ID+".json", 16384)
	if err != nil {
		return "", nil, err
	}
	if found {
		ack, err := ValidateAck(data, batch)
		if err != nil {
			return "conflict", nil, nil
		}
		if ack.Status == "accepted" {
			return "acknowledged", data, nil
		}
		return "rejected", data, nil
	}
	data, found, err = readRemote(remote, "/incoming/"+batch.ID+".csv", MaxFileBytes)
	if err != nil {
		return "", nil, err
	}
	if found {
		if digest(data) != batch.SHA256 || int64(len(data)) != batch.Bytes {
			return "conflict", nil, nil
		}
		return "submitted", nil, nil
	}
	if batch.State == "submitted" {
		return "submitted", nil, nil
	}
	if batch.PublicationStarted {
		return "unknown", nil, nil
	}
	return "ready", nil, nil
}

// Publish has a durable fence before the only operation that exposes a final file.
func Publish(ctx context.Context, conn *pgxpool.Conn, remote *sftp.Client, batch *Batch, attempt string, data []byte) error {
	temp := "/incoming/." + batch.ID + "." + attempt + ".tmp"
	defer remote.Remove(temp) // Only temporary bytes; a published file is never removed.
	file, err := remote.OpenFile(temp, os.O_WRONLY|os.O_CREATE|os.O_EXCL)
	if err != nil {
		return err
	}
	_, copyErr := io.Copy(file, bytes.NewReader(data))
	closeErr := file.Close()
	if copyErr != nil {
		return copyErr
	}
	if closeErr != nil {
		return closeErr
	}
	uploaded, found, err := readRemote(remote, temp, MaxFileBytes)
	if err != nil {
		return err
	}
	if !found || digest(uploaded) != batch.SHA256 {
		return fmt.Errorf("temporary_integrity_mismatch")
	}
	// temp bytes verified -> COMMIT intent -> atomic rename -> verify final
	// Any crash after COMMIT is ambiguous, including a lost rename response.
	result, err := conn.Exec(ctx, `UPDATE batches SET publication_started=true,state='unknown',last_error='publication_pending' WHERE id=$1 AND state='ready' AND NOT publication_started`, batch.ID)
	if err != nil {
		return err
	}
	if result.RowsAffected() != 1 {
		return fmt.Errorf("publication_fence_changed")
	}
	batch.PublicationStarted = true
	return remote.Rename(temp, "/incoming/"+batch.ID+".csv")
}

func finish(ctx context.Context, conn *pgxpool.Conn, batch Batch, attempt, state, detail string, ack []byte) error {
	tx, err := conn.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)
	var acknowledgement any
	if ack != nil {
		acknowledgement = string(ack)
	}
	_, err = tx.Exec(ctx, `UPDATE batches SET state=$2,last_error=NULLIF($3,''),ack=COALESCE($4::jsonb,ack),
        next_attempt_at=clock_timestamp()+interval '30 seconds' WHERE id=$1 AND state IN ('ready','unknown','submitted')`, batch.ID, state, detail, acknowledgement)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `UPDATE delivery_attempts SET finished_at=clock_timestamp(),outcome=$2,detail=NULLIF($3,'') WHERE id=$1`, attempt, state, detail)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}

func (worker *Worker) Process(ctx context.Context, id string) error {
	conn, err := worker.Pool.Acquire(ctx)
	if err != nil {
		return err
	}
	defer conn.Release()
	var locked bool
	if err = conn.QueryRow(ctx, `SELECT pg_try_advisory_lock(hashtextextended($1,817203))`, id).Scan(&locked); err != nil || !locked {
		return err
	}
	defer func() {
		release, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		if _, err := conn.Exec(release, `SELECT pg_advisory_unlock(hashtextextended($1,817203))`, id); err != nil {
			conn.Conn().Close(release)
		}
	}()
	var batch Batch
	err = conn.QueryRow(ctx, `SELECT id::text,state,object_key,sha256,row_count,byte_count,publication_started FROM batches WHERE id=$1 AND state IN ('ready','unknown','submitted')`, id).
		Scan(&batch.ID, &batch.State, &batch.ObjectKey, &batch.SHA256, &batch.Rows, &batch.Bytes, &batch.PublicationStarted)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	attempt := newID()
	// A process that died left its attempt visible. The lock proves it no longer owns this batch.
	_, err = conn.Exec(ctx, `UPDATE delivery_attempts SET finished_at=clock_timestamp(),outcome='interrupted',detail='worker_lost' WHERE batch_id=$1 AND finished_at IS NULL`, id)
	if err != nil {
		return err
	}
	_, err = conn.Exec(ctx, `INSERT INTO delivery_attempts(id,batch_id) VALUES($1,$2)`, attempt, id)
	if err != nil {
		return err
	}
	state, detail := "ready", "transport_error"
	if batch.PublicationStarted {
		state = "unknown"
	}
	if batch.State == "submitted" {
		state = "submitted"
	}
	remote, err := OpenSFTP(worker.Config)
	if err != nil {
		return finish(ctx, conn, batch, attempt, state, detail, nil)
	}
	defer remote.Close()
	state, ack, err := Reconcile(remote.Client, batch)
	if err == nil && state == "ready" {
		data, readErr := ReadArtifact(ctx, worker.Store, worker.Config.Bucket, batch)
		if readErr != nil {
			if readErr.Error() == "artifact_integrity_mismatch" {
				return finish(ctx, conn, batch, attempt, "conflict", "artifact_integrity_mismatch", nil)
			}
			return finish(ctx, conn, batch, attempt, "ready", "object_read_failed", nil)
		}
		err = Publish(ctx, conn, remote.Client, &batch, attempt, data)
		if err == nil {
			state, ack, err = Reconcile(remote.Client, batch)
		}
	}
	if err != nil {
		state = "ready"
		if batch.PublicationStarted {
			state = "unknown"
		}
		if batch.State == "submitted" {
			state = "submitted"
		}
	} else {
		detail = ""
		if state == "unknown" {
			detail = "publication_outcome_unknown"
		}
		if state == "conflict" {
			detail = "remote_integrity_mismatch"
		}
	}
	slog.Info("delivery_attempt", "batch_id", batch.ID, "attempt_id", attempt, "outcome", state)
	return finish(ctx, conn, batch, attempt, state, detail, ack)
}

func (worker *Worker) Run(ctx context.Context) error {
	ticker := time.NewTicker(worker.Config.Poll)
	defer ticker.Stop()
	for {
		rows, err := worker.Pool.Query(ctx, `SELECT id::text FROM batches WHERE state IN ('ready','unknown','submitted') AND next_attempt_at<=clock_timestamp() ORDER BY next_attempt_at LIMIT 20`)
		if err != nil {
			return err
		}
		ids := []string{}
		for rows.Next() {
			var id string
			if err = rows.Scan(&id); err != nil {
				rows.Close()
				return err
			}
			ids = append(ids, id)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, id := range ids {
			call, cancel := context.WithTimeout(ctx, 60*time.Second)
			err = worker.Process(call, id)
			cancel()
			if err != nil && ctx.Err() == nil {
				slog.Error("delivery_failed", "batch_id", id, "error_type", fmt.Sprintf("%T", err))
			}
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}
