package service

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"log"
	"time"

	_ "github.com/go-sql-driver/mysql"
)

// OrderService 订单服务
type OrderService struct {
	db *sql.DB
}

// Order 订单结构
type Order struct {
	ID        string    `json:"id"`
	UserID    string    `json:"user_id"`
	ProductID string    `json:"product_id"`
	Quantity  int       `json:"quantity"`
	Status    string    `json:"status"` // pending, paid, cancelled, expired
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// NewOrderService 创建订单服务
func NewOrderService(db *sql.DB) *OrderService {
	return &OrderService{db: db}
}

// InitSchema 初始化数据库表
func (s *OrderService) InitSchema(ctx context.Context) error {
	queries := []string{
		`CREATE TABLE IF NOT EXISTS orders (
			id VARCHAR(64) PRIMARY KEY,
			user_id VARCHAR(64) NOT NULL,
			product_id VARCHAR(64) NOT NULL,
			quantity INT NOT NULL DEFAULT 1,
			status VARCHAR(20) NOT NULL DEFAULT 'pending',
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
			INDEX idx_user_id (user_id),
			INDEX idx_product_id (product_id),
			INDEX idx_status (status)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		
		`CREATE TABLE IF NOT EXISTS products (
			id VARCHAR(64) PRIMARY KEY,
			name VARCHAR(255) NOT NULL,
			stock INT NOT NULL DEFAULT 0,
			price DECIMAL(10,2) NOT NULL,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
		
		`CREATE TABLE IF NOT EXISTS stock_logs (
			id BIGINT AUTO_INCREMENT PRIMARY KEY,
			product_id VARCHAR(64) NOT NULL,
			change_amount INT NOT NULL,
			order_id VARCHAR(64),
			reason VARCHAR(50),
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
			INDEX idx_product_id (product_id),
			INDEX idx_order_id (order_id)
		) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4`,
	}
	
	for _, q := range queries {
		if _, err := s.db.ExecContext(ctx, q); err != nil {
			return fmt.Errorf("init schema failed: %w", err)
		}
	}
	
	return nil
}

// CreateOrder 创建订单
func (s *OrderService) CreateOrder(ctx context.Context, userID, productID string) (*Order, error) {
	orderID := generateOrderID(userID)
	order := &Order{
		ID:        orderID,
		UserID:    userID,
		ProductID: productID,
		Quantity:  1,
		Status:    "pending",
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
	}
	
	query := `INSERT INTO orders (id, user_id, product_id, quantity, status) VALUES (?, ?, ?, ?, ?)`
	_, err := s.db.ExecContext(ctx, query, order.ID, order.UserID, order.ProductID, order.Quantity, order.Status)
	if err != nil {
		return nil, fmt.Errorf("create order failed: %w", err)
	}
	
	// 记录库存变化
	if err := s.logStockChange(ctx, productID, -1, orderID, "create_order"); err != nil {
		log.Printf("记录库存日志失败: %v", err)
	}
	
	return order, nil
}

// UpdateOrderStatus 更新订单状态
func (s *OrderService) UpdateOrderStatus(ctx context.Context, orderID, status string) error {
	query := `UPDATE orders SET status = ?, updated_at = ? WHERE id = ?`
	result, err := s.db.ExecContext(ctx, query, status, time.Now(), orderID)
	if err != nil {
		return err
	}
	
	affected, _ := result.RowsAffected()
	if affected == 0 {
		return fmt.Errorf("order not found: %s", orderID)
	}
	
	return nil
}

// GetOrder 获取订单
func (s *OrderService) GetOrder(ctx context.Context, orderID string) (*Order, error) {
	query := `SELECT id, user_id, product_id, quantity, status, created_at, updated_at 
			  FROM orders WHERE id = ?`
	
	order := &Order{}
	err := s.db.QueryRowContext(ctx, query, orderID).Scan(
		&order.ID, &order.UserID, &order.ProductID,
		&order.Quantity, &order.Status,
		&order.CreatedAt, &order.UpdatedAt,
	)
	if err == sql.ErrNoRows {
		return nil, fmt.Errorf("order not found")
	}
	if err != nil {
		return nil, err
	}
	
	return order, nil
}

// GetUserOrders 获取用户订单
func (s *OrderService) GetUserOrders(ctx context.Context, userID string) ([]*Order, error) {
	query := `SELECT id, user_id, product_id, quantity, status, created_at, updated_at 
			  FROM orders WHERE user_id = ? ORDER BY created_at DESC`
	
	rows, err := s.db.QueryContext(ctx, query, userID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	
	var orders []*Order
	for rows.Next() {
		order := &Order{}
		if err := rows.Scan(
			&order.ID, &order.UserID, &order.ProductID,
			&order.Quantity, &order.Status,
			&order.CreatedAt, &order.UpdatedAt,
		); err != nil {
			return nil, err
		}
		orders = append(orders, order)
	}
	
	return orders, rows.Err()
}

// CheckUserOrdered 检查用户是否已下单
func (s *OrderService) CheckUserOrdered(ctx context.Context, userID, productID string) (bool, error) {
	query := `SELECT COUNT(*) FROM orders WHERE user_id = ? AND product_id = ? AND status != 'cancelled'`
	
	var count int
	err := s.db.QueryRowContext(ctx, query, userID, productID).Scan(&count)
	if err != nil {
		return false, err
	}
	
	return count > 0, nil
}

// CancelOrder 取消订单 (回滚库存)
func (s *OrderService) CancelOrder(ctx context.Context, orderID string) error {
	order, err := s.GetOrder(ctx, orderID)
	if err != nil {
		return err
	}
	
	// 更新订单状态
	if err := s.UpdateOrderStatus(ctx, orderID, "cancelled"); err != nil {
		return err
	}
	
	// 记录库存回滚
	if err := s.logStockChange(ctx, order.ProductID, 1, orderID, "cancel_order"); err != nil {
		log.Printf("记录库存回滚日志失败: %v", err)
	}
	
	return nil
}

// ExpireOrders 过期超时未支付订单
func (s *OrderService) ExpireOrders(ctx context.Context, timeout time.Duration) (int, error) {
	cutoff := time.Now().Add(-timeout)
	
	query := `UPDATE orders SET status = 'expired' 
			  WHERE status = 'pending' AND created_at < ?`
	
	result, err := s.db.ExecContext(ctx, query, cutoff)
	if err != nil {
		return 0, err
	}
	
	affected, _ := result.RowsAffected()
	return int(affected), nil
}

func (s *OrderService) logStockChange(ctx context.Context, productID string, amount int, orderID, reason string) error {
	query := `INSERT INTO stock_logs (product_id, change_amount, order_id, reason) VALUES (?, ?, ?, ?)`
	_, err := s.db.ExecContext(ctx, query, productID, amount, orderID, reason)
	return err
}

// OrderMessage 订单消息
type OrderMessage struct {
	OrderID   string `json:"order_id"`
	UserID    string `json:"user_id"`
	ProductID string `json:"product_id"`
	Type      string `json:"type"` // create, cancel, pay
	Timestamp int64  `json:"timestamp"`
}

func (m *OrderMessage) ToJSON() ([]byte, error) {
	return json.Marshal(m)
}

func ParseOrderMessage(data []byte) (*OrderMessage, error) {
	msg := &OrderMessage{}
	err := json.Unmarshal(data, msg)
	return msg, err
}

func generateOrderID(userID string) string {
	return fmt.Sprintf("ORD%d%s", time.Now().UnixNano(), userID[:min(8, len(userID))])
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}