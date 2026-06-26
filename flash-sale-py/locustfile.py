"""
压力测试工具 - 使用Locust进行分布式压测
"""

import random
import time
from locust import HttpUser, task, between, events
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class FlashSaleUser(HttpUser):
    """秒杀用户模拟"""
    
    # 等待时间: 0.1-0.5秒 (高并发)
    wait_time = between(0.1, 0.5)
    
    def on_start(self):
        """开始时执行"""
        # 生成唯一用户ID
        self.user_id = f"user_{random.randint(1, 1000000)}"
        self.product_id = "SKU_2024_FLASH"
    
    @task
    def subscribe(self):
        """秒杀请求"""
        start_time = time.time()
        
        try:
            response = self.client.get(
                f"/flash/subscribe?user_id={self.user_id}&product_id={self.product_id}",
                name="/flash/subscribe",
                catch_response=True
            )
            
            if response.status_code == 200:
                data = response.json()
                
                if data.get("code") == 0:
                    response.success()
                    logger.info(f"抢购成功: user={self.user_id}, order={data.get('order_id')}")
                elif data.get("code") == 1001:
                    response.success()  # 已购买
                elif data.get("code") == 1002:
                    response.success()  # 库存不足
                else:
                    response.failure(f"未知响应: {data}")
            elif response.status_code == 429:
                response.success()  # 限流
                logger.warning(f"请求被限流: user={self.user_id}")
            else:
                response.failure(f"HTTP {response.status_code}")
                
        except Exception as e:
            response.failure(f"异常: {e}")
            
        # 记录响应时间
        elapsed = time.time() - start_time
        logger.debug(f"响应时间: {elapsed*1000:.2f}ms")


# 事件处理
@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    logger.info("压测开始")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    logger.info("压测结束")


if __name__ == "__main__":
    import os
    os.system("locust -f locustfile.py --host=http://localhost:8000")