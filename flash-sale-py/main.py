"""
Flash Sale System - FastAPI Application
支持100万TPS的秒杀系统
"""

import asyncio
import hashlib
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import aiokafka

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============== 配置 ==============

class Config:
    """应用配置"""
    # Redis配置
    REDIS_HOST = "localhost"
    REDIS_PORT = 6379
    REDIS_DB = 0
    REDIS_POOL_SIZE = 1000
    REDIS_MAX_CONNECTIONS = 2000
    
    # 业务配置
    PRODUCT_ID = "SKU_2024_FLASH"
    TOTAL_STOCK = 10000
    STOCK_SHARDS = 100
    PER_SHARD_STOCK = TOTAL_STOCK // STOCK_SHARDS
    
    # Kafka配置
    KAFKA_BOOTSTRAP_SERVERS = ["localhost:9092"]
    KAFKA_TOPIC_ORDER = "flash-sale-orders"
    
    # 限流配置
    GLOBAL_QPS = 1000000
    USER_QPS = 100

config = Config()

# ============== 全局变量 ==============

redis_pool: Optional[redis.ConnectionPool] = None
redis_client: Optional[redis.Redis] = None
kafka_producer: Optional[aiokafka.AIOKafkaProducer] = None
rate_limiter: Optional['RateLimiter'] = None

# ============== Lua脚本 ==============

STOCK_DEDUCT_SCRIPT = """
local stock_key = KEYS[1]
local user_key = KEYS[2]

-- 检查用户是否已购买
if redis.call('EXISTS', user_key) == 1 then
    return -2
end

-- 检查并扣减库存
local stock = tonumber(redis.call('GET', stock_key) or '0')
if stock < 1 then
    return -1
end

redis.call('DECR', stock_key)
redis.call('SETEX', user_key, 86400, '1')
return stock - 1
"""

STOCK_RESTORE_SCRIPT = """
local stock_key = KEYS[1]
local user_key = KEYS[2]

redis.call('DEL', user_key)
redis.call('INCR', stock_key)
return redis.call('GET', stock_key)
"""

RATE_LIMIT_SCRIPT = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)

if count < limit then
    redis.call('ZADD', key, now, now .. ':' .. math.random())
    redis.call('PEXPIRE', key, window)
    return 1
end

