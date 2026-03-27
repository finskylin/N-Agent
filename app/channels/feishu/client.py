"""
Feishu HTTP Client
飞书 API HTTP 客户端

负责：
- 获取和缓存 tenant_access_token
- 发送消息（post 格式 + 交互卡片）
- 下载媒体资源
"""
import asyncio
import time
import json
from typing import Optional, Dict, Any
from loguru import logger
import httpx

from app.config import settings

# 飞书 API 基础 URL
FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
LARK_API_BASE = "https://open.larksuite.com/open-apis"

# token 缓存（app_id -> {token, expire_at}）
_token_cache: Dict[str, Dict] = {}
_token_lock = asyncio.Lock()


def _get_api_base() -> str:
    """根据配置返回 API 基础 URL"""
    domain = getattr(settings, "feishu_domain", "feishu")
    return LARK_API_BASE if domain == "lark" else FEISHU_API_BASE


async def get_tenant_access_token() -> Optional[str]:
    """
    获取 tenant_access_token（带缓存，提前 5 分钟刷新）

    Returns:
        access_token 字符串，获取失败返回 None
    """
    app_id = getattr(settings, "feishu_app_id", None)
    app_secret = getattr(settings, "feishu_app_secret", None)

    if not app_id or not app_secret:
        logger.warning("[Feishu] feishu_app_id or feishu_app_secret not configured")
        return None

    now = time.time()
    async with _token_lock:
        cached = _token_cache.get(app_id)
        if cached and cached["expire_at"] > now + 300:
            return cached["token"]

        # 刷新 token
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{_get_api_base()}/auth/v3/tenant_access_token/internal",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
                data = resp.json()
                if data.get("code") == 0:
                    token = data["tenant_access_token"]
                    expire = data.get("expire", 7200)
                    _token_cache[app_id] = {
                        "token": token,
                        "expire_at": now + expire,
                    }
                    logger.debug(f"[Feishu] tenant_access_token refreshed (expire={expire}s)")
                    return token
                else:
                    logger.error(f"[Feishu] Failed to get token: {data}")
                    return None
        except Exception as e:
            logger.error(f"[Feishu] Token request error: {e}")
            return None


async def send_message(
    receive_id: str,
    receive_id_type: str,
    content: Dict[str, Any],
    msg_type: str = "post",
) -> Optional[str]:
    """
    发送新消息

    Args:
        receive_id: 接收者 ID（open_id / chat_id 等）
        receive_id_type: ID 类型（open_id / chat_id / user_id）
        content: 消息内容（dict，会被 json.dumps）
        msg_type: 消息类型（post / interactive）

    Returns:
        message_id 或 None
    """
    token = await get_tenant_access_token()
    if not token:
        return None

    url = f"{_get_api_base()}/im/v1/messages?receive_id_type={receive_id_type}"
    body = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            data = resp.json()
            if data.get("code") == 0:
                message_id = data["data"]["message_id"]
                logger.debug(f"[Feishu] Message sent: {message_id}")
                return message_id
            else:
                logger.warning(f"[Feishu] send_message failed: {data}")
                return None
    except Exception as e:
        logger.error(f"[Feishu] send_message error: {e}")
        return None


