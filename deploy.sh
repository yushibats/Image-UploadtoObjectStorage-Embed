#!/bin/bash
# -*- coding: utf-8 -*-
"""
デプロイメントスクリプト
本番環境への安全なデプロイを実行
"""

set -e  # エラー時に停止

# 色付きログ出力
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 設定
APP_NAME="chatoci-images"
DOCKER_IMAGE="$APP_NAME:latest"
CONTAINER_NAME="$APP_NAME-app"
BACKUP_DIR="./backups"
ENV_FILE=".env"

# 前提条件チェック
check_prerequisites() {
    log_info "前提条件をチェック中..."
    
    # Docker の確認
    if ! command -v docker &> /dev/null; then
        log_error "Docker がインストールされていません"
        exit 1
    fi
    
    # Docker Compose の確認
    if ! command -v docker-compose &> /dev/null; then
        log_error "Docker Compose がインストールされていません"
        exit 1
    fi
    
    # 環境設定ファイルの確認
    if [ ! -f "$ENV_FILE" ]; then
        log_error "環境設定ファイル ($ENV_FILE) が見つかりません"
        log_info ".env.example をコピーして設定してください"
        exit 1
    fi
    
    log_success "前提条件チェック完了"
}

# バックアップ作成
create_backup() {
    log_info "バックアップを作成中..."
    
    mkdir -p "$BACKUP_DIR"
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    BACKUP_FILE="$BACKUP_DIR/backup_$TIMESTAMP.tar.gz"
    
    # 現在の設定をバックアップ
    tar -czf "$BACKUP_FILE" \
        --exclude="$BACKUP_DIR" \
        --exclude=".git" \
        --exclude="__pycache__" \
        --exclude="*.pyc" \
        --exclude="venv" \
        --exclude="node_modules" \
        .
    
    log_success "バックアップ作成完了: $BACKUP_FILE"
}

# テスト実行
run_tests() {
    log_info "テストを実行中..."
    
    # Python テスト
    if [ -f "test_app.py" ]; then
        python -m pytest test_app.py -v
        log_success "Python テスト完了"
    else
        log_warning "テストファイルが見つかりません"
    fi
}

# Docker イメージビルド
build_image() {
    log_info "Docker イメージをビルド中..."
    
    docker build -t "$DOCKER_IMAGE" .
    
    log_success "Docker イメージビルド完了"
}

# アプリケーションデプロイ
deploy_app() {
    log_info "アプリケーションをデプロイ中..."
    
    # 既存コンテナの停止と削除
    if docker ps -a | grep -q "$CONTAINER_NAME"; then
        log_info "既存コンテナを停止中..."
        docker stop "$CONTAINER_NAME" || true
        docker rm "$CONTAINER_NAME" || true
    fi
    
    # 新しいコンテナの起動
    docker-compose up -d
    
    # ヘルスチェック
    log_info "ヘルスチェック中..."
    sleep 10
    
    for i in {1..30}; do
        if curl -f http://localhost:5000/health > /dev/null 2>&1; then
            log_success "アプリケーションが正常に起動しました"
            return 0
        fi
        log_info "ヘルスチェック待機中... ($i/30)"
        sleep 2
    done
    
    log_error "ヘルスチェックに失敗しました"
    return 1
}

# ロールバック
rollback() {
    log_warning "ロールバックを実行中..."
    
    # 最新のバックアップを探す
    LATEST_BACKUP=$(ls -t "$BACKUP_DIR"/backup_*.tar.gz 2>/dev/null | head -n1)
    
    if [ -z "$LATEST_BACKUP" ]; then
        log_error "バックアップファイルが見つかりません"
        return 1
    fi
    
    log_info "バックアップから復元中: $LATEST_BACKUP"
    
    # 現在のファイルを一時的に移動
    mv . "../${APP_NAME}_temp" 2>/dev/null || true
    
    # バックアップから復元
    tar -xzf "$LATEST_BACKUP"
    
    # アプリケーション再起動
    docker-compose down
    docker-compose up -d
    
    log_success "ロールバック完了"
}

# クリーンアップ
cleanup() {
    log_info "クリーンアップ中..."
    
    # 古いDockerイメージの削除
    docker image prune -f
    
    # 古いバックアップの削除（30日以上古いもの）
    find "$BACKUP_DIR" -name "backup_*.tar.gz" -mtime +30 -delete 2>/dev/null || true
    
    log_success "クリーンアップ完了"
}

# メイン処理
main() {
    log_info "=== ChatOCI Images デプロイメント開始 ==="
    
    case "${1:-deploy}" in
        "deploy")
            check_prerequisites
            create_backup
            run_tests
            build_image
            if deploy_app; then
                cleanup
                log_success "=== デプロイメント完了 ==="
            else
                log_error "デプロイメントに失敗しました"
                read -p "ロールバックしますか? (y/N): " -n 1 -r
                echo
                if [[ $REPLY =~ ^[Yy]$ ]]; then
                    rollback
                fi
                exit 1
            fi
            ;;
        "rollback")
            rollback
            ;;
        "test")
            run_tests
            ;;
        "build")
            build_image
            ;;
        "cleanup")
            cleanup
            ;;
        *)
            echo "使用方法: $0 [deploy|rollback|test|build|cleanup]"
            echo "  deploy   - 完全なデプロイメント実行（デフォルト）"
            echo "  rollback - 最新バックアップからロールバック"
            echo "  test     - テストのみ実行"
            echo "  build    - Docker イメージビルドのみ"
            echo "  cleanup  - クリーンアップのみ"
            exit 1
            ;;
    esac
}

# スクリプト実行
main "$@"
