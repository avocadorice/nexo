package api

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

type fixture struct {
	pool                                                            *pgxpool.Pool
	handler                                                         http.Handler
	customerID, otherID, transactionID, token, otherToken, opsToken string
}

func setup(t *testing.T) fixture {
	t.Helper()
	url := os.Getenv("TEST_DATABASE_URL")
	if url == "" {
		t.Skip("TEST_DATABASE_URL required for PostgreSQL integration tests")
	}
	ctx := context.Background()
	admin, err := pgx.Connect(ctx, url)
	if err != nil {
		t.Fatal(err)
	}
	schema := "api_test_" + strings.ReplaceAll(newID(), "-", "")
	if _, err = admin.Exec(ctx, "CREATE SCHEMA "+schema); err != nil {
		t.Fatal(err)
	}
	config, err := pgxpool.ParseConfig(url)
	if err != nil {
		t.Fatal(err)
	}
	config.ConnConfig.RuntimeParams["search_path"] = schema
	config.MaxConns = 16
	pool, err := pgxpool.NewWithConfig(ctx, config)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { pool.Close(); _, _ = admin.Exec(ctx, "DROP SCHEMA "+schema+" CASCADE"); _ = admin.Close(ctx) })
	migration, err := os.ReadFile(filepath.Join("..", "..", "migrations", "001_initial.sql"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = pool.Exec(ctx, string(migration)); err != nil {
		t.Fatal(err)
	}
	f := fixture{pool: pool, handler: New(pool, 4, ""), customerID: newID(), otherID: newID(), transactionID: newID(), token: newID() + newID(), otherToken: newID() + newID(), opsToken: newID() + newID()}
	for _, id := range []string{f.customerID, f.otherID} {
		if _, err = pool.Exec(ctx, `INSERT INTO customers(id,label) VALUES($1,'test customer')`, id); err != nil {
			t.Fatal(err)
		}
	}
	for _, entry := range []struct {
		token    string
		customer any
		role     string
	}{{f.token, f.customerID, "customer"}, {f.otherToken, f.otherID, "customer"}, {f.opsToken, nil, "ops"}} {
		digest := sha256.Sum256([]byte(entry.token))
		if _, err = pool.Exec(ctx, `INSERT INTO tokens(hash,customer_id,role,expires_at) VALUES($1,$2,$3,clock_timestamp()+interval '1 hour')`, hex.EncodeToString(digest[:]), entry.customer, entry.role); err != nil {
			t.Fatal(err)
		}
	}
	if _, err = pool.Exec(ctx, `INSERT INTO transactions(id,customer_id,network,currency,amount_minor) VALUES($1,$2,'visa','USD',9007199254740993)`, f.transactionID, f.customerID); err != nil {
		t.Fatal(err)
	}
	return f
}

func request(h http.Handler, method, path, token, key string, body any) *httptest.ResponseRecorder {
	var payload bytes.Buffer
	if body != nil {
		_ = json.NewEncoder(&payload).Encode(body)
	}
	r := httptest.NewRequest(method, path, &payload)
	if token != "" {
		r.Header.Set("Authorization", "Bearer "+token)
	}
	if key != "" {
		r.Header.Set("Idempotency-Key", key)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestConcurrentIntakeAndAuthorization(t *testing.T) {
	f := setup(t)
	body := CreateRequest{TransactionID: f.transactionID, AmountMinor: "9007199254740993", Reason: "FRAUD"}
	const workers = 12
	responses := make(chan *httptest.ResponseRecorder, workers)
	var group sync.WaitGroup
	for range workers {
		group.Add(1)
		go func() {
			defer group.Done()
			responses <- request(f.handler, "POST", "/api/chargebacks", f.token, "same-request", body)
		}()
	}
	group.Wait()
	close(responses)
	created := 0
	id := ""
	for response := range responses {
		if response.Code != 200 && response.Code != 201 {
			t.Fatalf("concurrent intake: %d %s", response.Code, response.Body)
		}
		if response.Code == 201 {
			created++
		}
		var result map[string]any
		if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
			t.Fatal(err)
		}
		if result["amount_minor"] != "9007199254740993" {
			t.Fatal("amount lost precision")
		}
		if id == "" {
			id = result["id"].(string)
		} else if id != result["id"] {
			t.Fatal("replay returned different id")
		}
	}
	if created != 1 {
		t.Fatalf("created %d times", created)
	}
	var n int
	if err := f.pool.QueryRow(context.Background(), `SELECT count(*) FROM audit_events WHERE event='chargeback.accepted'`).Scan(&n); err != nil || n != 1 {
		t.Fatalf("audit count %d: %v", n, err)
	}
	checks := []struct {
		method, path, token, key string
		body                     any
		status                   int
	}{
		{"GET", "/api/chargebacks/" + id, f.otherToken, "", nil, 404},
		{"GET", "/api/chargebacks/" + id, f.token, "", nil, 200},
		{"GET", "/api/chargebacks/" + id, "", "", nil, 401},
		{"GET", "/api/ops/summary", f.token, "", nil, 403},
		{"GET", "/api/transactions", f.opsToken, "", nil, 403},
		{"GET", "/api/ops/summary", f.opsToken, "", nil, 200},
		{"POST", "/api/chargebacks", f.token, "different-key", body, 409},
		{"POST", "/api/chargebacks", f.otherToken, "other", body, 404},
	}
	for _, check := range checks {
		r := request(f.handler, check.method, check.path, check.token, check.key, check.body)
		if r.Code != check.status {
			t.Fatalf("%s %s: got %d want %d: %s", check.method, check.path, r.Code, check.status, r.Body)
		}
	}
	body.AmountMinor = "123"
	if r := request(f.handler, "POST", "/api/chargebacks", f.token, "same-request", body); r.Code != 409 {
		t.Fatalf("changed content: %d", r.Code)
	}
	if _, err := f.pool.Exec(context.Background(), `UPDATE tokens SET revoked=true WHERE customer_id=$1`, f.customerID); err != nil {
		t.Fatal(err)
	}
	if r := request(f.handler, "GET", "/api/transactions", f.token, "", nil); r.Code != 401 {
		t.Fatalf("revoked token: %d", r.Code)
	}
}

func TestValidationAndExactAmounts(t *testing.T) {
	for _, amount := range []string{"0", "-1", "1.0", "01", "1e2", "9223372036854775808", " 1", ""} {
		req := CreateRequest{TransactionID: newID(), AmountMinor: amount, Reason: "FRAUD"}
		if _, err := validateRequest(&req); err == nil {
			t.Errorf("accepted invalid amount %q", amount)
		}
	}
	req := CreateRequest{TransactionID: newID(), AmountMinor: "9223372036854775807", Reason: "OTHER"}
	if n, err := validateRequest(&req); err != nil || n != 9223372036854775807 {
		t.Fatalf("int64 maximum %d: %v", n, err)
	}
	f := setup(t)
	for _, body := range []any{
		map[string]any{"transaction_id": f.transactionID, "amount_minor": 123, "reason": "FRAUD"},
		map[string]any{"transaction_id": f.transactionID, "amount_minor": "123", "reason": "FRAUD", "customer_id": f.otherID},
		CreateRequest{TransactionID: f.transactionID, AmountMinor: "9007199254740994", Reason: "FRAUD"},
	} {
		if r := request(f.handler, "POST", "/api/chargebacks", f.token, newID(), body); r.Code != 400 {
			t.Fatalf("bad input: %d %s", r.Code, r.Body)
		}
	}
	if r := request(f.handler, "POST", "/api/chargebacks", f.token, "", req); r.Code != 400 {
		t.Fatalf("missing idempotency key: %d", r.Code)
	}
}

func TestDatabaseAssignmentAndArtifactImmutability(t *testing.T) {
	f := setup(t)
	ctx := context.Background()
	r := request(f.handler, "POST", "/api/chargebacks", f.token, "claim", CreateRequest{TransactionID: f.transactionID, AmountMinor: "123", Reason: "OTHER"})
	if r.Code != 201 {
		t.Fatal(r.Body.String())
	}
	var cb struct {
		ID string `json:"id"`
	}
	if err := json.Unmarshal(r.Body.Bytes(), &cb); err != nil {
		t.Fatal(err)
	}
	var partition int
	if err := f.pool.QueryRow(ctx, `SELECT partition FROM chargebacks WHERE id=$1`, cb.ID).Scan(&partition); err != nil {
		t.Fatal(err)
	}
	slot := time.Now().UTC().Truncate(6 * time.Hour).Add(6 * time.Hour)
	if _, err := f.pool.Exec(ctx, `INSERT INTO slots(cutoff) VALUES($1)`, slot); err != nil {
		t.Fatal(err)
	}
	first, second := newID(), newID()
	for _, id := range []string{first, second} {
		if _, err := f.pool.Exec(ctx, `INSERT INTO batches(id,slot,network,partition) VALUES($1,$2,'visa',$3)`, id, slot, partition); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := f.pool.Exec(ctx, `UPDATE chargebacks SET batch_id=$1 WHERE id=$2`, first, cb.ID); err != nil {
		t.Fatal(err)
	}
	for _, query := range []string{
		fmt.Sprintf(`UPDATE chargebacks SET batch_id='%s' WHERE id='%s'`, second, cb.ID),
		fmt.Sprintf(`UPDATE chargebacks SET batch_id=NULL WHERE id='%s'`, cb.ID),
		fmt.Sprintf(`UPDATE chargebacks SET amount_minor=124 WHERE id='%s'`, cb.ID),
		fmt.Sprintf(`DELETE FROM chargebacks WHERE id='%s'`, cb.ID),
		`DELETE FROM audit_events`,
	} {
		if _, err := f.pool.Exec(ctx, query); err == nil {
			t.Fatalf("invariant allowed: %s", query)
		}
	}
	if _, err := f.pool.Exec(ctx, `UPDATE batches SET state='ready',row_count=1,object_key='batches/test.csv',sha256=repeat('a',64),byte_count=100 WHERE id=$1`, first); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, `UPDATE batches SET sha256=repeat('b',64) WHERE id=$1`, first); err == nil {
		t.Fatal("artifact hash changed")
	}
	for range 2 {
		if _, err := f.pool.Exec(ctx, `UPDATE batches SET publication_started=true WHERE id=$1`, first); err != nil {
			t.Fatal(err)
		}
	}
	var publications int
	if err := f.pool.QueryRow(ctx, `SELECT count(*) FROM audit_events WHERE batch_id=$1 AND event='publication_intent'`, first).Scan(&publications); err != nil || publications != 1 {
		t.Fatalf("publication audit count %d: %v", publications, err)
	}
	if _, err := f.pool.Exec(ctx, `UPDATE batches SET publication_started=false WHERE id=$1`, first); err == nil {
		t.Fatal("publication intent reverted")
	}
	if _, err := f.pool.Exec(ctx, `UPDATE batches SET state='acknowledged' WHERE id=$1`, first); err != nil {
		t.Fatal(err)
	}
	if _, err := f.pool.Exec(ctx, `UPDATE batches SET state='ready' WHERE id=$1`, first); err == nil {
		t.Fatal("terminal state regressed")
	}
}
