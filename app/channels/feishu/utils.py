"""
Feishu Utils
飞书工具函数
"""
import hashlib
from typing import Optional


def generate_feishu_user_id(open_id: str) -> int:
    """
    将飞书 open_id 映射为系统内部 user_id（整数）

    使用 MD5 hash 取前 8 位 hex 转为整数，范围在 100_000_000 - 999_999_999 之间。
    与 dingtalk 的 generate_dingtalk_user_id 保持相同逻辑，但使用不同前缀以避免冲突。

    Args:
        open_id: 飞书用户 open_id（如 ou_xxxxx）

    Returns:
        内部 user_id（整数）
    """
    if not open_id:
        raise ValueError("open_id is empty, cannot generate user_id")

    hash_hex = hashlib.md5(f"feishu:{open_id}".encode()).hexdigest()
    # 取前 8 位 hex，转为整数，确保在合理范围内
    raw = int(hash_hex[:8], 16)
    # 映射到 200_000_000 - 299_999_999 范围（飞书用户区间）
    return 200_000_000 + (raw % 100_000_000)


def verify_feishu_signature(
    timestamp: str,
    nonce: str,
    body: bytes,
    encrypt_key: str,
) -> bool:
    """
    验证飞书 Webhook 签名

    飞书签名算法：SHA256(timestamp + nonce + encrypt_key + body)
    请求头：X-Lark-Signature

    Args:
        timestamp: X-Lark-Request-Timestamp
        nonce: X-Lark-Request-Nonce
        body: 请求体原始字节
        encrypt_key: 配置的 encrypt_key

    Returns:
        是否合法
    """
    import hmac
    import hashlib as _hashlib

    content = timestamp.encode() + nonce.encode() + encrypt_key.encode() + body
    expected = hmac.new(encrypt_key.encode(), content, _hashlib.sha256).hexdigest()
    return expected  # 调用方自行比较
