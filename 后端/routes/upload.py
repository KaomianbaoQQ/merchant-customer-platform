"""图片上传与访问（顾客上传退换货凭证 / 客服聊天图片）。

上传：POST /api/upload  ——  multipart/form-data，字段名 files（可多张）
       返回 {"ok": true, "urls": ["/images/xxx.jpg", ...]}
访问：GET  /images/<filename> —— 商品图片、退换货凭证、聊天图片统一从这里取
       （同时修复此前 IMG() 指向 /images/ 却无路由、商品图片打不开的问题）
"""
import os

from flask import Blueprint, jsonify, request, send_file

from 公共 import store

bp = Blueprint("upload", __name__)

_ALLOWED_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_MAX_BYTES = 8 * 1024 * 1024  # 单张上限 8MB，避免把服务器撑爆


@bp.post("/api/upload")
def api_upload():
    files = request.files.getlist("files")
    if not files:
        return jsonify({"ok": False, "error": "未选择文件"}), 400

    urls = []
    try:
        for f in files:
            if not f or not f.filename:
                continue
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in _ALLOWED_EXT:
                continue
            data = f.read()
            if not data or len(data) > _MAX_BYTES:
                continue
            name = store.save_image(f.filename, data)
            urls.append("/images/" + name)
    except Exception as e:  # 上传失败不能拖垮主流程
        return jsonify({"ok": False, "error": "上传失败：" + str(e)}), 500

    if not urls:
        return jsonify({"ok": False, "error": "没有可上传的有效图片（支持 jpg/png/webp/gif，单张 ≤ 8MB）"}), 400
    return jsonify({"ok": True, "urls": urls})


@bp.route("/images/<path:filename>")
def serve_image(filename):
    """按文件名返回图片；优先新上传目录，再兜底历史目录。"""
    path = store.resolve_image(filename)
    if not path:
        return jsonify({"ok": False, "error": "图片不存在"}), 404
    return send_file(path)
