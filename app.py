#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle Cloud Storage 画像プロキシアプリケーション
OCIオブジェクトストレージの画像を認証付きで表示・アップロードするFlaskアプリ

追加: /upload でアップロード後に OCI Generative AI (Cohere Embed v4) で
画像をベクトル化し、Oracle Database（23c VECTOR型を想定）に保存します。

必要な環境変数:
- DB_USER, DB_PASSWORD, DB_DSN
- OCI_COMPARTMENT_OCID
- OCI_COHERE_EMBED_MODEL (例: cohere.embed-v4.0)

必要パッケージ:
- oci
- oracledb
"""

import os
import uuid
import base64
import array
import mimetypes
import structlog
from io import BytesIO
from datetime import datetime
from typing import Optional
from pathlib import Path

from flask import Flask, Response, request, jsonify, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman

import oracledb
import oci
from oci.object_storage import ObjectStorageClient
from oci.exceptions import ServiceError
import sentry_sdk
from sentry_sdk.integrations.flask import FlaskIntegration

from config import settings, get_config

from dotenv import load_dotenv
load_dotenv()

# 構造化ログの設定
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer(ensure_ascii=False)
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    wrapper_class=structlog.stdlib.BoundLogger,
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger(__name__)

# =========================================
# OCI Object Storage クライアント
# =========================================
class OCIClient:
    """OCI Object Storage クライアントラッパー"""

    def __init__(self):
        self.client: Optional[ObjectStorageClient] = None
        self.namespace: Optional[str] = None
        self._initialize()

    def _initialize(self):
        """OCI クライアントの初期化"""
        try:
            config_file = os.path.expanduser(settings.OCI_CONFIG_FILE)
            if not os.path.exists(config_file):
                logger.warning("OCI設定ファイルが見つかりません", config_file=config_file)
                return

            config = oci.config.from_file(
                file_location=config_file,
                profile_name=settings.OCI_PROFILE
            )

            if settings.OCI_REGION:
                config['region'] = settings.OCI_REGION

            self.client = ObjectStorageClient(config)
            self.namespace = self.client.get_namespace().data

            logger.info("OCI接続成功",
                        namespace=self.namespace,
                        region=config.get('region'))

        except Exception as e:
            logger.error("OCI設定の初期化に失敗", error=str(e))
            self.client = None
            self.namespace = None

    def is_connected(self) -> bool:
        return self.client is not None and self.namespace is not None

    def get_object(self, bucket_name: str, object_name: str):
        if not self.is_connected():
            raise RuntimeError("OCI クライアントが初期化されていません")
        return self.client.get_object(
            namespace_name=self.namespace,
            bucket_name=bucket_name,
            object_name=object_name
        )

    def put_object(self, bucket_name: str, object_name: str, data, content_type: str = None):
        if not self.is_connected():
            raise RuntimeError("OCI クライアントが初期化されていません")
        return self.client.put_object(
            namespace_name=self.namespace,
            bucket_name=bucket_name,
            object_name=object_name,
            put_object_body=data,
            content_type=content_type
        )

# グローバル OCI クライアント
oci_client = OCIClient()

# =========================================
# ユーティリティ
# =========================================
def allowed_file(filename: str) -> bool:
    if not filename or '.' not in filename:
        return False
    extension = filename.rsplit('.', 1)[1].lower()
    return extension in settings.ALLOWED_EXTENSIONS

def _embed_image_with_cohere_v4(data_uris: list[str]) -> list[array.array]:
    """
    OCI Generative AI (Cohere Embed v4)で画像のembeddingを生成。
    入力は data URI (data:<mime>;base64,<payload>) の配列。
    戻り値: array('f')（float32）を要素にもつリスト（1枚なら長さ1）
    """
    config = oci.config.from_file(
        os.path.expanduser(settings.OCI_CONFIG_FILE),
        profile_name=settings.OCI_PROFILE
    )
    region = settings.OCI_REGION or config.get("region")
    if not region:
        raise RuntimeError("OCIリージョンが解決できません。settings.OCI_REGION または設定ファイルを確認してください。")

    gai = oci.generative_ai_inference.GenerativeAiInferenceClient(
        config=config,
        service_endpoint=f"https://inference.generativeai.{region}.oci.oraclecloud.com",
        retry_strategy=oci.retry.NoneRetryStrategy(),
        timeout=(10, 240),
    )

    details = oci.generative_ai_inference.models.EmbedTextDetails()
    details.serving_mode = oci.generative_ai_inference.models.OnDemandServingMode(
        model_id=os.environ.get("OCI_COHERE_EMBED_MODEL", "cohere.embed-v4.0")
    )
    details.input_type = "IMAGE"
    details.inputs = data_uris
    details.truncate = "NONE"
    details.compartment_id = os.environ["OCI_COMPARTMENT_OCID"]

    resp = gai.embed_text(details)
    out: list[array.array] = []
    for emb in resp.data.embeddings:
        out.append(array.array("f", emb))
    return out

def _save_embedding_to_db(bucket: str, object_name: str, content_type: str,
                          file_size: int, embedding: array.array):
    """
    python-oracledb Thin で VECTOR 型にINSERT
    期待テーブル: img_embeddings(bucket, object_name, content_type, file_size, uploaded_at, embedding)
    embedding は VECTOR(1536, FLOAT32) を想定
    """
    conn = oracledb.connect(
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
        dsn=os.environ["DB_DSN"],
    )
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO img_embeddings
              (bucket, object_name, content_type, file_size, uploaded_at, embedding)
            VALUES
              (:1, :2, :3, :4, SYSTIMESTAMP, :5)
            """,
            [bucket, object_name, content_type, file_size, embedding],
        )
        conn.commit()
    finally:
        conn.close()

