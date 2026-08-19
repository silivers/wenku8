"""图片独立爬取引擎：轮询遍历直到全部成功。

与文本爬取（CrawlerEngine）完全解耦：
- 文本阶段只把图片登记进 images 表（status=pending），HTML 保留远程 URL；
- 本模块负责轮询下载：每轮全站遍历所有「有待下载图片且 book_status='active'」
  的小说，失败图片 attempt_count+1 留待下一轮，直到全部 downloaded；
- attempt_count 达到 max_rounds 仍失败 → skipped（死链兜底，保证收敛）。

线程安全：与 CrawlerEngine 同构——每个 worker 在自己的事件循环内构建独立的
ImageCrawler 实例（独立 RateLimiter = 独立限速预算），跨线程只共享 WAL 库。
"""

from __future__ import annotations

import asyncio
import hashlib
import queue
import random
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from novel.core.exceptions import FetchError
from novel.core.paths import COVERS_DIR, IMAGES_DIR
from novel.core.storage import StorageBackend
from novel.http.client import AsyncHTTPClient
from novel.storage.sqlite_backend import SQLiteBackend
from novel.utils.text import safe_path_segment


class RateLimiter:
    """请求限速：间隔 + 随机抖动（自 crawler.py 迁入，插图阶段专用）。"""

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


@dataclass
class ImageCrawlReport:
    """一轮图片爬取的汇总报告。"""

    rounds: int = 0        # 实际执行的轮数
    novels: int = 0        # 参与下载的书目数（含多轮重复）
    images_ok: int = 0     # 本轮成功下载数
    images_failed: int = 0  # 本轮失败数（累计 attempt_count+1）
    skipped: int = 0       # 兜底置 skipped 数（死链）
    remaining: int = 0     # 结束后仍未成功的图片数（应为 0，除非被跳过）


