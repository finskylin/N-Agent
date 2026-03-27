"""
DingTalk Uploader Service
通用钉钉文件/图片上传服务

解决内网文件（MinIO）无法在钉钉中外网访问的问题。
使用钉钉开放平台 API 上传文件获取公网可访问 URL。

使用方式:
    from app.services.dingtalk_uploader import DingTalkUploader

    # 上传图片
    url = await DingTalkUploader.upload_image(png_bytes, "screenshot.png")

    # 上传文件
    url = await DingTalkUploader.upload_file(file_bytes, "report.pdf")

    # 便捷方法：从本地路径上传
    url = await DingTalkUploader.get_public_url("/path/to/file.pdf")
"""

import asyncio
import os
import time
from typing import Optional
from loguru import logger


# Token 缓存（复用 dingtalk_private_message 的模式）
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}
_token_lock = asyncio.Lock()


async def _get_access_token() -> Optional[str]:
    """
    获取钉钉 access_token（带缓存，asyncio.Lock 防止 thundering herd）

    使用应用凭证 (client_id, client_secret) 通过 OAuth2 客户端凭证模式获取
    """
    global _token_cache

    # 快速路径：缓存有效直接返回（无需加锁）
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 300:
        return _token_cache["access_token"]

    async with _token_lock:
        # 双重检查：其他协程可能已经刷新了
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"] - 300:
            return _token_cache["access_token"]

        from app.config import settings
        client_id = settings.dingtalk_client_id
        client_secret = settings.dingtalk_client_secret

        if not client_id or not client_secret:
            logger.warning("[DingTalkUploader] Missing DINGTALK_CLIENT_ID or DINGTALK_CLIENT_SECRET")
            return None

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                    json={
                        "appKey": client_id,
                        "appSecret": client_secret,
                    },
                )
                response.raise_for_status()
                data = response.json()

                access_token = data.get("accessToken")
                expires_in = data.get("expireIn", 7200)

                if access_token:
                    _token_cache["access_token"] = access_token
                    _token_cache["expires_at"] = time.time() + expires_in
                    logger.debug(f"[DingTalkUploader] Access token obtained, expires in {expires_in}s")
                    return access_token
                else:
                    logger.error(f"[DingTalkUploader] Failed to get access token: {data}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalkUploader] Token request failed: {e}")
            return None


