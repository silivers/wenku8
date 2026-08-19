"""适配器抽象基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from novel.core.exceptions import FetchError
from novel.core.models import ChapterContent, ChapterInfo, ImageRef, NovelInfo


class BaseAdapter(ABC):
    """站点适配器抽象基类。

    引擎通过此接口与站点交互，不感知任何站点细节。
    简单站点可继承 ConfigAdapter 用 YAML 配置驱动；
    复杂站点直接继承本类编写代码。
    """

    @property
    @abstractmethod
    def site_name(self) -> str:
        """全局唯一的站点标识符（小写）"""

    @property
    @abstractmethod
    def base_url(self) -> str:
        """站点根 URL"""

    @property
    def encoding(self) -> str:
        """默认页面编码。设为 "auto" 则自动检测。"""
        return "utf-8"

    def get_rate_limit(self) -> float:
        """该站点的最小请求间隔（秒）"""
        return 3.0

    def is_takedown(self, content: str) -> bool:
        """检测章节内容是否为下架/版权删除页面。

        子类可覆盖以添加站点特定的下架检测模式。
        检测到下架后，引擎会跳过该小说的所有剩余章节。
        """
        return False

    @abstractmethod
    async def search(self, keyword: str, page: int = 1) -> tuple[list[NovelInfo], bool]:
        """搜索小说。返回 (结果列表, 是否有下一页)。"""

    @abstractmethod
    async def get_novel_info(self, novel_id: str) -> NovelInfo:
        """获取小说元信息"""

    @abstractmethod
    async def get_chapter_list(self, novel_id: str) -> list[ChapterInfo]:
        """获取章节目录"""

    @abstractmethod
    async def get_chapter_content(self, chapter_id: str, url: str = "") -> ChapterContent:
        """获取章节正文。url 可为空，由适配器自行构造。"""

    async def get_cover(self, novel_id: str) -> bytes | None:
        """下载封面图片。默认返回 None，子类可覆盖。"""
        return None

    def supports_site_wide(self) -> bool:
        """是否支持全站遍历（iter_all_novels）。

        CLI 的 crawl-all 会先探测此能力，避免直接撞 NotImplementedError。
        默认 False；实现了 iter_all_novels 的适配器应覆盖为 True。
        """
        return False

    def set_fetcher(self, fetcher) -> None:
        """注入引擎统一中间件抓取器（由 CrawlerEngine 构造时调用）。

        注入后，fetch_text / fetch_response 走完整中间件链
        （UA 轮换、限速、编码、校验、重试），而非直接 client.fetch。
        """
        self._fetcher = fetcher

    async def login(self, username: str | None = None, password: str | None = None) -> bool:
        """站点登录（可选能力）。默认不支持，返回 False。

        需要登录的适配器（如 wenku8）应覆盖此方法：登录成功后把会话 Cookie
        写入共享 http_client，并在 self._session_cookies 缓存，使所有请求带登录态。
        """
        return False

    async def fetch_response(
        self, url: str, params: dict | None = None, cookies: dict | None = None
    ) -> Any:
        """发起一次请求，返回 Response（已注入中间件链）。

        未注入 fetcher 时回退到直接 client.fetch（CLI 的 search 等轻量入口）。

        若适配器已完成登录（self._session_cookies 非空），则合并进本次请求，
        使会话态贯穿中间件链（封面/插图走 fetch_bytes 则由 client 级 Cookie 覆盖）。
        """
        from novel.core.middleware import Request

        session_cookies = getattr(self, "_session_cookies", None) or {}
        merged_cookies = {**session_cookies, **(cookies or {})}
        request = Request(url=url, params=params or {}, cookies=merged_cookies)
        fetcher = getattr(self, "_fetcher", None)
        if fetcher is not None:
            return await fetcher.fetch(request, self)

        client = getattr(self, "_http_client", None)
        if client is None:
            from novel.http.client import AsyncHTTPClient

            client = AsyncHTTPClient()
            self._http_client = client
        return await client.fetch(request)

    async def fetch_text(
        self, url: str, params: dict | None = None, cookies: dict | None = None
    ) -> str:
        """抓取 URL 并解码为文本（共享逻辑，子类复用，消除复制粘贴）。

        - 注入 fetcher 时，Response.text 已由 EncodingMiddleware 完成解码；
        - 未注入 fetcher（回退路径）时，沿用三级编码检测 + 解码（保留 gbk 等声明语义）。
        - 抓取在重试耗尽后仍失败 / 被拦截时抛出 FetchError，调用方据此判定失败。
        """
        response = await self.fetch_response(url, params, cookies)
        if response is None:
            raise FetchError(f"抓取失败（重试耗尽或被拦截）: {url}")

        fetcher = getattr(self, "_fetcher", None)
        if fetcher is not None:
            return response.text or ""

        from novel.utils.encoding import detect_encoding, safe_decode

        encoding, _ = detect_encoding(
            response.raw_body, self.encoding, response.charset
        )
        text, _ = safe_decode(response.raw_body, encoding)
        return text

    async def iter_all_novels(
        self, filter_params: dict[str, Any] | None = None
    ) -> AsyncIterator[NovelInfo]:
        """遍历站点所有小说。

        子类应实现此方法以支持全站爬取。
        默认实现抛出 NotImplementedError。

        Args:
            filter_params: 可选过滤条件（如分类、排序）
        Yields:
            NovelInfo 对象（至少包含 site_id 和 title）
        """
        raise NotImplementedError(
            f"{self.site_name} 适配器未实现全站遍历 (iter_all_novels)"
        )

    async def close(self) -> None:
        """清理资源"""
        pass
