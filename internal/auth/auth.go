package auth

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"net/http"
	"strings"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

var ErrUnauthorized = errors.New("unauthorized")

type Principal struct {
	CustomerID *string
	Role       string
}

func Authenticate(ctx context.Context, pool *pgxpool.Pool, r *http.Request) (Principal, error) {
	var principal Principal
	header := r.Header.Get("Authorization")
	if !strings.HasPrefix(header, "Bearer ") {
		return principal, ErrUnauthorized
	}
	token := strings.TrimPrefix(header, "Bearer ")
	if len(token) < 32 || len(token) > 256 || strings.ContainsAny(token, " \t\n\r") {
		return principal, ErrUnauthorized
	}
	digest := sha256.Sum256([]byte(token))
	err := pool.QueryRow(ctx, `SELECT customer_id::text, role FROM tokens
		WHERE hash=$1 AND NOT revoked AND expires_at > clock_timestamp()`, hex.EncodeToString(digest[:])).Scan(&principal.CustomerID, &principal.Role)
	if errors.Is(err, pgx.ErrNoRows) {
		return principal, ErrUnauthorized
	}
	return principal, err
}
