package flashsale

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	testRedisAddr = "localhost:6379"
	testProductID = "TEST_SKU_001"
	testTotalStock = 10000
	testShards     = 100
)

// BenchmarkStockService 库存服务压测
func BenchmarkStockService(b *testing.B) {
	client := redis.NewClient(&redis.Options{
		Addr:     testRedisAddr,
		PoolSize: 100,
	})
	defer client.Close()

	ctx := context.Background()
	stockSvc := NewTestStockService(client, testTotalStock, testShards)

	// 初始化
	InitTestStock(ctx, client)

	b.ResetTimer()
	b.RunParallel(func(pb *testing.PB) {
		userID := fmt.Sprintf("user%d", atomic.AddInt64(new(int64), 1))
		stockSvc.DeductStock(ctx, testProductID, userID)
	})
}

// TestStockService_RaceCondition 测试并发扣减
func TestStockService_RaceCondition(t *testing.T) {
	client := redis.NewClient(&redis.Options{
		Addr:     testRedisAddr,
		PoolSize: 1000,
	})
	defer client.Close()

	ctx := context.Background()
	InitTestStock(ctx, client)

	stockSvc := NewTestStockService(client, testTotalStock, testShards)

	var (
		success int64
		fail    int64
	)

	// 模拟10万并发请求
	var wg sync.WaitGroup
	concurrency := 100000

	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()

			userID := fmt.Sprintf("user_unique_%d", id)
			result, err := stockSvc.DeductStock(ctx, testProductID, userID)

			if err != nil {
				t.Logf("错误: %v", err)
				atomic.AddInt64(&fail, 1)
				return
			}

			if result >= 0 {
				atomic.AddInt64(&success, 1)
			} else {
				atomic.AddInt64(&fail, 1)
			}
		}(i)
	}

	wg.Wait()

	// 验证
	totalSold := atomic.LoadInt64(&success)
	totalFail := atomic.LoadInt64(&fail)

	t.Logf("=== 测试结果 ===")
	t.Logf("总请求: %d", concurrency)
	t.Logf("成功: %d", totalSold)
	t.Logf("失败: %d", totalFail)
	t.Logf("库存验证: %d (应为: %d)", totalSold, testTotalStock)

	// 断言
	if totalSold > testTotalStock {
		t.Errorf("超卖! 售出: %d, 库存: %d", totalSold, testTotalStock)
	}
}

// TestStockService_Rollback 测试库存回滚
func TestStockService_Rollback(t *testing.T) {
	client := redis.NewClient(&redis.Options{
		Addr:     testRedisAddr,
		PoolSize: 100,
	})
	defer client.Close()

	ctx := context.Background()
	InitTestStock(ctx, client)

	stockSvc := NewTestStockService(client, testTotalStock, testShards)

	// 扣减
	userID := "test_user_rollback"
	result, err := stockSvc.DeductStock(ctx, testProductID, userID)
	if err != nil {
		t.Fatalf("扣减失败: %v", err)
	}
	if result < 0 {
		t.Fatalf("扣减失败: %d", result)
	}

	// 获取当前库存
	beforeStock := result

	// 回滚
	if err := stockSvc.RestoreStock(ctx, testProductID, userID); err != nil {
		t.Fatalf("回滚失败: %v", err)
	}

	// 验证库存恢复
	afterStock, err := stockSvc.GetStock(ctx)
	if err != nil {
		t.Fatalf("获取库存失败: %v", err)
	}

	if afterStock != beforeStock+1 {
		t.Errorf("库存回滚验证失败: before=%d, after=%d, expected=%d", beforeStock, afterStock, beforeStock+1)
	}
}

// TestRateLimiter 测试限流
func TestRateLimiter(t *testing.T) {
	client := redis.NewClient(&redis.Options{
		Addr:     testRedisAddr,
		PoolSize: 100,
	})
	defer client.Close()

	ctx := context.Background()
	limiter := NewTestLimiter(client)

	// 测试滑动窗口限流
	key := "test:ratelimit"
	windowMs := int64(1000)
	limit := int64(100)

	var allowed int64
	var denied int64

	for i := 0; i < 200; i++ {
		ok, err := limiter.Allow(ctx, key, windowMs, limit)
		if err != nil {
			t.Fatalf("限流检查失败: %v", err)
		}

		if ok {
			allowed++
		} else {
			denied++
		}
	}

	t.Logf("允许: %d, 拒绝: %d", allowed, denied)

	if allowed > limit {
		t.Errorf("限流失败! 允许数: %d, 限制: %d", allowed, limit)
	}
}

// 辅助函数

func InitTestStock(ctx context.Context, client *redis.Client) {
	pipe := client.Pipeline()
	perShard := testTotalStock / testShards

	for i := 0; i < testShards; i++ {
		key := fmt.Sprintf("flash:stock:%d", i)
		pipe.Set(ctx, key, perShard, 0)
	}

	// 清空用户购买记录
	for i := 0; i < 10000; i++ {
		userKey := fmt.Sprintf("flash:user:%s:user_%d", testProductID, i)
		pipe.Del(ctx, userKey)
	}

	pipe.Exec(ctx)
}

