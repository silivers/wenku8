"""配置驱动的适配器（YAML 配置 → 适配器实例）"""

from __future__ import annotations

import re
from typing import Any

from lxml import html as lxml_html
from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.models import ChapterContent, ChapterInfo, ImageRef, NovelInfo


class ConfigAdapter(BaseAdapter):
    """YAML 配置驱动的适配器。

    适合结构简单的静态站点：固定选择器、简单分页、固定编码。
    复杂站点请继承 BaseAdapter 编写代码适配器。

    支持通过 YAML 配置登录态（login）与代理（proxy）：
        login:
          url: "/login.php"          # 相对 base_url
          method: POST
          form: { username: ..., password: ... }   # 或 json: {...}
          headers: { Referer: ... }
        proxy: "http://127.0.0.1:7890"
    """

    def __init__(self, config: dict[str, Any]):
        self._config = config
        self._http_client = None  # 由引擎注入或惰性创建
        self._login_cfg = config.get("login")
        self._proxy = config.get("proxy")
        self._cookies: dict[str, str] = {}
        self._authed = False

    @property
    def site_name(self) -> str:
        return self._config["site_name"]

    @property
    def base_url(self) -> str:
        return self._config["base_url"]

    @property
    def encoding(self) -> str:
        return self._config.get("encoding", "utf-8")

    def get_rate_limit(self) -> float:
        return self._config.get("rate_limit", 1.0)

    async def search(self, keyword: str, page: int = 1) -> tuple[list[NovelInfo], bool]:
        search_cfg = self._config.get("search")
        if not search_cfg:
            return [], False

        url = self.base_url + search_cfg["url_template"].format(
            keyword=keyword, page=page
        )
        html_text = await self.fetch_text(url)
        tree = lxml_html.fromstring(html_text)

        items = tree.xpath(search_cfg["items_selector"])
        results: list[NovelInfo] = []
        fields = search_cfg.get("fields", {})

        for item in items:
            novel = self._extract_fields(item, fields, NovelInfo)
            if novel and novel.title:
                results.append(novel)

        # 简单判断是否有下一页
        has_next = len(results) > 0 and page < 100
        return results, has_next

    async def get_novel_info(self, novel_id: str) -> NovelInfo:
        cfg = self._config.get("novel_info")
        if not cfg:
            return NovelInfo(site_id=novel_id, title=novel_id, author="")

        url = self.base_url + cfg["url_template"].format(novel_id=novel_id)
        html_text = await self.fetch_text(url)
        tree = lxml_html.fromstring(html_text)

        fields = cfg.get("fields", {})
        novel = self._extract_fields(tree, fields, NovelInfo)
        if novel is None:
            novel = NovelInfo(site_id=novel_id, title=novel_id, author="")

        novel.site_id = novel_id

        # 状态映射
        status_mapping = cfg.get("status_mapping", {})
        if novel.status and novel.status in status_mapping:
            novel.status = status_mapping[novel.status]

        return novel

    async def get_chapter_list(self, novel_id: str) -> list[ChapterInfo]:
        cfg = self._config.get("chapter_list")
        if not cfg:
            return []

        # chapter_list 可能需要从 novel_info 页面或独立页面获取
        novel_cfg = self._config.get("novel_info", {})
        if "url_template" in novel_cfg:
            url = self.base_url + novel_cfg["url_template"].format(novel_id=novel_id)
        else:
            url = self.base_url + f"/book/{novel_id}"

        html_text = await self.fetch_text(url)
        tree = lxml_html.fromstring(html_text)

        selector = cfg["selector"]
        elements = tree.xpath(selector)
        fields = cfg.get("fields", {})

        chapters: list[ChapterInfo] = []
        for i, elem in enumerate(elements):
            ch = self._extract_fields(elem, fields, ChapterInfo)
            if ch and ch.title:
                ch.index = i + 1
                if not ch.url:
                    ch.url = ""
                elif not ch.url.startswith("http"):
                    ch.url = self.base_url + ch.url
                chapters.append(ch)

        return chapters

    async def get_chapter_content(
        self, chapter_id: str, url: str = ""
    ) -> ChapterContent:
        cfg = self._config.get("chapter_content")
        if not cfg:
            return ChapterContent(
                chapter_id=chapter_id,
                title=chapter_id,
                content="",
            )

        if not url:
            url = self.base_url + cfg.get("url_template", "").format(
                chapter_id=chapter_id
            )

        html_text = await self.fetch_text(url)
        tree = lxml_html.fromstring(html_text)

        # 移除指定元素
        for remove_sel in cfg.get("remove_selectors", []):
            for elem in tree.xpath(remove_sel):
                parent = elem.getparent()
                if parent is not None:
                    parent.remove(elem)

        # 提取正文
        content_elem = tree.xpath(cfg["selector"])
        if content_elem:
            content = lxml_html.tostring(content_elem[0], encoding="unicode")
        else:
            content = ""

        # 提取标题
        title_sel = cfg.get("title_selector", "")
        title = ""
        if title_sel:
            title_elems = tree.xpath(title_sel)
            if title_elems:
                title = title_elems[0].text or ""

        # 提取图片
        images = []
        image_sel = cfg.get("image_selector", "")
        if image_sel:
            for j, img_elem in enumerate(tree.xpath(image_sel)):
                src = img_elem.get("src", "")
                if src and not src.startswith("http"):
                    src = self.base_url + src
                images.append(ImageRef(url=src, index=j))

        return ChapterContent(
            chapter_id=chapter_id,
            title=title or chapter_id,
            content=content,
            content_format="html",
            images=images,
            raw_encoding=self.encoding,
        )

    def _extract_fields(self, element, fields: dict, model_class):
        """从 lxml 元素中按字段配置提取数据。"""
        data = {}
        for field_name, selector in fields.items():
            if field_name.endswith("_regex"):
                continue
            results = element.xpath(selector)
            if results:
                value = results[0]
                if hasattr(value, "text"):
                    value = value.text or ""
                value = str(value).strip()

                # 正则提取
                regex_key = f"{field_name}_regex"
                if regex_key in fields:
                    match = re.search(fields[regex_key], value)
                    if match:
                        value = match.group(1)

                data[field_name] = value

        # 映射到模型
        if model_class == NovelInfo:
            return NovelInfo(
                site_id=data.get("novel_id", ""),
                title=data.get("title", ""),
                author=data.get("author", ""),
                description=data.get("description", ""),
                cover_url=data.get("cover_url"),
                status=data.get("status", ""),
            )
        elif model_class == ChapterInfo:
            return ChapterInfo(
                chapter_id=data.get("chapter_id", data.get("url", "")),
                title=data.get("title", ""),
                url=data.get("url", ""),
            )
        return None

    def supports_site_wide(self) -> bool:
        # 配置驱动的适配器默认未实现全站遍历
        return False

    def supports_login(self) -> bool:
        """是否配置了登录态（用于需要鉴权的列表/正文页）。"""
        return bool(self._login_cfg)

    async def _ensure_client(self):
        """惰性创建（带代理支持）HTTP 客户端。"""
        client = getattr(self, "_http_client", None)
        if client is None:
            from novel.http.client import AsyncHTTPClient

            client = AsyncHTTPClient(proxies=self._proxy or None)
            self._http_client = client
        return client

    async def ensure_authenticated(self) -> None:
        """执行登录并保存 Cookie（幂等）。已登录则跳过。"""
        if self._authed or not self._login_cfg:
            return

        client = await self._ensure_client()
        cfg = self._login_cfg
        url = self.base_url + cfg["url"]
        method = str(cfg.get("method", "POST")).upper()
        headers = dict(cfg.get("headers", {}))

        from novel.core.middleware import Request

        request = Request(
            url=url,
            method=method,
            headers=headers,
            data=cfg.get("form") or cfg.get("data"),
            json_body=cfg.get("json"),
            cookies=self._cookies,
        )
        response = await client.fetch(request)
        if response.cookies:
            self._cookies.update(response.cookies)
        self._authed = True
        logger.info(f"[{self.site_name}] 登录完成 (status={response.status_code})")

    async def fetch_text(self, url: str, params: dict | None = None) -> str:
        """抓取 URL 并解码为文本（支持登录态 Cookie 与代理）。

        登录态 Cookie 随请求注入；解码逻辑统一交给基类（注入 fetcher 时走
        EncodingMiddleware，回退路径走三级编码检测）。
        """
        await self.ensure_authenticated()
        return await super().fetch_text(url, params, cookies=self._cookies)
