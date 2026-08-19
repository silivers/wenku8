"""异步爬虫引擎核心"""

from __future__ import annotations

import asyncio
import hashlib
import random
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

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
    NovelInfo,
)
from novel.core.pipeline import Pipeline, PipelineContext
from novel.core.paths import COVERS_DIR, IMAGES_DIR
from novel.utils.text import safe_path_segment
from novel.core.storage import StorageBackend
from novel.http.client import AsyncHTTPClient


class RateLimiter:
    """请求限速：间隔 + 随机抖动"""

    def __init__(self, interval: float = 0.1):
        self._interval = interval
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            jitter = random.uniform(0, self._interval * 0.3)
            wait_time = self._interval + jitter
            elapsed = time.monotonic() - self._last_request
            if elapsed < wait_time:
                await asyncio.sleep(wait_time - elapsed)
            self._last_request = time.monotonic()


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
        chapter_range: tuple[int, int] | None = None,
        max_retries: int = 3,
        resume_task_id: str | None = None,
        image_concurrency: int = 4,
    ):
        self.adapter = adapter
        self.storage = storage
        self.http_client = http_client
        self.request_mw = request_middlewares or []
        self.response_mw = response_middlewares or []
        self.pipelines = pipelines or []
        self.semaphore = asyncio.Semaphore(max_concurrency)

        # 让站点声明的限速真正生效：把 RateLimitMiddleware 的间隔抬高到
        # 至少 adapter.get_rate_limit()（修复 M1：wenku8 的 5.0s 此前被 3.0s 覆盖）
        site_rate = adapter.get_rate_limit()
        for mw in self.request_mw:
            if isinstance(mw, RateLimitMiddleware):
                mw.ensure_interval_at_least(site_rate)

        # 插图限速：插图走 http_client.fetch_bytes，不经中间件链，单独限速。
        # 取值优先级：用户 --delay > 站点声明限速（避免被全局默认 3.0s 覆盖）。
        image_interval = rate_limit if rate_limit is not None else site_rate
        self.rate_limiter = RateLimiter(image_interval)

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

        self.chapter_range = chapter_range
        self.image_concurrency = max(1, image_concurrency)
        self.max_retries = max_retries
        self._cancelled = False
        self._takedown_detected = False
        self._resume_task_id = resume_task_id
        self._task_id: str | None = None
        from novel.engine.state import TaskManager

        self.task_manager = TaskManager(self.storage)

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

    # ---- 任务状态（断点续爬）----

    async def _ensure_task(self, novel_id: str, site: str, total: int) -> None:
        """确保存在爬取任务记录。

        - 若指定了 --resume 且任务存在，则复用该任务（状态置为 running）。
        - 否则创建新任务。
        已完成的章节由 storage.chapter_exists 在后续流程中自动跳过。
        """
        if self._resume_task_id:
            state = await self.storage.load_state(self._resume_task_id)
            if state is not None:
                self._task_id = self._resume_task_id
                await self.task_manager.update_task(
                    self._task_id, status="running", total=total
                )
                return
            logger.warning(
                f"未找到续爬任务 {self._resume_task_id}，将创建新任务"
            )

        self._task_id = await self.task_manager.create_task(novel_id, site)
        await self.task_manager.update_task(
            self._task_id, status="running", total=total
        )

    async def _update_task(
        self,
        status: str | None = None,
        crawled: int | None = None,
        failed: list[str] | None = None,
    ) -> None:
        """更新任务进度（任务未创建时静默跳过）。"""
        if self._task_id is None:
            return
        await self.task_manager.update_task(
            self._task_id, status=status, crawled=crawled, failed=failed
        )

    async def _check_pause(self) -> None:
        """若任务被标记为 paused，则停止当前爬取。"""
        if self._cancelled or not self._task_id:
            return
        try:
            state = await self.storage.load_state(self._task_id)
        except Exception:  # noqa: BLE001
            return
        if state and state.get("status") == "paused":
            self._cancelled = True
            logger.warning(f"任务 {self._task_id} 已被标记为暂停，停止爬取")

    async def crawl_novel(self, novel_id: str) -> CrawlResult:
        """爬取一本小说的完整流程。"""
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

        # 3. 章节范围过滤
        if self.chapter_range:
            start, end = self.chapter_range
            chapters = chapters[start - 1 : end]
            logger.info(f"章节范围过滤: 第{start}-{end}章，剩余 {len(chapters)} 章")

        # 任务状态记录（支持断点续爬，--resume 复用已有任务）
        await self._ensure_task(novel_id, self.adapter.site_name, total=len(chapters))

        # 4. 过滤已爬取的章节（单次批量 IN 查询，避免 N+1，修复 M2）
        chapter_ids = [ch.chapter_id for ch in chapters]
        existing_ids = await self.storage.filter_existing_chapters(
            chapter_ids, self.adapter.site_name
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

        # 章节写入一次性提交（save_chapter 不再逐条 commit，修复 M3）
        await self.storage.commit()

        # 6. 下载封面
        try:
            cover = await self.adapter.get_cover(novel_id)
            if cover:
                file_hash = hashlib.sha256(cover).hexdigest()
                cover_path = str(COVERS_DIR / f"{novel_internal_id}.jpg")
                COVERS_DIR.mkdir(parents=True, exist_ok=True)
                Path(cover_path).write_bytes(cover)
                await self.storage.save_image(
                    novel_internal_id, None, "cover",
                    novel_info.cover_url or "", cover_path,
                    file_hash=file_hash,
                )
                logger.info("封面下载完成")
            # 封面写入提交（save_image 不再逐条 commit，修复 M3）
            await self.storage.commit()
        except Exception as e:
            logger.warning(f"封面下载失败: {e}")

        logger.info(
            f"爬取完成: 成功 {len(results)} 章, 失败 {len(failed)} 章, 跳过 {len(skipped)} 章"
        )

        # 更新任务状态为完成
        await self._update_task(
            status="completed", crawled=len(results), failed=failed
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
        # 检查任务是否被外部暂停（如 `tasks pause` 命令）
        await self._check_pause()
        if self._cancelled:
            logger.info(f"已取消，跳过: {chapter.title}")
            return None

        if self._takedown_detected:
            return None

        async with self.semaphore:
            await self._check_pause()
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

                # 下载章节插图
                if content.images:
                    content = await self._download_chapter_images(
                        content, novel_internal_id
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
                    f"{f', {len(content.images)} 图' if content.images else ''})"
                )
                return content

            except Exception as e:
                # 抓取失败 / 解析异常：向上传播，由 crawl_novel 的 gather 计入 failed
                logger.error(f"章节爬取失败 [{chapter.chapter_id}]: {e}")
                raise

    async def _download_chapter_images(
        self, content: ChapterContent, novel_internal_id: str
    ) -> ChapterContent:
        """下载章节插图到本地，替换 HTML 中的远程 URL 为本地路径。

        多张插图并发下载（受 image_concurrency 信号量约束），
        单张图片之间仍受 rate_limiter 限速。
        """
        from novel.core.models import ImageRef

        if not content.images:
            return content

        # 净化 chapter_id 后再用于路径，防路径穿越（修复 M4）。
        # safe_path_segment 仅保留 [A-Za-z0-9_-]，不含任何路径分隔符，
        # 故拼接结果必然停留在 IMAGES_DIR/{novel_id}/ 之下。
        safe_id = safe_path_segment(content.chapter_id)
        img_dir = IMAGES_DIR / novel_internal_id / safe_id
        img_dir.mkdir(parents=True, exist_ok=True)

        sem = asyncio.Semaphore(self.image_concurrency)

        async def _download_one(img: ImageRef) -> tuple[ImageRef, str] | None:
            async with sem:
                try:
                    await self.rate_limiter.wait()
                    img_bytes = await self.http_client.fetch_bytes(img.url)
                    if not img_bytes:
                        logger.warning(f"图片下载为空: {img.url}")
                        return None

                    file_hash = hashlib.sha256(img_bytes).hexdigest()
                    ext = self._detect_image_ext(img_bytes, img.url)
                    filename = f"img_{img.index:04d}{ext}"
                    local_path = str(img_dir / filename)
                    Path(local_path).write_bytes(img_bytes)

                    epub_path = f"images/{safe_id}/{filename}"
                    await self.storage.save_image(
                        novel_internal_id=novel_internal_id,
                        chapter_internal_id=None,
                        resource_type="illustration",
                        remote_url=img.url,
                        local_path=local_path,
                        caption=img.caption,
                        file_hash=file_hash,
                        image_order=img.index,
                    )
                    logger.debug(f"图片已下载: {img.url} -> {local_path}")
                    return (img, epub_path)
                except Exception as e:
                    logger.warning(f"图片下载失败 [{img.url}]: {e}")
                    return None

        results = await asyncio.gather(
            *[_download_one(img) for img in content.images]
        )

        downloaded: list[ImageRef] = []
        local_paths: dict[str, str] = {}
        for r in results:
            if r:
                img, epub_path = r
                local_paths[img.url] = epub_path
                downloaded.append(img)

        if local_paths and content.content:
            for remote_url, local_url in local_paths.items():
                content.content = content.content.replace(remote_url, local_url)

        content.images = downloaded
        return content

    @staticmethod
    def _detect_image_ext(data: bytes, url: str) -> str:
        """根据文件头判断图片扩展名。"""
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return ".png"
        if data[:3] == b"\xff\xd8\xff":
            return ".jpg"
        if data[:4] == b"GIF8":
            return ".gif"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return ".webp"
        # 从 URL 推断
        for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
            if ext in url.lower():
                return ext
        return ".jpg"

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
