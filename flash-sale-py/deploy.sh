#!/bin/bash
# Flash Sale System - Deployment Script

set -e

NAMESPACE="flash-sale"
REGISTRY="your-registry.example.com"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Build Docker images
build_images() {
    log_info "Building Docker images..."
    
    # API image
    docker build -t flash-sale:latest -f Dockerfile .
    
    # Consumer image
    docker build -t flash-sale-consumer:latest -f Dockerfile --target api .
    
    # Tag for registry
    docker tag flash-sale:latest ${REGISTRY}/flash-sale-api:latest
    docker tag flash-sale-consumer:latest ${REGISTRY}/flash-sale-consumer:latest
    
    log_info "Images built successfully"
}

# Push images to registry
push_images() {
    log_info "Pushing images to registry..."
    
    docker push ${REGISTRY}/flash-sale-api:latest
    docker push ${REGISTRY}/flash-sale-consumer:latest
    
    log_info "Images pushed successfully"
}

# Deploy to Kubernetes
deploy_k8s() {
    log_info "Deploying to Kubernetes..."
    
    # Create namespace
    kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -
    
    # Apply k8s manifests
    kubectl apply -f k8s/flash-sale.yaml -n ${NAMESPACE}
    
    # Wait for deployment
    kubectl rollout status deployment/flash-sale-api -n ${NAMESPACE}
    kubectl rollout status deployment/flash-sale-consumer -n ${NAMESPACE}
    
    log_info "Deployment completed"
}

# Local development with docker-compose
deploy_local() {
    log_info "Starting local environment with docker-compose..."
    
    docker-compose up -d
    
    log_info "Local environment started"
    log_info "API: http://localhost:8000"
    log_info "Locust: http://localhost:8089"
}

# Run load test
load_test() {
    log_info "Running load test..."
    
    locust -f locustfile.py \
        --host=http://localhost:8000 \
        --headless \
        -u 10000 \
        -r 1000 \
        --run-time 60s \
        --spawn-rate 100 \
        --csv results
}

# Cleanup
cleanup() {
    log_warn "Cleaning up..."
    
    kubectl delete -f k8s/flash-sale.yaml -n ${NAMESPACE} --ignore-not-found=true
    docker-compose down -v
    
    log_info "Cleanup completed"
}

# Show status
status() {
    log_info "Checking status..."
    
    echo ""
    echo "=== Kubernetes Pods ==="
    kubectl get pods -n ${NAMESPACE}
    
    echo ""
    echo "=== Kubernetes Services ==="
    kubectl get svc -n ${NAMESPACE}
    
    echo ""
    echo "=== Local Containers ==="
    docker-compose ps
}

# Show logs
logs() {
    local pod=$1
    kubectl logs -f deployment/${pod} -n ${NAMESPACE}
}

# Scale deployment
scale() {
    local replicas=$1
    log_info "Scaling to $replicas replicas..."
    kubectl scale deployment/flash-sale-api --replicas=$replicas -n ${NAMESPACE}
}

# Main
case "$1" in
    build)
        build_images
        ;;
    push)
        push_images
        ;;
    deploy)
        deploy_k8s
        ;;
    local)
        deploy_local
        ;;
    test)
        load_test
        ;;
    cleanup)
        cleanup
        ;;
    status)
        status
        ;;
    logs)
        logs "${2:-flash-sale-api}"
        ;;
    scale)
        scale "${2:-10}"
        ;;
    all)
        build_images
        push_images
        deploy_k8s
        ;;
    *)
        echo "Usage: $0 {build|push|deploy|local|test|cleanup|status|logs|scale}"
        echo ""
        echo "Commands:"
        echo "  build    - Build Docker images"
        echo "  push     - Push images to registry"
        echo "  deploy   - Deploy to Kubernetes"
        echo "  local    - Start local environment"
        echo "  test     - Run load test"
        echo "  cleanup  - Cleanup resources"
        echo "  status   - Show status"
        echo "  logs     - Show logs (optional: pod name)"
        echo "  scale    - Scale replicas (default: 10)"
        echo "  all      - Build, push, and deploy"
        exit 1
        ;;
esac