# =========================================
# Flask アプリケーション
# =========================================
def create_app(config_name: str = None) -> Flask:
    app = Flask(__name__)

    if config_name:
        config_class = get_config(config_name)
        app.config.from_object(config_class)

    app.config.update(
        SECRET_KEY=settings.SECRET_KEY,
        MAX_CONTENT_LENGTH=settings.MAX_CONTENT_LENGTH,
        SESSION_COOKIE_SECURE=settings.SESSION_COOKIE_SECURE,
        SESSION_COOKIE_HTTPONLY=settings.SESSION_COOKIE_HTTPONLY,
        SESSION_COOKIE_SAMESITE=settings.SESSION_COOKIE_SAMESITE,
        PERMANENT_SESSION_LIFETIME=settings.PERMANENT_SESSION_LIFETIME,
    )

    # Sentry
    if settings.SENTRY_DSN and settings.SENTRY_DSN.strip():
        try:
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                integrations=[FlaskIntegration()],
                environment=settings.SENTRY_ENVIRONMENT,
                traces_sample_rate=0.1,
            )
            logger.info("Sentry初期化完了", environment=settings.SENTRY_ENVIRONMENT)
        except Exception as e:
            logger.warning("Sentry初期化に失敗", error=str(e))
    else:
        logger.info("Sentry DSNが設定されていません。エラー監視は無効です。")

    # CORS
    CORS(app,
         origins=settings.CORS_ORIGINS,
         methods=settings.CORS_METHODS,
         allow_headers=['Content-Type', 'Authorization'])

    # レート制限
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        storage_uri=settings.RATELIMIT_STORAGE_URL,
        default_limits=[settings.RATELIMIT_DEFAULT]
    )

    # セキュリティヘッダー
    try:
        Talisman(app,
                 force_https=settings.FORCE_HTTPS,
                 content_security_policy=settings.CONTENT_SECURITY_POLICY)  # ← 修正
        logger.info("Talisman セキュリティヘッダー設定完了")
    except Exception as e:
        logger.warning("Talisman 設定に失敗、基本的なセキュリティヘッダーを手動設定", error=str(e))

        @app.after_request
        def add_security_headers(response):
            response.headers['X-Content-Type-Options'] = 'nosniff'
            response.headers['X-Frame-Options'] = 'DENY'
            response.headers['X-XSS-Protection'] = '1; mode=block'
            if settings.FORCE_HTTPS:
                response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
            return response

    # ----------------------------
    # ルーティング
    # ----------------------------
    @app.route('/')
    def index():
        is_connected = oci_client.is_connected()
        return jsonify({
            'status': 'running',
            'oci_connected': is_connected,
            'message': 'OK' if is_connected else 'OCI接続が初期化されていません',
            'endpoints': {
                'image_proxy': '/img/<bucket>/<object_name>',
                'upload': '/upload (POST)',
                'health': '/health',
                'test': '/test'
            }
        })

    @app.route('/test')
    def test_page():
        return send_from_directory('.', 'test.html')

    @app.route('/test.html')
    def test_upload_page():
        return send_from_directory('.', 'test.html')

    @app.route('/img/<bucket>/<path:obj>')
    @limiter.limit("50 per minute")
    def serve_image(bucket, obj):
        try:
            if not oci_client.is_connected():
                logger.error("画像取得失敗 - OCI接続エラー")
                return jsonify({'error': 'OCI接続エラー'}), 500

            logger.info("画像取得開始", bucket=bucket, object=obj)
            response = oci_client.get_object(bucket, obj)
            content_type = response.headers.get('Content-Type', 'image/jpeg')

            logger.info("画像取得成功", object=obj, content_type=content_type)
            return Response(
                response.data.content,
                mimetype=content_type,
                headers={
                    'Cache-Control': 'max-age=3600',
                    'Content-Disposition': f'inline; filename="{obj.split("/")[-1]}"'
                }
            )

        except ServiceError as e:
            if e.status == 404:
                logger.warning("画像が見つかりません", bucket=bucket, object=obj)
                return jsonify({'error': '画像が見つかりません'}), 404
            else:
                logger.error("OCI サービスエラー", error=str(e))
                return jsonify({'error': 'OCI サービスエラー'}), 500
        except Exception as e:
            logger.error("画像取得中に予期しないエラー", error=str(e))
            return jsonify({'error': '画像取得に失敗しました'}), 500

    @app.route('/upload', methods=['POST'])
    @limiter.limit(settings.RATELIMIT_UPLOAD)
    def upload_image():
        """
        画像をOCI Object Storageにアップロードし、同時にEmbeddingをDBへ保存
        """
        try:
            if not oci_client.is_connected():
                logger.error("アップロード失敗 - OCI接続エラー")
                return jsonify({'error': 'OCI接続エラー'}), 500

            if 'file' not in request.files:
                return jsonify({'error': 'ファイルが選択されていません'}), 400

            file = request.files['file']
            if file.filename == '':
                return jsonify({'error': 'ファイル名が空です'}), 400

            if not allowed_file(file.filename):
                return jsonify({
                    'error': f'許可されていないファイル形式です。許可形式: {", ".join(settings.ALLOWED_EXTENSIONS)}'
                }), 400

            # === ファイルを一度メモリに読み込む（Embed/PUTの双方で使う）===
            raw = file.read()
            file_size = len(raw)
            if file_size > settings.MAX_CONTENT_LENGTH:
                max_size_mb = settings.MAX_CONTENT_LENGTH // (1024 * 1024)
                return jsonify({'error': f'ファイルサイズが大きすぎます。最大サイズ: {max_size_mb}MB'}), 400

            # === 保存先/メタ ===
            bucket = request.form.get('bucket', settings.OCI_BUCKET)
            folder = request.form.get('folder', '')
            ext = file.filename.rsplit('.', 1)[1].lower()
            unique_filename = f"{uuid.uuid4().hex}.{ext}"
            object_name = f"{folder.strip('/')}/{unique_filename}" if folder else unique_filename

            # Content-Type を確定（未設定なら拡張子から推定、最後の砦は image/png）
            content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or 'image/png'

            logger.info("アップロード開始", bucket=bucket, object=object_name, size=file_size)

            # === (A) 画像 → data URI → Embedding ===
            embedding = None
            try:
                b64img = base64.b64encode(raw).decode('ascii')
                data_uri = f"data:{content_type};base64,{b64img}"  # ← 重要：data URI 形式
                embeddings = _embed_image_with_cohere_v4([data_uri])  # 1件/呼び出し
                if embeddings:
                    embedding = embeddings[0]  # array('f') 1536次元を想定
                    logger.info("画像embedding生成成功", dims=len(embedding))
            except Exception as e:
                logger.error("画像embedding生成失敗", error=str(e))

            # === (B) Object Storage にPUT ===
            oci_client.put_object(
                bucket_name=bucket,
                object_name=object_name,
                data=BytesIO(raw),
                content_type=content_type
            )

            # === (C) DB に保存（embeddingがある場合のみ） ===
            if embedding is not None:
                try:
                    _save_embedding_to_db(bucket, object_name, content_type, file_size, embedding)
                except Exception as e:
                    logger.error("DB保存失敗（embedding）", error=str(e))

            proxy_url = f"/img/{bucket}/{object_name}"

            logger.info("アップロード成功", object=object_name)

            return jsonify({
                'success': True,
                'message': 'アップロードが完了しました',
                'data': {
                    'object_name': object_name,
                    'bucket': bucket,
                    'proxy_url': proxy_url,
                    'file_size': file_size,
                    'content_type': content_type,
                    'uploaded_at': datetime.now().isoformat(),
                    'embedding_saved': embedding is not None
                }
            })

        except ServiceError as e:
            logger.error("OCI サービスエラー", error=str(e))
            return jsonify({'error': 'アップロードに失敗しました（OCI エラー）'}), 500
        except Exception as e:
            logger.error("アップロード中に予期しないエラー", error=str(e))
            return jsonify({'error': 'アップロードに失敗しました'}), 500

    @app.route('/health')
    def health_check():
        is_connected = oci_client.is_connected()
        return jsonify({
            'status': 'healthy' if is_connected else 'unhealthy',
            'oci_connection': 'OK' if is_connected else 'OCI接続が初期化されていません',
            'timestamp': datetime.now().isoformat()
        }), 200 if is_connected else 503

    # エラーハンドラー
    @app.errorhandler(413)
    def too_large(e):
        logger.warning("ファイルサイズ制限エラー")
        return jsonify({'error': 'ファイルサイズが大きすぎます'}), 413

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'error': 'エンドポイントが見つかりません'}), 404

    @app.errorhandler(500)
    def internal_error(e):
        logger.error("内部サーバーエラー", error=str(e))
        return jsonify({'error': '内部サーバーエラーが発生しました'}), 500

    @app.errorhandler(ServiceError)
    def handle_oci_error(e):
        logger.error("OCI サービスエラー",
                    status=e.status,
                    code=e.code,
                    message=e.message)
        return jsonify({
            'error': 'OCI サービスエラーが発生しました',
            'details': e.message if settings.DEBUG else None
        }), 500

    return app

def create_production_app() -> Flask:
    return create_app('production')

def create_development_app() -> Flask:
    return create_app('development')

def create_testing_app() -> Flask:
    return create_app('testing')

# デフォルトアプリケーション（開発用）
app = create_development_app()

if __name__ == '__main__':
    env = os.getenv('FLASK_ENV', 'development')
    port = int(os.getenv('PORT', settings.PORT))
    debug = settings.DEBUG

    logger.info("アプリケーション開始",
                environment=env,
                port=port,
                debug=debug)

    app.run(host=settings.HOST, port=port, debug=debug)
