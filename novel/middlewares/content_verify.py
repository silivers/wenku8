"""内容完整性校验中间件"""

from __future__ import annotations

from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.middleware import Request, Response, ResponseMiddleware


class ContentVerifyMiddleware(ResponseMiddleware):
    """响应内容校验：蜜罐模式。

    发现问题记日志，不阻断流程（除非蜜罐模式匹配且配置 drop_on_honeypot）。
    不做字数检查 —— 引言、后记、插图页等章节本身就很短，不应视为异常。
    """

    def __init__(
        self,
        honeypot_patterns: list[str] | None = None,
        drop_on_honeypot: bool = False,
        **_kwargs,
    ):
        self.honeypot_patterns = honeypot_patterns or [
            "请刷新页面重试",
            "检测到异常访问",
            "访问过于频繁",
        ]
        self.drop_on_honeypot = drop_on_honeypot

    async def process_response(
        self, response: Response, request: Request, adapter: BaseAdapter
    ) -> Response | None:
        text = response.text or ""

        # 蜜罐检测
        for pattern in self.honeypot_patterns:
            if pattern in text:
                logger.error(
                    f"蜜罐模式检测: '{pattern}' found in {request.url}"
                )
                if self.drop_on_honeypot:
                    return None  # 触发重试
                break

        return response
