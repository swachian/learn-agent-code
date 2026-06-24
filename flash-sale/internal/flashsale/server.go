package flashsale

import (
	"context"
	"errors"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	_ "net/http/pprof"
	"os"
	"os/signal"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/redis/go-redis/v9"

	"flash-sale/internal/mq"
	"flash-sale/internal/service"
)

const (
	ProductID = "SKU2024"
	
	// 库存配置
	TotalStock    = 10000
	StockShards   = 100  // 分片数量
	PerShardStock = TotalStock / StockShards
)

// Server 秒杀服务器
type Server struct {
	config     *Config
	redis      *redis.Client
	stockSvc   *service.StockService
	limiter    *service.DistributedLimiter
	
	// 统计
	stats struct {
		totalRequests int64
		successCount  int64
		failCount     int64
		totalStock    int64
		mu            sync.RWMutex
	}
}

// Config 配置
type Config struct {
	ServerPort  string
	RedisAddr   string
	RedisPool   int
	KafkaBroker []string
	KafkaTopic  string
}

// Response 统一响应
type Response struct {
	Code    int         `json:"code"`
	Message string      `json:"message"`
	Data    interface{} `json:"data,omitempty"`
}

// NewServer 创建服务器
func NewServer(cfg *Config) (*Server, error) {
	s := &Server{config: cfg}
	
	// 初始化Redis
	s.redis = redis.NewClient(&redis.Options{
		Addr:         cfg.RedisAddr,
		PoolSize:     cfg.RedisPool,
		MinIdleConns: 100,
	})
	
	ctx := context.Background()
	if err := s.redis.Ping(ctx).Err(); err != nil {
		return nil, fmt.Errorf("redis connection failed: %w", err)
	}
	
	// 初始化服务
	s.stockSvc = service.NewStockService(s.redis, TotalStock, StockShards)
	s.limiter = service.NewDistributedLimiter(s.redis)
	
	// 初始化统计
	atomic.StoreInt64(&s.stats.totalStock, TotalStock)
	
	return s, nil
}

// Init 初始化
func (s *Server) Init(ctx context.Context) error {
	// 预热缓存
	if err := service.WarmupCache(ctx, s.redis, ProductID, TotalStock, StockShards); err != nil {
		log.Printf("缓存预热警告: %v", err)
	}
	
	// 初始化库存
	pipe := s.redis.Pipeline()
	for i := 0; i < StockShards; i++ {
		key := fmt.Sprintf("flash:stock:%d", i)
		pipe.Set(ctx, key, PerShardStock, 0)
	}
	if _, err := pipe.Exec(ctx); err != nil {
		return fmt.Errorf("init stock failed: %w", err)
	}
	
	log.Printf("秒杀系统初始化完成: 总库存=%d, 分片=%d", TotalStock, StockShards)
	return nil
}

// Run 启动服务器
func (s *Server) Run() error {
	// pprof
	go func() {
		log.Println(http.ListenAndServe(":6060", nil))
	}()
	
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())
	r.Use(s.rateLimitMiddleware())
	r.Use(s.requestLogger())
	
	// 路由
	r.GET("/health", s.healthCheck)
	r.GET("/flash/subscribe", s.handleSubscribe)
	r.GET("/flash/stock", s.getStock)
	r.GET("/flash/stats", s.getStats)
	
	// 信号处理
	go func() {
		sig := make(chan os.Signal, 1)
		signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
		<-sig
		s.printStats()
		os.Exit(0)
	}()
	
	log.Printf("秒杀服务启动: %s", s.config.ServerPort)
	return r.Run(s.config.ServerPort)
}

// rateLimitMiddleware 限流中间件
func (s *Server) rateLimitMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		clientIP := c.ClientIP()
		key := fmt.Sprintf("flash:ratelimit:%s", clientIP)
		
		// 滑动窗口限流: 10万/秒/用户
		allowed, err := s.limiter.Allow(c.Request.Context(), key, 1000, 100000)
		if err != nil {
			log.Printf("限流检查失败: %v", err)
			allowed = true // 失败时放行
		}
		
		if !allowed {
			c.JSON(http.StatusTooManyRequests, Response{
				Code:    429,
				Message: "请求过于频繁，请稍后重试",
			})
			c.Abort()
			return
		}
		c.Next()
	}
}