class ImageCrawler:
    """图片轮询爬取引擎。

    Args:
        storage: 存储后端（images 表状态机）
        http_client: 直连下载客户端（fetch_bytes；登录 Cookie 已随会话注入）
        image_concurrency: 单本小说内图片并发下载数
        interval: 图片请求间隔（秒）。图片走 CDN 域名，与正文 5s 限速无关，
                  可放宽；wenku8 插图 CDN 建议 1.0s 起步。
        max_rounds: 单张图片的最大尝试轮数，超过置 skipped（死链兜底）。
    """

    def __init__(
        self,
        storage: StorageBackend,
        http_client: AsyncHTTPClient,
        *,
        image_concurrency: int = 4,
        interval: float = 1.0,
        max_rounds: int = 10,
    ):
        self.storage = storage
        self.http_client = http_client
        self.image_concurrency = max(1, image_concurrency)
        self.rate_limiter = RateLimiter(max(0.1, interval))
        self.max_rounds = max(1, max_rounds)

    async def run(self, site: str) -> ImageCrawlReport:
        """轮询遍历：一轮全站 → 失败书下一轮，直到无 pending/failed 图片。

        Returns:
            汇总报告（rounds / novels / images_ok / images_failed / skipped）。
        """
        report = ImageCrawlReport()
        while True:
            novels = await self.storage.get_novels_with_pending_images(
                site, max_attempts=self.max_rounds
            )
            if not novels:
                break
            report.rounds += 1
            logger.info(
                f"[{site}] 图片轮询第 {report.rounds} 轮：{len(novels)} 本书待下载"
            )
            for nv in novels:
                report.novels += 1
                ok, fail = await self._crawl_novel_images(nv["id"])
                report.images_ok += ok
                report.images_failed += fail

        # 死链兜底：attempt_count>=max_rounds 仍失败的置 skipped，保证收敛
        report.skipped = await self.storage.skip_exhausted_images(self.max_rounds)
        if report.skipped:
            logger.warning(f"[{site}] {report.skipped} 张图片重试耗尽，置为 skipped")
        else:
            logger.info(f"[{site}] 图片全部下载成功（或已跳过），轮询结束")

        stats = await self.storage.get_image_stats(site)
        report.remaining = sum(
            stats["status_counts"].get(s, 0)
            for s in ("pending", "failed")
        )
        return report

    async def _crawl_novel_images(self, novel_internal_id: str) -> tuple[int, int]:
        """下载一本书的全部待下载图片。返回 (成功数, 失败数)。"""
        rows = await self.storage.get_pending_images(novel_internal_id)
        if not rows:
            return 0, 0
        sem = asyncio.Semaphore(self.image_concurrency)

        async def _one(row: dict) -> bool:
            async with sem:
                return await self._download_one(row)

        results = await asyncio.gather(*[_one(r) for r in rows])
        return sum(1 for ok in results if ok), sum(1 for ok in results if not ok)

    async def _download_one(self, row: dict) -> bool:
        """下载单张图片：成功→mark_image_downloaded + 回写；失败→mark_image_failed。"""
        try:
            await self.rate_limiter.wait()
            data = await self.http_client.fetch_bytes(row["remote_url"])
            if not data:
                raise FetchError("empty body")

            local_path, epub_rel, file_hash = self._write_file(row, data)
            await self.storage.mark_image_downloaded(
                row["id"], local_path, file_hash
            )

            if row["resource_type"] == "illustration":
                # 回写 HTML：把远程 URL 替换为本地相对路径（按 novel_id 全量
                # REPLACE，幂等；同一 URL 跨章出现也一并覆盖）
                await self.storage.rewrite_chapter_image_url(
                    row["novel_id"], row["remote_url"], epub_rel
                )
            else:  # cover：记录本地路径到 novels.cover_local
                await self.storage.set_novel_cover(row["novel_id"], local_path)

            logger.debug(
                f"图片已下载 [{row['resource_type']}]: "
                f"{row['remote_url']} -> {local_path}"
            )
            return True
        except Exception as e:  # noqa: BLE001
            await self.storage.mark_image_failed(row["id"], str(e))
            logger.warning(
                f"图片下载失败 (attempt {row['attempt_count'] + 1}/"
                f"{self.max_rounds}) [{row['remote_url']}]: {e}"
            )
            return False

    def _write_file(
        self, row: dict, data: bytes
    ) -> tuple[str, str, str]:
        """落盘图片并返回 (本地绝对路径, EPUB 相对路径, sha256)。

        - 插图：data/images/{novel_id}/{safe_chapter_id}/img_{order:04d}{ext}
          epub_rel = images/{safe_chapter_id}/{filename}（与 EPUB 导出器约定一致）
        - 封面：data/covers/{novel_id}.jpg，epub_rel 为空
        """
        file_hash = hashlib.sha256(data).hexdigest()
        ext = self._detect_image_ext(data, row["remote_url"])

        if row["resource_type"] == "cover" or not row["chapter_site_id"]:
            # 封面固定 .jpg：与 EPUB 导出器 _get_cover_path 的约定一致
            # （data/covers/{novel_id}.jpg），保持旧行为不变。
            COVERS_DIR.mkdir(parents=True, exist_ok=True)
            local_path = str(COVERS_DIR / f"{row['novel_id']}.jpg")
            Path(local_path).write_bytes(data)
            return local_path, "", file_hash

        # 净化 chapter_site_id 后再用于路径，防路径穿越（M4 语义保留）：
        # safe_path_segment 仅保留 [A-Za-z0-9_-]，不含路径分隔符，
        # 拼接结果必然停留在 IMAGES_DIR/{novel_id}/ 之下。
        safe_id = safe_path_segment(row["chapter_site_id"])
        img_dir = IMAGES_DIR / row["novel_id"] / safe_id
        img_dir.mkdir(parents=True, exist_ok=True)
        filename = f"img_{int(row['image_order'] or 0):04d}{ext}"
        local_path = str(img_dir / filename)
        Path(local_path).write_bytes(data)
        return local_path, f"images/{safe_id}/{filename}", file_hash

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


