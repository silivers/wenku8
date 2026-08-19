"""中间件感知的抓取器：统一请求/响应中间件链 + 重试回退。

适配器通过它发起所有 HTTP 请求，确保 UA 轮换、限速、编码检测、
内容校验、重试等逻辑对『全部真实请求』生效（修复评审 H1 双下载 /
H2 中间件失效 / M1 站点限速 / M5 重试耗尽存错误页）。

引擎在构造时把 Fetcher 注入适配器（adapter.set_fetcher），适配器原有的
fetch_text / fetch_response 即走此链路，不再直接 client.fetch 绕过中间件。
"""

from __future__ import annotations

import asyncio

from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.middleware import Request, Response


class Fetcher:
    """带请求/响应中间件链与重试的抓取器。"""

    def __init__(
        self,
        client,
        request_mw: list | None = None,
        response_mw: list | None = None,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ):
        self._client = client
        self._request_mw = list(request_mw or [])
        self._response_mw = list(response_mw or [])
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    async def fetch(
        self, request: Request, adapter: BaseAdapter | None = None
    ) -> Response | None:
        """执行一次完整请求链（含重试）。

        返回 Response；重试耗尽 / 被中间件拦截 / 网络异常耗尽时返回 None。
        """
        for attempt in range(self._max_retries + 1):
            req = request

            # 请求中间件链（UA 轮换、限速等）
            for mw in self._request_mw:
                req = await mw.process_request(req, adapter)
                if req is None:
                    logger.warning(f"请求被中间件拦截: {request.url}")
                    return None

            # 实际 HTTP 请求
            try:
                resp = await self._client.fetch(req)
            except Exception as e:  # noqa: BLE001
                if attempt < self._max_retries:
                    wait = self._backoff_base * (2 ** attempt)
                    logger.debug(
                        f"请求异常，重试 [{attempt + 1}/{self._max_retries}]: "
                        f"{request.url} ({e})"
                    )
                    await asyncio.sleep(wait)
                    request.meta["_retry_count"] = attempt + 1
                    request.retry_count = attempt + 1
                    continue
                logger.error(f"请求异常且重试耗尽: {request.url} ({e})")
                return None

            # 响应中间件链（编码、内容校验、重试信号等）
            retry = False
            for mw in self._response_mw:
                resp = await mw.process_response(resp, req, adapter)
                if resp is None:
                    retry = True
                    break

            if retry:
                if attempt < self._max_retries:
                    wait = self._backoff_base * (2 ** attempt)
                    logger.debug(
                        f"响应触发重试 [{attempt + 1}/{self._max_retries}]: "
                        f"{request.url}"
                    )
                    await asyncio.sleep(wait)
                    request.meta["_retry_count"] = attempt + 1
                    request.retry_count = attempt + 1
                    continue
                logger.warning(f"重试次数耗尽: {request.url} [{resp and resp.status_code}]")
                return None

            return resp

        return None
