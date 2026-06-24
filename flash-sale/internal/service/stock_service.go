package service

import (
	"context"
	"fmt"
	"sync"
	"sync/atomic"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	// Lua脚本: 批量扣减库存
	BatchStockLuaScript = `
local stock_key = KEYS[1]
local user_key = KEYS[2]
local quantity = tonumber(ARGV[1])

-- 检查用户是否已购买
if redis.call('EXISTS', user_key) == 1 then
    return -2
end

-- 检查并扣减库存
local stock = tonumber(redis.call('GET', stock_key) or '0')
if stock < quantity then
    return -1
end

redis.call('DECRBY', stock_key, quantity)
redis.call('SETEX', user_key, 86400, '1')
return stock - quantity
`

	// Lua脚本: 恢复库存 (用于取消订单/超时回滚)
	RestoreStockLuaScript = `
local stock_key = KEYS[1]
local user_key = KEYS[2]
local quantity = tonumber(ARGV[1])

-- 删除用户购买记录
redis.call('DEL', user_key)
-- 恢复库存
redis.call('INCRBY', stock_key, quantity)
return redis.call('GET', stock_key)
`

	// Lua脚本: 滑动窗口限流
	SlidingWindowLuaScript = `
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

-- 删除窗口外的记录
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)

-- 统计窗口内请求数
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, now .. ':' .. math.random())
    redis.call('EXPIRE', key, math.ceil(window / 1000))
    return 1
end

return 0
`
)

// StockService 库存服务
type StockService struct {
	redis        *redis.Client
	stockLua     *redis.Script
	restoreLua   *redis.Script
	shards       int
	perShardStock int
}

// NewStockService 创建库存服务
func NewStockService(redisClient *redis.Client, totalStock, shards int) *StockService {
	return &StockService{
		redis:        redisClient,
		stockLua:     redis.NewScript(BatchStockLuaScript),
		restoreLua:   redis.NewScript(RestoreStockLuaScript),
		shards:       shards,
		perShardStock: totalStock / shards,
	}
}

// GetStockKey 获取库存分片Key
func (s *StockService) GetStockKey(shardID int) string {
	return fmt.Sprintf("flash:stock:%d", shardID)
}

// GetUserKey 获取用户购买记录Key
func (s *StockService) GetUserKey(productID, userID string) string {
	return fmt.Sprintf("flash:user:%s:%s", productID, userID)
}

// GetAllStockKeys 获取所有库存分片Keys
func (s *StockService) GetAllStockKeys() []string {
	keys := make([]string, s.shards)
	for i := 0; i < s.shards; i++ {
		keys[i] = s.GetStockKey(i)
	}
	return keys
}

// DeductStock 扣减库存
// 返回值: >0 剩余库存, -1 库存不足, -2 用户已购买, -3 系统错误
func (s *StockService) DeductStock(ctx context.Context, productID, userID string) (int64, error) {
	shardID := selectShard(userID, s.shards)
	stockKey := s.GetStockKey(shardID)
	userKey := s.GetUserKey(productID, userID)
	
	result, err := s.stockLua.Run(ctx, s.redis, []string{stockKey, userKey}, 1).Int64()
	if err != nil {
		return -3, fmt.Errorf("stock deduction failed: %w", err)
	}
	
	return result, nil
}

// DeductStockMulti 多分片扣减 (尝试多个分片直到成功)
func (s *StockService) DeductStockMulti(ctx context.Context, productID, userID string) (bool, error) {
	// 尝试顺序: 用户ID哈希 -> 随机 -> 全量扫描
	strategies := [][]int{
		// 策略1: 固定分片
		{selectShard(userID, s.shards)},
		// 策略2: 随机分片
		{randomShard(s.shards)},
		// 策略3: 连续分片
		generateConsecutiveShards(s.shards),
	}
	
	for _, shards := range strategies {
		for _, shardID := range shards {
			stockKey := s.GetStockKey(shardID)
			userKey := s.GetUserKey(productID, userID)
			
			result, err := s.stockLua.Run(ctx, s.redis, []string{stockKey, userKey}, 1).Int64()
			if err != nil {
				continue
			}
			
			if result >= 0 {
				return true, nil
			}
			if result == -2 {
				return false, nil // 用户已购买
			}
		}
	}
	
	return false, nil
}

// RestoreStock 恢复库存 (取消订单时调用)
func (s *StockService) RestoreStock(ctx context.Context, productID, userID string) error {
	shardID := selectShard(userID, s.shards)
	stockKey := s.GetStockKey(shardID)
	userKey := s.GetUserKey(productID, userID)
	
	_, err := s.restoreLua.Run(ctx, s.redis, []string{stockKey, userKey}, 1).Result()
	return err
}

