"""User-Agent 轮换中间件"""

from __future__ import annotations

import random

from novel.core.adapter import BaseAdapter
from novel.core.middleware import Request, RequestMiddleware

DEFAULT_UA_LIST = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
]


class UserAgentMiddleware(RequestMiddleware):
    """随机轮换 User-Agent。"""

    def __init__(
        self,
        ua_list: list[str] | None = None,
        rotate: bool = True,
        suffix: str = "",
    ):
        self.ua_list = ua_list or DEFAULT_UA_LIST
        self.rotate = rotate
        self.suffix = suffix

    async def process_request(
        self, request: Request, adapter: BaseAdapter
    ) -> Request | None:
        ua = random.choice(self.ua_list) if self.rotate else self.ua_list[0]
        if self.suffix:
            ua = f"{ua} {self.suffix}"
        request.headers["User-Agent"] = ua
        return request