class DingTalkUploader:
    """
    通用钉钉文件上传服务

    使用钉钉开放平台的媒体文件上传 API，将文件上传到钉钉图床，
    获取公网可访问的 URL。适用于：
    - 组件截图上传
    - 报告文件上传
    - 任何需要公网访问的内网文件
    """

    # 钉钉旧版上传 API（需要 access_token query 参数）
    UPLOAD_MEDIA_URL = "https://oapi.dingtalk.com/media/upload"

    @staticmethod
    async def upload_image(
        image_bytes: bytes,
        filename: str = "image.png",
    ) -> Optional[str]:
        """
        上传图片到钉钉图床，返回公网可访问 URL

        Args:
            image_bytes: 图片二进制数据
            filename: 文件名

        Returns:
            钉钉图床 URL，失败返回 None
        """
        return await DingTalkUploader._upload_media(
            file_bytes=image_bytes,
            filename=filename,
            media_type="image",
        )

    @staticmethod
    async def upload_file(
        file_bytes: bytes,
        filename: str,
        filetype: str = "file",
    ) -> Optional[str]:
        """
        上传文件到钉钉，返回公网可访问 URL

        Args:
            file_bytes: 文件二进制数据
            filename: 文件名
            filetype: 文件类型 (image/voice/video/file)

        Returns:
            钉钉文件 URL，失败返回 None
        """
        return await DingTalkUploader._upload_media(
            file_bytes=file_bytes,
            filename=filename,
            media_type=filetype,
        )

    @staticmethod
    async def get_public_url(local_file_path: str) -> Optional[str]:
        """
        将本地文件上传到钉钉，返回公网 URL（便捷方法）

        Args:
            local_file_path: 本地文件路径

        Returns:
            公网 URL，失败返回 None
        """
        if not os.path.exists(local_file_path):
            logger.error(f"[DingTalkUploader] File not found: {local_file_path}")
            return None

        filename = os.path.basename(local_file_path)
        ext = os.path.splitext(filename)[1].lower()

        # 根据扩展名判断媒体类型
        image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
        media_type = "image" if ext in image_exts else "file"

        with open(local_file_path, "rb") as f:
            file_bytes = f.read()

        return await DingTalkUploader._upload_media(
            file_bytes=file_bytes,
            filename=filename,
            media_type=media_type,
        )

    @staticmethod
    async def _upload_media(
        file_bytes: bytes,
        filename: str,
        media_type: str = "image",
    ) -> Optional[str]:
        """
        核心上传方法：使用钉钉旧版 API 上传媒体文件

        钉钉旧版 API: POST https://oapi.dingtalk.com/media/upload?access_token=xxx&type=image
        返回 media_id，组装为可访问 URL。

        对于图片类型，钉钉会返回可直接访问的 URL。
        对于文件类型，返回 media_id 用于消息发送。
        """
        access_token = await _get_access_token()
        if not access_token:
            logger.error("[DingTalkUploader] No access token, cannot upload")
            return None

        try:
            import httpx

            # 确定 Content-Type
            ext = os.path.splitext(filename)[1].lower()
            content_type_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".bmp": "image/bmp",
                ".webp": "image/webp",
                ".pdf": "application/pdf",
                ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            }
            content_type = content_type_map.get(ext, "application/octet-stream")

            async with httpx.AsyncClient(timeout=30) as client:
                # 使用 multipart/form-data 上传
                response = await client.post(
                    DingTalkUploader.UPLOAD_MEDIA_URL,
                    params={
                        "access_token": access_token,
                        "type": media_type,
                    },
                    files={
                        "media": (filename, file_bytes, content_type),
                    },
                )
                response.raise_for_status()
                data = response.json()

                errcode = data.get("errcode", 0)
                if errcode != 0:
                    logger.error(
                        f"[DingTalkUploader] Upload failed: "
                        f"errcode={errcode}, errmsg={data.get('errmsg')}"
                    )
                    return None

                logger.info(f"[DingTalkUploader] API response: {data}")

                media_id = data.get("media_id", "")
                media_url = data.get("url", "")

                if media_url:
                    # 钉钉直接返回了公网可访问 URL
                    logger.info(
                        f"[DingTalkUploader] Upload success (url): {filename} -> {media_url[:60]}..."
                    )
                    return media_url
                elif media_id and media_type == "image":
                    # 图片的 media_id 可以直接在钉钉 Markdown 中作为图片 URL 使用
                    # 例如: ![报告截图](@lALPxxxxx)
                    logger.info(
                        f"[DingTalkUploader] Upload success (image media_id): {filename} -> {media_id[:30]}..."
                    )
                    return media_id
                elif media_id:
                    # 文件类型返回 media_id，用于钉钉消息发送（sampleFile msgKey）
                    logger.info(
                        f"[DingTalkUploader] Upload success (file media_id): {filename} -> {media_id[:30]}..."
                    )
                    return media_id
                else:
                    logger.warning(f"[DingTalkUploader] Upload returned no URL or media_id: {data}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalkUploader] Upload failed: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    @staticmethod
    async def upload_from_url(url: str, filename: str = None) -> Optional[str]:
        """
        从 URL 下载文件后上传到钉钉，返回公网 URL。

        将对外暴露的 localhost/外部IP 地址替换为容器内部可访问地址后下载。

        Args:
            url: 文件 HTTP URL
            filename: 文件名（可选，默认从 URL 推断）

        Returns:
            钉钉公网 URL，失败返回 None
        """
        if not url:
            return None

        import re as _re

        # 将 localhost/外部IP:PORT 替换为容器内可访问的内部地址
        _ext_host = os.getenv("AGENT_EXTERNAL_HOST", "localhost")
        _agent_port = os.getenv("AGENT_SERVICE_PORT", "8000")
        _agent_internal = os.getenv("AGENT_INTERNAL_BASE_URL", f"http://host.docker.internal:{_agent_port}")

        download_url = _re.sub(
            rf'http://(?:localhost|127\.0\.0\.1|{_re.escape(_ext_host)}):{_agent_port}',
            _agent_internal,
            url,
        )

        logger.info(f"[DingTalkUploader] Downloading from: {download_url}")

        try:
            import httpx
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(download_url)
                resp.raise_for_status()
                file_bytes = resp.content

            # 推断文件名和媒体类型
            if not filename:
                filename = url.rsplit("/", 1)[-1] or "file"
            ext = os.path.splitext(filename)[1].lower()
            media_type = "image" if ext in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"} else "file"

            logger.info(f"[DingTalkUploader] Downloaded {len(file_bytes)} bytes, uploading as {media_type}: {filename}")
            return await DingTalkUploader._upload_media(file_bytes, filename, media_type)

        except Exception as e:
            logger.error(f"[DingTalkUploader] upload_from_url failed for {url}: {e}")
            import traceback
            logger.debug(traceback.format_exc())
            return None

    @staticmethod
    async def upload_image_and_get_markdown(
        image_bytes: bytes,
        filename: str = "image.png",
        title: str = "图片",
    ) -> Optional[str]:
        """
        上传图片并返回钉钉 Markdown 格式的图片引用

        如果上传成功，返回 ![title](url) 格式
        如果失败，返回 None
        """
        url = await DingTalkUploader.upload_image(image_bytes, filename)
        if url and not url.startswith("dingtalk://"):
            return f"![{title}]({url})"
        return None
