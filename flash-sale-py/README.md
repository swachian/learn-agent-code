# Flash Sale System - Python Edition

## Architecture

```
┌────────────────────────────────────────────────────────────────────┐
│                          Kubernetes Cluster                         │
│                                                                     │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                        │
│  │  Ingress │──▶│ Gateway  │──▶│ FastAPI  │                        │
│  │ (Nginx)  │   │ (限流)   │   │ (多副本) │                        │
│  └──────────┘   └──────────┘   └──────────┘                        │
│                                     │                               │
│                     ┌───────────────┼───────────────┐              │
│                     ▼               ▼               ▼              │
│               ┌─────────┐     ┌─────────┐     ┌─────────┐          │
│               │  Redis  │     │  Kafka  │     │  MySQL  │          │
│               │ Cluster │     │ Cluster │     │ Cluster │          │
│               └─────────┘     └─────────┘     └─────────┘          │
└────────────────────────────────────────────────────────────────────┘
```

## Design Principles

### 1. 100万TPS 实现方案

| 策略 | 实现 | 效果 |
|------|------|------|
| Redis分片 | 100个分片，每分片100库存 | 热点分散 |
| Lua原子操作 | 扣库存+标记用户 原子化 | 防止超卖 |
| 异步订单 | Kafka消息队列 | 削峰填谷 |
| 连接池复用 | Redis Pool + async | 减少开销 |
| 本地缓存 | LRU + 预热 | 减少Redis请求 |

### 2. 防超卖方案

```lua
-- stock_deduct.lua
local stock_key = KEYS[1]
local user_key = KEYS[2]

-- 1. 检查用户是否已购买
if redis.call('EXISTS', user_key) == 1 then
    return -2  -- 已购买
end

-- 2. 检查库存
local stock = tonumber(redis.call('GET', stock_key) or '0')
if stock < 1 then
    return -1  -- 库存不足
end

-- 3. 扣减库存 + 标记用户 (原子操作)
redis.call('DECR', stock_key)
redis.call('SETEX', user_key, 86400, '1')
return stock - 1
```

### 3. 限流策略

- **全局限流**: 滑动窗口 100万/秒
- **单用户限流**: 令牌桶 100/秒
- **熔断降级**: Hystrix 模式

## Quick Start

```bash
# 本地开发
pip install -r requirements.txt
redis-server
uvicorn main:app --reload

# 测试
ab -n 100000 -c 1000 http://localhost:8000/flash/subscribe?user_id=test
```