package api

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"nexo/internal/auth"
)

type Server struct {
	Pool       *pgxpool.Pool
	Partitions int
}
type principalKey struct{}

var uuidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
var amountPattern = regexp.MustCompile(`^[1-9][0-9]{0,18}$`)
var keyPattern = regexp.MustCompile(`^[!-~]{1,128}$`)

func New(pool *pgxpool.Pool, partitions int, staticDir string) http.Handler {
	s := &Server{Pool: pool, Partitions: partitions}
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, r *http.Request) { writeJSON(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("GET /readyz", func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
		defer cancel()
		if pool.Ping(ctx) != nil {
			fail(w, 503, "database unavailable")
			return
		}
		writeJSON(w, 200, map[string]string{"status": "ok"})
	})
	mux.Handle("GET /api/transactions", s.authorize("customer", s.ListTransactions))
	mux.Handle("POST /api/chargebacks", s.authorize("customer", s.CreateChargeback))
	mux.Handle("GET /api/chargebacks", s.authorize("customer", s.ListChargebacks))
	mux.Handle("GET /api/chargebacks/{id}", s.authorize("customer", s.GetChargeback))
	mux.Handle("GET /api/ops/summary", s.authorize("ops", s.OpsSummary))
	mux.Handle("GET /api/ops/batches", s.authorize("ops", s.ListBatches))
	mux.Handle("GET /api/ops/batches/{id}", s.authorize("ops", s.GetBatch))
	if staticDir != "" {
		mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServer(http.Dir(staticDir))))
		mux.HandleFunc("GET /{$}", func(w http.ResponseWriter, r *http.Request) {
			http.Redirect(w, r, "/static/index.html", http.StatusTemporaryRedirect)
		})
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("X-Content-Type-Options", "nosniff")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; frame-ancestors 'none'; base-uri 'self'")
		w.Header().Set("Cache-Control", "no-store")
		ctx, cancel := context.WithTimeout(r.Context(), 12*time.Second)
		defer cancel()
		mux.ServeHTTP(w, r.WithContext(ctx))
	})
}

func (s *Server) authorize(role string, next http.HandlerFunc) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		principal, err := auth.Authenticate(r.Context(), s.Pool, r)
		if errors.Is(err, auth.ErrUnauthorized) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			fail(w, 401, "authentication required")
			return
		}
		if err != nil {
			fail(w, 503, "database unavailable")
			return
		}
		if principal.Role != role {
			fail(w, 403, "role not permitted")
			return
		}
		next(w, r.WithContext(context.WithValue(r.Context(), principalKey{}, principal)))
	})
}

func customer(r *http.Request) string {
	return *r.Context().Value(principalKey{}).(auth.Principal).CustomerID
}
func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}
func fail(w http.ResponseWriter, status int, message string) {
	writeJSON(w, status, map[string]string{"error": message})
}
func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 15) | 64
	b[8] = (b[8] & 63) | 128
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:16])
}

type CreateRequest struct {
	TransactionID string `json:"transaction_id"`
	AmountMinor   string `json:"amount_minor"`
	Reason        string `json:"reason"`
}

func validateRequest(req *CreateRequest) (int64, error) {
	if !uuidPattern.MatchString(req.TransactionID) {
		return 0, errors.New("transaction_id must be a UUID")
	}
	req.TransactionID = strings.ToLower(req.TransactionID)
	if !amountPattern.MatchString(req.AmountMinor) {
		return 0, errors.New("amount_minor must be a positive integer string")
	}
	amount, err := strconv.ParseInt(req.AmountMinor, 10, 64)
	if err != nil {
		return 0, errors.New("amount_minor exceeds supported range")
	}
	switch req.Reason {
	case "FRAUD", "DUPLICATE", "NOT_RECEIVED", "OTHER":
	default:
		return 0, errors.New("invalid reason")
	}
	return amount, nil
}

