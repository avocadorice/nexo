package api

import (
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestEditorHelperPermissionOnlyOnLocalExplorer(t *testing.T) {
	directory := t.TempDir()
	for _, file := range []string{"explorer.html", "index.html", "ops.html"} {
		if err := os.WriteFile(filepath.Join(directory, file), []byte("<!doctype html>"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	handler := New(nil, 4, directory)
	for _, test := range []struct {
		url     string
		allowed bool
	}{
		{"http://localhost:8080/static/explorer.html", true},
		{"http://127.0.0.1:8080/static/explorer.html", true},
		{"https://nexo.example/static/explorer.html", false},
		{"http://localhost:8080/static/", false},
		{"http://localhost:8080/static/ops.html", false},
		{"http://localhost:8080/healthz", false},
	} {
		t.Run(test.url, func(t *testing.T) {
			response := httptest.NewRecorder()
			handler.ServeHTTP(response, httptest.NewRequest("GET", test.url, nil))
			policy := response.Header().Get("Content-Security-Policy")
			if strings.Contains(policy, "connect-src 'self' http://127.0.0.1:8765") != test.allowed {
				t.Fatalf("unexpected helper permission: %s", policy)
			}
			for _, restriction := range []string{"default-src 'self'", "script-src 'self'", "object-src 'none'", "frame-ancestors 'none'"} {
				if !strings.Contains(policy, restriction) {
					t.Fatalf("missing restriction %q", restriction)
				}
			}
		})
	}
}
