# -*- coding: utf-8 -*-
"""
OCI画像プロキシアプリケーション設定例
このファイルをconfig.pyにコピーして設定を変更してください
"""

import os

# Flask設定
SECRET_KEY = os.getenv('SECRET_KEY', 'your-secret-key-here')
DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'

# OCI設定
OCI_CONFIG_FILE = os.getenv('OCI_CONFIG_FILE', '~/.oci/config')
OCI_PROFILE = os.getenv('OCI_PROFILE', 'DEFAULT')
OCI_BUCKET = os.getenv('OCI_BUCKET', 'chatbot-images')

# アップロード設定
MAX_CONTENT_LENGTH = 10 * 1024 * 1024  # 10MB
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}

# ログ設定
LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