return 0
"""

# ============== 初始化 ==============

async def init_redis():
    """初始化Redis连接池"""
    global redis_pool, redis_client
    
    redis_pool = redis.ConnectionPool(
        host=config.REDIS_HOST,
        port=config.REDIS_PORT,
        db=config.REDIS_DB,
        max_connections=config.REDIS_MAX_CONNECTIONS,
        decode_responses=True
    )
    redis_client = redis.Redis(connection_pool=redis_pool)
    
    await init_stock()
    logger.info(f"Redis连接池已初始化, 最大连接: {config.REDIS_MAX_CONNECTIONS}")

async def init_stock():
    """初始化库存到Redis"""
    pipe = redis_client.pipeline()
    
    for i in range(config.STOCK_SHARDS):
        key = f"flash:stock:{i}"
        pipe.set(key, config.PER_SHARD_STOCK)
    
    pipe.delete(f"flash:user:{config.PRODUCT_ID}:*")
    
    await pipe.execute()
    logger.info(f"库存初始化完成: 总库存={config.TOTAL_STOCK}, 分片={config.STOCK_SHARDS}")

async def init_kafka():
    """初始化Kafka生产者"""
    global kafka_producer
    
    try:
        kafka_producer = aiokafka.AIOKafkaProducer(
            bootstrap_servers=config.KAFKA_BOOTSTRAP_SERVERS,
            acks='all',
            retries=3,
            max_batch_size=16384,
            linger_ms=10,
        )
        await kafka_producer.start()
        logger.info("Kafka生产者已启动")
    except Exception as e:
        logger.warning(f"Kafka启动失败: {e}, 将使用异步处理")
        kafka_producer = None

async def close_connections():
    """关闭所有连接"""
    global redis_pool, redis_client, kafka_producer
    
    if redis_client:
        await redis_client.aclose()
    if kafka_producer:
        await kafka_producer.stop()
    logger.info("连接已关闭")

# ============== 限流器 ==============

class RateLimiter:
    """滑动窗口限流器"""
    
    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client
        self.global_limit_key = "flash:ratelimit:global"
        self.user_limit_prefix = "flash:ratelimit:user:"
    
    async def check_global(self) -> bool:
        """全局限流检查"""
        now = time.time() * 1000
        window = 1000
        
        result = await self.redis.eval(
            RATE_LIMIT_SCRIPT,
            1,
            self.global_limit_key,
            now,
            window,
            config.GLOBAL_QPS
        )
        return result == 1
    
    async def check_user(self, user_id: str) -> bool:
        """单用户限流检查"""
        now = time.time() * 1000
        window = 1000
        key = f"{self.user_limit_prefix}{user_id}"
        
        result = await self.redis.eval(
            RATE_LIMIT_SCRIPT,
            1,
            key,
            now,
            window,
            config.USER_QPS
        )
        return result == 1

# ============== 工具函数 ==============

def get_shard_id(user_id: str, total_shards: int) -> int:
    """根据用户ID选择分片"""
    h = int(hashlib.md5(user_id.encode()).hexdigest(), 16)
    return h % total_shards

def generate_order_id(user_id: str) -> str:
    """生成订单号"""
    timestamp = int(time.time() * 1000000)
    return f"ORD{timestamp}{user_id[:8]}"

# ============== 数据模型 ==============

class FlashSubscribeResponse(BaseModel):
    code: int
    message: str
    order_id: Optional[str] = None
    stock: Optional[int] = None

class StockResponse(BaseModel):
    current_stock: int
    total_stock: int
    sold: int

class HealthResponse(BaseModel):
    status: str
    redis: str
    stock: int

# ============== API路由 ==============

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("Flash Sale服务启动中...")
    await init_redis()
    await init_kafka()
    global rate_limiter
    rate_limiter = RateLimiter(redis_client)
    logger.info("Flash Sale服务已启动")
    
    yield
    
    await close_connections()

app = FastAPI(
    title="Flash Sale API",
    description="高性能秒杀系统",
    version="1.0.0",
    lifespan=lifespan
)

# 添加CORS中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 限流中间件
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    if request.url.path in ["/health", "/metrics", "/docs", "/openapi.json"]:
        return await call_next(request)
    
    if not await rate_limiter.check_global():
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"code": 429, "message": "系统繁忙，请稍后重试"}
        )
    
    return await call_next(request)

@app.get("/flash/subscribe", response_model=FlashSubscribeResponse)
async def subscribe(user_id: str, product_id: Optional[str] = None):
    """秒杀接口"""
    product_id = product_id or config.PRODUCT_ID
    
    # 用户限流检查
    if not await rate_limiter.check_user(user_id):
        return FlashSubscribeResponse(
            code=1003,
            message="请求过于频繁，请稍后重试"
        )
    
    # 扣减库存
    shard_id = get_shard_id(user_id, config.STOCK_SHARDS)
    stock_key = f"flash:stock:{shard_id}"
    user_key = f"flash:user:{product_id}:{user_id}"
    
    try:
        result = await redis_client.eval(
            STOCK_DEDUCT_SCRIPT,
            2,
            stock_key,
            user_key
        )
    except Exception as e:
        logger.error(f"Redis执行失败: {e}")
        return FlashSubscribeResponse(
            code=500,
            message="系统错误，请稍后重试"
        )
    
    # 处理结果
    if result == -2:
        return FlashSubscribeResponse(code=1001, message="您已购买过该商品")
    elif result == -1:
        return FlashSubscribeResponse(code=1002, message="库存不足")
    else:
        order_id = generate_order_id(user_id)
        asyncio.create_task(create_order_async(user_id, product_id, order_id))
        return FlashSubscribeResponse(
            code=0,
            message="恭喜，抢购成功!",
            order_id=order_id
        )

async def create_order_async(user_id: str, product_id: str, order_id: str):
    """异步创建订单"""
    order_data = {
        "order_id": order_id,
        "user_id": user_id,
        "product_id": product_id,
        "action": "create",
        "timestamp": time.time()
    }
    
    if kafka_producer:
        try:
            await kafka_producer.send_and_wait(
                config.KAFKA_TOPIC_ORDER,
                json.dumps(order_data).encode()
            )
            logger.info(f"[Kafka] 订单消息已发送: {order_id}")
        except Exception as e:
            logger.error(f"[Kafka] 发送失败: {e}")
    else:
        logger.info(f"[本地] 订单创建: {order_id}")

@app.get("/flash/stock", response_model=StockResponse)
async def get_stock():
    """获取当前库存"""
    pipe = redis_client.pipeline()
    
    for i in range(config.STOCK_SHARDS):
        pipe.get(f"flash:stock:{i}")
    
    stocks = await pipe.execute()
    current_stock = sum(int(s or 0) for s in stocks)
    
    return StockResponse(
        current_stock=current_stock,
        total_stock=config.TOTAL_STOCK,
        sold=config.TOTAL_STOCK - current_stock
    )

@app.get("/health", response_model=HealthResponse)
async def health_check():
    """健康检查"""
    redis_status = "ok"
    try:
        await redis_client.ping()
    except Exception:
        redis_status = "error"
    
    pipe = redis_client.pipeline()
    for i in range(config.STOCK_SHARDS):
        pipe.get(f"flash:stock:{i}")
    stocks = await pipe.execute()
    current_stock = sum(int(s or 0) for s in stocks)
    
    return HealthResponse(
        status="healthy" if redis_status == "ok" else "unhealthy",
        redis=redis_status,
        stock=current_stock
    )

@app.post("/flash/restore")
async def restore_stock(user_id: str, product_id: Optional[str] = None):
    """恢复库存"""
    product_id = product_id or config.PRODUCT_ID
    
    shard_id = get_shard_id(user_id, config.STOCK_SHARDS)
    stock_key = f"flash:stock:{shard_id}"
    user_key = f"flash:user:{product_id}:{user_id}"
    
    try:
        await redis_client.eval(
            STOCK_RESTORE_SCRIPT,
            2,
            stock_key,
            user_key
        )
        return {"code": 0, "message": "库存已恢复"}
    except Exception as e:
        logger.error(f"恢复库存失败: {e}")
        return {"code": 500, "message": "系统错误"}

@app.get("/metrics")
async def metrics():
    """Prometheus监控指标"""
    pipe = redis_client.pipeline()
    for i in range(config.STOCK_SHARDS):
        pipe.get(f"flash:stock:{i}")
    stocks = await pipe.execute()
    current_stock = sum(int(s or 0) for s in stocks)
    
    return {
        "flash_sale_stock_total": config.TOTAL_STOCK,
        "flash_sale_stock_current": current_stock,
        "flash_sale_stock_sold": config.TOTAL_STOCK - current_stock
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)