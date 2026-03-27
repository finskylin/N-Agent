"""
Channel Types

通道相关的类型定义，包括消息结构、回复载荷、回调接口等。
"""
from dataclasses import dataclass, field
from typing import List, Optional, Any, Callable, Awaitable
from enum import Enum


class ChannelCapability(Enum):
    """通道能力声明"""
    STREAMING = "streaming"        # 支持流式输出
    MARKDOWN = "markdown"          # 支持 Markdown
    CARD = "card"                  # 支持交互卡片
    IMAGE = "image"                # 支持图片
    FILE = "file"                  # 支持文件
    FEEDBACK = "feedback"          # 支持反馈按钮
    THREAD = "thread"              # 支持消息线程


class MessageType(Enum):
    """消息类型"""
    TEXT = "text"
    IMAGE = "image"
    FILE = "file"
    AUDIO = "audio"
    VIDEO = "video"
    RICH_TEXT = "richText"     # 富文本（钉钉）/ Post（飞书）
    STICKER = "sticker"
    MIXED = "mixed"            # 混合消息（多图+文字）


@dataclass
class MediaAttachment:
    """媒体附件"""
    type: str                      # image, file, audio, video
    path: str                      # 本地保存路径
    url: Optional[str] = None      # 原始 URL（如有）
    filename: Optional[str] = None # 文件名
    content_type: Optional[str] = None  # MIME 类型
    size_bytes: int = 0


@dataclass
class LinkInfo:
    """链接信息"""
    url: str
    title: Optional[str] = None
    domain: Optional[str] = None


@dataclass
class MentionInfo:
    """@提及信息"""
    user_id: str
    user_name: Optional[str] = None
    raw_key: Optional[str] = None  # 原始 @key（用于去除）


@dataclass
class QuotedMessage:
    """引用消息（完整版，包含媒体）"""
    message_id: str
    content: str = ""                         # 文本内容
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    content_type: str = "text"                # 消息类型
    attachments: List[MediaAttachment] = field(default_factory=list)  # 引用消息中的附件
    links: List[LinkInfo] = field(default_factory=list)               # 引用消息中的链接
    create_time: Optional[int] = None


@dataclass
class InboundMessage:
    """标准化入站消息结构"""
    # 基本信息（无默认值字段在前）
    message_id: str
    conversation_id: str
    sender_id: str
    channel: str                         # dingtalk, feishu, wecom

    # 消息内容（无默认值字段）
    message_type: MessageType
    raw_content: str                     # 原始消息内容
    text_content: str                    # 提取的纯文本内容

    # 可选字段（有默认值）
    sender_name: Optional[str] = None

    # 附件
    attachments: List[MediaAttachment] = field(default_factory=list)

    # 链接
    links: List[LinkInfo] = field(default_factory=list)

    # @提及
    mentions: List[MentionInfo] = field(default_factory=list)

    # 引用消息
    quoted_message: Optional[QuotedMessage] = None

    # 线程信息
    thread_root_id: Optional[str] = None
    parent_message_id: Optional[str] = None

    # 是否 @ 了机器人
    mentioned_bot: bool = False

    # 原始消息对象（调试用）
    raw: Optional[Any] = None


@dataclass
class ChannelMessage:
    """通道消息（简化版，用于内部传递）"""
    message_id: str
    conversation_id: str
    sender_id: str
    sender_nick: Optional[str]
    content: str
    attachments: list
    channel: str
    raw: Any


@dataclass
class ReplyPayload:
    """回复载荷"""
    text: str = ""
    markdown: str = ""
    images: list = None   # 图片 URL 列表
    files: list = None    # 文件 URL 列表
    reply_to_id: str = ""
    is_final: bool = False
    metadata: dict = None

    # 报告字段
    report_name: str = ""             # 报告英文名称（用于文件命名）
    send_report_file: bool = False    # 是否需要发送报告文件

    def __post_init__(self):
        if self.images is None:
            self.images = []
        if self.files is None:
            self.files = []
        if self.metadata is None:
            self.metadata = {}


@dataclass
class AgentCallbacks:
    """Agent 回调接口"""

    # 流式输出回调：每次 Agent 产生新文本时触发
    on_partial_reply: Callable[[ReplyPayload], Awaitable[None]] = None

    # 分块回复回调：Agent 产生完整段落时触发
    on_block_reply: Callable[[ReplyPayload], Awaitable[None]] = None

    # 工具结果回调：工具执行完成时触发
    on_tool_result: Callable[[str, dict], Awaitable[None]] = None

    # 完成回调
    on_finish: Callable[[ReplyPayload], Awaitable[None]] = None

    # 错误回调
    on_error: Callable[[str], Awaitable[None]] = None

    # 文本清除回调：Agent 清除已输出文本时触发（如 phase2_handoff、policy_error）
    on_text_clear: Callable[[str], Awaitable[None]] = None  # reason: str

    # 工具调用回调：工具开始执行时触发
    on_tool_call: Callable[[str, dict], Awaitable[None]] = None  # name, input

    # 状态回调：Agent 状态变化时触发
    on_status: Callable[[str, dict], Awaitable[None]] = None  # status, data

    # 报告就绪回调：报告生成完成时触发
    on_report_ready: Callable[[dict], Awaitable[None]] = None  # report_data

    # 组件渲染回调：UI 组件就绪时触发
    on_component: Callable[[dict], Awaitable[None]] = None  # component_data