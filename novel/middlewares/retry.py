"""重试中间件（指数退避信号）"""

from __future__ import annotations

from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.exceptions import RequestBlockedError
from novel.core.middleware import Request, RequestMiddleware, Response, ResponseMiddleware


class RetryMiddleware(RequestMiddleware, ResponseMiddleware):
    """指数退避重试（仅作『信号』，实际退避由 Fetcher 统一执行）。

    - 作为响应中间件：命中可重试状态码时返回 None，告知 Fetcher 重试；
      否则原样返回。
    - 作为请求中间件：原样返回（retry_count 由 Fetcher 维护）。
    - 自身不再 sleep / 计数，避免与 Fetcher 的退避重复（评审 L3 双重试）。
    """

    def __init__(
        self,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        retry_status_codes: list[int] | None = None,
    ):
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.retry_status_codes = retry_status_codes or [429, 500, 502, 503, 504]

    async def process_request(
        self, request: Request, adapter: BaseAdapter
    ) -> Request | None:
        return request

    async def process_response(
        self, response: Response, request: Request, adapter: BaseAdapter
    ) -> Response | None:
        if response.status_code in self.retry_status_codes:
            logger.debug(
                f"触发重试信号 [{response.status_code}]: {request.url}"
            )
            return None  # 告知 Fetcher 重试
        return response
