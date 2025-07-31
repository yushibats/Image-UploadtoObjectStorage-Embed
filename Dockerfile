# マルチステージビルドを使用した最適化されたDockerfile
FROM python:3.12-slim as builder

# 作業ディレクトリの設定
WORKDIR /app

# システムの依存関係をインストール
RUN apt-get update && apt-get install -y \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Pythonの依存関係をインストール
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# 本番環境用イメージ
FROM python:3.12-slim

# 非rootユーザーの作成
RUN groupadd -r appuser && useradd -r -g appuser appuser

# 作業ディレクトリの設定
WORKDIR /app

# システムの依存関係をインストール（最小限）
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# ビルダーステージからPythonパッケージをコピー
COPY --from=builder /root/.local /home/appuser/.local

# アプリケーションファイルをコピー
COPY --chown=appuser:appuser . .

# 環境変数の設定
ENV PATH=/home/appuser/.local/bin:$PATH
ENV PYTHONPATH=/app
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# ポートの公開
EXPOSE 8000

# 非rootユーザーに切り替え
USER appuser

# ヘルスチェック
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# アプリケーションの起動
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--worker-class", "sync", "--timeout", "120", "--keep-alive", "2", "--max-requests", "1000", "--max-requests-jitter", "100", "wsgi:application"]
