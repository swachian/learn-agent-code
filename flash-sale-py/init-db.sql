-- Flash Sale Database Schema

-- 1. 订单表
CREATE TABLE IF NOT EXISTS orders (
    id VARCHAR(64) PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    product_id VARCHAR(64) NOT NULL,
    quantity INT NOT NULL DEFAULT 1,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    CONSTRAINT uk_user_product UNIQUE (user_id, product_id)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_product_id ON orders(product_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_created_at ON orders(created_at);

-- 2. 商品表
CREATE TABLE IF NOT EXISTS products (
    id VARCHAR(64) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    price DECIMAL(10, 2) NOT NULL,
    stock INT NOT NULL DEFAULT 0,
    stock_shards INT NOT NULL DEFAULT 100,
    status VARCHAR(20) NOT NULL DEFAULT 'inactive',
    flash_sale_start TIMESTAMP,
    flash_sale_end TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_products_status ON products(status);
CREATE INDEX IF NOT EXISTS idx_products_flash_time ON products(flash_sale_start, flash_sale_end);

-- 3. 库存变更日志表
CREATE TABLE IF NOT EXISTS stock_logs (
    id BIGSERIAL PRIMARY KEY,
    product_id VARCHAR(64) NOT NULL,
    shard_id INT NOT NULL,
    change_amount INT NOT NULL,
    order_id VARCHAR(64),
    reason VARCHAR(50) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_stock_logs_product_id ON stock_logs(product_id);
CREATE INDEX IF NOT EXISTS idx_stock_logs_order_id ON stock_logs(order_id);
CREATE INDEX IF NOT EXISTS idx_stock_logs_created_at ON stock_logs(created_at);

-- 4. 用户购买记录表 (用于查询)
CREATE TABLE IF NOT EXISTS user_purchases (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    product_id VARCHAR(64) NOT NULL,
    order_id VARCHAR(64) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_user_purchases_user_product ON user_purchases(user_id, product_id);

-- 5. 秒杀活动表
CREATE TABLE IF NOT EXISTS flash_sales (
    id BIGSERIAL PRIMARY KEY,
    product_id VARCHAR(64) NOT NULL,
    name VARCHAR(255) NOT NULL,
    total_stock INT NOT NULL,
    remaining_stock INT NOT NULL,
    price DECIMAL(10, 2) NOT NULL,
    original_price DECIMAL(10, 2),
    start_time TIMESTAMP NOT NULL,
    end_time TIMESTAMP NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_flash_sales_status ON flash_sales(status);
CREATE INDEX IF NOT EXISTS idx_flash_sales_time ON flash_sales(start_time, end_time);

-- 初始化测试商品
INSERT INTO products (id, name, price, stock, status, flash_sale_start, flash_sale_end)
VALUES (
    'SKU_2024_FLASH',
    'iPhone 15 Pro Max 256GB',
    9999.00,
    10000,
    'active',
    NOW(),
    NOW() + INTERVAL '7 days'
)
ON CONFLICT (id) DO NOTHING;

-- 初始化秒杀活动
INSERT INTO flash_sales (product_id, name, total_stock, remaining_stock, price, original_price, start_time, end_time, status)
VALUES (
    'SKU_2024_FLASH',
    'iPhone 15 Pro Max 限时秒杀',
    10000,
    10000,
    7999.00,
    9999.00,
    NOW(),
    NOW() + INTERVAL '7 days',
    'active'
)
ON CONFLICT DO NOTHING;