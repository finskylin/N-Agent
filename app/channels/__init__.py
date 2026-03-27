"""
Channel Plugin Architecture

插件化通道架构，支持钉钉、飞书、企微等多渠道扩展。
"""
from app.channels.manager import ChannelManager
from app.channels.types import (
    AgentCallbacks,
    ChannelCapability,
    ChannelMessage,
    ReplyPayload,
    InboundMessage,
    MessageType,
    MediaAttachment,
    LinkInfo,
    MentionInfo,
    QuotedMessage,
)

__all__ = [
    "ChannelManager",
    "AgentCallbacks",
    "ChannelCapability",
    "ChannelMessage",
    "ReplyPayload",
    "InboundMessage",
    "MessageType",
    "MediaAttachment",
    "LinkInfo",
    "MentionInfo",
    "QuotedMessage",
]