func NewTestStockService(client *redis.Client, totalStock, shards int) *TestStockService {
	return &TestStockService{
		redis:         client,
		shards:        shards,
		perShardStock: totalStock / shards,
		luaScript: `
local stock_key = KEYS[1]
local user_key = KEYS[2]
local quantity = tonumber(ARGV[1])

if redis.call('EXISTS', user_key) == 1 then
    return -2
end

local stock = tonumber(redis.call('GET', stock_key) or '0')
if stock < quantity then
    return -1
end

redis.call('DECRBY', stock_key, quantity)
redis.call('SETEX', user_key, 86400, '1')
return stock - quantity
`,
	}
}

type TestStockService struct {
	redis         *redis.Client
	shards        int
	perShardStock int
	luaScript     string
}

func (s *TestStockService) selectShard(userID string) int {
	h := 0
	for _, c := range userID {
		h = 31*h + int(c)
	}
	return h % s.shards
}

func (s *TestStockService) DeductStock(ctx context.Context, productID, userID string) (int64, error) {
	shardID := s.selectShard(userID)
	stockKey := fmt.Sprintf("flash:stock:%d", shardID)
	userKey := fmt.Sprintf("flash:user:%s:%s", productID, userID)

	result, err := s.redis.Eval(ctx, s.luaScript, []string{stockKey, userKey}, 1).Int64()
	if err != nil {
		return -3, err
	}
	return result, nil
}

func (s *TestStockService) RestoreStock(ctx context.Context, productID, userID string) error {
	shardID := s.selectShard(userID)
	stockKey := fmt.Sprintf("flash:stock:%d", shardID)
	userKey := fmt.Sprintf("flash:user:%s:%s", productID, userID)

	luaScript := `
local stock_key = KEYS[1]
local user_key = KEYS[2]
redis.call('DEL', user_key)
redis.call('INCRBY', stock_key, 1)
return redis.call('GET', stock_key)
`
	_, err := s.redis.Eval(ctx, luaScript, []string{stockKey, userKey}).Result()
	return err
}

func (s *TestStockService) GetStock(ctx context.Context) (int64, error) {
	var total int64
	for i := 0; i < s.shards; i++ {
		key := fmt.Sprintf("flash:stock:%d", i)
		stock, _ := s.redis.Get(ctx, key).Int64()
		total += stock
	}
	return total, nil
}

func NewTestLimiter(client *redis.Client) *TestLimiter {
	return &TestLimiter{redis: client}
}

type TestLimiter struct {
	redis *redis.Client
}

func (l *TestLimiter) Allow(ctx context.Context, key string, windowMs, limit int64) (bool, error) {
	luaScript := `
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, now .. ':' .. math.random())
    redis.call('EXPIRE', key, math.ceil(window / 1000))
    return 1
end
return 0
`
	now := time.Now().UnixMilli()
	result, err := l.redis.Eval(ctx, luaScript, []string{key}, now, windowMs, limit).Int64()
	if err != nil {
		return false, err
	}
	return result == 1, nil
}

// StressTest 压力测试
func StressTest(concurrency int, duration time.Duration) {
	client := &http.Client{Timeout: 5 * time.Second}
	url := "http://localhost:8080/flash/subscribe?user_id="

	var (
		total    int64
		success  int64
		fail     int64
		startAt  = time.Now()
	)

	ctx, cancel := context.WithTimeout(context.Background(), duration)
	defer cancel()

	var wg sync.WaitGroup
	for i := 0; i < concurrency; i++ {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()

			for {
				select {
				case <-ctx.Done():
					return
				default:
					userID := fmt.Sprintf("stress_user_%d_%d", id, time.Now().UnixNano())
					resp, err := client.Get(url + userID)

					atomic.AddInt64(&total, 1)

					if err != nil {
						atomic.AddInt64(&fail, 1)
						continue
					}
					resp.Body.Close()

					// 只统计200响应
					if resp.StatusCode == 200 {
						atomic.AddInt64(&success, 1)
					}
				}
			}
		}(i)
	}

	// 进度打印
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-ticker.C:
				elapsed := time.Since(startAt).Seconds()
				t := atomic.LoadInt64(&total)
				s := atomic.LoadInt64(&success)
				f := atomic.LoadInt64(&fail)

				log.Printf("[%s] QPS: %.0f | Total: %d | Success: %d | Fail: %d",
					time.Since(startAt).Round(time.Second),
					float64(t)/elapsed,
					t, s, f,
				)
			case <-ctx.Done():
				return
			}
		}
	}()

	wg.Wait()

	elapsed := time.Since(startAt).Seconds()
	log.Printf("=== 压测完成 ===")
	log.Printf("总耗时: %.2fs", elapsed)
	log.Printf("总请求: %d", total)
	log.Printf("成功: %d (%.2f%%)", success, float64(success)/float64(total)*100)
	log.Printf("失败: %d (%.2f%%)", fail, float64(fail)/float64(total)*100)
	log.Printf("QPS: %.2f", float64(total)/elapsed)
}