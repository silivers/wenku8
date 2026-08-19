"""wenku8.net 适配器

关键特性：
- HTTPS + GBK 编码
- 支持账号登录（login.php），登录态以 Cookie 形式贯穿全部请求
- 章节正文多页合并
- 插图提取（pic.777743.xyz CDN）
- 全站遍历采用 ID 遍历（列表页需登录）
- 5 秒限速
"""

from __future__ import annotations

import re
from typing import Any, AsyncIterator
from urllib.parse import urljoin

from lxml import html as lxml_html
from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.models import ChapterContent, ChapterInfo, ImageRef, NovelInfo


# 正文噪声（页眉 / 页脚 / 录入信息），爬取时从 div#content 中剔除
_AD_TEXT_PATTERNS = [
    re.compile(r"最新最全的日本动漫轻小说.*?为你一网打尽！"),
    re.compile(r"本文来自\s*轻小说文库", re.IGNORECASE),
    re.compile(r"轻小说文库\s*\(http://www\.wenku8\.com\)"),
    re.compile(r"台版\s*转自\s*天使动漫论坛"),
    re.compile(r"轻书架\s*[×xX]\s*天使动漫录入组"),
    re.compile(r"天使动漫论坛"),
    re.compile(r"图源\s*[:：].+"),
    re.compile(r"录入\s*[:：].+"),
    re.compile(r"\(?http://www\.wenku8\.com\)?"),
]


