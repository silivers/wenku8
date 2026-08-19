"""多线程爬取 worker 池（每 worker 一份独立引擎实例）

设计要点
--------
项目是 asyncio（单线程事件循环 + 协作式并发）。所谓「多线程」= 启动 N 个 OS 线程，
每个线程跑一个独立的 asyncio 事件循环，并在该循环内构建一个**完整且私有**的
`CrawlerEngine`。

为什么能提速
------------
`RateLimitMiddleware` 的域名锁是**每实例**的。单进程内所有请求共用一把
`www.wenku8.net` 锁 -> 并发被吸成 5s 一队的串行流。每个 worker 拥有独立的
`RateLimitMiddleware` 实例 -> 独立的 5s 限速预算 -> N 个 worker ≈ N× 吞吐。
每个 worker 仍各自守 5s，属合规范围内的横向扩展。

线程安全红线
------------
- 所有 loop-bound 对象（CrawlerEngine / adapter 内的 _fetcher/_http_client /
  AsyncHTTPClient / SQLiteBackend(aiosqlite) / 限速锁）**只在其所属线程内创建与使用**。
- adapter 必须用 `registry.build()`（工厂，返回新实例），绝不能用 `registry.get()` 单例。
- 跨线程只传不可变标量：novel_id: str、db_path: str、site: str。
- 唯一共享可变状态：有界 `queue.Queue` + 线程安全的统计计数 `threading.Lock`。
- 唯一共享文件：novels.db（WAL + busy_timeout + 写重试，见 storage/sqlite_backend.py）。
"""

from __future__ import annotations

import asyncio
import queue
import threading
from typing import Any

from loguru import logger

from novel.config import AppConfig


# 分块大小由 config `crawl.block_size` 控制（默认 500）：外层每次领取的
# ID 区间长度（如 1-500、501-1000）。块内按「单本书号」粒度领取（所有 worker
# 在块内竞争最低空闲书号，单本爬完立即再领，块内负载均衡）；整块领完再切下一
# 块；**单个整空白块**（块内 0 有效小说）即判定全局末尾。


