"""异步爬虫引擎核心"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.exceptions import FetchError
from novel.core.fetcher import Fetcher
from novel.core.middleware import (
    RequestMiddleware,
    ResponseMiddleware,
)
from novel.middlewares.rate_limit import RateLimitMiddleware
from novel.middlewares.retry import RetryMiddleware
from novel.core.models import (
    ChapterContent,
    ChapterInfo,
    CrawlResult,
    ImageRef,
    NovelInfo,
)
from novel.core.pipeline import Pipeline, PipelineContext
from novel.core.storage import StorageBackend
from novel.http.client import AsyncHTTPClient


class CrawlerEngine:
    """异步爬虫引擎。

    引擎不知道任何站点细节，所有站点知识封装在适配器中。
    引擎负责：调度、中间件链执行、并发控制、管道执行、存储。
    """

    def __init__(
        self,
        adapter: BaseAdapter,
        storage: StorageBackend,
        http_client: AsyncHTTPClient,
        request_middlewares: list[RequestMiddleware] | None = None,
        response_middlewares: list[ResponseMiddleware] | None = None,
        pipelines: list[Pipeline] | None = None,
        max_concurrency: int = 8,
        rate_limit: float | None = None,
        max_retries: int = 3,
    ):
        self.adapter = adapter
        self.storage = storage
        self.http_client = http_client
        self.request_mw = request_middlewares or []
        self.response_mw = response_middlewares or []
        self.pipelines = pipelines or []
        self.semaphore = asyncio.Semaphore(max_concurrency)

        # 正文请求限速：取 config 值（crawl.rate_limit）与站点声明（adapter
        # get_rate_limit）的较大者，把 RateLimitMiddleware 间隔抬高到该值——
        # 保证配置是主控、站点声明是合规下限（修复 M1：wenku8 的 5.0s 此前
        # 曾被中间件默认 1.0s 覆盖）。
        effective_rate = max(adapter.get_rate_limit(), rate_limit or 0)
        for mw in self.request_mw:
            if isinstance(mw, RateLimitMiddleware):
                mw.ensure_interval_at_least(effective_rate)

        # 统一中间件抓取器：注入适配器，使其所有 fetch 都经过 UA 轮换 /
        # 限速 / 编码 / 校验 / 重试（修复 H1 双下载 + H2 中间件失效 + M5）。
        backoff_base = 1.0
        for mw in self.response_mw:
            if isinstance(mw, RetryMiddleware):
                backoff_base = mw.backoff_base
                break
        self.fetcher = Fetcher(
            self.http_client,
            self.request_mw,
            self.response_mw,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )
        adapter.set_fetcher(self.fetcher)

        self.max_retries = max_retries
        self._cancelled = False
        self._takedown_detected = False

    def cancel(self) -> None:
        """请求停止爬取"""
        self._cancelled = True
        logger.warning("收到停止信号，将在当前章节完成后停止")

    async def aclose(self) -> None:
        """释放中间件持有的资源。"""
        for mw in self.request_mw + self.response_mw:
            close = getattr(mw, "close", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001
                    pass

    async def crawl_novel(self, novel_id: str) -> CrawlResult:
        """爬取一本小说的完整流程（全站爬取的单本内部步骤）。"""
        self._cancelled = False
        self._takedown_detected = False

        # 1. 获取元信息
        logger.info(f"[{self.adapter.site_name}] 获取小说信息: {novel_id}")
        novel_info = await self.adapter.get_novel_info(novel_id)
        novel_internal_id = await self.storage.save_novel(novel_info, self.adapter.site_name)
        logger.info(f"小说: {novel_info.title} - {novel_info.author}")
        # 显示该书链接（适配器提供 get_novel_url 时打印，便于人工核对/访问）
        get_url = getattr(self.adapter, "get_novel_url", None)
        if get_url:
            try:
                logger.info(f"链接: {get_url(novel_id)}")
            except Exception:  # noqa: BLE001
                pass

        # 2. 获取章节目录
        logger.info("获取章节目录...")
        chapters = await self.adapter.get_chapter_list(novel_id)
        logger.info(f"共 {len(chapters)} 章")

        # 3. 过滤已爬取的章节（单次批量 IN 查询，避免 N+1，修复 M2；
        #    限定 novel_id 防跨书章节 ID 撞号误跳，修复 M6）
        chapter_ids = [ch.chapter_id for ch in chapters]
        existing_ids = await self.storage.filter_existing_chapters(
            chapter_ids, self.adapter.site_name, novel_internal_id
        )
        pending = [ch for ch in chapters if ch.chapter_id not in existing_ids]
        skipped = [ch.chapter_id for ch in chapters if ch.chapter_id in existing_ids]
        logger.info(f"待爬取 {len(pending)} 章，已跳过 {len(skipped)} 章")

        # 5. 并发下载章节
        results: list[ChapterContent] = []
        failed: list[str] = []

        if pending:
            tasks = [self._crawl_chapter(ch, novel_internal_id) for ch in pending]
            done = await asyncio.gather(*tasks, return_exceptions=True)

            for i, result in enumerate(done):
                if isinstance(result, Exception):
                    ch_id = pending[i].chapter_id
                    failed.append(ch_id)
                    logger.error(f"章节爬取失败 [{ch_id}]: {result}")
                elif result is not None:
                    results.append(result)

            # 下架检测跳过的章节计入 skipped
            if self._takedown_detected:
                takedown_skipped = len(pending) - len(results) - len(failed)
                if takedown_skipped > 0:
                    skipped.extend(
                        pending[i].chapter_id
                        for i in range(len(pending))
                        if not isinstance(done[i], Exception)
                        and done[i] is None
                    )
                    logger.warning(
                        f"因下架/版权删除，跳过 {takedown_skipped} 章"
                    )
                # 下架书目写入书籍状态：图片轮询阶段据此跳过该书的图片
                await self.storage.set_novel_status(
                    novel_internal_id, "takedown"
                )
                logger.warning(
                    f"书籍状态置为 takedown: {novel_info.title}"
                )

        # 章节写入一次性提交（save_chapter 不再逐条 commit，修复 M3）
        await self.storage.commit()

        # 6. 登记封面（不再即时下载）：与插图同走 images 表 pending，
        #    由图片轮询阶段统一下载。
        if novel_info.cover_url:
            await self.storage.save_images(
                novel_internal_id, None, "cover",
                [ImageRef(url=novel_info.cover_url, index=0)],
            )
            logger.info("封面已登记（待图片轮询下载）")

        logger.info(
            f"爬取完成: 成功 {len(results)} 章, 失败 {len(failed)} 章, 跳过 {len(skipped)} 章"
        )

        return CrawlResult(
            novel=novel_info,
            chapters=results,
            failed_chapters=failed,
            skipped_chapters=skipped,
        )

    async def _crawl_chapter(
        self, chapter: ChapterInfo, novel_internal_id: str
    ) -> ChapterContent | None:
        """爬取单个章节。

        适配器的 get_chapter_content 内部通过统一 Fetcher 抓取（含 UA 轮换 /
        限速 / 编码 / 校验 / 重试，且每个 URL 只抓一次），引擎不再自行预抓取，
        故 H1 双下载、H2 中间件失效一并消除。
        抓取失败（重试耗尽 / 被拦截）会抛 FetchError 并向上传播，由 gather 计入
        failed，不会当作正文入库（修复 M5）。
        """
        if self._cancelled:
            logger.info(f"已取消，跳过: {chapter.title}")
            return None

        if self._takedown_detected:
            return None

        async with self.semaphore:
            if self._cancelled:
                return None
            if self._takedown_detected:
                return None

            try:
                # 适配器经统一中间件链抓取并解析（多页合并也在适配器内完成）
                content = await self.adapter.get_chapter_content(
                    chapter.chapter_id, chapter.url
                )

                # 下架/版权删除检测
                if self.adapter.is_takedown(content.content):
                    self._takedown_detected = True
                    logger.warning(
                        f"检测到下架/版权删除页面，跳过剩余所有章节: {chapter.title}"
                    )
                    return None

                # 补充章节信息
                content.volume = chapter.volume
                if not content.word_count:
                    from novel.utils.text import count_text_length

                    content.word_count = count_text_length(content.content)

                # 数据处理管道
                ctx = PipelineContext(
                    site=self.adapter.site_name,
                    novel_id=novel_internal_id,
                    storage=self.storage,
                )
                for pipe in self.pipelines:
                    content = await pipe.process_item(content, ctx)
                    if content is None:
                        logger.info(f"章节被管道丢弃: {chapter.title}")
                        return None

                # 登记章节插图（不再即时下载）：写入 images 表 pending，
                # 由图片轮询阶段（images crawl）统一下载并回写 HTML
                if content.images:
                    await self.storage.save_images(
                        novel_internal_id, content.chapter_id,
                        "illustration", content.images,
                    )

                # 存储
                await self.storage.save_chapter(
                    content,
                    novel_internal_id,
                    self.adapter.site_name,
                    chapter_index=chapter.index,
                    volume=chapter.volume,
                )

                logger.info(
                    f"已爬取 [{chapter.index}] {chapter.title} "
                    f"({content.word_count} 字"
                    f"{f', 登记 {len(content.images)} 图' if content.images else ''})"
                )
                return content

            except Exception as e:
                # 抓取失败 / 解析异常：向上传播，由 crawl_novel 的 gather 计入 failed
                logger.error(f"章节爬取失败 [{chapter.chapter_id}]: {e}")
                raise

    async def crawl_site(
        self,
        max_novels: int | None = None,
        novel_filter: dict[str, Any] | None = None,
        write_last_id: bool = True,
    ) -> list[CrawlResult]:
        """全站爬取：遍历站点所有小说并逐一爬取。

        Args:
            max_novels: 最多爬取多少本，None 表示不限制
            novel_filter: 过滤条件（如 categories / start_id / end_id），传给适配器
            write_last_id: 遍历结束后是否把最后一个有效编号写入 site_meta
                           作为站点精确数量。分块领取模式下 worker 不走
                           crawl_site（末尾由确认 worker 显式 set_last_novel_id），
                           本参数仅对单线程 crawl-all 生效，传 True
                           交给 coordinator（discover 模式）或上层统一处理。
        Returns:
            每本小说的爬取结果列表
        """
        self._cancelled = False
        results: list[CrawlResult] = []
        novel_count = 0

        site = self.adapter.site_name

        # —— 续爬起点：CLI --start-id > 已存精确数量 > 1 ——
        cli_start = int(novel_filter["start_id"]) if (
            novel_filter and "start_id" in novel_filter
        ) else None
        stored = await self.storage.get_last_novel_id(site)
        if cli_start is not None:
            start_id = cli_start
        elif stored:
            start_id = stored + 1
        else:
            start_id = 1

        if stored:
            logger.info(
                f"[{site}] 续爬模式：从编号 {start_id} 继续"
                f"（上次精确数量 {stored}）"
            )
        else:
            logger.info(f"[{site}] 全站首次爬取：从编号 {start_id} 开始")

        effective_filter = dict(novel_filter or {})
        effective_filter["start_id"] = start_id

        logger.info(f"[{site}] 开始全站爬取...")

        last_id = 0  # 最后一个有效小说编号，遍历结束后即精确数量
        # 缺章/整本失败的书：收尾前重扫补缺（方案 A，见下）
        incomplete: list[tuple[str, str]] = []
        async for novel_info in self.adapter.iter_all_novels(effective_filter):
            if self._cancelled:
                logger.warning("全站爬取已取消")
                break

            if max_novels and novel_count >= max_novels:
                logger.info(f"已达到上限 {max_novels} 本，停止爬取")
                break

            novel_count += 1
            last_id = int(novel_info.site_id)
            logger.info(
                f"全站爬取 [{novel_count}] {novel_info.title} (ID: {novel_info.site_id})"
            )

            try:
                result = await self.crawl_novel(novel_info.site_id)
                results.append(result)
                if result.failed_chapters:
                    # 章节级失败：书已计入进度但内容不完整，加入待补清单
                    incomplete.append((novel_info.site_id, novel_info.title))
            except Exception as e:
                logger.error(
                    f"全站爬取失败 [{novel_info.site_id} {novel_info.title}]: {e}"
                )
                results.append(CrawlResult(
                    novel=novel_info,
                    chapters=[],
                    failed_chapters=[],
                    skipped_chapters=[],
                ))
                incomplete.append((novel_info.site_id, novel_info.title))

        # —— 方案 A：收尾前重扫缺章书（crawl_novel 章节差集幂等，只补缺章）——
        # 防止「爬过但没爬完的书」被计入进度后，下次续爬不再经过它而缺章永久丢失。
        still_incomplete: list[tuple[str, str]] = []
        if incomplete:
            logger.warning(
                f"[{site}] {len(incomplete)} 本书缺章/失败，收尾前重扫补缺: "
                f"{[sid for sid, _ in incomplete]}"
            )
            for sid, title in incomplete:
                try:
                    r = await self.crawl_novel(sid)
                    if r.failed_chapters:
                        still_incomplete.append((sid, title))
                except Exception as e:  # noqa: BLE001
                    logger.error(f"重扫仍失败 [{sid} {title}]: {e}")
                    still_incomplete.append((sid, title))
            if still_incomplete:
                # 剔除：续爬起点回退到「仍失败的最小书号 - 1」，
                # 下次从该书续爬会重新经过它（网络恢复后自动补上）
                min_failed = min(int(sid) for sid, _ in still_incomplete)
                last_id = min(last_id, min_failed - 1)
                logger.warning(
                    f"[{site}] {len(still_incomplete)} 本书重扫后仍缺章，"
                    f"续爬起点回退到 {last_id}: {[sid for sid, _ in still_incomplete]}"
                )

        # 遍历结束：保存精确数量（最后一个有内容的有效编号），供下次续爬直接读取
        if last_id and write_last_id:
            await self.storage.set_last_novel_id(site, last_id)
            logger.info(
                f"[{site}] 全站遍历结束，已保存精确数量: {last_id}"
                + (f"（续爬起点 {start_id}）" if stored else "（首次爬取）")
            )
        elif last_id and not write_last_id:
            logger.debug(
                f"[{site}] 分片模式：跳过 site_meta 回写（由上层统一处理）"
            )

        logger.info(
            f"全站爬取完成: 共 {len(results)} 本, "
            f"成功 {sum(1 for r in results if r.chapters)}, "
            f"失败 {sum(1 for r in results if not r.chapters)}"
        )
        return results
