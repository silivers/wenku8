"""内容去重管道测试（覆盖同小说内去重、跨小说不误判）。"""

from __future__ import annotations

import asyncio

from novel.core.models import ChapterContent
from novel.core.pipeline import PipelineContext
from novel.pipelines.deduplication import DeduplicationPipeline


class FakeStorage:
    def __init__(self):
        # novel_id -> set(content_hash)
        self.data: dict[str, set[str]] = {}

    async def content_hash_exists(self, content_hash: str, novel_id: str | None = None) -> bool:
        if novel_id is None:
            return any(content_hash in s for s in self.data.values())
        return novel_id in self.data and content_hash in self.data[novel_id]


def test_within_novel_dedup():
    async def _():
        storage = FakeStorage()
        pipe = DeduplicationPipeline(storage=storage)
        ctx = PipelineContext(site="t", novel_id="N1", storage=storage)

        c1 = ChapterContent(chapter_id="1", title="a", content="<p>same body</p>")
        r1 = await pipe.process_item(c1, ctx)
        assert r1 is not None and r1.content_hash

        # 同一小说内相同内容 -> 丢弃
        storage.data.setdefault("N1", set()).add(r1.content_hash)
        c2 = ChapterContent(chapter_id="2", title="b", content="<p>same body</p>")
        r2 = await pipe.process_item(c2, ctx)
        assert r2 is None

    asyncio.run(_())


def test_cross_novel_not_deduped():
    async def _():
        storage = FakeStorage()
        pipe = DeduplicationPipeline(storage=storage)
        ctx1 = PipelineContext(site="t", novel_id="N1", storage=storage)
        ctx2 = PipelineContext(site="t", novel_id="N2", storage=storage)

        c1 = ChapterContent(chapter_id="1", title="a", content="<p>same body</p>")
        r1 = await pipe.process_item(c1, ctx1)
        assert r1 is not None
        storage.data.setdefault("N1", set()).add(r1.content_hash)

        # 不同小说相同内容 -> 不丢弃
        c3 = ChapterContent(chapter_id="3", title="c", content="<p>same body</p>")
        r3 = await pipe.process_item(c3, ctx2)
        assert r3 is not None

    asyncio.run(_())
