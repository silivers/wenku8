"""内容去重管道"""

from __future__ import annotations

import hashlib

from loguru import logger

from novel.core.models import ChapterContent
from novel.core.pipeline import Pipeline, PipelineContext


class DeduplicationPipeline(Pipeline):
    """基于内容哈希（SHA-256）的精确去重。

    仅在同一小说内去重，避免不同小说的相同错误页/版权声明互相误判。
    """

    def __init__(self, storage=None):
        self._storage = storage

    async def process_item(
        self, item: ChapterContent, context: PipelineContext
    ) -> ChapterContent | None:
        content_bytes = item.content.encode("utf-8")
        content_hash = hashlib.sha256(content_bytes).hexdigest()

        storage = self._storage or context.storage
        if storage and await storage.content_hash_exists(content_hash, context.novel_id):
            logger.info(f"重复内容已跳过: {item.chapter_id} (hash={content_hash[:12]}...)")
            return None  # 丢弃

        item.content_hash = content_hash
        return item
