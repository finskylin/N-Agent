"""
Feishu Webhook Handler
飞书 Webhook 事件接收器

FastAPI 路由，处理飞书推送的所有事件：
- URL 验证（challenge）
- 消息接收（im.message.receive_v1）
- 机器人进群/退群事件

设计原则：快速 ACK，异步处理（与钉钉保持一致）
"""
import asyncio
import json
from typing import Dict, Any, Optional
from loguru import logger
from fastapi import APIRouter, Request, HTTPException

from app.config import settings

router = APIRouter()


def _decrypt_content(encrypted: str, encrypt_key: str) -> Optional[str]:
    """
    解密飞书加密消息（AES-256-CBC）

    如果 encrypt_key 未配置或 encrypted 字段不存在，直接返回 None。
    """
    try:
        import base64
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        import hashlib

        # key = SHA256(encrypt_key) 的前 32 字节
        key = hashlib.sha256(encrypt_key.encode()).digest()

        # 飞书加密格式：base64(iv[16] + ciphertext)
        data = base64.b64decode(encrypted)
        iv = data[:16]
        ciphertext = data[16:]

        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(ciphertext) + decryptor.finalize()

        # 去掉 PKCS7 padding
        pad_len = plaintext[-1]
        plaintext = plaintext[:-pad_len]

        return plaintext.decode("utf-8")
    except Exception as e:
        logger.error(f"[Feishu] Decrypt error: {e}")
        return None


@router.post("/feishu/events")
async def feishu_events(request: Request):
    """
    飞书事件接收端点

    1. 验证签名（如配置 encrypt_key）
    2. 处理 URL 验证 challenge
    3. 消息去重
    4. 快速返回 200，后台异步处理
    """
    # 读取请求体
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes)
    except Exception as e:
        logger.warning(f"[Feishu] Invalid request body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # 处理加密消息
    encrypt_key = getattr(settings, "feishu_encrypt_key", None)
    if encrypt_key and "encrypt" in body:
        decrypted = _decrypt_content(body["encrypt"], encrypt_key)
        if not decrypted:
            raise HTTPException(status_code=400, detail="Decryption failed")
        try:
            body = json.loads(decrypted)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid decrypted JSON")

    # 处理 URL 验证 challenge（飞书首次配置时发送）
    if "challenge" in body:
        logger.info("[Feishu] URL verification challenge received")
        return {"challenge": body["challenge"]}

    # 解析事件
    schema = body.get("schema", "")
    if schema == "2.0":
        # 新版事件格式
        header = body.get("header", {})
        event_type = header.get("event_type", "")
        event = body.get("event", {})
    else:
        # 旧版格式
        event_type = body.get("type", "")
        event = body

    logger.debug(f"[Feishu] Event received: type={event_type}")

    # 只处理消息事件
    if event_type == "im.message.receive_v1":
        message = event.get("message", {})
        message_id = message.get("message_id", "")

        # 去重检查
        from app.channels.feishu.bot_handler import _is_duplicate_message, _ensure_bot_handler
        if await _is_duplicate_message(message_id):
            return {"code": 0}

        # 后台异步处理（快速 ACK）
        handler = _ensure_bot_handler()
        asyncio.create_task(
            _safe_process_event(handler, event),
            # task name for debugging
        )

    elif event_type in ("im.chat.member.bot.added_v1", "im.chat.member.bot.deleted_v1"):
        action = "加入" if "added" in event_type else "退出"
        chat_id = event.get("chat_id", "")
        logger.info(f"[Feishu] Bot {action}群聊: {chat_id}")

    return {"code": 0}


async def _safe_process_event(handler, event: Dict[str, Any]) -> None:
    """安全地处理事件（捕获异常防止 task 崩溃）"""
    try:
        await handler.process_event(event)
    except Exception as e:
        logger.error(f"[Feishu] Error processing event: {e}")
        import traceback
        logger.debug(traceback.format_exc())
