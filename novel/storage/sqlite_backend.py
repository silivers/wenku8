"""SQLite 异步存储后端"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from novel.core.models import ChapterContent, NovelInfo
from novel.core.storage import StorageBackend

# 初始化锁：多线程 worker 池并发连接同一（尚不存在的）库文件时，
# 各自的 aiosqlite.connect + PRAGMA journal_mode=WAL 会互相锁住（database is locked）。
# 用全局锁保证同一时刻只有一个连接做「建库 + 迁移」，其余等待后连接已存在的库。
_INIT_LOCK = threading.Lock()


class SQLiteBackend(StorageBackend):
    """SQLite 异步存储后端。零配置，单文件。"""

    def __init__(self, db_path: str = "data/novels.db"):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        # 连接实例唯一 id：用于跨 worker 关联日志（哪个线程用了哪个连接）
        self._conn_id = uuid.uuid4().hex[:8]
        # 锁重试参数：执行/提交路径的统一退避上限（见 _run_with_lock_retry）
        self._LOCK_MAX_RETRIES = 25
        self._LOCK_BACKOFF_BASE = 0.02

    async def init(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        # 序列化初始化（跨线程）。各 worker 线程独立事件循环，同一线程内 init
        # 不会被其他协程打断，故在单线程内跨 await 持锁是安全的；跨线程则串行化，
        # 仅第一个连接真正建库+迁移，其余连接已存在的库（WAL 已是默认，无竞争）。
        with _INIT_LOCK:
            self._conn = await aiosqlite.connect(
                self.db_path, timeout=5.0, isolation_level=None
            )
            self._conn.row_factory = aiosqlite.Row
            # WAL + 小 busy_timeout：多线程（每 worker 独立连接）并发写同一文件。
            # 关键：busy_timeout 必须【小】（200ms），而非大值。WAL 是单写者模型，
            # 大 busy_timeout（如 30s）会让每次写冲突在 SQLite 内部自旋等待几十秒，
            # 在多线程 writer 下退化为类 livelock（所有连接都在等待，无人能写）。
            # 小 busy_timeout 让冲突【快速失败】，交给 _run_with_lock_retry 的
            # Python 侧指数退避重试（封顶 0.5s）兜底，整体吞吐与延迟都更优。
            # 短事务（save_chapter/save_images 每章/每批 commit）已把写锁持有降到毫秒级，
            # 200ms 通常足以让另一连接的毫秒事务提交，极少触发应用层重试。
            # connect(timeout=5.0) 作为双保险：sqlite3 层 busy handler 上限 5s。
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA synchronous=NORMAL")
            await self._conn.execute("PRAGMA busy_timeout=200")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._run_migrations()
        # —— 诊断：回读 PRAGMA 确认真正生效 ——
        # 多线程 database is locked 秒级爆发的头号嫌疑就是 busy_timeout 没落到连接上。
        # 每次初始化都回读并打印（线程名 + 连接 id），便于跨 worker 关联。
        try:
            bt_row = await (await self._conn.execute("PRAGMA busy_timeout")).fetchone()
            jm_row = await (await self._conn.execute("PRAGMA journal_mode")).fetchone()
            syn_row = await (await self._conn.execute("PRAGMA synchronous")).fetchone()
            bt = bt_row[0] if bt_row else "?"
            jm = jm_row[0] if jm_row else "?"
            syn = syn_row[0] if syn_row else "?"
        except Exception as _e:  # noqa: BLE001
            bt = jm = syn = f"read-fail:{_e}"
        logger.info(
            f"DB-INIT | conn_id={self._conn_id} "
            f"thread={threading.current_thread().name}"
            f"({threading.current_thread().ident}) "
            f"busy_timeout={bt} journal_mode={jm} synchronous={syn} db={self.db_path}"
        )

    async def _commit_retry(self) -> None:
        """提交事务，遇 'database is locked'/'busy' 指数退避重试。

        多线程（每 worker 独立连接）并发写时，写冲突会被 SQLite 短暂拒绝。
        busy_timeout 已覆盖绝大多数情况；此处作为兜底，确保批量提交不丢数据。
        统一走 _run_with_lock_retry，与 _exec 共用退避/诊断逻辑。
        """
        async def _do():
            await self.conn.commit()
        await self._run_with_lock_retry(_do, "COMMIT", None)

    async def _run_with_lock_retry(self, fn, sql: str, parameters: Any) -> Any:
        """统一执行包装：遇 'database is locked' / 'database is busy' 指数退避重试。

        为什么必须在「执行」路径也重试（而不仅靠 busy_timeout）：
        SQLite 的 PRAGMA busy_timeout 只对 SQLITE_BUSY 自旋等待，对 SQLITE_LOCKED
        （死锁规避，瞬时返回）**无效**。多线程并发写时，一个连接持有写事务、
        另一个连接想「读→写升级」会立刻拿到 SQLITE_LOCKED，busy_timeout 兜不住，
        于是 INSERT 第一次冲突就被 SQLite 瞬时拒绝。此处对执行路径也做退避重试，
        让赢家连接提交释放锁后，输家连接退避重试用即可成功（与 _commit_retry 对齐）。

        注意：重试只覆盖 'lock'/'busy' 类错误；其它 OperationalError 原样抛出。
        """
        last: Exception | None = None
        for attempt in range(self._LOCK_MAX_RETRIES):
            try:
                return await fn()
            except sqlite3.OperationalError as e:
                msg = str(e).lower()
                if "lock" in msg or "busy" in msg:
                    last = e
                    if attempt < self._LOCK_MAX_RETRIES - 1:
                        backoff = min(self._LOCK_BACKOFF_BASE * (2 ** attempt), 0.5)
                        logger.debug(
                            f"DB-RETRY | conn_id={self._conn_id} attempt={attempt} "
                            f"锁冲突，退避 {backoff:.3f}s: {msg}"
                        )
                        await asyncio.sleep(backoff)
                        continue
                    # 退避耗尽仍失败 → 打印完整现场后抛出
                    await self._diag_lock(sql, parameters)
                    raise
                raise
        if last:
            await self._diag_lock(sql, parameters)
            raise last

    async def _exec(self, sql: str, parameters: Any = None):
        """带锁重试的 conn.execute 包装。"""
        async def _do():
            if parameters is not None:
                return await self._conn.execute(sql, parameters)
            return await self._conn.execute(sql)
        return await self._run_with_lock_retry(_do, sql, parameters)

    async def _executescript(self, sql: str) -> None:
        """带锁重试的 conn.executescript 包装。"""
        async def _do():
            return await self._conn.executescript(sql)
        await self._run_with_lock_retry(_do, sql, None)

    async def _diag_lock(self, sql: str, parameters: Any) -> None:
        """打印锁错误的完整现场，用于定位多线程并发写根因。

        输出：连接 id、线程、当前 busy_timeout/journal_mode/synchronous 回读值、
        WAL/DB 文件大小、出错 SQL（截断）、参数（截断）、当前全部活跃线程、
        调用栈（最后 10 帧）。
        """
        try:
            bt_row = await (await self._conn.execute("PRAGMA busy_timeout")).fetchone()
            bt = bt_row[0] if bt_row else "?"
        except Exception:  # noqa: BLE001
            bt = "?"
        try:
            jm_row = await (await self._conn.execute("PRAGMA journal_mode")).fetchone()
            jm = jm_row[0] if jm_row else "?"
        except Exception:  # noqa: BLE001
            jm = "?"
        try:
            syn_row = await (await self._conn.execute("PRAGMA synchronous")).fetchone()
            syn = syn_row[0] if syn_row else "?"
        except Exception:  # noqa: BLE001
            syn = "?"
        wal_path = f"{self.db_path}-wal"
        wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
        try:
            db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0
        except Exception:  # noqa: BLE001
            db_size = "?"
        threads = [f"{t.name}:{t.ident}" for t in threading.enumerate()]
        stack = "".join(traceback.format_stack()[-10:])
        logger.error(
            "DB-LOCK-DIAG | conn_id={} thread={}({}) busy_timeout={} journal_mode={} "
            "synchronous={} db_size={} wal_size={}\n  SQL={}\n  PARAMS={}\n"
            "  ACTIVE_THREADS={}\n  STACK={}".format(
                self._conn_id,
                threading.current_thread().name,
                threading.current_thread().ident,
                bt, jm, syn, db_size, wal_size,
                (sql or "")[:400], str(parameters)[:200],
                threads, stack,
            )
        )

    async def close(self) -> None:
        if self._conn:
            # 兜底提交：确保引擎未显式提交的数据不会因进程退出而丢失
            try:
                await self._conn.commit()
            except Exception:  # noqa: BLE001
                pass
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not initialized. Call init() first.")
        return self._conn

    async def _run_migrations(self) -> None:
        """执行数据库迁移脚本。"""
        # 确保 schema_migrations 表存在
        await self._exec(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        await self.conn.commit()

        # 获取当前版本
        cursor = await self._exec(
            "SELECT MAX(version) FROM schema_migrations"
        )
        row = await cursor.fetchone()
        current_version = row[0] if row and row[0] else 0

        # 加载迁移文件
        migrations_dir = Path(__file__).parent / "migrations"
        if not migrations_dir.exists():
            return

        for sql_file in sorted(migrations_dir.glob("*.sql")):
            version = int(sql_file.stem.split("_")[0])
            if version > current_version:
                sql = sql_file.read_text("utf-8")
                await self._executescript(sql)
                await self._exec(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now().isoformat()),
                )
                await self.conn.commit()
                logger.info(f"执行数据库迁移: {sql_file.name}")

    # ---- Novel ----

    async def save_novel(self, novel: NovelInfo, site: str) -> str:
        """保存小说元信息，返回内部 ID。"""
        # 检查是否已存在
        cursor = await self._exec(
            "SELECT id FROM novels WHERE site=? AND site_novel_id=?",
            (site, novel.site_id),
        )
        row = await cursor.fetchone()

        if row:
            internal_id = row["id"]
            await self._exec(
                """UPDATE novels SET title=?, author=?, description=?, cover_url=?,
                   status=?, categories=?, tags=?, word_count=?, last_updated=?,
                   crawl_time=?, extra=? WHERE id=?""",
                (
                    novel.title, novel.author, novel.description,
                    novel.cover_url, novel.status,
                    json.dumps(novel.categories, ensure_ascii=False),
                    json.dumps(novel.tags, ensure_ascii=False),
                    novel.word_count, novel.last_updated,
                    datetime.now().isoformat(),
                    json.dumps(novel.extra, ensure_ascii=False),
                    internal_id,
                ),
            )
        else:
            internal_id = str(uuid.uuid4())
            await self._exec(
                """INSERT INTO novels (id, site, site_novel_id, title, author,
                   description, cover_url, status, categories, tags, word_count,
                   last_updated, crawl_time, extra)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    internal_id, site, novel.site_id, novel.title, novel.author,
                    novel.description, novel.cover_url, novel.status,
                    json.dumps(novel.categories, ensure_ascii=False),
                    json.dumps(novel.tags, ensure_ascii=False),
                    novel.word_count, novel.last_updated,
                    datetime.now().isoformat(),
                    json.dumps(novel.extra, ensure_ascii=False),
                ),
            )

        await self._commit_retry()
        return internal_id

    async def get_novel(self, novel_id: str, site: str) -> NovelInfo | None:
        cursor = await self._exec(
            "SELECT * FROM novels WHERE site=? AND site_novel_id=?", (site, novel_id)
        )
        row = await cursor.fetchone()
        return self._row_to_novel(row) if row else None

    async def get_novel_by_internal_id(self, internal_id: str) -> NovelInfo | None:
        cursor = await self._exec(
            "SELECT * FROM novels WHERE id=?", (internal_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_novel(row) if row else None

    async def get_novel_list(self, site: str | None = None) -> list[dict[str, Any]]:
        if site:
            cursor = await self._exec(
                "SELECT * FROM novels WHERE site=? ORDER BY crawl_time DESC", (site,)
            )
        else:
            cursor = await self._exec(
                "SELECT * FROM novels ORDER BY crawl_time DESC"
            )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def delete_novel(self, internal_id: str) -> None:
        await self._exec("DELETE FROM novels WHERE id=?", (internal_id,))
        await self._commit_retry()

    # ---- Chapter ----

    async def save_chapter(
        self,
        chapter: ChapterContent,
        novel_internal_id: str,
        site: str,
        chapter_index: float = 0,
        volume: str | None = None,
    ) -> str:
        cursor = await self._exec(
            "SELECT id FROM chapters WHERE novel_id=? AND site_chapter_id=?",
            (novel_internal_id, chapter.chapter_id),
        )
        row = await cursor.fetchone()

        # 图片不再随章节落库（004 迁移已 DROP chapters.images 列）：
        # 插图由 _crawl_chapter 经 save_images 独立登记到 images 表（pending），
        # 下载成功后由图片爬取阶段回写 HTML。此处只存正文。
        if row:
            internal_id = row["id"]
            await self._exec(
                """UPDATE chapters SET title=?, content=?, content_format=?,
                   word_count=?, author_note=?, is_crawled=1, crawl_time=?,
                   content_hash=?, volume=?, chapter_index=? WHERE id=?""",
                (
                    chapter.title, chapter.content, chapter.content_format,
                    chapter.word_count, chapter.author_note,
                    datetime.now().isoformat(), chapter.content_hash,
                    volume, chapter_index, internal_id,
                ),
            )
        else:
            internal_id = str(uuid.uuid4())
            await self._exec(
                """INSERT INTO chapters (id, novel_id, site_chapter_id, title,
                   chapter_index, content, content_format, word_count, author_note,
                   is_crawled, crawl_time, content_hash, volume)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    internal_id, novel_internal_id, chapter.chapter_id,
                    chapter.title, chapter_index, chapter.content,
                    chapter.content_format, chapter.word_count,
                    chapter.author_note, 1,
                    datetime.now().isoformat(), chapter.content_hash, volume,
                ),
            )

        # 每章提交一次（短事务）：多线程 WAL 单写者模型下，写事务会独占写锁直到
        # commit。若把整本小说的所有 INSERT 攒到 crawl_novel 末尾才提交，写锁会被
        # 该 worker 横跨几十秒的网络抓取长期持有，其余 worker 写同一 WAL 库时全部
        # busy_timeout 超时 / 重试耗尽而丢章节。每章提交把写锁持有降到毫秒级，配合
        # busy_timeout + _run_with_lock_retry 即可零丢失。crawl_novel 末尾的 commit
        # 仍保留为兜底（无未决事务时为 no-op）。
        await self._commit_retry()
        return internal_id

    async def chapter_exists(self, chapter_id: str, novel_id: str, site: str) -> bool:
        cursor = await self._exec(
            """SELECT 1 FROM chapters c
               JOIN novels n ON c.novel_id = n.id
               WHERE c.site_chapter_id=? AND c.novel_id=? AND n.site=?""",
            (chapter_id, novel_id, site),
        )
        row = await cursor.fetchone()
        return row is not None

    async def filter_existing_chapters(
        self, chapter_ids: list[str], site: str, novel_id: str
    ) -> set[str]:
        """批量检查已存在的章节（单次 IN 查询，修复 M2 的 N+1 逐条查询）。

        必须限定 novel_id：wenku8 的 site_chapter_id 是各书独立的相对编号
        （"1"、"2"...），不限定小说会把 B 书的章节误判为 A 书已爬而跳过
        （多书/全站爬取必然触发）。M6 修复。
        """
        if not chapter_ids:
            return set()
        placeholders = ",".join("?" * len(chapter_ids))
        cursor = await self._exec(
            f"""SELECT c.site_chapter_id FROM chapters c
                JOIN novels n ON c.novel_id = n.id
                WHERE c.site_chapter_id IN ({placeholders})
                  AND c.novel_id=? AND n.site=?""",
            (*chapter_ids, novel_id, site),
        )
        return {row["site_chapter_id"] for row in await cursor.fetchall()}

    async def commit(self) -> None:
        """提交未决事务。"""
        await self._commit_retry()

    async def content_hash_exists(self, content_hash: str, novel_id: str | None = None) -> bool:
        """检查内容哈希是否已存在。

        Args:
            content_hash: SHA-256 哈希值
            novel_id: 如果提供，仅检查该小说内的章节（同小说去重）。
                      如果为 None，则全局检查（跨所有小说）。
        """
        if novel_id:
            cursor = await self._exec(
                "SELECT 1 FROM chapters WHERE content_hash=? AND novel_id=?",
                (content_hash, novel_id),
            )
        else:
            cursor = await self._exec(
                "SELECT 1 FROM chapters WHERE content_hash=?", (content_hash,)
            )
        row = await cursor.fetchone()
        return row is not None

    async def get_chapters(self, novel_internal_id: str) -> list[dict[str, Any]]:
        cursor = await self._exec(
            "SELECT * FROM chapters WHERE novel_id=? ORDER BY chapter_index",
            (novel_internal_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ---- Image（独立图片爬取任务，替代原 resources/save_image）----
    # 文本爬取阶段只登记 pending（save_images），下载/重试由图片轮询阶段驱动
    # （mark_image_downloaded / mark_image_failed / skip_exhausted_images）。
    # 所有写操作保持短事务（每调用一次 commit），沿用 WAL 三铁律。

    async def save_images(
        self,
        novel_internal_id: str,
        chapter_site_id: str | None,
        resource_type: str,
        images: list,
    ) -> int:
        """批量登记图片为 pending。UNIQUE(novel_id, remote_url) 保证幂等：
        断点续爬/重爬时同一张图不会产生重复行。返回本次插入条数。"""
        if not images:
            return 0
        inserted = 0
        now = datetime.now().isoformat()
        for img in images:
            url = getattr(img, "url", "") or ""
            if not url:
                continue
            cursor = await self._exec(
                """INSERT OR IGNORE INTO images
                   (novel_id, chapter_site_id, resource_type, remote_url,
                    image_order, status, created_at, updated_at)
                   VALUES (?,?,?,?,?,'pending',?,?)""",
                (
                    novel_internal_id, chapter_site_id, resource_type,
                    url, getattr(img, "index", 0) or 0, now, now,
                ),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                inserted += cursor.rowcount
        # 每章一次提交（短事务）：与 save_chapter 同理，多线程 WAL 单写者下
        # 写锁持有降到毫秒级，避免饿死其它 worker。
        await self._commit_retry()
        return inserted

    async def get_novels_with_pending_images(
        self, site: str, max_attempts: int = 10, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """返回有待下载图片（pending/failed 且 attempt<max_attempts、
        book_status='active'）的小说。轮询循环每一轮用它取候选书目。"""
        sql = (
            """SELECT n.id, n.title, COUNT(*) AS pending_count
               FROM images i
               JOIN novels n ON i.novel_id = n.id
               WHERE n.site=? AND n.book_status='active'
                 AND i.status IN ('pending','failed')
                 AND i.attempt_count < ?
               GROUP BY n.id, n.title
               ORDER BY n.id"""
        )
        params: list[Any] = [site, max_attempts]
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        cursor = await self._exec(sql, tuple(params))
        return [dict(row) for row in await cursor.fetchall()]

    async def get_pending_images(self, novel_internal_id: str) -> list[dict[str, Any]]:
        cursor = await self._exec(
            """SELECT * FROM images
               WHERE novel_id=? AND status IN ('pending','failed')
               ORDER BY chapter_site_id, image_order, id""",
            (novel_internal_id,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def count_pending_images(self, site: str, site_novel_id: str) -> int:
        cursor = await self._exec(
            """SELECT COUNT(*) AS c FROM images i
               JOIN novels n ON i.novel_id=n.id
               WHERE n.site=? AND n.site_novel_id=?
                 AND i.status IN ('pending','failed')""",
            (site, site_novel_id),
        )
        row = await cursor.fetchone()
        return int(row["c"]) if row else 0

    async def mark_image_downloaded(
        self, image_id: int, local_path: str, file_hash: str
    ) -> None:
        await self._exec(
            """UPDATE images SET status='downloaded', local_path=?, file_hash=?,
               last_error=NULL, updated_at=? WHERE id=?""",
            (local_path, file_hash, datetime.now().isoformat(), image_id),
        )
        await self._commit_retry()

    async def mark_image_failed(self, image_id: int, error: str) -> None:
        await self._exec(
            """UPDATE images SET status='failed', attempt_count=attempt_count+1,
               last_error=?, last_attempt_at=?, updated_at=? WHERE id=?""",
            (
                (error or "")[:500], datetime.now().isoformat(),
                datetime.now().isoformat(), image_id,
            ),
        )
        await self._commit_retry()

    async def skip_exhausted_images(self, max_rounds: int) -> int:
        """把 attempt_count>=max_rounds 仍失败的图片置 skipped（死链兜底，
        保证轮询必然收敛，不因死链死循环）。返回受影响行数。"""
        cursor = await self._exec(
            """UPDATE images SET status='skipped', updated_at=?
               WHERE status='failed' AND attempt_count>=?""",
            (datetime.now().isoformat(), max_rounds),
        )
        await self._commit_retry()
        return cursor.rowcount if cursor.rowcount else 0

    async def rewrite_chapter_image_url(
        self, novel_id: str, remote_url: str, local_url: str
    ) -> None:
        """把章节 HTML 中的远程图片 URL 替换为本地相对路径。

        按 novel_id 全量 REPLACE（不带 site_chapter_id 条件）：同一远程 URL
        跨章出现时一并覆盖；对已替换过的章节是 no-op（URL 已不存在），幂等。
        """
        await self._exec(
            """UPDATE chapters SET content = replace(content, ?, ?)
               WHERE novel_id=? AND content IS NOT NULL""",
            (remote_url, local_url, novel_id),
        )
        await self._commit_retry()

    async def set_novel_status(self, novel_internal_id: str, status: str) -> None:
        await self._exec(
            "UPDATE novels SET book_status=? WHERE id=?",
            (status, novel_internal_id),
        )
        await self._commit_retry()

    async def set_novel_cover(
        self, novel_internal_id: str, local_path: str
    ) -> None:
        await self._exec(
            "UPDATE novels SET cover_local=? WHERE id=?",
            (local_path, novel_internal_id),
        )
        await self._commit_retry()

    async def get_image_stats(self, site: str) -> dict[str, Any]:
        """图片爬取统计：各 status 计数、当前轮次、待下载书目数。"""
        cursor = await self._exec(
            """SELECT i.status, COUNT(*) AS c
               FROM images i JOIN novels n ON i.novel_id=n.id
               WHERE n.site=?
               GROUP BY i.status""",
            (site,),
        )
        status_counts: dict[str, int] = {
            row["status"]: int(row["c"]) for row in await cursor.fetchall()
        }
        cursor = await self._exec(
            "SELECT COALESCE(MAX(attempt_count),0) AS m FROM images"
        )
        row = await cursor.fetchone()
        round_no = int(row["m"]) if row else 0
        pending_novels = await self.get_novels_with_pending_images(
            site, max_attempts=1 << 30
        )
        return {
            "status_counts": status_counts,
            "current_round": round_no,
            "pending_novels": len(pending_novels),
        }

    # ---- Site meta（全站遍历精确数量）----

    async def get_last_novel_id(self, site: str) -> int:
        """读取站点最后一个有效小说编号；未爬过返回 0。"""
        cursor = await self._exec(
            "SELECT last_novel_id FROM site_meta WHERE site=?", (site,)
        )
        row = await cursor.fetchone()
        return int(row["last_novel_id"]) if row else 0

    async def set_last_novel_id(self, site: str, last_id: int) -> None:
        """保存站点最后一个有效小说编号（UPSERT，独立提交，崩溃也不丢进度）。"""
        await self._exec(
            """INSERT INTO site_meta (site, last_novel_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(site) DO UPDATE SET
                   last_novel_id=excluded.last_novel_id,
                   updated_at=excluded.updated_at""",
            (site, int(last_id), datetime.now().isoformat()),
        )
        await self._commit_retry()

    # ---- Stats ----

    async def get_stats(self) -> dict[str, Any]:
        cursor = await self._exec("SELECT COUNT(*) as count FROM novels")
        novel_count = (await cursor.fetchone())["count"]

        cursor = await self._exec("SELECT COUNT(*) as count FROM chapters")
        chapter_count = (await cursor.fetchone())["count"]

        cursor = await self._exec(
            "SELECT SUM(word_count) as total FROM chapters"
        )
        row = await cursor.fetchone()
        total_words = row["total"] if row and row["total"] else 0

        cursor = await self._exec(
            "SELECT site, COUNT(*) as count FROM novels GROUP BY site"
        )
        sites = {row["site"]: row["count"] for row in await cursor.fetchall()}

        cursor = await self._exec(
            "SELECT status, COUNT(*) as c FROM images GROUP BY status"
        )
        image_status = {
            row["status"]: int(row["c"]) for row in await cursor.fetchall()
        }
        cursor = await self._exec("SELECT COUNT(*) as count FROM images")
        image_total = (await cursor.fetchone())["count"]

        return {
            "novels": novel_count,
            "chapters": chapter_count,
            "total_words": total_words,
            "sites": sites,
            "images": image_total,
            "image_status": image_status,
        }

    # ---- Helpers ----

    @staticmethod
    def _row_to_novel(row: aiosqlite.Row) -> NovelInfo:
        categories = json.loads(row["categories"]) if row["categories"] else []
        tags = json.loads(row["tags"]) if row["tags"] else []
        extra = json.loads(row["extra"]) if row["extra"] else {}
        return NovelInfo(
            site_id=row["site_novel_id"],
            title=row["title"],
            author=row["author"] or "",
            description=row["description"] or "",
            cover_url=row["cover_url"],
            status=row["status"] or "",
            categories=categories,
            tags=tags,
            word_count=row["word_count"] or 0,
            last_updated=row["last_updated"],
            extra=extra,
        )