// requestLogger 请求日志
func (s *Server) requestLogger() gin.HandlerFunc {
	return func(c *gin.Context) {
		start := time.Now()
		c.Next()
		log.Printf("[%s] %s %s %dms", 
			c.ClientIP(), c.Request.Method, c.Request.URL.Path, 
			time.Since(start).Milliseconds())
	}
}

// handleSubscribe 处理秒杀请求
func (s *Server) handleSubscribe(c *gin.Context) {
	atomic.AddInt64(&s.stats.totalRequests, 1)
	
	userID := c.Query("user_id")
	if userID == "" {
		c.JSON(http.StatusBadRequest, Response{
			Code:    400,
			Message: "user_id is required",
		})
		return
	}
	
	ctx := c.Request.Context()
	
	// 1. 分布式限流 (全局)
	globalKey := "flash:ratelimit:global"
	allowed, err := s.limiter.Allow(ctx, globalKey, 1000, 1000000) // 100万TPS
	if err == nil && !allowed {
		atomic.AddInt64(&s.stats.failCount, 1)
		c.JSON(http.StatusOK, Response{
			Code:    1003,
			Message: "系统繁忙，请稍后重试",
		})
		return
	}
	
	// 2. 扣减库存 (分片 + Lua原子操作)
	result, err := s.stockSvc.DeductStock(ctx, ProductID, userID)
	if err != nil {
		log.Printf("库存扣减失败: %v", err)
		atomic.AddInt64(&s.stats.failCount, 1)
		c.JSON(http.StatusInternalServerError, Response{
			Code:    500,
			Message: "系统错误，请稍后重试",
		})
		return
	}
	
	switch result {
	case -2:
		atomic.AddInt64(&s.stats.failCount, 1)
		c.JSON(http.StatusOK, Response{
			Code:    1001,
			Message: "您已购买过该商品",
		})
	case -1:
		atomic.AddInt64(&s.stats.failCount, 1)
		c.JSON(http.StatusOK, Response{
			Code:    1002,
			Message: "库存不足",
		})
	case -3:
		atomic.AddInt64(&s.stats.failCount, 1)
		c.JSON(http.StatusOK, Response{
			Code:    500,
			Message: "系统错误",
		})
	default:
		// 3. 成功 - 异步处理订单
		orderID := s.generateOrderID(userID)
		go s.processOrderAsync(userID, orderID)
		
		atomic.AddInt64(&s.stats.successCount, 1)
		c.JSON(http.StatusOK, Response{
			Code:    0,
			Message: "恭喜，抢购成功!",
			Data: map[string]interface{}{
				"order_id": orderID,
			},
		})
	}
}

// processOrderAsync 异步处理订单
func (s *Server) processOrderAsync(userID, orderID string) {
	log.Printf("[异步] 创建订单: user=%s, order=%s", userID, orderID)
	
	// 实际场景中发送到Kafka:
	// msg := &mq.FlashSaleMessage{
	//     UserID:    userID,
	//     ProductID: ProductID,
	//     OrderID:   orderID,
	//     Action:    "create",
	// }
	// s.producer.SendMessage(context.Background(), msg)
}

// getStock 获取库存
func (s *Server) getStock(c *gin.Context) {
	ctx := c.Request.Context()
	
	stock, err := s.stockSvc.GetStock(ctx)
	if err != nil {
		c.JSON(http.StatusInternalServerError, Response{
			Code:    500,
			Message: "获取库存失败",
		})
		return
	}
	
	c.JSON(http.StatusOK, Response{
		Code:    0,
		Message: "success",
		Data: map[string]interface{}{
			"current_stock": stock,
			"total_stock":   TotalStock,
			"sold":          TotalStock - stock,
		},
	})
}

