"""User-Agent 中间件单元测试（覆盖 1.3 合规 UA 后缀）。"""

from __future__ import annotations

import asyncio

from novel.core.middleware import Request
from novel.middlewares.user_agent import UserAgentMiddleware


def test_suffix_appended():
    async def _():
        mw = UserAgentMiddleware(suffix="(novel-crawler)")
        req = Request(url="http://example.com")
        out = await mw.process_request(req, None)
        assert out.headers["User-Agent"].endswith("(novel-crawler)")

    asyncio.run(_())


def test_no_suffix():
    async def _():
        mw = UserAgentMiddleware(suffix="")
        req = Request(url="http://example.com")
        out = await mw.process_request(req, None)
        assert "(novel-crawler)" not in out.headers["User-Agent"]

    asyncio.run(_())


def test_request_not_blocked():
    async def _():
        mw = UserAgentMiddleware(suffix="bot")
        req = Request(url="http://example.com")
        out = await mw.process_request(req, None)
        assert out is not None

    asyncio.run(_())
