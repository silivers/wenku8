"""BaseAdapter.fetch_text 共享逻辑测试（覆盖 2.1 去重 fetch 到基类）。

用假 HTTP 客户端模拟响应，验证三级编码检测 + 解码在基类中可用。
"""

from __future__ import annotations

import asyncio

from novel.core.adapter import BaseAdapter
from novel.core.middleware import Request, Response


class _FakeResponse:
    def __init__(self, raw_body: bytes, charset: str = "utf-8"):
        self.raw_body = raw_body
        self.charset = charset
        self.status_code = 200


class FakeClient:
    def __init__(self, raw_body: bytes, charset: str = "utf-8"):
        self._resp = _FakeResponse(raw_body, charset)

    async def fetch(self, request: Request) -> Response:
        return self._resp


class TAdapter(BaseAdapter):
    site_name = "t"
    base_url = "http://t"
    encoding = "utf-8"

    async def search(self, *a, **k):
        return ([], False)

    async def get_novel_info(self, *a, **k):
        raise NotImplementedError

    async def get_chapter_list(self, *a, **k):
        raise NotImplementedError

    async def get_chapter_content(self, *a, **k):
        raise NotImplementedError


def test_fetch_text_utf8():
    async def _():
        data = "你好，世界".encode("utf-8")
        adapter = TAdapter()
        adapter._http_client = FakeClient(data)
        text = await adapter.fetch_text("http://t/x")
        assert text == "你好，世界"

    asyncio.run(_())


def test_fetch_text_gbk():
    async def _():
        data = "轻小说".encode("gbk")
        adapter = TAdapter()
        adapter.encoding = "gbk"  # 适配器声明 gbk，信任该编码
        adapter._http_client = FakeClient(data)
        text = await adapter.fetch_text("http://t/y")
        assert text == "轻小说"

    asyncio.run(_())
