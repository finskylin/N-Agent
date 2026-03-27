"""
钉钉媒体文件下载工具

修复:
- GET → POST（钉钉下载 API 要求 POST + JSON body）
- 增加 access_token 缓存（asyncio.Lock 防并发刷新）
"""
import asyncio
import time
from typing import Optional

import httpx
from loguru import logger


# ── Token 缓存（复用 dingtalk_uploader 的模式）──────────────────────
_token_cache = {
    "access_token": None,
    "expires_at": 0,
}
_token_lock = asyncio.Lock()


async def _get_access_token() -> Optional[str]:
    """
    获取钉钉 access_token（带缓存，asyncio.Lock 防止 thundering herd）
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
        client_id = getattr(settings, "dingtalk_client_id", "")
        client_secret = getattr(settings, "dingtalk_client_secret", "")

        if not client_id or not client_secret:
            logger.error("[DingTalk-Media] Missing DINGTALK_CLIENT_ID or DINGTALK_CLIENT_SECRET")
            return None

        try:
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
                    logger.debug(f"[DingTalk-Media] Access token obtained, expires in {expires_in}s")
                    return access_token
                else:
                    logger.error(f"[DingTalk-Media] Failed to get access token: {data}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalk-Media] Token request failed: {e}")
            return None


# ── 媒体文件下载 ────────────────────────────────────────────────────
async def download_dingtalk_media(
    download_code: str,
    robot_code: str,
    timeout: float = 30.0,
) -> bytes:
    """
    下载钉钉媒体文件（图片、文件等）

    使用 POST + JSON body 调用钉钉下载 API（GET 方式会返回 404）。
    参考: https://open.dingtalk.com/document/orgapp/download-media-files

    Args:
        download_code: 钉钉下载码
        robot_code: 机器人编码
        timeout: 下载超时时间

    Returns:
        媒体文件的二进制数据
    """
    # 钉钉下载 API
    base_url = "https://api.dingtalk.com/v1.0/robot/messageFiles/download"

    try:
        # 1. 获取 access_token（带缓存）
        access_token = await _get_access_token()
        if not access_token:
            return b""

        # 2. POST 方式下载媒体文件（与 document_reader skill 保持一致）
        async with httpx.AsyncClient(timeout=timeout) as client:
            media_response = await client.post(
                base_url,
                headers={
                    "x-acs-dingtalk-access-token": access_token,
                    "Content-Type": "application/json",
                },
                json={
                    "downloadCode": download_code,
                    "robotCode": robot_code,
                },
            )

            if media_response.status_code == 200:
                # 钉钉 API 返回的是 JSON，包含 downloadUrl
                resp_data = media_response.json()
                download_url = resp_data.get("downloadUrl")
                if not download_url:
                    logger.error(f"[DingTalk-Media] No downloadUrl in response: {resp_data}")
                    return b""

                # 3. 从临时 URL 下载实际文件
                file_response = await client.get(download_url)
                if file_response.status_code == 200:
                    logger.info(f"[DingTalk-Media] Downloaded {len(file_response.content)} bytes")
                    return file_response.content
                else:
                    logger.error(
                        f"[DingTalk-Media] File download failed: {file_response.status_code}"
                    )
                    return b""
            else:
                logger.error(
                    f"[DingTalk-Media] API failed: {media_response.status_code} - {media_response.text[:200]}"
                )
                return b""

    except Exception as e:
        logger.error(f"[DingTalk-Media] Download error: {e}")
        return b""
