package mq

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"time"

	"github.com/IBM/sarama"
)

// KafkaConfig Kafka配置
type KafkaConfig struct {
	Brokers       []string
	Topic         string
	ConsumerGroup string
	Partitions    int
}

// FlashSaleMessage 秒杀消息
type FlashSaleMessage struct {
	UserID     string `json:"user_id"`
	ProductID  string `json:"product_id"`
	OrderID    string `json:"order_id"`
	Action     string `json:"action"` // subscribe, cancel
	Timestamp  int64  `json:"timestamp"`
}

// Producer Kafka生产者
type Producer struct {
	client sarama.SyncProducer
	topic  string
}

// NewProducer 创建Kafka生产者
func NewProducer(cfg KafkaConfig) (*Producer, error) {
	saramaConfig := sarama.NewConfig()
	saramaConfig.Producer.RequiredAcks = sarama.WaitForAll
	saramaConfig.Producer.Retry.Max = 3
	saramaConfig.Producer.Return.Successes = true
	saramaConfig.Producer.Partitioner = sarama.NewHashPartitioner

	client, err := sarama.NewSyncProducer(cfg.Brokers, saramaConfig)
	if err != nil {
		return nil, fmt.Errorf("create producer failed: %w", err)
	}

	return &Producer{
		client: client,
		topic:  cfg.Topic,
	}, nil
}

// SendMessage 发送消息
func (p *Producer) SendMessage(ctx context.Context, msg *FlashSaleMessage) error {
	msg.Timestamp = time.Now().UnixMilli()
	
	data, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("marshal message failed: %w", err)
	}

	kafkaMsg := &sarama.ProducerMessage{
		Topic:     p.topic,
		Key:       sarama.StringEncoder(msg.UserID), // 按用户ID分区，保证同一用户消息有序
		Value:     sarama.ByteEncoder(data),
		Timestamp: time.Now(),
	}

	partition, offset, err := p.client.SendMessage(kafkaMsg)
	if err != nil {
		return fmt.Errorf("send message failed: %w", err)
	}

	log.Printf("[MQ] 消息已发送: partition=%d, offset=%d, user=%s", partition, offset, msg.UserID)
	return nil
}

// Close 关闭生产者
func (p *Producer) Close() error {
	return p.client.Close()
}

// Consumer Kafka消费者
type Consumer struct {
	client      sarama.ConsumerGroup
	topic       string
	handler     *ConsumerHandler
	ctx         context.Context
	cancel      context.CancelFunc
	wg          sync.WaitGroup
}

// ConsumerHandler 消息处理
type ConsumerHandler struct {
	handlers []MessageHandler
}

// MessageHandler 消息处理函数
type MessageHandler func(msg *FlashSaleMessage) error