class Wenku8Adapter(BaseAdapter):
    """wenku8.net 站点适配器。

    站点结构（2026 实地验证）：
        小说详情:  https://www.wenku8.net/book/{id}.htm
        章节目录:  https://www.wenku8.net/novel/{id//1000}/{id}/index.htm
        章节正文:  https://www.wenku8.net/novel/{id//1000}/{id}/{chapter_href}.htm
        封面图片:  http://img.wenku8.com/image/{id//1000}/{id}/{id}s.jpg
        插图 CDN:  https://pic.777743.xyz/{id//1000}/{id}/{chapter_id}/{img_id}.jpg

    注意：章节目录按 ID 分桶存放（{id//1000}），早期代码误写成固定 /novel/0/
    仅对 id<1000 凑巧正确，id>=1000 会 404，这是历史「链接生成逻辑有误」的根因。
    列表/搜索页需登录，全站遍历采用 ID 递增方式。
    """

    @property
    def site_name(self) -> str:
        return "wenku8"

    @property
    def base_url(self) -> str:
        return "https://www.wenku8.net"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/login.php?do=submit"

    @property
    def encoding(self) -> str:
        return "gbk"

    def get_rate_limit(self) -> float:
        return 5.0

    def is_takedown(self, content: str) -> bool:
        """检测 wenku8 下架/版权删除页面。

        仅用精确的下架提示语判定。注意「本文来自 轻小说文库」是站点的
        标准页脚版权声明，出现在每一篇正常章节页，绝不能作为下架信号，
        否则会误杀全部正常章节（历史实现就曾因此把每章都判为下架而跳过）。
        """
        takedown_patterns = [
            "因版权问题，文库不再提供该小说的阅读",
            "不再提供该小说的阅读",
        ]
        return any(p in content for p in takedown_patterns)

    def __init__(self):
        self._http_client = None
        # 登录态 Cookie：login() 成功后填充，fetch_text / fetch_bytes 均会携带
        self._session_cookies: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 搜索（需登录，不支持）
    # ------------------------------------------------------------------

    async def search(self, keyword: str, page: int = 1) -> tuple[list[NovelInfo], bool]:
        """wenku8 搜索需登录，公开访问不可用。"""
        logger.warning("wenku8 搜索功能需要登录，当前不支持")
        return [], False

    # ------------------------------------------------------------------
    # 小说详情
    # ------------------------------------------------------------------

    def get_novel_url(self, novel_id: str | int) -> str:
        """返回小说详情页 URL（供爬取日志/输出显示该书链接）。"""
        return f"{self.base_url}/book/{novel_id}.htm"

    async def get_novel_info(self, novel_id: str) -> NovelInfo:
        """获取小说元信息。

        页面结构：详情信息在 table 的 td 中，格式为 "标签：值"。
        """
        url = f"{self.base_url}/book/{novel_id}.htm"
        html_text = await self.fetch_text(url)
        tree = lxml_html.fromstring(html_text)

        # 检测错误页
        title_tag = tree.xpath("//title/text()")
        if title_tag and ("出错" in title_tag[0] or "错误" in title_tag[0]):
            raise ValueError(f"小说 ID {novel_id} 不存在或已下架")

        # 标题：<title>文学少女 - 野村美月 - Fami通文库 - 轻小说文库</title>
        title = novel_id
        if title_tag:
            title = title_tag[0].split(" - ")[0].strip()

        # 遍历所有 td，按行解析 "标签：值" 格式
        author = ""
        status = ""
        description = ""
        categories: list[str] = []
        tags: list[str] = []
        last_updated = ""
        word_count = 0

        all_tds = tree.xpath("//td")
        for td in all_tds:
            # 按行拆分，每行可能是一个独立的 "标签：值" 字段
            for line in td.text_content().split("\n"):
                line = line.strip()
                if not line or "：" not in line:
                    continue

                label, _, value = line.partition("：")
                label = label.strip()
                value = value.strip()

                if label == "小说作者":
                    author = value
                elif label == "文章状态":
                    if "完结" in value:
                        status = "completed"
                    elif "连载" in value:
                        status = "ongoing"
                    else:
                        status = value
                elif label == "文库分类":
                    if value:
                        categories.append(value)
                elif label == "最后更新":
                    last_updated = value
                elif label == "全文长度":
                    digits = re.sub(r"[^\d]", "", value)
                    if digits:
                        word_count = int(digits)
                elif label == "作品Tags":
                    tags = [t.strip() for t in value.split() if t.strip()]
                elif label == "内容简介":
                    description = value

        # 封面 URL
        cover_url = self._build_cover_url(novel_id)

        return NovelInfo(
            site_id=novel_id,
            title=title,
            author=author,
            description=description,
            cover_url=cover_url,
            status=status,
            categories=categories,
            tags=tags,
            last_updated=last_updated,
            word_count=word_count,
        )

    # ------------------------------------------------------------------
    # 章节目录
    # ------------------------------------------------------------------

    def _build_chapter_index_url(self, novel_id: str | int) -> str:
        """构造章节目录 URL。

        关键：wenku8 按 ID 分桶存放，目录在 /novel/{id//1000}/{id}/index.htm，
        而非早期误写的固定 /novel/0/{id}/（仅 id<1000 凑巧正确）。
        """
        nid = int(novel_id)
        return f"{self.base_url}/novel/{nid // 1000}/{nid}/index.htm"

    async def get_chapter_list(self, novel_id: str) -> list[ChapterInfo]:
        """获取章节目录。

        页面结构：table[@class="css"] 内按行排列，
        卷标题行 td[@class="vcss"]，章节行 td//a[@href]。
        章节链接为相对路径（如 2.htm），需拼接 /novel/{id//1000}/{id}/ 前缀。
        """
        index_url = self._build_chapter_index_url(novel_id)
        html_text = await self.fetch_text(index_url)
        tree = lxml_html.fromstring(html_text)

        chapters: list[ChapterInfo] = []
        current_volume: str | None = None

        rows = tree.xpath('//table[@class="css"]//tr')
        if not rows:
            logger.warning(f"wenku8: 未找到章节表格 (novel_id={novel_id})")
            return chapters

        chapter_index = 0
        for row in rows:
            # 卷标题行
            vol_elem = row.xpath('.//td[@class="vcss"]')
            if vol_elem:
                current_volume = vol_elem[0].text_content().strip()
                continue

            # 章节链接行
            links = row.xpath('.//td//a[@href]')
            for link in links:
                href = link.get("href", "").strip()
                title = link.text_content().strip()
                if not href or not title:
                    continue
                # 跳过非 .htm 链接
                if not href.endswith(".htm"):
                    continue

                # 拼接完整 URL（urljoin 会替换 index.htm 为 2.htm）
                full_url = urljoin(index_url, href)
                # 从 href 提取章节 ID（如 2.htm -> 2）
                match = re.search(r"(\d+)\.htm", href)
                chapter_id = match.group(1) if match else href

                chapter_index += 1
                chapters.append(ChapterInfo(
                    chapter_id=chapter_id,
                    title=title,
                    volume=current_volume,
                    index=chapter_index,
                    url=full_url,
                ))

        logger.info(f"wenku8: 获取到 {len(chapters)} 章 (共 {len(set(c.volume for c in chapters if c.volume))} 卷)")
        return chapters

    # ------------------------------------------------------------------
    # 章节正文
    # ------------------------------------------------------------------

    async def get_chapter_content(
        self, chapter_id: str, url: str = ""
    ) -> ChapterContent:
        """获取章节正文（支持多页合并）。

        页面结构：
            标题: div#title
            正文: div#content
            下一页: a[contains(text(), "下一页")] href 为相对路径
            插图: div#content 内 img[src]
        """
        if not url:
            raise ValueError("get_chapter_content 需要 url 参数")

        pages_content: list[str] = []
        pages_images: list[ImageRef] = []
        title = ""
        page_url = url
        img_index = 0

        while True:
            html_text = await self.fetch_text(page_url)
            tree = lxml_html.fromstring(html_text)

            # 标题（仅第一页）
            if not title:
                title_elems = tree.xpath('//div[@id="title"]/text()')
                if title_elems:
                    title = title_elems[0].strip()
                if not title:
                    title_tag = tree.xpath("//title/text()")
                    if title_tag:
                        title = title_tag[0].split(" - ")[0].strip()

            # 正文
            content_elems = tree.xpath('//div[@id="content"]')
            if not content_elems:
                content_elems = tree.xpath('//td[@id="content"]')

            if content_elems:
                # 清理 script / 广告 div
                for elem in content_elems[0].xpath('.//script'):
                    elem.getparent().remove(elem)
                for elem in content_elems[0].xpath('.//div[contains(@class, "ad")]'):
                    elem.getparent().remove(elem)
                # 清理正文内的页眉/页脚/录入噪声（如「本文来自 轻小说文库」等）
                self._strip_ad_text(content_elems[0])

                page_html = lxml_html.tostring(
                    content_elems[0], encoding="unicode"
                )
                pages_content.append(page_html)

                # 提取插图
                for img in content_elems[0].xpath('.//img[@src]'):
                    src = img.get("src", "").strip()
                    if not src:
                        continue
                    if not src.startswith("http"):
                        src = urljoin(page_url, src)
                    pages_images.append(ImageRef(
                        url=src,
                        alt=img.get("alt", ""),
                        index=img_index,
                    ))
                    img_index += 1

            # 检查下一页（仅跟同章节分页，如 2_2.htm，不跟下一章 3.htm）
            next_page = tree.xpath('//a[contains(text(), "下一页")]')
            if next_page:
                next_href = next_page[0].get("href", "").strip()
                if next_href and next_href.endswith(".htm"):
                    # wenku8 多页格式: {chapter_id}_{page}.htm
                    # 只有 href 包含 "_" 才是同章分页
                    if "_" in next_href:
                        page_url = urljoin(page_url, next_href)
                        continue

            break

        merged_content = "\n".join(pages_content)

        # 字数统计
        from novel.utils.text import count_text_length

        word_count = count_text_length(merged_content)

        return ChapterContent(
            chapter_id=chapter_id,
            title=title or chapter_id,
            content=merged_content,
            content_format="html",
            images=pages_images,
            word_count=word_count,
            raw_encoding="gbk",
        )

    # ------------------------------------------------------------------
    # 正文噪声清理
    # ------------------------------------------------------------------

    def _strip_ad_text(self, elem) -> None:
        """从正文元素中剔除页眉/页脚/录入等噪声文本节点。

        这些噪声（如「本文来自 轻小说文库」「图源：夜殇」「录入：夜殇」）
        常以独立行出现在 div#content 内。这里遍历所有文本节点（.text / .tail）
        删除匹配片段并折叠残留空行，避免正文出现大段空白。
        注意：仅清理展示噪声，不影响 is_takedown 对下架页的判定
        （下架信号用精确短语单独判断）。
        """
        for node in elem.iter():
            for attr in ("text", "tail"):
                val = getattr(node, attr)
                if not val:
                    continue
                new = val
                for pat in _AD_TEXT_PATTERNS:
                    new = pat.sub("", new)
                if new != val:
                    # 折叠被删空后残留的连续空行
                    new = re.sub(r"\n[ \t]*\n", "\n", new)
                    setattr(node, attr, new)

    # ------------------------------------------------------------------
    # 封面
    # ------------------------------------------------------------------

    async def get_cover(self, novel_id: str) -> bytes | None:
        """下载封面图片。

        URL 规律: http://img.wenku8.com/image/{id//1000}/{id}/{id}s.jpg
        """
        cover_url = self._build_cover_url(novel_id)
        if not cover_url:
            return None

        if self._http_client is None:
            from novel.http.client import AsyncHTTPClient

            self._http_client = AsyncHTTPClient()

        try:
            return await self._http_client.fetch_bytes(cover_url)
        except Exception as e:
            logger.warning(f"封面下载失败 [{novel_id}]: {e}")
            return None

    # ------------------------------------------------------------------
    # 登录
    # ------------------------------------------------------------------

    async def login(self, username: str | None = None, password: str | None = None) -> bool:
        """登录 wenku8，使后续请求携带会话 Cookie（贯穿 crawl / 章节 / 封面 / 插图）。

        登录成功后把服务端下发的 Cookie 写入共享 http_client（set_cookies），
        并缓存到 self._session_cookies 由基类的 fetch_text 合并进每个请求，
        从而全链路（含不经中间件链的 fetch_bytes 封面/插图下载）都带登录态。

        Args:
            username / password: 账号密码；为空则跳过登录并返回 False。
        Returns:
            是否登录成功（以服务端下发用户态 Cookie 为判定）。
        """
        if not username or not password:
            logger.warning("wenku8: 未提供账号或密码，跳过登录")
            return False

        if self._http_client is None:
            from novel.http.client import AsyncHTTPClient

            self._http_client = AsyncHTTPClient()

        from novel.core.middleware import Request

        data = {
            "username": username,
            "password": password,
            "action": "login",
        }
        request = Request(
            url=self.login_url,
            method="POST",
            data=data,
            headers={"Referer": f"{self.base_url}/login.php"},
        )

        try:
            resp = await self._http_client.fetch(request)
        except Exception as e:  # noqa: BLE001
            logger.error(f"wenku8: 登录请求失败: {e}")
            return False

        # 提取并持久化会话 Cookie
        cookies = dict(resp.cookies or {})
        if cookies and self._http_client is not None:
            self._http_client.set_cookies(cookies)
        self._session_cookies = cookies

        # 成功判定：服务端下发了用户态 Cookie（jieqiUserInfo / jieqiUserId）
        ok = any(k.lower().startswith("jieqiuser") or k.lower() == "phpsessid"
                 for k in cookies)
        if ok:
            logger.info("wenku8: 登录成功（用户态 Cookie 已下发并写入会话）")
        else:
            logger.warning(
                "wenku8: 登录响应未包含用户态 Cookie，可能账号或密码错误"
            )
        return bool(ok)

    # ------------------------------------------------------------------
    # 全站遍历
    # ------------------------------------------------------------------

    # 连续多少个无效编号后判定已到站点末尾（停止遍历）。
    # 仅作兜底默认；引擎/CLI 会从 config `crawl.blank_limit` 读取后经
    # novel_filter 传入（配置优先，不在此写死）。
    CONSECUTIVE_SKIP_LIMIT = 5

    async def iter_all_novels(
        self, filter_params: dict[str, Any] | None = None
    ) -> AsyncIterator[NovelInfo]:
        """遍历 wenku8 全站小说（不设上限，由连续无效编号判定结尾）。

        wenku8 的列表页（articlelist.php / toplist.php / search.php）均需登录，
        因此采用 ID 递增遍历：从 start_id 开始递增，请求 book/{id}.htm。

        遍历规则（设计要点）：
        - 命中有效小说：yield 并清零连续无效计数；
        - 命中错误页（标题含「出错」「错误」）：跳过并累加连续无效计数，
          达到 blank_limit 即判定已到站点末尾，结束遍历；
        - 网络/解析异常：仅记录并继续，不计入连续无效计数，
          避免站点抖动误判为「到末尾」。

        遍历结束后，最后一个被 yield 的有效编号即站点精确小说数量，
        由引擎写入 site_meta，供下次续爬直接读取。

        Args:
            filter_params:
                start_id:    起始 ID（默认 1；续爬时由引擎填入，= 已存精确数量 + 1）
                end_id:      终止 ID（含）；仅内部 ID 分片并行遍历使用，
                             不通过 CLI 暴露。为 None 时保持「不设上限」语义。
                blank_limit: 连续空白判定末尾的阈值（config `crawl.blank_limit`，
                             默认 100；未传入时回退类常量兜底）。
        """
        filter_params = filter_params or {}
        novel_id = int(filter_params.get("start_id", 1))
        end_id = filter_params.get("end_id")
        if end_id is not None:
            end_id = int(end_id)
        blank_limit = int(filter_params.get("blank_limit", self.CONSECUTIVE_SKIP_LIMIT))
        consecutive = 0

        if end_id is not None:
            logger.info(f"wenku8: 扫描 ID 区间 [{novel_id}, {end_id}]")
        else:
            logger.info(f"wenku8: 全站 ID 遍历（从 {novel_id} 起，不设上限）")

        while True:
            # 带 end_id（分块领取 / 分段调用）时到达段尾即停止（不触发 100 连续无效判定）
            if end_id is not None and novel_id > end_id:
                logger.info(f"wenku8: 到达区间终点 {end_id}，结束本段扫描")
                break

            # —— 网络层：瞬断不影响末尾判定 ——
            try:
                url = f"{self.base_url}/book/{novel_id}.htm"
                html_text = await self.fetch_text(url)
            except Exception as e:
                logger.debug(f"wenku8: ID {novel_id} 请求异常({e})，继续")
                novel_id += 1
                continue

            # —— 页面层：错误页 vs 有效小说 ——
            tree = lxml_html.fromstring(html_text)
            title_tag = tree.xpath("//title/text()")

            if title_tag and ("出错" in title_tag[0] or "错误" in title_tag[0]):
                consecutive += 1
                if consecutive >= blank_limit:
                    logger.info(
                        f"wenku8: 连续 {consecutive} 个无效编号，"
                        f"判定已到站点末尾（最后有效 ID={novel_id - consecutive}）"
                    )
                    break
                novel_id += 1
                continue

            # 命中有效小说：清零连续无效计数
            consecutive = 0

            title = str(novel_id)
            if title_tag:
                title = title_tag[0].split(" - ")[0].strip()

            # 快速提取作者
            author = ""
            for td in tree.xpath("//td"):
                text = td.text_content().strip()
                if text.startswith("小说作者："):
                    author = text[len("小说作者："):].strip()
                    break

            yield NovelInfo(
                site_id=str(novel_id),
                title=title,
                author=author,
            )
            novel_id += 1

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _build_cover_url(self, novel_id: str | int) -> str:
        """构造封面图片 URL。

        规律: http://img.wenku8.com/image/{id//1000}/{id}/{id}s.jpg
        """
        nid = int(novel_id)
        prefix = nid // 1000
        return f"http://img.wenku8.com/image/{prefix}/{nid}/{nid}s.jpg"

    def supports_site_wide(self) -> bool:
        return True

    async def close(self) -> None:
        if self._http_client:
            await self._http_client.close()
            self._http_client = None
