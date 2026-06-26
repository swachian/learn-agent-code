"""
Kafka消费者 - 处理秒杀订单
"""

import asyncio
import json
import logging
import os
from datetime import datetime

import aiokafka
import asyncpg

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 配置
KAFKA_BOOTSTRAP_SERVERS = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = "flash-sale-orders"
CONSUMER_GROUP = "flash-sale-order-consumer"

# 数据库配置
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")
DB_NAME = os.getenv("DB_NAME", "flashsale")


async def init_db():
    """初始化数据库连接池"""
    pool = await asyncpg.create_pool(
        host=DB_HOST,
        port=DB_PORT,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        min_size=10,
        max_size=50
    )
    
    # 创建表
    async with pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id VARCHAR(64) PRIMARY KEY,
                user_id VARCHAR(64) NOT NULL,
                product_id VARCHAR(64) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        ''')
        
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)
        ''')
        
        await conn.execute('''
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)
        ''')
    
    return pool


async def process_order(conn, order_data: dict):
    """处理订单"""
    order_id = order_data["order_id"]
    user_id = order_data["user_id"]
    product_id = order_data["product_id"]
    
    # 模拟数据库操作
    try:
        await conn.execute(
            '''
            INSERT INTO orders (id, user_id, product_id, status)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (id) DO NOTHING
            ''',
            order_id, user_id, product_id, "pending"
        )
        logger.info(f"订单已创建: {order_id}")
    except Exception as e:
        logger.error(f"创建订单失败: {e}")


async def consume_orders(db_pool):
    """消费订单消息"""
    consumer = aiokafka.AIOKafkaConsumer(
        KAFKA_TOPIC,
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        group_id=CONSUMER_GROUP,
        auto_offset_reset='latest',
        enable_auto_commit=True
    )
    
    await consumer.start()
    logger.info(f"Kafka消费者已启动, topic: {KAFKA_TOPIC}")
    
    try:
        async for msg in consumer:
            try:
                order_data = json.loads(msg.value.decode())
                action = order_data.get("action")
                
                if action == "create":
                    async with db_pool.acquire() as conn:
                        await process_order(conn, order_data)
                else:
                    logger.warning(f"未知动作: {action}")
                    
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败: {e}")
            except Exception as e:
                logger.error(f"处理消息失败: {e}")
                
    finally:
        await consumer.stop()


async def main():
    """主函数"""
    logger.info("初始化数据库...")
    db_pool = await init_db()
    
    logger.info("启动消费者...")
    await consume_orders(db_pool)


if __name__ == "__main__":
    asyncio.run(main())