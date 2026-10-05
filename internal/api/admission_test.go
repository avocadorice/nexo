package api

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestOverloadRejectsBeforeHandler(t *testing.T) {
	entered, release := make(chan struct{}), make(chan struct{})
	handler := ObserveAndLimit(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { close(entered); <-release; w.WriteHeader(201) }), 1)
	done := make(chan struct{})
	go func() {
		handler.ServeHTTP(httptest.NewRecorder(), httptest.NewRequest("POST", "/api/chargebacks", nil))
		close(done)
	}()
	<-entered
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, httptest.NewRequest("POST", "/api/chargebacks", nil))
	if response.Code != 429 || response.Header().Get("Retry-After") != "1" {
		t.Fatalf("overload response: %d", response.Code)
	}
	close(release)
	<-done
}
