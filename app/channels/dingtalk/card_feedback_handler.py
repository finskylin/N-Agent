"""
DingTalk Card Feedback Handler
钉钉卡片按钮回调处理器（点赞/踩）

当用户点击 AI 卡片上的 👍/👎 按钮时，钉钉通过 Stream 推送回调事件。
本处理器解析按钮 ID，保存反馈到数据库，并更新卡片按钮显示选中状态。
"""

import json
from loguru import logger

try:
    import dingtalk_stream
    from dingtalk_stream import AckMessage
    from dingtalk_stream.card_callback import CardCallbackMessage
    DINGTALK_AVAILABLE = True
except ImportError:
    DINGTALK_AVAILABLE = False
    logger.warning("[CardFeedback] dingtalk-stream not installed, handler disabled")


if DINGTALK_AVAILABLE:
    class FeedbackCardCallbackHandler(dingtalk_stream.CallbackHandler):
        """
        处理卡片按钮回调

        按钮 ID 格式: "like_{report_id}" 或 "dislike_{report_id}"
        回调后更新卡片按钮为选中状态
        """

        def __init__(self):
            super().__init__()


        async def process(self, callback: dingtalk_stream.CallbackMessage):
            """
            处理卡片按钮回调

            Args:
                callback: 钉钉回调消息

            Returns:
                (status, response_data) 元组
            """
            try:
                card_msg = CardCallbackMessage.from_dict(callback.data)

                # 提取按钮 ID
                content = card_msg.content or {}
                logger.debug(f"[CardFeedback] Raw callback content: {content}")
                button_id = ""
                if isinstance(content, dict):
                    # AI 卡片按钮回调的实际结构：
                    # {"cardPrivateData": {"actionIds": [...], "params": {"id": "like_xxx", "text": "👍 有帮助"}}}
                    private_data = content.get("cardPrivateData", {})
                    if isinstance(private_data, dict):
                        params = private_data.get("params", {})
                        if isinstance(params, dict):
                            button_id = params.get("id", "")
                        if not button_id:
                            button_id = private_data.get("actionId", "")
                    if not button_id:
                        button_id = content.get("actionId", "") or content.get("id", "")

                card_instance_id = card_msg.card_instance_id or ""
                user_id_str = card_msg.user_id or ""

                logger.info(
                    f"[CardFeedback] Received callback: button_id={button_id}, "
                    f"card_id={card_instance_id[:16]}..., user={user_id_str[:10]}..."
                )

                # 解析 button_id: "like_{report_id}" 或 "dislike_{report_id}"
                if button_id.startswith("like_"):
                    report_id = button_id[5:]
                    rating = "like"
                elif button_id.startswith("dislike_"):
                    report_id = button_id[8:]
                    rating = "dislike"
                else:
                    logger.warning(f"[CardFeedback] Unknown button_id: {button_id}")
                    return AckMessage.STATUS_OK, {}

                logger.info(
                    f"[CardFeedback] Received feedback: report={report_id}, "
                    f"rating={rating}, user={user_id_str[:10]}..."
                )

                # 保存反馈到数据库
                try:
                    from app.agent.v4.persistence import V4PersistenceLayer
                    from app.channels.dingtalk.utils import generate_dingtalk_user_id
                    persistence = V4PersistenceLayer()
                    if not user_id_str:
                        logger.warning("[CardFeedback] Empty user_id_str, skipping feedback save")
                        return AckMessage.STATUS_OK, {}
                    _uid = generate_dingtalk_user_id(user_id_str)
                    await persistence.save_report_feedback(
                        report_id=report_id,
                        session_id="",
                        rating=rating,
                        channel="dingtalk",
                        user_id=_uid,
                    )
                    logger.info(f"[CardFeedback] Feedback saved: report={report_id}, rating={rating}")
                except Exception as e:
                    logger.error(f"[CardFeedback] Failed to save feedback: {e}")

                # 桥接知识引擎反馈学习
                try:
                    from app.api.chat_v4 import _bridge_feedback_to_knowledge_engine
                    await _bridge_feedback_to_knowledge_engine(
                        user_id=_uid,
                        session_id="",
                        rating=rating,
                    )
                except Exception as e:
                    logger.debug(f"[CardFeedback] Knowledge engine bridge failed (non-fatal): {e}")

                # 更新卡片按钮选中状态
                # 需要发送完整 cardData（含原始 msgContent），否则卡片内容会丢失
                try:
                    await self._update_card_buttons(card_instance_id, report_id, rating)
                except Exception as e:
                    logger.warning(f"[CardFeedback] Failed to update card buttons: {e}")

                # Send feedback prompt (no Redis pending state)
                try:
                    await self._send_feedback_prompt(card_instance_id, user_id_str, rating)
                except Exception as e:
                    logger.warning(f"[CardFeedback] Failed to send feedback prompt: {e}")

                return AckMessage.STATUS_OK, {}

            except Exception as e:
                logger.error(f"[CardFeedback] Error processing callback: {e}")
                import traceback
                logger.debug(traceback.format_exc())
                return AckMessage.STATUS_OK, {}

        async def _get_card_data_from_cache(self, card_instance_id: str) -> dict:
            """获取卡片关联数据（report_id + markdown）"""
            return {}

        async def _update_card_buttons(self, card_instance_id: str, report_id: str, rating: str):
            """通过 put_card_data 更新卡片按钮选中状态

            关键：必须发送完整的 cardData（含 msgContent），否则内容会丢失。
            """
            # 缓存不可用，跳过按钮更新
            logger.info("[CardFeedback] Skipping button update (no cache available)")
            return

            # 构造选中状态的按钮
            # 钉钉 AI 卡片按钮有效颜色值：blue（主色高亮）、gray（次要灰色）
            if rating == "like":
                new_buttons = [
                    {"text": "👍 已反馈", "color": "blue", "id": f"like_{report_id}", "request": True},
                    {"text": "👎 需改进", "color": "gray", "id": f"dislike_{report_id}", "request": True},
                ]
            else:
                new_buttons = [
                    {"text": "👍 有帮助", "color": "gray", "id": f"like_{report_id}", "request": True},
                    {"text": "👎 已反馈", "color": "blue", "id": f"dislike_{report_id}", "request": True},
                ]

            # 构造完整的 cardData（与 AIMarkdownCardInstance.get_card_data 一致）
            # 注意：cardParamMap 所有值必须是字符串类型，否则钉钉返回 400
            card_data = {
                "msgContent": markdown,
                "staticMsgContent": "",
                "msgTitle": "分析结果",
                "flowStatus": "3",  # FINISHED（必须是字符串）
                "sys_full_json_obj": json.dumps({
                    "order": ["msgTitle", "msgContent", "staticMsgContent", "msgButtons"],
                    "msgButtons": new_buttons,
                }),
            }

            # 调用 put_card_data API
            access_token = self.dingtalk_client.get_access_token()
            if not access_token:
                logger.warning("[CardFeedback] No access token, skip button update")
                return

            import requests as http_requests
            from dingtalk_stream.utils import DINGTALK_OPENAPI_ENDPOINT
            from dingtalk_stream.card_replier import CardReplier

            url = DINGTALK_OPENAPI_ENDPOINT + "/v1.0/card/instances"
            body = {
                "outTrackId": card_instance_id,
                "cardData": {"cardParamMap": card_data},
            }
            resp = http_requests.put(
                url,
                headers=CardReplier.get_request_header(access_token),
                json=body,
            )
            if resp.status_code == 200:
                logger.info(f"[CardFeedback] Card buttons updated: {rating} selected")
            else:
                logger.warning(f"[CardFeedback] Card update failed: status={resp.status_code}, body={resp.text[:200]}")

        async def _send_feedback_prompt(
            self, card_instance_id: str, user_id: str, rating: str
        ):
            """通过钉钉 API 发送追问消息，提示用户可以补充反馈

            使用 /v1.0/robot/oToMessages/batchSend 发送私信。
            """
            access_token = self.dingtalk_client.get_access_token()
            if not access_token:
                logger.warning("[CardFeedback] No access token, skip feedback prompt")
                return

            from app.config import settings
            robot_code = settings.dingtalk_client_id

            label = "👍 有帮助" if rating == "like" else "👎 需改进"
            prompt_text = (
                f"收到您的反馈「{label}」，感谢！\n\n"
                "如需补充说明原因，请引用回复本条消息，或发送以「反馈:」开头的消息。\n"
                "例如：反馈:数据不够准确"
            )

            import httpx
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            headers = {
                "x-acs-dingtalk-access-token": access_token,
                "Content-Type": "application/json",
            }
            payload = {
                "robotCode": robot_code,
                "userIds": [user_id],
                "msgKey": "sampleText",
                "msgParam": json.dumps({"content": prompt_text}, ensure_ascii=False),
            }

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                    if resp.status_code == 200:
                        logger.info(f"[CardFeedback] Feedback prompt sent to user {user_id[:10]}...")
                    else:
                        logger.warning(
                            f"[CardFeedback] Feedback prompt failed: "
                            f"status={resp.status_code}, body={resp.text[:200]}"
                        )
            except Exception as e:
                logger.warning(f"[CardFeedback] Feedback prompt request error: {e}")

        async def _get_report_id_from_card(self, card_instance_id: str) -> str:
            """从 Redis 查找 card_instance_id 对应的 report_id（兼容旧格式）"""
            data = await self._get_card_data_from_cache(card_instance_id)
            return data.get("report_id", "")

else:
    class FeedbackCardCallbackHandler:
        def __init__(self):
            raise ImportError("dingtalk-stream not installed")