async def reply_message(
    message_id: str,
    content: Dict[str, Any],
    msg_type: str = "post",
    reply_in_thread: bool = False,
) -> Optional[str]:
    """
    回复消息

    Args:
        message_id: 被回复的消息 ID
        content: 消息内容
        msg_type: 消息类型
        reply_in_thread: 是否以消息串方式回复

    Returns:
        新消息的 message_id 或 None
    """
    token = await get_tenant_access_token()
    if not token:
        return None

    url = f"{_get_api_base()}/im/v1/messages/{message_id}/reply"
    body = {
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
        "reply_in_thread": reply_in_thread,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            data = resp.json()
            if data.get("code") == 0:
                return data["data"]["message_id"]
            else:
                logger.warning(f"[Feishu] reply_message failed: {data}")
                return None
    except Exception as e:
        logger.error(f"[Feishu] reply_message error: {e}")
        return None


async def edit_message(
    message_id: str,
    content: Dict[str, Any],
    msg_type: str = "post",
) -> bool:
    """
    编辑/更新已发送的消息（用于流式更新）

    Args:
        message_id: 要编辑的消息 ID
        content: 新的消息内容
        msg_type: 消息类型

    Returns:
        是否成功
    """
    token = await get_tenant_access_token()
    if not token:
        return False

    url = f"{_get_api_base()}/im/v1/messages/{message_id}"
    body = {
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=body,
            )
            data = resp.json()
            if data.get("code") == 0:
                return True
            else:
                logger.warning(f"[Feishu] edit_message failed: {data}")
                return False
    except Exception as e:
        logger.error(f"[Feishu] edit_message error: {e}")
        return False


async def get_message(message_id: str) -> Optional[Dict[str, Any]]:
    """
    获取消息详情（用于读取引用消息的实际内容）

    Args:
        message_id: 消息 ID

    Returns:
        消息详情 dict（含 msg_type, content 等字段），失败返回 None
    """
    token = await get_tenant_access_token()
    if not token:
        return None

    url = f"{_get_api_base()}/im/v1/messages/{message_id}"

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if data.get("code") == 0:
                items = data.get("data", {}).get("items", [])
                if items:
                    return items[0]
                return None
            else:
                logger.warning(f"[Feishu] get_message failed: {data}")
                return None
    except Exception as e:
        logger.error(f"[Feishu] get_message error: {e}")
        return None


async def download_resource(message_id: str, file_key: str, resource_type: str = "file") -> Optional[bytes]:
    """
    下载消息附件（图片/文件）

    Args:
        message_id: 消息 ID
        file_key: 资源 key
        resource_type: image 或 file

    Returns:
        文件字节内容或 None
    """
    token = await get_tenant_access_token()
    if not token:
        return None

    url = f"{_get_api_base()}/im/v1/messages/{message_id}/resources/{file_key}"
    params = {"type": resource_type}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            if resp.status_code == 200:
                return resp.content
            else:
                logger.warning(f"[Feishu] download_resource failed: status={resp.status_code}")
                return None
    except Exception as e:
        logger.error(f"[Feishu] download_resource error: {e}")
        return None


async def upload_file(file_path: str, file_type: str = "stream") -> Optional[str]:
    """
    上传文件到飞书，返回 file_key

    Args:
        file_path: 本地文件路径
        file_type: 飞书文件类型，普通文件用 "stream"，其他: opus/mp4/pdf/doc/xls/ppt/stream

    Returns:
        file_key 字符串，失败返回 None
    """
    import os
    token = await get_tenant_access_token()
    if not token:
        return None

    url = f"{_get_api_base()}/im/v1/files"
    file_name = os.path.basename(file_path)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(file_path, "rb") as f:
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": file_type, "file_name": file_name},
                    files={"file": (file_name, f)},
                )
            data = resp.json()
            if data.get("code") == 0:
                file_key = data.get("data", {}).get("file_key", "")
                logger.info(f"[Feishu] upload_file ok: {file_name} -> {file_key}")
                return file_key
            else:
                logger.warning(f"[Feishu] upload_file failed: {data}")
                return None
    except Exception as e:
        logger.error(f"[Feishu] upload_file error: {e}")
        return None


async def send_file(receive_id: str, receive_id_type: str, file_key: str) -> Optional[str]:
    """
    发送文件消息

    Args:
        receive_id: 接收方 ID（open_id 或 chat_id）
        receive_id_type: open_id / chat_id / user_id
        file_key: 已上传文件的 file_key

    Returns:
        message_id 或 None
    """
    content = json.dumps({"file_key": file_key})
    return await send_message(receive_id, receive_id_type, content, msg_type="file")


def build_post_content(markdown_text: str) -> Dict:
    """
    将 Markdown 文本转换为飞书 post 格式内容（用于 ACK / 短消息）

    飞书 post 消息使用 zh_cn.content 包含 [[{tag: "md", text: ...}]] 结构
    """
    return {
        "zh_cn": {
            "content": [[
                {
                    "tag": "md",
                    "text": markdown_text,
                }
            ]]
        }
    }


# 交互卡片单块内容字符上限（30KB 留余量）
_CARD_CHUNK_LIMIT = 25_000