// NewConsumerGroup 创建消费者组
func NewConsumerGroup(cfg KafkaConfig, handlers ...MessageHandler) (*Consumer, error) {
	saramaConfig := sarama.NewConfig()
	saramaConfig.Consumer.Group.Rebalance.GroupStrategies = []sarama.BalanceStrategy{
		sarama.NewBalanceStrategyRoundRobin(),
	}
	saramaConfig.Consumer.Offsets.Initial = sarama.OffsetNewest

	client, err := sarama.NewConsumerGroup(cfg.Brokers, cfg.ConsumerGroup, saramaConfig)
	if err != nil {
		return nil, fmt.Errorf("create consumer group failed: %w", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	
	handler := &ConsumerHandler{handlers: handlers}
	
	return &Consumer{
		client:  client,
		topic:   cfg.Topic,
		handler: handler,
		ctx:     ctx,
		cancel:  cancel,
	}, nil
}

// Start 启动消费者
func (c *Consumer) Start() error {
	c.wg.Add(1)
	go func() {
		defer c.wg.Done()
		for {
			select {
			case <-c.ctx.Done():
				return
			default:
				if err := c.client.Consume(c.ctx, []string{c.topic}, c.handler); err != nil {
					log.Printf("[MQ] 消费错误: %v", err)
					time.Sleep(time.Second)
				}
			}
		}
	}()
	
	log.Printf("[MQ] 消费者已启动, topic=%s", c.topic)
	return nil
}

// Stop 停止消费者
func (c *Consumer) Stop() error {
	c.cancel()
	c.wg.Wait()
	return c.client.Close()
}

// Setup 实现 ConsumerGroupHandler
func (h *ConsumerHandler) Setup(sarama.ConsumerGroupSession) error {
	return nil
}

// Cleanup 实现 ConsumerGroupHandler
func (h *ConsumerHandler) Cleanup(sarama.ConsumerGroupSession) error {
	return nil
}

// ConsumeClaim 实现 ConsumerGroupHandler
func (h *ConsumerHandler) ConsumeClaim(session sarama.ConsumerGroupSession, claim sarama.ConsumerGroupClaim) error {
	for {
		select {
		case msg, ok := <-claim.Messages():
			if !ok {
				return nil
			}
			
			var flashMsg FlashSaleMessage
			if err := json.Unmarshal(msg.Value, &flashMsg); err != nil {
				log.Printf("[MQ] 解析消息失败: %v", err)
				session.MarkMessage(msg, "")
				continue
			}
			
			// 处理消息
			for _, handler := range h.handlers {
				if err := handler(&flashMsg); err != nil {
					log.Printf("[MQ] 处理消息失败: %v", err)
				}
			}
			
			session.MarkMessage(msg, "")
			
		case <-session.Context().Done():
			return nil
		}
	}
}

// MockProducer 模拟生产者 (用于测试)
type MockProducer struct {
	messages chan *FlashSaleMessage
}

// NewMockProducer 创建模拟生产者
func NewMockProducer(buffer int) *MockProducer {
	return &MockProducer{
		messages: make(chan *FlashSaleMessage, buffer),
	}
}

// SendMessage 发送消息
func (p *MockProducer) SendMessage(ctx context.Context, msg *FlashSaleMessage) error {
	select {
	case p.messages <- msg:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Messages 返回消息通道
func (p *MockProducer) Messages() <-chan *FlashSaleMessage {
	return p.messages
}

// Close 关闭
func (p *MockProducer) Close() error {
	close(p.messages)
	return nil
}

// BatchProducer 批量生产者
type BatchProducer struct {
	producer *Producer
	batchSize int
	flushInterval time.Duration
	buffer     []*FlashSaleMessage
	mu         sync.Mutex
}

// NewBatchProducer 创建批量生产者
func NewBatchProducer(cfg KafkaConfig, batchSize int, flushInterval time.Duration) (*BatchProducer, error) {
	producer, err := NewProducer(cfg)
	if err != nil {
		return nil, err
	}
	
	p := &BatchProducer{
		producer:     producer,
		batchSize:   batchSize,
		flushInterval: flushInterval,
		buffer:      make([]*FlashSaleMessage, 0, batchSize),
	}
	
	go p.flushLoop()
	return p, nil
}

// SendMessage 发送消息 (批量)
func (p *BatchProducer) SendMessage(ctx context.Context, msg *FlashSaleMessage) error {
	p.mu.Lock()
	p.buffer = append(p.buffer, msg)
	shouldFlush := len(p.buffer) >= p.batchSize
	p.mu.Unlock()
	
	if shouldFlush {
		p.flush()
	}
	return nil
}

func (p *BatchProducer) flushLoop() {
	ticker := time.NewTicker(p.flushInterval)
	defer ticker.Stop()
	
	for range ticker.C {
		p.flush()
	}
}

func (p *BatchProducer) flush() {
	p.mu.Lock()
	if len(p.buffer) == 0 {
		p.mu.Unlock()
		return
	}
	
	messages := p.buffer
	p.buffer = make([]*FlashSaleMessage, 0, p.batchSize)
	p.mu.Unlock()
	
	for _, msg := range messages {
		if err := p.producer.SendMessage(context.Background(), msg); err != nil {
			log.Printf("[MQ] 批量发送失败: %v", err)
		}
	}
}

func (p *BatchProducer) Close() error {
	p.flush()
	return p.producer.Close()
}

// OrderProcessor 订单处理器
type OrderProcessor struct {
	handlers map[string]func(msg *FlashSaleMessage) error
}

// NewOrderProcessor 创建订单处理器
func NewOrderProcessor() *OrderProcessor {
	return &OrderProcessor{
		handlers: make(map[string]func(msg *FlashSaleMessage) error),
	}
}

// RegisterHandler 注册处理器
func (p *OrderProcessor) RegisterHandler(action string, handler func(msg *FlashSaleMessage) error) {
	p.handlers[action] = handler
}

// Process 处理消息
func (p *OrderProcessor) Process(msg *FlashSaleMessage) error {
	handler, ok := p.handlers[msg.Action]
	if !ok {
		return fmt.Errorf("unknown action: %s", msg.Action)
	}
	return handler(msg)
}