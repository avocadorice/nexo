package delivery

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

type deliveryFixture struct {
	worker         Worker
	root, customer string
	objects        []string
}

func deliverySetup(t *testing.T) *deliveryFixture {
	t.Helper()
	databaseURL := os.Getenv("TEST_DATABASE_URL")
	if databaseURL == "" {
		t.Skip("TEST_DATABASE_URL required for real PostgreSQL/SFTP tests")
	}
	repo, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	python := filepath.Join(repo, ".venv", "bin", "python")
	if _, err = os.Stat(python); err != nil {
		t.Skip("uv sync required for the real Python SFTP simulator")
	}
	ctx := context.Background()
	admin, err := pgx.Connect(ctx, databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	schema := "delivery_test_" + strings.ReplaceAll(newID(), "-", "")
	if _, err = admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	config, err := pgxpool.ParseConfig(databaseURL)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close(); _, _ = admin.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE"); _ = admin.Close(ctx) })
	migration, err := os.ReadFile(filepath.Join(repo, "migrations", "001_initial.sql"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, string(migration)); err != nil {
		t.Fatal(err)
	}
	f := &deliveryFixture{root: t.TempDir(), customer: newID(), worker: Worker{Pool: pool}}
	if _, err = pool.Exec(ctx, `INSERT INTO customers(id,label) VALUES($1,'delivery integration test')`, f.customer); err != nil {
		t.Fatal(err)
	}
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	address := listener.Addr().String()
	_, port, _ := net.SplitHostPort(address)
	_ = listener.Close()
	private, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	keyPath := filepath.Join(f.root, "host_key")
	if err = os.WriteFile(keyPath, pem.EncodeToMemory(&pem.Block{Type: "RSA PRIVATE KEY", Bytes: x509.MarshalPKCS1PrivateKey(private)}), 0600); err != nil {
		t.Fatal(err)
	}
	signer, err := ssh.NewSignerFromKey(private)
	if err != nil {
		t.Fatal(err)
	}
	trustPath := filepath.Join(f.root, "known_hosts")
	if err = os.WriteFile(trustPath, []byte(knownhosts.Line([]string{address}, signer.PublicKey())+"\n"), 0600); err != nil {
		t.Fatal(err)
	}
	f.worker.Config = Config{SFTPAddress: address, SFTPUser: "nexo-test", SFTPPassword: newID() + newID(), KnownHosts: trustPath, Poll: time.Second}
	logFile, err := os.Create(filepath.Join(f.root, "simulator.log"))
	if err != nil {
		t.Fatal(err)
	}
	process := exec.Command(python, "-m", "nexo.cli", "simulator")
	process.Dir = repo
	process.Env = append(os.Environ(), "SIM_ROOT="+f.root, "SIM_HOST_KEY="+keyPath, "SIM_HOST=127.0.0.1", "SIM_PORT="+port, "SFTP_USER="+f.worker.Config.SFTPUser, "SFTP_PASSWORD="+f.worker.Config.SFTPPassword, "PYTHONPATH="+filepath.Join(repo, "src"))
	process.Stdout = logFile
	process.Stderr = logFile
	if err = process.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = process.Process.Kill(); _ = process.Wait(); _ = logFile.Close() })
	deadline := time.Now().Add(10 * time.Second)
	for {
		remote, connectErr := OpenSFTP(f.worker.Config)
		if connectErr == nil {
			remote.Close()
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("SFTP simulator did not start: %T", connectErr)
		}
		time.Sleep(50 * time.Millisecond)
	}
	if endpoint := os.Getenv("TEST_S3_ENDPOINT"); endpoint != "" {
		if credentialsPath := os.Getenv("TEST_S3_CREDENTIALS"); credentialsPath != "" {
			data, err := os.ReadFile(credentialsPath)
			if err != nil {
				t.Fatal("cannot read test S3 credentials file")
			}
			var values struct {
				Key    string `json:"s3_key"`
				Secret string `json:"s3_secret"`
			}
			if err = json.Unmarshal(data, &values); err != nil || values.Key == "" || values.Secret == "" {
				t.Fatal("invalid test S3 credential fields")
			}
			t.Setenv("AWS_ACCESS_KEY_ID", values.Key)
			t.Setenv("AWS_SECRET_ACCESS_KEY", values.Secret)
		}
		t.Setenv("AWS_REGION", "us-east-1")
		t.Setenv("AWS_EC2_METADATA_DISABLED", "true")
		f.worker.Config.Endpoint = endpoint
		f.worker.Config.Bucket = "nexo-delivery-test-" + newID()
		f.worker.Store, err = NewObjectStore(ctx, f.worker.Config)
		if err != nil {
			t.Fatal(err)
		}
		if _, err = f.worker.Store.CreateBucket(ctx, &s3.CreateBucketInput{Bucket: aws.String(f.worker.Config.Bucket)}); err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() {
			cleanup, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			defer cancel()
			for _, key := range f.objects {
				_, _ = f.worker.Store.DeleteObject(cleanup, &s3.DeleteObjectInput{Bucket: aws.String(f.worker.Config.Bucket), Key: aws.String(key)})
			}
			_, _ = f.worker.Store.DeleteBucket(cleanup, &s3.DeleteBucketInput{Bucket: aws.String(f.worker.Config.Bucket)})
		})
	}
	return f
}

