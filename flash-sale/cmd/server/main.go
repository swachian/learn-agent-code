package main

import (
	"context"
	"log"
	"os"
	"os/signal"
	"syscall"

	"flash-sale/internal/flashsale"
)

func main() {
	// 配置
	cfg := &flashsale.Config{
		ServerPort:  ":8080",
		RedisAddr:   getEnv("REDIS_ADDR", "localhost:6379"),
		RedisPool:   1000,
		KafkaBroker: []string{getEnv("KAFKA_BROKER", "localhost:9092")},
		KafkaTopic:  getEnv("KAFKA_TOPIC", "flash-sale-orders"),
	}

	// 创建服务器
	server, err := flashsale.NewServer(cfg)
	if err != nil {
		log.Fatalf("创建服务器失败: %v", err)
	}

	// 初始化
	ctx := context.Background()
	if err := server.Init(ctx); err != nil {
		log.Fatalf("初始化失败: %v", err)
	}

	// 启动
	log.Println("秒杀服务启动中...")

	// 优雅关闭
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		log.Println("收到关闭信号，正在关闭...")
		os.Exit(0)
	}()

	if err := server.Run(); err != nil {
		log.Fatalf("服务运行失败: %v", err)
	}
}

func getEnv(key, defaultVal string) string {
	if val := os.Getenv(key); val != "" {
		return val
	}
	return defaultVal
}