class CrawlWorkerPool:
    """多线程爬取池。

    Args:
        config:       AppConfig（只读，可安全跨线程传引用）
        site:         站点名（如 wenku8）
        db_path:      数据库文件路径（所有 worker 共享同一 WAL 文件）
        workers:      worker 线程数（>=2 才真正多线程；传入 1 时 CLI 直接走单线程路径）
        username/password: 登录凭据（每个 worker 线程内各自登录）
        max_concurrency / max_retries: 每 worker 内并发参数
        mode:         "discover"（默认，coordinator 发现 + worker 消费管线）
                      "shard"（分块领取：块内单本粒度 work-stealing，整空白块判定末尾）
    """

    def __init__(
        self,
        config: AppConfig,
        site: str,
        db_path: str,
        *,
        workers: int = 2,
        username: str | None = None,
        password: str | None = None,
        max_concurrency: int = 8,
        max_retries: int | None = None,
        mode: str = "discover",
    ):
        self.config = config
        self.site = site
        self.db_path = db_path
        self.workers = max(1, int(workers))
        self.creds = (username, password)
        self.max_concurrency = max_concurrency
        self.max_retries = max_retries
        self.mode = mode
        # 分块大小从 config crawl.block_size 读取（默认 500）
        self.block_size = max(1, int(config.crawl.block_size))

        self.task_q: "queue.Queue[str | None]" = queue.Queue(
            maxsize=max(1, self.workers * 16)
        )
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self._stats = {"novels": 0, "ok": 0, "fail": 0, "chapters": 0}
        self._truncated = False
        # 缺章/整本失败的书号（方案 A）：收尾前由 _finalize 重扫补缺
        self._incomplete: set[str] = set()

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    async def arun(
        self, max_novels: int | None = None, start_id: int | None = None
    ) -> dict[str, int]:
        """异步入口（由 CLI 的 run_async 驱动）。

        workers<=1 不应调用本方法（CLI 直接走单线程 crawl_site）。
        """
        if self.workers <= 1:
            raise RuntimeError(
                "worker 数必须 >= 2，单线程请直接用 CrawlerEngine.crawl_site"
            )

        if self.mode == "shard":
            await self._run_shard(max_novels, start_id)
        else:
            self._start_discover_workers()
            await self._coordinator_discover(max_novels, start_id)
            # 等待 worker 消费完队列并退出（在 executor 线程里 join，不阻塞事件循环）
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._join_all)

        # 方案 A：重扫缺章书并修正 last_novel_id（防缺章书被计入进度后不再重扫）
        await self._finalize()

        return dict(self._stats)

    async def _finalize(self) -> None:
        """收尾：重扫缺章书（crawl_novel 章节差集幂等，只补缺章）。

        重扫后仍失败的书：把续爬起点回退到「最小失败书号 - 1」——下次
        `crawl-all` 从该书续爬会重新经过它（网络恢复后自动补上），防止
        「爬过但没爬完」的书被计入 last_novel_id 后缺章永久丢失。
        """
        if not self._incomplete:
            return
        with self._lock:
            pending = sorted(self._incomplete, key=int)
        logger.warning(
            f"[{self.site}] {len(pending)} 本书缺章/失败，收尾前重扫补缺: {pending}"
        )
        engine, storage, http_client, adapter = await self._build_engine()
        still: list[str] = []
        try:
            for sid in pending:
                try:
                    r = await engine.crawl_novel(sid)
                    if r.failed_chapters:
                        still.append(sid)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"重扫仍失败 [{sid}]: {e}")
                    still.append(sid)
            if still:
                min_failed = min(int(s) for s in still)
                current = await storage.get_last_novel_id(self.site)
                corrected = min(current, min_failed - 1)
                await storage.set_last_novel_id(self.site, corrected)
                logger.warning(
                    f"[{self.site}] {len(still)} 本书重扫后仍缺章，"
                    f"续爬起点回退到 {corrected}: {still}"
                )
        finally:
            await self._safe_close(engine, storage, http_client, adapter)

    # ------------------------------------------------------------------
    # 每 worker 私有引擎构建（必须在 worker 自己的事件循环内调用）
    # ------------------------------------------------------------------

    async def _build_engine(self):
        """在线程的事件循环内构建【私有】引擎所需全部组件。"""
        from novel.adapters.registry import AdapterRegistry
        from novel.http.client import AsyncHTTPClient
        from novel.storage.sqlite_backend import SQLiteBackend
        from novel.engine.crawler import CrawlerEngine
        from novel.cli.main import (
            _build_request_middlewares,
            _build_response_middlewares,
            _build_pipelines,
            _get_max_retries,
            maybe_login,
            resolve_site_rate_limit,
        )

        storage = SQLiteBackend(self.db_path)
        http_client = AsyncHTTPClient(
            timeout_connect=self.config.http.timeout_connect,
            timeout_read=self.config.http.timeout_read,
            follow_redirects=self.config.http.follow_redirects,
            proxies=self.config.http.proxy or None,
        )
        # 工厂：每 worker 独立适配器实例（关键红线，绝不能用 get() 单例）
        registry = AdapterRegistry()
        registry.load_all()
        adapter = registry.build(self.site)
        adapter._http_client = http_client

        req_mw = _build_request_middlewares(self.config, http_client)
        resp_mw = _build_response_middlewares(self.config)

        # 本线程内登录：cookie 写入本线程 http_client，不跨线程共享
        await maybe_login(adapter, self.creds[0], self.creds[1])

        await storage.init()

        pipelines = _build_pipelines(self.config)
        engine = CrawlerEngine(
            adapter=adapter,
            storage=storage,
            http_client=http_client,
            request_middlewares=req_mw,
            response_middlewares=resp_mw,
            pipelines=pipelines,
            max_concurrency=self.max_concurrency,
            rate_limit=resolve_site_rate_limit(self.config, self.site, adapter),
            max_retries=self.max_retries or _get_max_retries(self.config),
        )
        return engine, storage, http_client, adapter

    # ------------------------------------------------------------------
    # 模式 A：coordinator 发现 + worker 消费（管线并行）
    # ------------------------------------------------------------------

    def _start_discover_workers(self) -> None:
        for _ in range(self.workers):
            t = threading.Thread(target=self._worker_loop_discover, daemon=True)
            t.start()
            self._threads.append(t)

    def _worker_loop_discover(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            engine, storage, http_client, adapter = loop.run_until_complete(
                self._build_engine()
            )
            while not self._stop.is_set():
                item = self.task_q.get()
                if item is None:
                    self.task_q.task_done()
                    break
                try:
                    result = loop.run_until_complete(engine.crawl_novel(item))
                    with self._lock:
                        self._stats["novels"] += 1
                        if result.chapters:
                            self._stats["ok"] += 1
                            self._stats["chapters"] += len(result.chapters)
                        else:
                            self._stats["fail"] += 1
                        if result.failed_chapters:
                            self._incomplete.add(item)
                except Exception as e:  # noqa: BLE001
                    logger.error(f"worker 爬取失败 [{item}]: {e}")
                    with self._lock:
                        self._stats["novels"] += 1
                        self._stats["fail"] += 1
                        self._incomplete.add(item)
                finally:
                    self.task_q.task_done()
        finally:
            try:
                loop.run_until_complete(
                    self._safe_close(engine, storage, http_client, adapter)
                )
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    async def _coordinator_discover(
        self, max_novels: int | None, start_id: int | None
    ) -> None:
        # coordinator 也用独立引擎做发现（其 RateLimitMiddleware 独立，发现尊重 5s）。
        # 它只负责 iter_all_novels 发现 + 入队，不负责爬取。
        engine, storage, http_client, adapter = await self._build_engine()
        try:
            start = start_id or 1
            count = 0
            last_id = 0
            # blank_limit 从 config crawl.blank_limit 读取（连续空白判定末尾阈值）
            discover_filter = {
                "start_id": start,
                "blank_limit": self.config.crawl.blank_limit,
            }
            async for novel_info in adapter.iter_all_novels(discover_filter):
                if self._stop.is_set():
                    break
                if max_novels and count >= max_novels:
                    self._truncated = True
                    break
                count += 1
                last_id = int(novel_info.site_id)
                # 满则阻塞，背压保护；每个 ID 仅入队一次 -> 仅一个 worker 消费，
                # 避免多 worker 写同一本小说造成存储竞争。
                self.task_q.put(novel_info.site_id)

            # 精确数量仅由 coordinator 写入（分块领取模式末尾由确认 worker
            # 显式 set_last_novel_id，不走此路径）
            if last_id and not self._truncated:
                await storage.set_last_novel_id(self.site, last_id)
        finally:
            # 投递毒丸：每个 worker 一个 None，确保全部退出
            for _ in range(self.workers):
                self.task_q.put(None)
            await self._safe_close(engine, storage, http_client, adapter)

    # ------------------------------------------------------------------
    # 模式 B：分块领取（外层 500 分块，块内按单本书号粒度竞争领取）
    # ------------------------------------------------------------------

    async def _run_shard(
        self, max_novels: int | None, start_id: int | None
    ) -> None:
        """分块领取（模式 B）：块内单本粒度 work-stealing、动态末尾。

        外层：按 config.crawl.block_size（默认 500）分块（1-N、N+1-2N…）。
        所有 worker 在【当前块内】原子领取「序号最低的空闲书号」（单本粒度，
        块内负载均衡），整块领完后整体切到下一块；**单个整空白块**（块内 0 有效
        小说）判定全局末尾，由确认 worker 把全局最后有效书号写入
        site_meta.last_novel_id（= 网站总书数，供下次续爬）。
        """
        bs = self.block_size
        start = start_id or 1
        # 分块共享状态（跨线程，全部由 self._lock 保护）
        self._block_start = start
        self._block_end = start + bs - 1
        self._next_block_id = start
        self._block_valid = 0
        self._steal_done = False
        self._global_last_valid = 0

        logger.info(
            f"[{self.site}] 分块领取模式：{self.workers} worker，"
            f"块大小 {bs}，起点 {start}，整空白块判定末尾"
        )

        self._threads = []
        for _ in range(self.workers):
            t = threading.Thread(
                target=self._worker_loop_stealing,
                args=(max_novels,),
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._join_all)

    def _worker_loop_stealing(self, max_novels: int | None) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            engine, storage, http_client, adapter = loop.run_until_complete(
                self._build_engine()
            )
            try:
                while True:
                    # 原子领取块内一个书号（跨线程只共享 _lock 保护的标量）；
                    # 越过块尾即收尾当前块：空白块→全局结束，否则切下一块。
                    with self._lock:
                        if self._steal_done or self._stop.is_set():
                            break
                        if max_novels and self._stats["novels"] >= max_novels:
                            self._steal_done = True
                            break
                        nid = self._next_block_id
                        self._next_block_id += 1
                        if nid > self._block_end:
                            if self._block_valid == 0:
                                # 当前块整空白 → 全局末尾
                                self._steal_done = True
                                # 最后有效书号 = 网站总书数，写入 site_meta 供续爬
                                try:
                                    loop.run_until_complete(
                                        storage.set_last_novel_id(
                                            self.site, self._global_last_valid
                                        )
                                    )
                                except Exception:  # noqa: BLE001
                                    pass
                                break
                            # 整块领完且有内容 → 切下一块
                            self._block_start = nid
                            self._block_end = nid + self.block_size - 1
                            self._block_valid = 0
                            logger.info(
                                f"[{self.site}] 进入下一块 "
                                f"[{nid}, {nid + self.block_size - 1}]"
                            )
                            continue

                    # 锁外爬取该书号：ValueError=无效书号（空白信号），
                    # 其余异常=爬取失败（书号可能有效，不算空白，继续）。
                    try:
                        result = loop.run_until_complete(
                            engine.crawl_novel(str(nid))
                        )
                        valid = True
                    except ValueError:
                        valid = False
                        result = None
                    except Exception as exc:  # noqa: BLE001
                        logger.error(f"worker 爬取失败 [{nid}]: {exc}")
                        valid = True
                        result = None
                        # 书号有效但整本失败：加入待补清单，收尾重扫
                        with self._lock:
                            self._incomplete.add(str(nid))

                    with self._lock:
                        if valid:
                            self._block_valid += 1
                            self._global_last_valid = max(
                                self._global_last_valid, nid
                            )
                        self._stats["novels"] += 1
                        if valid and result is not None and result.chapters:
                            self._stats["ok"] += 1
                            self._stats["chapters"] += len(result.chapters)
                        else:
                            self._stats["fail"] += 1
                        if valid and result is not None and result.failed_chapters:
                            self._incomplete.add(str(nid))
            finally:
                loop.run_until_complete(
                    self._safe_close(engine, storage, http_client, adapter)
                )
        finally:
            loop.close()

    # ------------------------------------------------------------------
    # 共享工具
    # ------------------------------------------------------------------

    def _join_all(self) -> None:
        for t in self._threads:
            t.join()

    async def _safe_close(self, engine, storage, http_client, adapter) -> None:
        """释放本 worker 的全部 loop-bound 资源（全部异步关闭 + 重试）。"""
        try:
            await engine.aclose()
        except Exception:  # noqa: BLE001
            pass
        try:
            if adapter is not None and hasattr(adapter, "close"):
                await adapter.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await http_client.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            await storage.close()
        except Exception:  # noqa: BLE001
            pass
