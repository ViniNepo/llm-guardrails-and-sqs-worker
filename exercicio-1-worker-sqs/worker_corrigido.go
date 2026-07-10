package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/sqs/types"
)

type sqsAPI interface {
	ReceiveMessage(ctx context.Context, in *sqs.ReceiveMessageInput, optFns ...func(*sqs.Options)) (*sqs.ReceiveMessageOutput, error)
	DeleteMessageBatch(ctx context.Context, in *sqs.DeleteMessageBatchInput, optFns ...func(*sqs.Options)) (*sqs.DeleteMessageBatchOutput, error)
}

type Worker struct {
	client sqsAPI
	cfg    Config
}

type Config struct {
	QueueURL          string
	NumWorkers        int
	WaitTimeSeconds   int32
	MaxMessages       int32
	VisibilityTimeout int32
	HandleTimeout     time.Duration
	EndpointURL       string
}

type SessionEvent struct {
	SessionID  string    `json:"session_id"`
	CustomerID string    `json:"customer_id"`
	ClosedAt   time.Time `json:"closed_at"`
	Sentiment  string    `json:"sentiment"`
}

type backoff struct {
	base, max, cur time.Duration
}

func newBackoff(base, max time.Duration) *backoff {
	return &backoff{base: base, max: max, cur: base}
}

func loadConfig() Config {
	return Config{
		QueueURL:          mustEnv("QUEUE_URL"),
		NumWorkers:        getEnvInt("NUM_WORKERS", 10),
		WaitTimeSeconds:   int32(getEnvInt("WAIT_TIME_SECONDS", 20)),
		MaxMessages:       int32(getEnvInt("MAX_MESSAGES", 10)),
		VisibilityTimeout: int32(getEnvInt("VISIBILITY_TIMEOUT", 60)),
		HandleTimeout:     time.Duration(getEnvInt("HANDLE_TIMEOUT_SEC", 45)) * time.Second,
		EndpointURL:       os.Getenv("AWS_ENDPOINT_URL"),
	}
}

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	slog.SetDefault(logger)

	cfg := loadConfig()

	awsCfg, err := config.LoadDefaultConfig(context.Background())
	if err != nil {
		slog.Error("falha ao carregar config AWS", "err", err)
		os.Exit(1)
	}

	client := sqs.NewFromConfig(awsCfg, func(o *sqs.Options) {
		if cfg.EndpointURL != "" {
			o.BaseEndpoint = aws.String(cfg.EndpointURL)
			slog.Info("usando endpoint SQS customizado (modo local)", "endpoint", cfg.EndpointURL)
		}
	})

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()

	w := &Worker{client: client, cfg: cfg}
	w.Run(ctx)

	slog.Info("worker finalizado com sucesso")
}

func (w *Worker) Run(ctx context.Context) {
	msgCh := make(chan types.Message)
	var wg sync.WaitGroup

	for i := 0; i < w.cfg.NumWorkers; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			w.consume(id, msgCh)
		}(i)
	}

	w.poll(ctx, msgCh)

	close(msgCh)
	wg.Wait()
}

func (w *Worker) poll(ctx context.Context, out chan<- types.Message) {
	backoff := newBackoff(200*time.Millisecond, 10*time.Second)

	for {
		select {
		case <-ctx.Done():
			slog.Info("sinal de shutdown recebido; parando de buscar mensagens")
			return
		default:
		}

		out2, err := w.client.ReceiveMessage(ctx, &sqs.ReceiveMessageInput{
			QueueUrl:            &w.cfg.QueueURL,
			MaxNumberOfMessages: w.cfg.MaxMessages,
			WaitTimeSeconds:     w.cfg.WaitTimeSeconds,
			VisibilityTimeout:   w.cfg.VisibilityTimeout,
		})
		if err != nil {
			if errors.Is(err, context.Canceled) {
				return
			}
			slog.Error("erro no ReceiveMessage", "err", err)
			if !backoff.Sleep(ctx) {
				return
			}
			continue
		}
		backoff.Reset()

		for _, msg := range out2.Messages {
			select {
			case out <- msg:
			case <-ctx.Done():
				return
			}
		}
	}
}

func (w *Worker) consume(id int, in <-chan types.Message) {
	for msg := range in {
		msgCtx, cancel := context.WithTimeout(context.Background(), w.cfg.HandleTimeout)
		err := handleMessage(msgCtx, aws.ToString(msg.Body))
		cancel()

		if err != nil {
			slog.Error("falha ao processar mensagem; será reentregue",
				"worker", id, "messageId", aws.ToString(msg.MessageId), "err", err)
			continue
		}

		delCtx, delCancel := context.WithTimeout(context.Background(), 5*time.Second)
		_, delErr := w.client.DeleteMessageBatch(delCtx, &sqs.DeleteMessageBatchInput{
			QueueUrl: &w.cfg.QueueURL,
			Entries: []types.DeleteMessageBatchRequestEntry{{
				Id:            msg.MessageId,
				ReceiptHandle: msg.ReceiptHandle,
			}},
		})
		delCancel()
		if delErr != nil {
			slog.Error("falha ao apagar mensagem processada; risco de reprocessamento",
				"worker", id, "messageId", aws.ToString(msg.MessageId), "err", delErr)
		}
	}
}

func handleMessage(ctx context.Context, body string) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}

	var evt SessionEvent
	if err := json.Unmarshal([]byte(body), &evt); err != nil {
		return fmt.Errorf("payload inválido (não é JSON de SessionEvent): %w", err)
	}

	if evt.SessionID == "" || evt.CustomerID == "" {
		return fmt.Errorf("evento sem session_id/customer_id: %q", body)
	}

	slog.Info("sessão processada",
		"session_id", evt.SessionID,
		"customer_id", evt.CustomerID,
		"sentiment", evt.Sentiment,
		"closed_at", evt.ClosedAt.Format(time.RFC3339),
	)
	return nil
}

func (b *backoff) Sleep(ctx context.Context) bool {
	t := time.NewTimer(b.cur)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		b.cur *= 2
		if b.cur > b.max {
			b.cur = b.max
		}
		return true
	}
}

func (b *backoff) Reset() { b.cur = b.base }

func mustEnv(k string) string {
	v := os.Getenv(k)
	if v == "" {
		slog.Error("variável de ambiente obrigatória ausente", "var", k)
		os.Exit(1)
	}
	return v
}

func getEnvInt(k string, def int) int {
	v := os.Getenv(k)
	if v == "" {
		return def
	}
	n, err := strconv.Atoi(v)
	if err != nil {
		return def
	}
	return n
}
