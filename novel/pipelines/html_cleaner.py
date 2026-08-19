"""HTML 清洗管道"""

from __future__ import annotations

from loguru import logger

from novel.core.models import ChapterContent
from novel.core.pipeline import Pipeline, PipelineContext


class HTMLCleanerPipeline(Pipeline):
    """清洗 HTML 内容：移除广告、脚本、样式等无用元素。"""

    DEFAULT_REMOVE_SELECTORS = [
        "//*[contains(@class, 'ad')]",
        "//*[contains(@class, 'advert')]",
        "//*[contains(@class, 'recommend')]",
        "//*[contains(@class, 'share')]",
        "//*[contains(@class, 'comment')]",
        "//script",
        "//style",
        "//iframe",
        "//ins",
        "//noscript",
        "//comment()",
    ]

    def __init__(self, remove_selectors: list[str] | None = None):
        self.remove_selectors = remove_selectors or self.DEFAULT_REMOVE_SELECTORS

    async def process_item(
        self, item: ChapterContent, context: PipelineContext
    ) -> ChapterContent | None:
        if item.content_format != "html" or not item.content:
            return item

        try:
            from lxml import html as lxml_html

            tree = lxml_html.fromstring(item.content)

            # 移除指定元素
            for selector in self.remove_selectors:
                for elem in tree.xpath(selector):
                    parent = elem.getparent()
                    if parent is not None:
                        parent.remove(elem)

            # 清理属性，只保留 href/src/alt
            for elem in tree.iter():
                for attr in list(elem.attrib):
                    if attr not in ("href", "src", "alt", "class"):
                        del elem.attrib[attr]

            item.content = lxml_html.tostring(
                tree, encoding="unicode", pretty_print=True
            )
        except Exception as e:
            logger.warning(f"HTML 清洗失败 [{item.chapter_id}]: {e}")

        return item
