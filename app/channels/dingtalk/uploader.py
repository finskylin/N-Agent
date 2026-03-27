"""
DingTalk Uploader

钉钉媒体上传器，处理图片、文件的上传和下载。
"""
import os
import tempfile
from typing import Optional
from loguru import logger

import httpx


class DingTalkUploader:
    """
    钉钉媒体上传器

    职责：
    1. 上传图片到钉钉图床
    2. 上传文件到钉钉云盘
    3. 下载钉钉媒体文件
    """

    def __init__(self, config: dict, client=None):
        """
        初始化上传器

        Args:
            config: 插件配置
            client: 钉钉 Stream 客户端
        """
        self.config = config
        self.client = client
        self.robot_code = config.get("robot_code", "")

        # API 端点
        self._api_base = "https://api.dingtalk.com/v1.0"

    async def upload_from_url(self, image_url: str) -> Optional[str]:
        """
        从 URL 上传图片到钉钉

        Args:
            image_url: 图片 URL

        Returns:
            media_id 或 None
        """
        try:
            # 下载图片
            async with httpx.AsyncClient(timeout=30) as http_client:
                resp = await http_client.get(image_url)
                if resp.status_code != 200:
                    logger.error(f"[DingTalk] Failed to download image: {resp.status_code}")
                    return None

                buffer = resp.content

            # 保存到临时文件
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                f.write(buffer)
                temp_path = f.name

            try:
                # 上传到钉钉
                return await self._upload_image_file(temp_path)
            finally:
                os.unlink(temp_path)

        except Exception as e:
            logger.error(f"[DingTalk] Failed to upload image from URL: {e}")
            return None

    async def _upload_image_file(self, filepath: str) -> Optional[str]:
        """
        上传图片文件到钉钉

        Args:
            filepath: 本地文件路径

        Returns:
            media_id 或 None
        """
        try:
            # 获取 access_token
            access_token = await self._get_access_token()
            if not access_token:
                return None

            # 上传图片
            url = f"{self._api_base}/media/upload"

            async with httpx.AsyncClient(timeout=60) as http_client:
                with open(filepath, 'rb') as f:
                    files = {'media': f}
                    headers = {'x-acs-dingtalk-access-token': access_token}
                    params = {'type': 'image'}

                    resp = await http_client.post(
                        url,
                        files=files,
                        headers=headers,
                        params=params
                    )

                if resp.status_code == 200:
                    result = resp.json()
                    return result.get('mediaId')
                else:
                    logger.error(f"[DingTalk] Failed to upload image: {resp.text}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalk] Failed to upload image file: {e}")
            return None

    async def upload_file(self, filepath: str, filename: str) -> Optional[str]:
        """
        上传文件到钉钉

        Args:
            filepath: 本地文件路径
            filename: 文件名

        Returns:
            media_id 或 None
        """
        try:
            # 获取 access_token
            access_token = await self._get_access_token()
            if not access_token:
                return None

            # 上传文件
            url = f"{self._api_base}/media/upload"

            async with httpx.AsyncClient(timeout=120) as http_client:
                with open(filepath, 'rb') as f:
                    files = {'media': (filename, f)}
                    headers = {'x-acs-dingtalk-access-token': access_token}
                    params = {'type': 'file'}

                    resp = await http_client.post(
                        url,
                        files=files,
                        headers=headers,
                        params=params
                    )

                if resp.status_code == 200:
                    result = resp.json()
                    return result.get('mediaId')
                else:
                    logger.error(f"[DingTalk] Failed to upload file: {resp.text}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalk] Failed to upload file: {e}")
            return None

    async def download_file(
        self,
        download_code: str,
        robot_code: str = None
    ) -> Optional[bytes]:
        """
        下载钉钉媒体文件

        Args:
            download_code: 下载码
            robot_code: 机器人代码

        Returns:
            文件内容或 None
        """
        try:
            # 获取 access_token
            access_token = await self._get_access_token()
            if not access_token:
                return None

            robot_code = robot_code or self.robot_code

            # 下载文件
            url = f"{self._api_base}/robot/messageFiles/download"
            params = {
                'downloadCode': download_code,
                'robotCode': robot_code,
            }
            headers = {'x-acs-dingtalk-access-token': access_token}

            async with httpx.AsyncClient(timeout=60) as http_client:
                resp = await http_client.get(url, headers=headers, params=params)

                if resp.status_code == 200:
                    return resp.content
                else:
                    logger.error(f"[DingTalk] Failed to download file: {resp.status_code}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalk] Failed to download file: {e}")
            return None

    async def _get_access_token(self) -> Optional[str]:
        """
        获取钉钉 access_token

        Returns:
            access_token 或 None
        """
        try:
            # 尝试从客户端获取
            if self.client and hasattr(self.client, 'access_token'):
                return self.client.access_token

            # 手动获取
            url = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
            data = {
                "appKey": self.config.get("client_id"),
                "appSecret": self.config.get("client_secret"),
            }

            async with httpx.AsyncClient(timeout=10) as http_client:
                resp = await http_client.post(url, json=data)

                if resp.status_code == 200:
                    result = resp.json()
                    return result.get('accessToken')
                else:
                    logger.error(f"[DingTalk] Failed to get access token: {resp.text}")
                    return None

        except Exception as e:
            logger.error(f"[DingTalk] Failed to get access token: {e}")
            return None