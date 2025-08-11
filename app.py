#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Oracle Cloud Storage 画像プロキシアプリケーション
OCIオブジェクトストレージの画像を認証付きで表示・アップロードするFlaskアプリ

機能:
- /upload: 画像をOCI Object Storageへ保存し、同時に OCI Generative AI (Cohere Embed v4) で
  画像をベクトル化 → Oracle Database(23c VECTOR想定)に保存
- /img/<bucket>/<obj>: オブジェクトストレージの画像をプロキシ配信
- /test: CSP nonce方式で動く自己完結UI（外部CDN不使用）
- /health, /: ヘルスチェック

環境変数(必須):
- DB_USER, DB_PASSWORD, DB_DSN
- OCI_COMPARTMENT_OCID
- OCI_COHERE_EMBED_MODEL (例: cohere.embed-v4.0)

依存:
- flask, flask-cors, flask-limiter, flask-talisman, structlog
- oci, oracledb, python-dotenv, sentry-sdk
"""

import os
import uuid
import base64
import array
import structlog
import mimetypes
import secrets
from io import BytesIO
from datetime import datetime
from typing import Optional
from pathlib import Path

from flask import Flask, Response, request, jsonify, send_from_directory, g, make_response
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

from dotenv import load_dotenv
load_dotenv()

# 設定モジュール（あなたの既存のconfig.pyを使用）
from config import settings, get_config

# 構造化ログ
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
    def __init__(self):
        self.client: Optional[ObjectStorageClient] = None
        self.namespace: Optional[str] = None
        self._initialize()

    def _initialize(self):
        try:
            cfg_file = os.path.expanduser(settings.OCI_CONFIG_FILE)
            if not os.path.exists(cfg_file):
                logger.warning("OCI設定ファイルが見つかりません", config_file=cfg_file)
                return

            config = oci.config.from_file(
                file_location=cfg_file,
                profile_name=settings.OCI_PROFILE
            )
            if settings.OCI_REGION:
                config['region'] = settings.OCI_REGION

            self.client = ObjectStorageClient(config)
            self.namespace = self.client.get_namespace().data
            logger.info("OCI接続成功", namespace=self.namespace, region=config.get('region'))

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
    Cohere Embed v4 で画像のembeddingを生成（data URIを渡す）
    戻り: array('f') (float32) のリスト
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
    python-oracledb Thinで VECTOR にINSERT
    期待テーブル: img_embeddings(bucket, object_name, content_type, file_size, uploaded_at, embedding)
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
# Flask アプリ
# =========================================
def create_app(config_name: str = None) -> Flask:
    app = Flask(__name__)

    # 設定
    if config_name:
        app.config.from_object(get_config(config_name))
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
        logger.info("Sentry無効")

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

    # Talisman（cspは使わない）
    try:
        Talisman(app, force_https=settings.FORCE_HTTPS)
        logger.info("Talisman初期化（cspは自前設定）")
    except Exception as e:
        logger.warning("Talisman初期化失敗", error=str(e))

    # --- Security Headers (always) ---
    @app.after_request
    def add_security_headers(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['X-XSS-Protection'] = '1; mode=block'
        if settings.FORCE_HTTPS:
            response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
        return response

    # --- Nonce発行（/test のときだけ） ---
    @app.before_request
    def set_nonce_for_test():
        if request.path in ("/test",):
            g.csp_nonce = secrets.token_urlsafe(16)

    # --- CSP (テストページだけnonceを使う) ---
    @app.after_request
    def set_csp(response):
        # /test: nonce方式（外部CDN無し・自己完結）
        if request.path == "/test":
            nonce = getattr(g, "csp_nonce", "")
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                f"style-src 'self' 'nonce-{nonce}'; "
                "img-src 'self' data: blob:; "
                "font-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'self'"
            )
        return response

    # -----------------------------------------
    # ルーティング
    # -----------------------------------------
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
        """自己完結UI（CDN不使用 / nonce付きインラインJS・CSS）"""
        nonce = getattr(g, "csp_nonce", "")
        html = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>画像アップロードテスト</title>
<style nonce="{nonce}">
body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans JP", "Apple Color Emoji", "Segoe UI Emoji"; margin: 24px; }}
h1 {{ font-size: 28px; display: flex; align-items: center; gap: 8px; }}
.container {{ max-width: 900px; }}
.row {{ margin: 12px 0; }}
input[type="text"] {{ width: 320px; padding: 6px; }}
button {{ padding: 10px 18px; border-radius: 6px; border: 1px solid #ddd; background:#2d6cdf; color:#fff; cursor:pointer; }}
button:disabled {{ background:#9eb7e5; cursor:not-allowed; }}
#dropzone {{ border:2px dashed #ccc; padding:20px; border-radius:8px; color:#333; }}
#result {{ margin-top:16px; padding:12px; border-radius:8px; background:#e7f7ea; display:none; }}
#error {{ margin-top:16px; padding:12px; border-radius:8px; background:#fdecea; color:#b00020; display:none; }}
.preview {{ margin-top:10px; max-width:300px; border:1px solid #ddd; border-radius:6px; }}
label {{ display:block; font-weight:600; margin-bottom:6px; }}
.small {{ color:#555; font-size: 12px; }}
</style>
</head>
<body>
<div class="container">
  <h1>🖼️ 画像アップロードテスト</h1>

  <div class="row">
    <label>バケット名:</label>
    <input id="bucket" type="text" value="{settings.OCI_BUCKET}">
  </div>
  <div class="row">
    <label>フォルダ (オプション):</label>
    <input id="folder" type="text" placeholder="例: avatars, uploads">
  </div>

  <div id="dropzone" class="row">
    📁 ここに画像ファイルをドラッグ＆ドロップするか、下のボタンでファイルを選択してください
    <div style="margin-top:8px">
      <input id="file" type="file" accept="image/*">
    </div>
    <div id="picked" class="small" style="margin-top:8px"></div>
    <img id="preview" class="preview" style="display:none">
  </div>

  <div class="row">
    <button id="uploadBtn" disabled>アップロード開始</button>
  </div>

  <div id="result"></div>
  <div id="error"></div>
</div>

<script nonce="{nonce}">
(() => {{
  const fileInput = document.getElementById('file');
  const dropzone = document.getElementById('dropzone');
  const uploadBtn = document.getElementById('uploadBtn');
  const picked = document.getElementById('picked');
  const preview = document.getElementById('preview');
  const bucket = document.getElementById('bucket');
  const folder = document.getElementById('folder');
  const result = document.getElementById('result');
  const errorBox = document.getElementById('error');

  let currentFile = null;

  function resetMessages() {{
    result.style.display = 'none';
    result.innerText = '';
    errorBox.style.display = 'none';
    errorBox.innerText = '';
  }}

  function onPicked(file) {{
    currentFile = file;
    picked.innerText = file ? `選択されたファイル: ${{file.name}} ( ${{(file.size/1024).toFixed(1)}} KB )` : '';
    uploadBtn.disabled = !file;
    if (file) {{
      const url = URL.createObjectURL(file);
      preview.src = url;
      preview.style.display = 'block';
    }} else {{
      preview.style.display = 'none';
    }}
  }}

  fileInput.addEventListener('change', (e) => {{
    resetMessages();
    onPicked(e.target.files[0]);
  }});

  dropzone.addEventListener('dragover', (e) => {{
    e.preventDefault();
  }});
  dropzone.addEventListener('drop', (e) => {{
    e.preventDefault();
    resetMessages();
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {{
      onPicked(e.dataTransfer.files[0]);
    }}
  }});

  uploadBtn.addEventListener('click', async () => {{
    resetMessages();
    if (!currentFile) return;

    const form = new FormData();
    form.append('file', currentFile);
    if (bucket.value) form.append('bucket', bucket.value);
    if (folder.value) form.append('folder', folder.value);

    try {{
      const res = await fetch('/upload', {{ method: 'POST', body: form }});
      const data = await res.json();

      if (!res.ok || data.error) {{
        throw new Error(data.error || 'アップロードに失敗しました');
      }}

      result.style.display = 'block';
      result.innerHTML = `
        ✅ アップロード成功!<br>
        オブジェクト名: <b>${{data.data.object_name}}</b><br>
        バケット: <b>${{data.data.bucket}}</b><br>
        プロキシURL: <a href="${{data.data.proxy_url}}" target="_blank">${{data.data.proxy_url}}</a><br>
        ファイルサイズ: ${{data.data.file_size}} bytes<br>
        embedding_saved: <b>${{data.data.embedding_saved}}</b>
        <div><img src="${{data.data.proxy_url}}" class="preview" style="margin-top:10px"></div>
      `;
    }} catch (err) {{
      errorBox.style.display = 'block';
      errorBox.innerText = 'エラー: ' + err.message;
    }}
  }});
}})();
</script>
</body>
</html>"""
        resp = make_response(html, 200)
        resp.headers["Content-Type"] = "text/html; charset=utf-8"
        return resp

    @app.route('/test.html')
    def legacy_test():
        """既存のtest.htmlをそのまま配信（CSPはnonce対応なし・開発用）"""
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

            # 読み込み
            raw = file.read()
            file_size = len(raw)
            if file_size > settings.MAX_CONTENT_LENGTH:
                max_size_mb = settings.MAX_CONTENT_LENGTH // (1024 * 1024)
                return jsonify({'error': f'ファイルサイズが大きすぎます。最大サイズ: {max_size_mb}MB'}), 400

            # 保存先/メタ
            bucket = request.form.get('bucket', settings.OCI_BUCKET)
            folder = request.form.get('folder', '')
            ext = file.filename.rsplit('.', 1)[1].lower()
            unique_filename = f"{uuid.uuid4().hex}.{ext}"
            object_name = f"{folder.strip('/')}/{unique_filename}" if folder else unique_filename
            content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or 'image/png'

            logger.info("アップロード開始", bucket=bucket, object=object_name, size=file_size)

            # Embedding
            embedding = None
            try:
                b64img = base64.b64encode(raw).decode('ascii')
                data_uri = f"data:{content_type};base64,{b64img}"
                embeddings = _embed_image_with_cohere_v4([data_uri])
                if embeddings:
                    embedding = embeddings[0]
                    logger.info("画像embedding生成成功", dims=len(embedding))
            except Exception as e:
                logger.error("画像embedding生成失敗", error=str(e))

            # Object Storage PUT
            oci_client.put_object(
                bucket_name=bucket,
                object_name=object_name,
                data=BytesIO(raw),
                content_type=content_type
            )

            # DB保存（embeddingがある時のみ）
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

    # エラーハンドラ
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


# デフォルト（開発）
app = create_development_app()

if __name__ == '__main__':
    env = os.getenv('FLASK_ENV', 'development')
    port = int(os.getenv('PORT', settings.PORT))
    debug = settings.DEBUG
    logger.info("アプリケーション開始", environment=env, port=port, debug=debug)
    app.run(host=settings.HOST, port=port, debug=debug)