// CreateChargeback commits intake and its audit record before returning an accepted identifier.
func (s *Server) CreateChargeback(w http.ResponseWriter, r *http.Request) {
	key := r.Header.Get("Idempotency-Key")
	if !keyPattern.MatchString(key) {
		fail(w, 400, "Idempotency-Key must contain 1 to 128 visible ASCII characters")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 4096)
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	var req CreateRequest
	if err := decoder.Decode(&req); err != nil {
		fail(w, 400, "invalid JSON request")
		return
	}
	var extra any
	if err := decoder.Decode(&extra); err != io.EOF {
		fail(w, 400, "request must contain one JSON object")
		return
	}
	amount, err := validateRequest(&req)
	if err != nil {
		fail(w, 400, err.Error())
		return
	}
	canonical, _ := json.Marshal(req)
	digest := sha256.Sum256(canonical)
	requestHash := hex.EncodeToString(digest[:])
	tx, err := s.Pool.Begin(r.Context())
	if err != nil {
		fail(w, 503, "database unavailable")
		return
	}
	defer func() { _ = tx.Rollback(r.Context()) }()
	var id, priorHash string
	err = tx.QueryRow(r.Context(), `SELECT id::text,request_hash FROM chargebacks WHERE customer_id=$1 AND idempotency_key=$2`, customer(r), key).Scan(&id, &priorHash)
	if err == nil {
		if priorHash != requestHash {
			fail(w, 409, "idempotency key has different request content")
			return
		}
		if err = tx.Commit(r.Context()); err != nil {
			fail(w, 503, "commit outcome unknown; retry with the same key")
			return
		}
		s.respondChargeback(w, r, id, 200)
		return
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		fail(w, 503, "database unavailable")
		return
	}
	var originalAmount int64
	err = tx.QueryRow(r.Context(), `SELECT amount_minor FROM transactions WHERE id=$1 AND customer_id=$2`, req.TransactionID, customer(r)).Scan(&originalAmount)
	if errors.Is(err, pgx.ErrNoRows) {
		fail(w, 404, "transaction not found")
		return
	}
	if err != nil {
		fail(w, 503, "database unavailable")
		return
	}
	if amount > originalAmount {
		fail(w, 400, "amount exceeds original transaction")
		return
	}
	id = newID()
	partitionHash := sha256.Sum256([]byte(id))
	partition := int(binary.BigEndian.Uint32(partitionHash[:4]) % uint32(s.Partitions))
	// Either unique key can win this race. The loser reads the durable winning result.
	err = tx.QueryRow(r.Context(), `INSERT INTO chargebacks
		(id,customer_id,transaction_id,idempotency_key,request_hash,amount_minor,reason,partition)
		VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT DO NOTHING RETURNING id::text`, id, customer(r), req.TransactionID, key, requestHash, amount, req.Reason, partition).Scan(&id)
	status := 201
	if errors.Is(err, pgx.ErrNoRows) {
		err = tx.QueryRow(r.Context(), `SELECT id::text,request_hash FROM chargebacks WHERE customer_id=$1 AND idempotency_key=$2`, customer(r), key).Scan(&id, &priorHash)
		if errors.Is(err, pgx.ErrNoRows) {
			fail(w, 409, "transaction already disputed")
			return
		}
		if err == nil && priorHash != requestHash {
			fail(w, 409, "idempotency key has different request content")
			return
		}
		status = 200
	}
	if err != nil {
		fail(w, 503, "database unavailable")
		return
	}
	if err = tx.Commit(r.Context()); err != nil {
		fail(w, 503, "commit outcome unknown; retry with the same key")
		return
	}
	w.Header().Set("Location", "/api/chargebacks/"+id)
	s.respondChargeback(w, r, id, status)
}

const chargebackColumns = `c.id,c.transaction_id,c.amount_minor::text AS amount_minor,
 t.currency,t.network,c.reason,COALESCE(b.state,'pending') AS status,c.batch_id,c.received_at`
const chargebackJoins = ` FROM chargebacks c JOIN transactions t ON t.id=c.transaction_id LEFT JOIN batches b ON b.id=c.batch_id `

