package delivery

import (
	"encoding/json"
	"testing"
)

func TestAcknowledgementRequiresExactManifest(t *testing.T) {
	batch := Batch{ID: "00000000-0000-4000-8000-000000000001", SHA256: digest([]byte("file")), Rows: 12}
	ack := Ack{Version: 1, BatchID: batch.ID, SHA256: batch.SHA256, RowCount: batch.Rows, Status: "accepted"}
	bytes, _ := json.Marshal(ack)
	if _, err := ValidateAck(bytes, batch); err != nil {
		t.Fatal(err)
	}
	for name, change := range map[string]func(*Ack){
		"batch": func(a *Ack) { a.BatchID = "other" }, "hash": func(a *Ack) { a.SHA256 = "bad" },
		"count": func(a *Ack) { a.RowCount++ }, "status": func(a *Ack) { a.Status = "pending" },
		"version": func(a *Ack) { a.Version = 2 },
	} {
		t.Run(name, func(t *testing.T) {
			copy := ack
			change(&copy)
			data, _ := json.Marshal(copy)
			if _, err := ValidateAck(data, batch); err == nil {
				t.Fatal("accepted invalid acknowledgement")
			}
		})
	}
	if _, err := ValidateAck(append(bytes, []byte("{}")...), batch); err == nil {
		t.Fatal("accepted trailing JSON")
	}
}
