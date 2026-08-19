"""限速中间件（域名级）"""

from __future__ import annotations

import asyncio
import random
import time
from collections import defaultdict

from novel.core.adapter import BaseAdapter
from novel.core.middleware import Request, RequestMiddleware


class RateLimitMiddleware(RequestMiddleware):
    """域名级限速：每个域名独立计时，间隔 + 随机抖动。"""

    def __init__(self, interval: float = 3.0, jitter: float = 0.3):
        self._interval = interval
        self._jitter = jitter
        self._last_request: dict[str, float] = defaultdict(float)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def process_request(
        self, request: Request, adapter: BaseAdapter
    ) -> Request | None:
        from urllib.parse import urlparse

        domain = urlparse(request.url).netloc
        lock = self._locks[domain]

        async with lock:
            jitter_val = random.uniform(0, self._interval * self._jitter)
            wait_time = self._interval + jitter_val
            elapsed = time.monotonic() - self._last_request[domain]
            if elapsed < wait_time:
                await asyncio.sleep(wait_time - elapsed)
            self._last_request[domain] = time.monotonic()

        return request

    def ensure_interval_at_least(self, interval: float) -> None:
        """把间隔抬高到至少 interval（用于让站点声明的限速真正生效）。

        例：config 默认 3.0s，但 wenku8 声明 5.0s，则取 5.0s。
        """
        if interval > self._interval:
            self._interval = interval
