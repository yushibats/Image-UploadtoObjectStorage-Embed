# -*- coding: utf-8 -*-
"""
Gunicorn 設定ファイル
本番環境での最適化されたWSGIサーバー設定
"""

import os
import multiprocessing

# サーバー設定
bind = f"0.0.0.0:{os.getenv('PORT', 8000)}"
workers = int(os.getenv('GUNICORN_WORKERS', multiprocessing.cpu_count() * 2 + 1))
worker_class = "sync"
worker_connections = 1000
max_requests = 1000
max_requests_jitter = 100
timeout = 120
keepalive = 2

# ログ設定
accesslog = "-"  # stdout
errorlog = "-"   # stderr
loglevel = os.getenv('LOG_LEVEL', 'info').lower()
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# プロセス設定
preload_app = True
daemon = False
pidfile = "/tmp/gunicorn.pid"
user = None
group = None
tmp_upload_dir = None

# セキュリティ設定
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# パフォーマンス設定
worker_tmp_dir = "/dev/shm"  # メモリ上の一時ディレクトリ（利用可能な場合）

# 開発環境設定
if os.getenv('FLASK_ENV') == 'development':
    reload = True
    workers = 1
    loglevel = 'debug'

def when_ready(server):
    """サーバー起動時のコールバック"""
    server.log.info("サーバーが起動しました")

def worker_int(worker):
    """ワーカープロセス中断時のコールバック"""
    worker.log.info("ワーカープロセスが中断されました: %s", worker.pid)

def pre_fork(server, worker):
    """ワーカープロセス作成前のコールバック"""
    server.log.info("ワーカープロセスを作成中: %s", worker.pid)

def post_fork(server, worker):
    """ワーカープロセス作成後のコールバック"""
    server.log.info("ワーカープロセスが作成されました: %s", worker.pid)

def pre_exec(server):
    """サーバー実行前のコールバック"""
    server.log.info("サーバーを実行中...")

def on_exit(server):
    """サーバー終了時のコールバック"""
    server.log.info("サーバーが終了しました")