def build_interactive_card(markdown_text: str, title: str = "") -> Dict:
    """
    构建飞书交互卡片（card kit / schema 2.0）

    使用 markdown element 渲染，支持完整 Markdown（表格、代码块、分隔线等）。
    超出 _CARD_CHUNK_LIMIT 时自动截断。

    Args:
        markdown_text: Markdown 格式内容
        title: 卡片标题（可选，显示在内容上方）
    """
    if len(markdown_text) > _CARD_CHUNK_LIMIT:
        markdown_text = markdown_text[:_CARD_CHUNK_LIMIT] + "\n\n> *(内容过长，已截断)*"

    elements = []
    if title:
        elements.append({"tag": "markdown", "content": f"**{title}**"})
        elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": markdown_text})

    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "body": {"elements": elements},
    }


# 飞书卡片每块最多允许的表格数（官方限制约 5，保守取 3）
_CARD_MAX_TABLES = 3


def _count_tables(text: str) -> int:
    """粗略统计 Markdown 表格数量（以连续含 | 的行块计）"""
    count = 0
    in_table = False
    for line in text.splitlines():
        stripped = line.strip()
        if "|" in stripped:
            if not in_table:
                count += 1
                in_table = True
        else:
            in_table = False
    return count


def split_markdown_for_cards(markdown_text: str) -> list:
    """
    将长文按段落切分为多个卡片块。
    每块满足：字符数 ≤ _CARD_CHUNK_LIMIT 且表格数 ≤ _CARD_MAX_TABLES。
    """
    if len(markdown_text) <= _CARD_CHUNK_LIMIT and _count_tables(markdown_text) <= _CARD_MAX_TABLES:
        return [markdown_text]

    chunks = []
    current: list = []
    current_len = 0
    current_tables = 0

    for para in markdown_text.split("\n\n"):
        para_len = len(para) + 2
        para_tables = _count_tables(para)
        over_len = current_len + para_len > _CARD_CHUNK_LIMIT
        over_tables = current_tables + para_tables > _CARD_MAX_TABLES
        if (over_len or over_tables) and current:
            chunks.append("\n\n".join(current))
            current = [para]
            current_len = para_len
            current_tables = para_tables
        else:
            current.append(para)
            current_len += para_len
            current_tables += para_tables

    if current:
        chunks.append("\n\n".join(current))

    return chunks


async def send_interactive(
    receive_id: str,
    receive_id_type: str,
    markdown_text: str,
    title: str = "",
) -> Optional[str]:
    """发送交互卡片消息，长文自动分段。卡片失败自动降级为 post。返回最后一条 message_id。"""
    chunks = split_markdown_for_cards(markdown_text)
    last_id = None
    for i, chunk in enumerate(chunks):
        chunk_title = (f"{title}（续 {i}）" if i > 0 else title) if title else (f"（续 {i}）" if i > 0 else "")
        card = build_interactive_card(chunk, title=chunk_title)
        mid = await send_message(receive_id, receive_id_type, card, msg_type="interactive")
        if not mid:
            # 降级：用 post 格式
            logger.warning("[Feishu] send_interactive fallback to post")
            mid = await send_message(receive_id, receive_id_type, build_post_content(chunk), msg_type="post")
        if mid:
            last_id = mid
    return last_id


async def reply_interactive(
    message_id: str,
    markdown_text: str,
    title: str = "",
) -> Optional[str]:
    """以交互卡片回复消息，长文自动分段。卡片失败自动降级为 post。返回最后一条 message_id。"""
    chunks = split_markdown_for_cards(markdown_text)
    last_id = None
    for i, chunk in enumerate(chunks):
        chunk_title = (f"{title}（续 {i}）" if i > 0 else title) if title else (f"（续 {i}）" if i > 0 else "")
        card = build_interactive_card(chunk, title=chunk_title)
        mid = await reply_message(message_id, card, msg_type="interactive")
        if not mid:
            # 降级：用 post 格式
            logger.warning("[Feishu] reply_interactive fallback to post")
            mid = await reply_message(message_id, build_post_content(chunk), msg_type="post")
        if mid:
            last_id = mid
    return last_id