func (f *deliveryFixture) mode(t *testing.T, mode string) {
	t.Helper()
	data, _ := json.Marshal(map[string]any{"mode": mode, "delay_seconds": 2})
	path := filepath.Join(f.root, "mode.json")
	if err := os.WriteFile(path+".tmp", data, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		t.Fatal(err)
	}
}

func (f *deliveryFixture) batch(t *testing.T, state string, publication bool, archive bool) (Batch, []byte) {
	t.Helper()
	ctx := context.Background()
	transaction, chargeback, id := newID(), newID(), newID()
	data := []byte(fmt.Sprintf("chargeback_id,transaction_id,network,currency,amount_minor,reason\r\n%s,%s,visa,USD,123,OTHER\r\n", chargeback, transaction))
	batch := Batch{ID: id, State: state, ObjectKey: "test/" + id + ".csv", SHA256: digest(data), Rows: 1, Bytes: int64(len(data)), PublicationStarted: publication}
	slot := time.Now().UTC().Truncate(6 * time.Hour).Add(6 * time.Hour)
	tx, err := f.worker.Pool.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(ctx)
	statements := []struct {
		sql  string
		args []any
	}{
		{`INSERT INTO slots(cutoff) VALUES($1) ON CONFLICT DO NOTHING`, []any{slot}},
		{`INSERT INTO transactions(id,customer_id,network,currency,amount_minor) VALUES($1,$2,'visa','USD',123)`, []any{transaction, f.customer}},
		{`INSERT INTO batches(id,slot,network,partition,row_count) VALUES($1,$2,'visa',0,1)`, []any{id, slot}},
		{`INSERT INTO chargebacks(id,customer_id,transaction_id,idempotency_key,request_hash,amount_minor,reason,partition,batch_id) VALUES($1,$2,$3,$5,repeat('a',64),123,'OTHER',0,$4)`, []any{chargeback, f.customer, transaction, id, chargeback}},
		{`UPDATE batches SET state=$2,object_key=$3,sha256=$4,byte_count=$5,publication_started=$6 WHERE id=$1`, []any{id, state, batch.ObjectKey, batch.SHA256, batch.Bytes, publication}},
	}
	for _, statement := range statements {
		if _, err = tx.Exec(ctx, statement.sql, statement.args...); err != nil {
			t.Fatal(err)
		}
	}
	if err = tx.Commit(ctx); err != nil {
		t.Fatal(err)
	}
	if archive {
		if f.worker.Store == nil {
			t.Fatal("test requires actual S3 endpoint")
		}
		if _, err = f.worker.Store.PutObject(ctx, &s3.PutObjectInput{Bucket: aws.String(f.worker.Config.Bucket), Key: aws.String(batch.ObjectKey), Body: bytes.NewReader(data)}); err != nil {
			t.Fatal(err)
		}
		f.objects = append(f.objects, batch.ObjectKey)
	}
	return batch, data
}

func (f *deliveryFixture) process(t *testing.T, id string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := f.worker.Process(ctx, id); err != nil {
		t.Fatal(err)
	}
}

func (f *deliveryFixture) state(t *testing.T, id string) (string, bool) {
	t.Helper()
	var state string
	var publication bool
	if err := f.worker.Pool.QueryRow(context.Background(), `SELECT state,publication_started FROM batches WHERE id=$1`, id).Scan(&state, &publication); err != nil {
		t.Fatal(err)
	}
	return state, publication
}

