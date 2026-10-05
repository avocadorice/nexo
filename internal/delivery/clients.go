package delivery

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"net"
	"os"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	awsconfig "github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/s3"
	"github.com/pkg/sftp"
	"golang.org/x/crypto/ssh"
	"golang.org/x/crypto/ssh/knownhosts"
)

const MaxFileBytes = 4 * 1024 * 1024

type Config struct {
	Bucket, Endpoint, SFTPAddress, SFTPUser, SFTPPassword, KnownHosts string
	Poll                                                              time.Duration
}

type Remote struct {
	Client *sftp.Client
	SSH    *ssh.Client
	Socket net.Conn
}

func (r *Remote) Close() { r.Client.Close(); r.SSH.Close(); r.Socket.Close() }

// OpenSFTP pins the receiver's SSH host key before sending credentials or files.
func OpenSFTP(config Config) (*Remote, error) {
	verify, err := knownhosts.New(config.KnownHosts)
	if err != nil {
		return nil, err
	}
	socket, err := net.DialTimeout("tcp", config.SFTPAddress, 5*time.Second)
	if err != nil {
		return nil, err
	}
	socket.SetDeadline(time.Now().Add(45 * time.Second))
	connection, channels, requests, err := ssh.NewClientConn(socket, config.SFTPAddress, &ssh.ClientConfig{
		User: config.SFTPUser, Auth: []ssh.AuthMethod{ssh.Password(config.SFTPPassword)},
		HostKeyCallback: verify, Timeout: 5 * time.Second,
	})
	if err != nil {
		socket.Close()
		return nil, err
	}
	client := ssh.NewClient(connection, channels, requests)
	files, err := sftp.NewClient(client)
	if err != nil {
		client.Close()
		socket.Close()
		return nil, err
	}
	return &Remote{files, client, socket}, nil
}

func NewObjectStore(ctx context.Context, config Config) (*s3.Client, error) {
	cfg, err := awsconfig.LoadDefaultConfig(ctx)
	if err != nil {
		return nil, err
	}
	return s3.NewFromConfig(cfg, func(options *s3.Options) {
		options.UsePathStyle = true
		if config.Endpoint != "" {
			options.BaseEndpoint = aws.String(config.Endpoint)
		}
	}), nil
}

func digest(data []byte) string { value := sha256.Sum256(data); return hex.EncodeToString(value[:]) }

// ReadArtifact verifies the persisted manifest, not an untrusted object metadata field.
func ReadArtifact(ctx context.Context, store *s3.Client, bucket string, batch Batch) ([]byte, error) {
	object, err := store.GetObject(ctx, &s3.GetObjectInput{Bucket: &bucket, Key: &batch.ObjectKey})
	if err != nil {
		return nil, err
	}
	defer object.Body.Close()
	data, err := io.ReadAll(io.LimitReader(object.Body, MaxFileBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > MaxFileBytes || int64(len(data)) != batch.Bytes || digest(data) != batch.SHA256 {
		return nil, fmt.Errorf("artifact_integrity_mismatch")
	}
	return data, nil
}

func readRemote(remote *sftp.Client, path string, limit int64) ([]byte, bool, error) {
	file, err := remote.Open(path)
	if os.IsNotExist(err) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, err
	}
	defer file.Close()
	data, err := io.ReadAll(io.LimitReader(file, limit+1))
	if err != nil {
		return nil, false, err
	}
	if int64(len(data)) > limit {
		return nil, true, fmt.Errorf("remote_file_too_large")
	}
	return data, true, nil
}
