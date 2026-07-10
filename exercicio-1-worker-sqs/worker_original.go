// worker_original.go
//
// ⚠️ CÓDIGO PROBLEMÁTICO — mantido APENAS para referência/diagnóstico.
// Não é compilado como parte do build (ver build tag abaixo) e não deve ser
// usado. A versão corrigida está em worker_corrigido.go.
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
				fmt.Println("erro:", err)
			}
			client.DeleteMessage(context.Background(), &sqs.DeleteMessageInput{
				QueueUrl:      &queueURL,
				ReceiptHandle: msg.ReceiptHandle,
			})
		}
	}
}

func handleMessage(body string) error {
	// processa evento de sessão encerrada
	// chamada a banco de dados, enriquecimento e logging
	return nil
}