func (f *deliveryFixture) awaitAck(t *testing.T, id string) {
	t.Helper()
	deadline := time.Now().Add(8 * time.Second)
	for {
		f.process(t, id)
		state, _ := f.state(t, id)
		if state == "acknowledged" {
			return
		}
		if state == "conflict" || state == "rejected" || time.Now().After(deadline) {
			t.Fatalf("expected acknowledgement, got %s", state)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func TestDeliveryRealServices(t *testing.T) {
	f := deliverySetup(t)
	t.Run("unknown_absence_never_republishes", func(t *testing.T) {
		f.mode(t, "missing_ack")
		batch, _ := f.batch(t, "unknown", true, false)
		f.process(t, batch.ID)
		f.process(t, batch.ID)
		if state, publication := f.state(t, batch.ID); state != "unknown" || !publication {
			t.Fatalf("unsafe state %s fence %v", state, publication)
		}
		if _, err := os.Stat(filepath.Join(f.root, "incoming", batch.ID+".csv")); !os.IsNotExist(err) {
			t.Fatal("unknown batch was republished")
		}
	})
	t.Run("remote_hash_mismatch_is_conflict", func(t *testing.T) {
		f.mode(t, "missing_ack")
		batch, _ := f.batch(t, "unknown", true, false)
		if err := os.WriteFile(filepath.Join(f.root, "incoming", batch.ID+".csv"), []byte("wrong file"), 0600); err != nil {
			t.Fatal(err)
		}
		f.process(t, batch.ID)
		if state, _ := f.state(t, batch.ID); state != "conflict" {
			t.Fatalf("mismatched remote file: %s", state)
		}
	})
	t.Run("submitted_knowledge_survives_absence_and_outage", func(t *testing.T) {
		f.mode(t, "missing_ack")
		batch, _ := f.batch(t, "submitted", true, false)
		f.process(t, batch.ID)
		if state, _ := f.state(t, batch.ID); state != "submitted" {
			t.Fatalf("absence regressed known submission: %s", state)
		}
		address := f.worker.Config.SFTPAddress
		f.worker.Config.SFTPAddress = "127.0.0.1:1"
		defer func() { f.worker.Config.SFTPAddress = address }()
		f.process(t, batch.ID)
		if state, _ := f.state(t, batch.ID); state != "submitted" {
			t.Fatalf("outage regressed known submission: %s", state)
		}
	})
	t.Run("normal_publication_and_late_ack", func(t *testing.T) {
		if f.worker.Store == nil {
			t.Skip("TEST_S3_ENDPOINT required for real object-storage publication")
		}
		f.mode(t, "late_ack")
		batch, data := f.batch(t, "ready", false, true)
		var workers sync.WaitGroup
		failures := make(chan error, 6)
		for range 6 {
			workers.Add(1)
			go func() {
				defer workers.Done()
				ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
				defer cancel()
				failures <- f.worker.Process(ctx, batch.ID)
			}()
		}
		workers.Wait()
		close(failures)
		for err := range failures {
			if err != nil {
				t.Fatal(err)
			}
		}
		if state, publication := f.state(t, batch.ID); state != "submitted" || !publication {
			t.Fatalf("publication state %s fence %v", state, publication)
		}
		remote, err := os.ReadFile(filepath.Join(f.root, "incoming", batch.ID+".csv"))
		if err != nil || !bytes.Equal(remote, data) {
			t.Fatalf("published bytes differ: %v", err)
		}
		f.awaitAck(t, batch.ID)
		var publications int
		if err := f.worker.Pool.QueryRow(context.Background(), `SELECT count(*) FROM audit_events WHERE batch_id=$1 AND event='publication_intent'`, batch.ID).Scan(&publications); err != nil || publications != 1 {
			t.Fatalf("concurrent publication count %d: %v", publications, err)
		}
	})
	t.Run("partial_upload_is_safely_retried", func(t *testing.T) {
		if f.worker.Store == nil {
			t.Skip("TEST_S3_ENDPOINT required for real object-storage publication")
		}
		f.mode(t, "partial_upload")
		batch, _ := f.batch(t, "ready", false, true)
		f.process(t, batch.ID)
		if state, publication := f.state(t, batch.ID); state != "ready" || publication {
			t.Fatalf("partial upload crossed publication fence: %s %v", state, publication)
		}
		if _, err := os.Stat(filepath.Join(f.root, "incoming", batch.ID+".csv")); !os.IsNotExist(err) {
			t.Fatal("partial upload published")
		}
		f.mode(t, "normal")
		f.awaitAck(t, batch.ID)
	})
	t.Run("lost_rename_reply_reconciles_same_file", func(t *testing.T) {
		if f.worker.Store == nil {
			t.Skip("TEST_S3_ENDPOINT required for real object-storage publication")
		}
		f.mode(t, "disconnect_after_rename")
		batch, data := f.batch(t, "ready", false, true)
		f.process(t, batch.ID)
		if state, publication := f.state(t, batch.ID); state != "unknown" || !publication {
			t.Fatalf("lost reply state %s fence %v", state, publication)
		}
		before, err := os.Stat(filepath.Join(f.root, "incoming", batch.ID+".csv"))
		if err != nil {
			t.Fatal(err)
		}
		f.mode(t, "normal")
		f.awaitAck(t, batch.ID)
		after, err := os.Stat(filepath.Join(f.root, "incoming", batch.ID+".csv"))
		if err != nil {
			t.Fatal(err)
		}
		actual, err := os.ReadFile(filepath.Join(f.root, "incoming", batch.ID+".csv"))
		if err != nil || !bytes.Equal(actual, data) || !os.SameFile(before, after) {
			t.Fatal("retry replaced original submitted file")
		}
		var n int
		if err = f.worker.Pool.QueryRow(context.Background(), `SELECT count(*) FROM audit_events WHERE batch_id=$1 AND event='publication_intent'`, batch.ID).Scan(&n); err != nil || n != 1 {
			t.Fatalf("publication count %d: %v", n, err)
		}
	})
}
