"""文本归一化管道"""

from __future__ import annotations

import html as html_module

from novel.core.models import ChapterContent
from novel.core.pipeline import Pipeline, PipelineContext
from novel.utils.text import normalize_whitespace, normalize_width


class TextNormalizerPipeline(Pipeline):
    """文本归一化：HTML 实体反转义、空白归一化、全角半角统一。"""

    def __init__(self, normalize_width: bool = False, unescape_entities: bool = True):
        self.normalize_width = normalize_width
        self.unescape_entities = unescape_entities

    async def process_item(
        self, item: ChapterContent, context: PipelineContext
    ) -> ChapterContent | None:
        text = item.content

        if self.unescape_entities:
            text = html_module.unescape(text)

        text = normalize_whitespace(text)

        if self.normalize_width:
            text = normalize_width(text)

        item.content = text
        return item
