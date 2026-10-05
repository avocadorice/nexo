package main

import (
	"context"
	"log/slog"
	"nexo/internal/db"
	"nexo/internal/delivery"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"
)

func main() {
	slog.SetDefault(slog.New(slog.NewJSONHandler(os.Stdout, nil)))
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	seconds := 5
	if raw := os.Getenv("WORKER_POLL_SECONDS"); raw != "" {
		var err error
		seconds, err = strconv.Atoi(raw)
		if err != nil || seconds < 1 || seconds > 300 {
			panic("WORKER_POLL_SECONDS must be 1..300")
		}
	}
	config := delivery.Config{Bucket: os.Getenv("S3_BUCKET"), Endpoint: os.Getenv("S3_ENDPOINT_URL"), SFTPAddress: os.Getenv("SFTP_ADDR"), SFTPUser: os.Getenv("SFTP_USER"), SFTPPassword: os.Getenv("SFTP_PASSWORD"), KnownHosts: os.Getenv("SFTP_KNOWN_HOSTS"), Poll: time.Duration(seconds) * time.Second}
	pool, err := db.Open(ctx, os.Getenv("DATABASE_URL"), 3)
	if err != nil {
		slog.Error("database_unavailable")
		os.Exit(1)
	}
	defer pool.Close()
	store, err := delivery.NewObjectStore(ctx, config)
	if err != nil {
		slog.Error("object_store_configuration_failed")
		os.Exit(1)
	}
	worker := delivery.Worker{Pool: pool, Store: store, Config: config}
	if err = worker.Run(ctx); err != nil {
		slog.Error("worker_failed", "error_type", "database_or_context")
		os.Exit(1)
	}
}