func (s *Server) respondChargeback(w http.ResponseWriter, r *http.Request, id string, status int) {
	var body []byte
	err := s.Pool.QueryRow(r.Context(), `SELECT row_to_json(q) FROM (SELECT `+chargebackColumns+chargebackJoins+` WHERE c.id=$1 AND c.customer_id=$2) q`, id, customer(r)).Scan(&body)
	if errors.Is(err, pgx.ErrNoRows) {
		fail(w, 404, "chargeback not found")
		return
	}
	if err != nil {
		fail(w, 503, "database unavailable")
		return
	}
	writeJSON(w, status, json.RawMessage(body))
}

func (s *Server) GetChargeback(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !uuidPattern.MatchString(id) {
		fail(w, 404, "chargeback not found")
		return
	}
	s.respondChargeback(w, r, id, 200)
}

func (s *Server) ListChargebacks(w http.ResponseWriter, r *http.Request) {
	s.queryJSON(w, r, `SELECT json_build_object('chargebacks',COALESCE(json_agg(q),'[]')) FROM
		(SELECT `+chargebackColumns+chargebackJoins+` WHERE c.customer_id=$1 ORDER BY c.received_at DESC,c.id LIMIT 100) q`, customer(r))
}

func (s *Server) ListTransactions(w http.ResponseWriter, r *http.Request) {
	s.queryJSON(w, r, `SELECT json_build_object('transactions',COALESCE(json_agg(q),'[]')) FROM
		(SELECT t.id,t.network,t.currency,t.amount_minor::text AS amount_minor,
		 EXISTS(SELECT 1 FROM chargebacks c WHERE c.transaction_id=t.id) AS disputed
		 FROM transactions t WHERE t.customer_id=$1 ORDER BY t.id LIMIT 100) q`, customer(r))
}

func (s *Server) ListBatches(w http.ResponseWriter, r *http.Request) {
	s.queryJSON(w, r, `SELECT json_build_object('batches',COALESCE(json_agg(q),'[]')) FROM
		(SELECT * FROM batches ORDER BY created_at DESC,id LIMIT 100) q`)
}

func (s *Server) GetBatch(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !uuidPattern.MatchString(id) {
		fail(w, 404, "batch not found")
		return
	}
	var body []byte
	err := s.Pool.QueryRow(r.Context(), `SELECT json_build_object('batch',to_jsonb(b),
		'attempts',COALESCE((SELECT json_agg(a) FROM (SELECT * FROM delivery_attempts WHERE batch_id=b.id ORDER BY started_at DESC LIMIT 100) a),'[]'),
		'events',COALESCE((SELECT json_agg(e) FROM (SELECT * FROM audit_events WHERE batch_id=b.id ORDER BY id DESC LIMIT 100) e),'[]'))
		FROM batches b WHERE b.id=$1`, id).Scan(&body)
	if errors.Is(err, pgx.ErrNoRows) {
		fail(w, 404, "batch not found")
		return
	}
	if err != nil {
		fail(w, 503, "database unavailable")
		return
	}
	writeJSON(w, 200, json.RawMessage(body))
}

func (s *Server) OpsSummary(w http.ResponseWriter, r *http.Request) {
	s.queryJSON(w, r, `SELECT json_build_object(
		'chargebacks',COALESCE((SELECT json_object_agg(state,n) FROM (SELECT COALESCE(b.state,'pending') AS state,count(*) AS n FROM chargebacks c LEFT JOIN batches b ON b.id=c.batch_id GROUP BY 1) q),'{}'),
		'batches',COALESCE((SELECT json_object_agg(state,n) FROM (SELECT state,count(*) AS n FROM batches GROUP BY state) q),'{}'),
		'oldest_pending_seconds',(SELECT GREATEST(0,EXTRACT(EPOCH FROM clock_timestamp()-min(received_at))) FROM chargebacks WHERE batch_id IS NULL))`)
}

func (s *Server) queryJSON(w http.ResponseWriter, r *http.Request, query string, args ...any) {
	var body []byte
	if err := s.Pool.QueryRow(r.Context(), query, args...).Scan(&body); err != nil {
		fail(w, 503, "database unavailable")
		return
	}
	writeJSON(w, 200, json.RawMessage(body))
}
