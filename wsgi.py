# -*- coding: utf-8 -*-
"""
WSGI エントリーポイント
本番環境でのアプリケーション起動用
"""

import os
from app import create_app, create_production_app, create_development_app

# 環境に応じたアプリケーション作成
env = os.getenv('FLASK_ENV', 'production')

if env == 'production':
    application = create_production_app()
elif env == 'development':
    application = create_development_app()
else:
    application = create_app(env)

# Gunicorn用のエイリアス
app = application

if __name__ == "__main__":
    # 開発サーバーでの起動
    port = int(os.getenv('PORT', 5000))
    application.run(host='0.0.0.0', port=port, debug=(env == 'development'))