class ImageWorkerPool:
    """多线程图片轮询池（二期：复用 worker_pool.py 的线程模型）。

    轮次天然是 barrier：
    - 每轮由主线程查「待下载书目」列表（get_novels_with_pending_images，
      只含 book_status='active' 且 attempt<max_rounds 的书），把 novel_id 入队；
    - N 个 worker 线程各自在自己的事件循环里构建【私有】ImageCrawler
      （独立 RateLimiter = 独立限速预算，≈ N× 吞吐，与文本爬取同构）；
    - 队列排空即本轮结束（queue.get_nowait 原子领取，无需毒丸），线程退出；
    - 主线程再查下一轮——失败图片 attempt_count 已 +1，天然收敛。

    线程安全红线与 worker_pool.py 一致：跨线程只传 novel_id 标量、
    每线程独立 storage/http_client 实例、唯一共享是 WAL 库与 queue。
    """

    def __init__(
        self,
        config,
        site: str,
        db_path: str,
        *,
        workers: int = 2,
        image_concurrency: int = 4,
        interval: float = 1.0,
        max_rounds: int = 10,
    ):
        from novel.config import AppConfig

        self.config: AppConfig = config
        self.site = site
        self.db_path = db_path
        self.workers = max(2, int(workers))
        self.image_concurrency = image_concurrency
        self.interval = max(0.1, interval)
        self.max_rounds = max(1, max_rounds)

    async def arun(self) -> ImageCrawlReport:
        """轮询遍历（多线程版）。"""
        report = ImageCrawlReport()
        loop = asyncio.get_event_loop()
        while True:
            # 主线程查询本轮待下载书目（短连接，查完即关）
            storage = SQLiteBackend(self.db_path)
            await storage.init()
            try:
                novels = await storage.get_novels_with_pending_images(
                    self.site, max_attempts=self.max_rounds
                )
            finally:
                await storage.close()
            if not novels:
                break
            report.rounds += 1
            logger.info(
                f"[{self.site}] 图片轮询第 {report.rounds} 轮："
                f"{len(novels)} 本书待下载（{self.workers} worker）"
            )

            task_q: "queue.Queue[str | None]" = queue.Queue()
            result_q: "queue.Queue[tuple[int, int]]" = queue.Queue()
            for nv in novels:
                task_q.put(nv["id"])

            threads = [
                threading.Thread(
                    target=self._worker_loop,
                    args=(task_q, result_q),
                    daemon=True,
                )
                for _ in range(self.workers)
            ]
            for t in threads:
                t.start()
            await loop.run_in_executor(None, self._join_all, threads)

            report.novels += len(novels)
            while not result_q.empty():
                ok, fail = result_q.get_nowait()
                report.images_ok += ok
                report.images_failed += fail

        # 死链兜底：attempt_count>=max_rounds 仍失败的置 skipped
        storage = SQLiteBackend(self.db_path)
        await storage.init()
        try:
            report.skipped = await storage.skip_exhausted_images(self.max_rounds)
            stats = await storage.get_image_stats(self.site)
            report.remaining = sum(
                stats["status_counts"].get(s, 0)
                for s in ("pending", "failed")
            )
        finally:
            await storage.close()
        if report.skipped:
            logger.warning(
                f"[{self.site}] {report.skipped} 张图片重试耗尽，置为 skipped"
            )
        return report

    def _worker_loop(
        self,
        task_q: "queue.Queue[str | None]",
        result_q: "queue.Queue[tuple[int, int]]",
    ) -> None:
        """worker 线程：私有事件循环 + 私有 ImageCrawler，消费队列到空为止。"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        ok = fail = 0
        try:
            crawler, storage, http_client = loop.run_until_complete(
                self._build_crawler()
            )
            try:
                while True:
                    try:
                        nid = task_q.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        o, f = loop.run_until_complete(
                            crawler._crawl_novel_images(nid)
                        )
                        ok += o
                        fail += f
                    except Exception as e:  # noqa: BLE001
                        logger.error(f"图片 worker 处理失败 [{nid}]: {e}")
                        fail += 1
            finally:
                loop.run_until_complete(self._safe_close(storage, http_client))
        finally:
            result_q.put((ok, fail))
            loop.close()

    async def _build_crawler(self):
        """在线程自己的事件循环内构建【私有】ImageCrawler。"""
        from novel.http.client import AsyncHTTPClient
        from novel.storage.sqlite_backend import SQLiteBackend

        storage = SQLiteBackend(self.db_path)
        http_client = AsyncHTTPClient(
            timeout_connect=self.config.http.timeout_connect,
            timeout_read=self.config.http.timeout_read,
            follow_redirects=self.config.http.follow_redirects,
            proxies=self.config.http.proxy or None,
        )
        await storage.init()
        crawler = ImageCrawler(
            storage=storage,
            http_client=http_client,
            image_concurrency=self.image_concurrency,
            interval=self.interval,
            max_rounds=self.max_rounds,
        )
        return crawler, storage, http_client

    @staticmethod
    def _join_all(threads: list[threading.Thread]) -> None:
        for t in threads:
            t.join()

    async def _safe_close(self, storage, http_client) -> None:
        try:
            await http_client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await storage.close()
        except Exception:  # noqa: BLE001
            pass