// GetStock 获取总库存
func (s *StockService) GetStock(ctx context.Context) (int64, error) {
	keys := s.GetAllStockKeys()
	pipe := s.redis.Pipeline()
	cmds := make([]*redis.StringCmd, len(keys))
	
	for i, key := range keys {
		cmds[i] = pipe.Get(ctx, key)
	}
	_, err := pipe.Exec(ctx)
	
	var total int64
	for _, cmd := range cmds {
		if stock, err := cmd.Int64(); err == nil {
			total += stock
		}
	}
	
	return total, err
}

// GetStockByShards 获取各分片库存
func (s *StockService) GetStockByShards(ctx context.Context) (map[int]int64, error) {
	pipe := s.redis.Pipeline()
	cmds := make([]*redis.StringCmd, s.shards)
	
	for i := 0; i < s.shards; i++ {
		cmds[i] = pipe.Get(ctx, fmt.Sprintf("flash:stock:%d", i))
	}
	_, err := pipe.Exec(ctx)
	
	result := make(map[int]int64)
	for i, cmd := range cmds {
		if stock, err := cmd.Int64(); err == nil {
			result[i] = stock
		} else {
			result[i] = 0
		}
	}
	
	return result, err
}

// CheckUserBought 检查用户是否已购买
func (s *StockService) CheckUserBought(ctx context.Context, productID, userID string) (bool, error) {
	userKey := s.GetUserKey(productID, userID)
	exists, err := s.redis.Exists(ctx, userKey).Result()
	return exists > 0, err
}

// 辅助函数

func selectShard(userID string, totalShards int) int {
	// 使用简单哈希选择分片
	h := hashString(userID)
	return int(h % int64(totalShards))
}

func randomShard(totalShards int) int {
	return int(time.Now().UnixNano() % int64(totalShards))
}

func generateConsecutiveShards(total int) []int {
	shards := make([]int, total)
	for i := 0; i < total; i++ {
		shards[i] = i
	}
	// 随机打乱
	for i := total - 1; i > 0; i-- {
		j := int(time.Now().UnixNano() % int64(i+1))
		shards[i], shards[j] = shards[j], shards[i]
	}
	return shards
}

func hashString(s string) int64 {
	var h int64
	for _, c := range s {
		h = 31*h + int64(c)
	}
	return h
}

// DistributedLimiter 分布式限流器
type DistributedLimiter struct {
	redis   *redis.Client
	limiter *redis.Script
}

// NewDistributedLimiter 创建分布式限流器
func NewDistributedLimiter(redisClient *redis.Client) *DistributedLimiter {
	return &DistributedLimiter{
		redis:   redisClient,
		limiter: redis.NewScript(SlidingWindowLuaScript),
	}
}

// Allow 检查是否允许通过
func (l *DistributedLimiter) Allow(ctx context.Context, key string, windowMs, limit int64) (bool, error) {
	now := time.Now().UnixMilli()
	result, err := l.limiter.Run(ctx, l.redis, []string{key}, now, windowMs, limit).Int64()
	if err != nil {
		return false, err
	}
	return result == 1, nil
}

// AtomicCounter 原子计数器
type AtomicCounter struct {
	redis *redis.Client
	key   string
	mu    sync.Mutex
	last  int64
}

// NewAtomicCounter 创建原子计数器
func NewAtomicCounter(redisClient *redis.Client, key string) *AtomicCounter {
	return &AtomicCounter{
		redis: redisClient,
		key:   key,
	}
}

// Incr 增加计数
func (c *AtomicCounter) Incr(ctx context.Context) (int64, error) {
	return c.redis.Incr(ctx, c.key).Result()
}

// Get 获取当前值
func (c *AtomicCounter) Get(ctx context.Context) (int64, error) {
	return c.redis.Get(ctx, c.key).Int64()
}

// Reset 重置计数器
func (c *AtomicCounter) Reset(ctx context.Context) error {
	return c.redis.Del(ctx, c.key).Err()
}

// 预热缓存
func WarmupCache(ctx context.Context, redisClient *redis.Client, productID string, totalStock, shards int) error {
	perShard := totalStock / shards
	pipe := redisClient.Pipeline()
	
	for i := 0; i < shards; i++ {
		key := fmt.Sprintf("flash:stock:%d", i)
		pipe.Set(ctx, key, perShard, 0)
	}
	
	// 预热热点数据
	warmupKeys := []string{
		"flash:product:" + productID,
		"flash:config:" + productID,
	}
	for _, k := range warmupKeys {
		pipe.Set(ctx, k, "1", 0)
	}
	
	_, err := pipe.Exec(ctx)
	return err
}

// LocalCache 本地缓存 (减少Redis请求)
type LocalCache struct {
	stock   int64
	updated atomic.Int64
	mu      sync.RWMutex
}

func NewLocalCache() *LocalCache {
	return &LocalCache{}
}

func (c *LocalCache) Get() int64 {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return c.stock
}

func (c *LocalCache) Set(stock int64) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.stock = stock
	c.updated.Store(time.Now().Unix())
}

func (c *LocalCache) IsExpired() bool {
	return time.Since(time.Unix(c.updated.Load(), 0)) > time.Second
}