// getStats 获取统计
func (s *Server) getStats(c *gin.Context) {
	c.JSON(http.StatusOK, Response{
		Code:    0,
		Message: "success",
		Data: map[string]interface{}{
			"total_requests": atomic.LoadInt64(&s.stats.totalRequests),
			"success":        atomic.LoadInt64(&s.stats.successCount),
			"fail":           atomic.LoadInt64(&s.stats.failCount),
			"qps":            s.stats.totalRequests / 60, // 简化计算
		},
	})
}

// healthCheck 健康检查
func (s *Server) healthCheck(c *gin.Context) {
	ctx := c.Request.Context()
	
	// Redis检查
	if err := s.redis.Ping(ctx).Err(); err != nil {
		c.JSON(http.StatusServiceUnavailable, Response{
			Code:    503,
			Message: "Redis不可用",
		})
		return
	}
	
	// 库存检查
	stock, _ := s.stockSvc.GetStock(ctx)
	if stock < 0 {
		c.JSON(http.StatusServiceUnavailable, Response{
			Code:    503,
			Message: "库存异常",
		})
		return
	}
	
	c.JSON(http.StatusOK, Response{
		Code:    0,
		Message: "healthy",
	})
}

func (s *Server) printStats() {
	log.Printf("=== 秒杀统计 ===")
	log.Printf("总请求: %d", atomic.LoadInt64(&s.stats.totalRequests))
	log.Printf("成功: %d", atomic.LoadInt64(&s.stats.successCount))
	log.Printf("失败: %d", atomic.LoadInt64(&s.stats.failCount))
}

func (s *Server) generateOrderID(userID string) string {
	return fmt.Sprintf("ORD%d%s", time.Now().UnixNano(), userID[:min(8, len(userID))])
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// MockOrderService 模拟订单服务
type MockOrderService struct {
	orders map[string]*Order
	mu     sync.RWMutex
}

type Order struct {
	ID        string
	UserID    string
	ProductID string
	Status    string
	CreatedAt time.Time
}

func NewMockOrderService() *MockOrderService {
	return &MockOrderService{
		orders: make(map[string]*Order),
	}
}

func (s *MockOrderService) Create(ctx context.Context, userID, productID, orderID string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	
	if _, exists := s.orders[orderID]; exists {
		return errors.New("order already exists")
	}
	
	s.orders[orderID] = &Order{
		ID:        orderID,
		UserID:    userID,
		ProductID: productID,
		Status:    "pending",
		CreatedAt: time.Now(),
	}
	
	return nil
}

// LoadTester 压测工具
type LoadTester struct {
	server     *Server
	concurrent int
	totalReq   int64
}

// NewLoadTester 创建压测工具
func NewLoadTester(s *Server, concurrent int) *LoadTester {
	return &LoadTester{
		server:     s,
		concurrent: concurrent,
	}
}

// Run 执行压测
func (t *LoadTester) Run(ctx context.Context) {
	var wg sync.WaitGroup
	atomic := int64(0)
	success := int64(0)
	fail := int64(0)
	
	start := time.Now()
	
	for i := 0; i < t.concurrent; i++ {
		wg.Add(1)
		go func(threadID int) {
			defer wg.Done()
			
			client := &http.Client{Timeout: time.Second}
			
			for {
				select {
				case <-ctx.Done():
					return
				default:
					userID := fmt.Sprintf("user%d", threadID*1000+rand.Intn(1000))
					url := fmt.Sprintf("http://localhost:8080/flash/subscribe?user_id=%s", userID)
					
					resp, err := client.Get(url)
					if err != nil {
						atomic.AddInt64(&fail, 1)
						continue
					}
					resp.Body.Close()
					
					atomic.AddInt64(&atomic, 1)
					if resp.StatusCode == 200 {
						atomic.AddInt64(&success, 1)
					}
				}
			}
		}(i)
	}
	
	// 打印进度
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ticker.C:
				elapsed := time.Since(start).Seconds()
				total := atomic.LoadInt64(&atomic)
				log.Printf("[压测] QPS: %.2f, 总请求: %d, 成功: %d, 失败: %d",
					float64(total)/elapsed, total, success, fail)
			case <-ctx.Done():
				return
			}
		}
	}()
	
	wg.Wait()
}