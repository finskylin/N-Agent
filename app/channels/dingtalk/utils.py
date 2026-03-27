"""
DingTalk 工具函数
"""
import hashlib

# 10亿起步，避免与 DB 自增 ID 冲突
DINGTALK_USER_ID_OFFSET = 1_000_000_000
# ~21亿范围
DINGTALK_USER_ID_RANGE = 2_147_483_647


def generate_dingtalk_user_id(sender_id: str) -> int:
    """
    从钉钉 sender_id 生成稳定的数值型 user_id。

    使用 SHA-256 哈希确保：
    - 同一 sender_id 始终生成相同 user_id（确定性）
    - 不同 sender_id 生成不同 user_id（低碰撞率）
    - 结果范围 [1_000_000_000, 3_147_483_647]（避免与 DB 自增 ID 冲突）

    Args:
        sender_id: 钉钉用户唯一标识

    Returns:
        稳定的数值型 user_id
    """
    if not sender_id or sender_id == "unknown":
        raise ValueError("sender_id is empty or unknown, cannot generate user_id")

    digest = hashlib.sha256(sender_id.encode()).hexdigest()
    numeric = int(digest[:15], 16) % DINGTALK_USER_ID_RANGE
    return DINGTALK_USER_ID_OFFSET + numeric
