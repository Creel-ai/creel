// Thin REST API wrapper around whatsmeow for the Creel WhatsApp bridge.
//
// This is a placeholder — a real implementation would import
// go.mau.fi/whatsmeow and wire up the event handlers.
//
// Endpoints:
//   GET  /health           → {"status":"ok"}
//   GET  /messages?since=  → {"messages":[...]}
//   POST /send             → {"status":"sent"}

package main

import (
	"encoding/json"
	"flag"
	"log"
	"net/http"
	"time"
)

type Message struct {
	Sender    string `json:"sender"`
	Text      string `json:"text"`
	Timestamp string `json:"timestamp"`
	MessageID string `json:"message_id"`
}

type SendRequest struct {
	Recipient string `json:"recipient"`
	Text      string `json:"text"`
}

func main() {
	listen := flag.String("listen", ":8080", "listen address")
	authDir := flag.String("auth-dir", "/data/auth", "auth state directory")
	flag.Parse()

	log.Printf("WhatsApp bridge starting (auth-dir=%s)", *authDir)

	http.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
	})

	http.HandleFunc("/messages", func(w http.ResponseWriter, r *http.Request) {
		// TODO: integrate with whatsmeow event store
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string][]Message{"messages": {}})
	})

	http.HandleFunc("/send", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		var req SendRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		// TODO: send via whatsmeow client
		log.Printf("Would send to %s: %s", req.Recipient, req.Text)
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"status":    "sent",
			"timestamp": time.Now().UTC().Format(time.RFC3339),
		})
	})

	log.Printf("Listening on %s", *listen)
	log.Fatal(http.ListenAndServe(*listen, nil))
}
