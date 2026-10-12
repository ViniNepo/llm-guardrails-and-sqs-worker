// worker_original.go
//
// ORIGINAL (BUGGY) CODE, kept only as a reference for the diagnosis.
// It is excluded from the build (see the build tag below) and must not be
// used. The fixed version is in worker_fixed.go.
//
//go:build ignore
// +build ignore

package main

import (
	"context"
	"fmt"

	"github.com/aws/aws-sdk-go-v2/service/sqs"
)

func processQueue(client *sqs.Client, queueURL string) {
	for {
		result, _ := client.ReceiveMessage(context.Background(), &sqs.ReceiveMessageInput{
			QueueUrl:            &queueURL,
			MaxNumberOfMessages: 1,
		})

		for _, msg := range result.Messages {
			err := handleMessage(*msg.Body)
			if err != nil {
				fmt.Println("error:", err)
			}
			client.DeleteMessage(context.Background(), &sqs.DeleteMessageInput{
				QueueUrl:      &queueURL,
				ReceiptHandle: msg.ReceiptHandle,
			})
		}
	}
}

func handleMessage(body string) error {
	// processes a "session closed" event:
	// database call, enrichment and logging
	return nil
}
