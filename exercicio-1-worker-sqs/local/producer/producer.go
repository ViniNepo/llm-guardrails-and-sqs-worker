package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/aws/aws-sdk-go-v2/aws"
	"github.com/aws/aws-sdk-go-v2/config"
	"github.com/aws/aws-sdk-go-v2/credentials"
	"github.com/aws/aws-sdk-go-v2/service/sqs"
	"github.com/aws/aws-sdk-go-v2/service/sqs/types"
)

type SessionEvent struct {
	SessionID  string    `json:"session_id"`
	CustomerID string    `json:"customer_id"`
	ClosedAt   time.Time `json:"closed_at"`
	Sentiment  string    `json:"sentiment"`
}

func main() {
	endpoint := getenv("AWS_ENDPOINT_URL", "http://localhost:9324")
	queueURL := getenv("QUEUE_URL", "http://localhost:9324/000000000000/session-closed")
	n, _ := strconv.Atoi(getenv("N_MESSAGES", "25"))

	cfg, err := config.LoadDefaultConfig(context.Background(),
		config.WithRegion("us-east-1"),
		config.WithCredentialsProvider(
			credentials.NewStaticCredentialsProvider("local", "local", "")),
	)
	if err != nil {
		log.Fatalf("config: %v", err)
	}
	client := sqs.NewFromConfig(cfg, func(o *sqs.Options) {
		o.BaseEndpoint = aws.String(endpoint)
	})

	sent := 0
	for i := 0; i < n; i += 10 {
		var entries []types.SendMessageBatchRequestEntry
		for j := i; j < i+10 && j < n; j++ {
			evt := SessionEvent{
				SessionID:  fmt.Sprintf("sess-%04d", j),
				CustomerID: fmt.Sprintf("cust-%03d", j%50),
				ClosedAt:   time.Now().UTC(),
				Sentiment:  []string{"neutral", "positive", "negative"}[j%3],
			}
			body, _ := json.Marshal(evt)
			entries = append(entries, types.SendMessageBatchRequestEntry{
				Id:          aws.String(strconv.Itoa(j)),
				MessageBody: aws.String(string(body)),
			})
		}
		out, err := client.SendMessageBatch(context.Background(), &sqs.SendMessageBatchInput{
			QueueUrl: aws.String(queueURL),
			Entries:  entries,
		})
		if err != nil {
			log.Fatalf("SendMessageBatch: %v", err)
		}
		sent += len(out.Successful)
	}
	fmt.Printf("Enviadas %d mensagens para %s\n", sent, queueURL)
}

func getenv(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}
