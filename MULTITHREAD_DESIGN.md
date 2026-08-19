# wenku8 爬虫 · 多线程方案设计

> 状态：**已实现**（2026-08-19）。落地代码见 `novel/engine/worker_pool.py`、
> `novel/storage/sqlite_backend.py`（`_INIT_LOCK` + `_commit_retry`）、
> `novel/adapters/registry.py`（`build()`）、`novel/engine/crawler.py`
> （`write_last_id`）、CLI `crawl-all --workers/--shard`。
> 验证：6 线程 WAL 并发 2400 章 0 锁；真实站点 `--workers 2 --shard` 并发落地
> 段1《文学少女》与段2《BLACK LAGOON》两本无锁库。

> 目标：在不违反站点声明限速（5s/请求）的前提下，把全站/批量爬取吞吐从「约 1 章/5s」提升到「N 章/5s」。

---

## 0. 现状瓶颈（为什么当前快不起来）

基于真实代码的确认（`novel/engine/crawler.py`、`novel/middlewares/rate_limit.py`）：

| 位置 | 现状 | 后果 |
|---|---|---|
| `CrawlerEngine.crawl_novel` (L224-230) | 章节用 `asyncio.gather` + `Semaphore(8)` 并发 | 看起来并发，但… |
| `RateLimitMiddleware` (rate_limit.py L21) | `self._locks: dict[domain, asyncio.Lock]` 是**每实例**的 | 整个进程只有 1 把 `www.wenku8.net` 的锁 |
| `Fetcher` 内持该 middleware 单实例 | 所有章节请求在锁处被排成 5s 一队 | Semaphore(8) 形同虚设，并发被吸收成串行 |
| `crawl_site` (L514) | `async for novel_info: await self.crawl_novel(...)` | 小说之间也逐本串行 |

**结论**：单进程内无论起多少并发，`www.wenku8.net` 域名上的请求始终被「一把锁 + 5s 间隔」串行化 → 实际吞吐 ≈ 1 请求/5s。提速的唯一干净杠杆是 **让每个 worker 拥有独立的限速预算**（独立 `RateLimitMiddleware` 实例 → 独立域名锁 → 独立 5s）。

---

## 1. 核心思路：每个 worker 拥有一个完整的引擎实例

项目是 **asyncio**（单线程事件循环 + 协作式并发），不是 OS 线程。所谓「多线程」= 启动 **N 个 OS 线程，每个线程跑一个独立的 `asyncio` 事件循环，并在该循环内构建一个完整的 `CrawlerEngine`**。

- 每个 worker 的 `RateLimitMiddleware` 是独立实例 → 独立 5s 预算 → N 个 worker ≈ N× 吞吐。
- 线程在 `await`（网络 I/O）时释放 **GIL**，所以 I/O 密集型爬取能近似线性扩展；lxml 解析是短 CPU 段，仅在极高并发时被 GIL 轻微串行化，整体收益仍远大于单进程。

---

## 2. 目标架构

```
                  协调线程（主线程，own event loop）
                  iter_all_novels() 发现 novel_id
                           │  (模式A) 或 分块领取 (模式B)
                           ▼
                  ┌─────────────────────────┐
                  │  task_queue  (queue.Queue) │  ← 唯一跨线程可变状态，有界
                  └─────────────────────────┘
            ┌──────────────┬──────────────┬──────────────┬──────────────┐
            ▼              ▼              ▼              ▼              ▼
        Worker-1       Worker-2       Worker-3       Worker-4  ...  Worker-N
    own event loop  own event loop  own event loop  own event loop
    own CrawlerEngine / RateLimit(5s) / AsyncHTTPClient / SQLiteBackend(WAL)
            │              │              │              │
            └──────────────┴──────────────┴──────────────┘
                           ▼
                  ┌─────────────────────────┐
                  │  novels.db  (WAL 模式)     │  ← 共享文件；前置 busy_timeout + 写重试
                  └─────────────────────────┘
```

每 worker 独立域名锁 ⇒ N× 吞吐；每 worker 仍守 5s ⇒ 合规范围内的横向扩展。

---

## 3. 组件职责

### 3.1 协调线程（Coordinator）
- 运行发现逻辑：模式 A 用 `adapter.iter_all_novels()`（串行 5s/本，发现即投递）；模式 B 为分块领取（外层 500 分块，块内按最低空闲书号细粒度领取）。
- 持有有界 `queue.Queue`，把 `novel_id`（str）投递给空闲 worker。
- 爬取结束后写 `site_meta.last_novel_id`（仅 coordinator 写，避免多线程竞争）。
- 监听取消信号，向队列投入 N 个 `None` 毒丸通知 worker 退出。

### 3.2 工作线程（Worker × N）
每个线程内：
1. `loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)`
2. **在线程内**构建私有引擎（见 §4 红线）：`http_client` / `storage` / middleware 列表 / `CrawlerEngine`。
3. 若需登录：`engine.login(creds)` 在本线程内把 cookie 写入本线程的 `http_client`。
4. 循环：`novel_id = queue.get(); if novel_id is None: break; loop.run_until_complete(engine.crawl_novel(novel_id))`。
5. 收尾：`await engine.aclose(); await storage.close()`。

### 3.3 共享边界（只有两处）
- `task_queue`：线程安全，唯一跨线程可变状态。
- `novels.db` 文件：经 WAL 多连接并发读写（见 §5）。

---

## 4. 线程安全红线（必须遵守，否则跨 loop 崩溃）

1. **每个 loop-bound 对象只在一个线程内创建并使用**：`CrawlerEngine`、`Wenku8Adapter`（其 `_fetcher`/`_http_client` 是 loop-bound）、`AsyncHTTPClient`、`SQLiteBackend`/`aiosqlite.Connection`、`RateLimitMiddleware` 的锁、`RateLimiter` 的锁。
2. **adapter 绝不能用 registry 单例**：`registry.get(site)` 返回缓存的同一实例，其 `_fetcher`/`_http_client` 属于创建它的线程。每 worker 必须用 `registry.build(site)`（工厂，返回新实例）或 `Wenku8Adapter()` 直接构造。
3. **中间件列表每 worker 新建**，`RateLimitMiddleware` 每 worker 独立（这是「独立 5s 预算」的落地点）。
4. **跨线程只传不可变标量**：`novel_id: str`、`db_path: str`、可选 `NovelInfo`（已发现的可直接复用，省去 `crawl_novel` 内的 `get_novel_info` 重复请求，顺带修掉 B1）。

---

## 5. SQLite 并发写入（前置条件，必须先合）

现状 `SQLiteBackend.init()` 已开 `PRAGMA journal_mode=WAL`，但**缺 `busy_timeout`、写操作无重试**。N 个 worker 各持一个 WAL 连接写同一文件，写冲突必现 `database is locked`。

必须补：
- `PRAGMA busy_timeout=200`（实际落地值。设计时曾拟 5000/30000，但多线程 writer 下大值会把每次写冲突放大成 SQLite 内部自旋等待（30s），退化为 livelock；小值让冲突快速失败，交给我方退避重试兜底）。
- `storage` 的 `execute`/`commit`/`save_*` 捕获 `sqlite3.OperationalError`（"database is locked"）做指数退避重试（如 50ms→100ms→200ms，最多 ~10 次）。
- **同一本小说只派给一个 worker**（queue 唯一投递）→ 同 novel 的 `save_novel`/`save_chapter` 不会跨线程竞争。
- `site_meta.last_novel_id` 只在 coordinator 写。

---

## 6. 登录态

当前 `Wenku8Adapter.login()` 把 cookie 写入本线程的 `http_client`（loop-bound）。多线程下：
- coordinator 可先用凭据 `login()` 验证可用性，失败即整体停止。
- 每个 worker 线程内再各自 `login()`（或预取 cookie dict 后 `http_client.set_cookies(...)`），cookie jar 不跨线程共享。

---

## 7. 全站遍历两种模式

- **模式 A（发现-消费）**：coordinator 跑 `iter_all_novels`（串行 5s/本）发现即入队，worker 消费。实现简单；但发现是瓶颈（首跑尾段需 ~8min 空等，上一轮已分析）。
- **模式 B（分块领取）**：【实现注】原设计「ID 静态分片（`[1, limit]` 切 N 段）」最终落地为：
  外层按 `BLOCK_SIZE=500` 分块（1-500、501-1000…），所有 worker 在当前块内原子领取
  「序号最低的空闲书号」（单本粒度、块内负载均衡），整块领完切下一块；**单个整空白块**
  判定全局末尾，确认 worker 把最后有效书号写入 `site_meta.last_novel_id`。
  续爬（stored 已知）仍用 `start_id = stored+1` 起步。
- **续爬（stored 已知）**：直接用 `start_id = stored+1` 起分片，跳过整段发现。

---

## 8. 每 worker 内并发（不变）

`CrawlerEngine(max_concurrency=8)` 维持现状。N 个 worker ⇒ 最多 8N 并发章节请求，受 N 个独立 5s 预算约束 ⇒ 实际 ≈ N/5 请求/秒（当前 1/5）。插图下载限速 `RateLimiter` 同理每 worker 独立。

---

## 9. 实现骨架（伪码）

```python
# novel/engine/worker_pool.py
import asyncio, threading, queue
from novel.engine.crawler import CrawlerEngine
from novel.adapters.registry import registry

def build_worker_engine(site, db_path, creds, rate_limit):
    """在线程内构建【私有】引擎。所有 loop-bound 对象都在本线程创建。"""
    adapter = registry.build(site)                 # 注意：build 非 get，返回新实例
    http_client = AsyncHTTPClient(...)
    if creds:
        asyncio.get_event_loop()  # 需先 set loop
        adapter.login(**creds)    # cookie 进本线程 http_client
    storage = SQLiteBackend(db_path); await storage.init()
    mws = [UAMiddleware(), RateLimitMiddleware(interval=rate_limit)]  # 独立锁
    rms = [RetryMiddleware(), VerifyMiddleware()]
    return CrawlerEngine(adapter, storage, http_client, mws, rms,
                         max_concurrency=8, rate_limit=rate_limit)

def _worker_loop(task_q, site, db_path, creds, rate_limit, stop):
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    engine = loop.run_until_complete(
        asyncio.sleep(0) or build_worker_engine(site, db_path, creds, rate_limit))
    while not stop.is_set():
        item = task_q.get()
        if item is None:
            task_q.task_done(); break
        try:
            loop.run_until_complete(engine.crawl_novel(item))
        finally:
            task_q.task_done()
    loop.run_until_complete(engine.aclose())

class CrawlWorkerPool:
    def __init__(self, site, db_path, workers=4, creds=None, rate_limit=5.0):
        self.task_q = queue.Queue(maxsize=workers * 16)
        self.threads = [threading.Thread(target=_worker_loop,
                         args=(self.task_q, site, db_path, creds, rate_limit, self._stop))
                         for _ in range(workers)]
    def start(self): [t.start() for t in self.threads]
    def submit(self, novel_id): self.task_q.put(novel_id)
    def join(self):
        self._stop.set()
        for _ in self.threads: self.task_q.put(None)
        [t.join() for t in self.threads]
```

> 注：`build_worker_engine` 内的 `adapter.login` 需在已 `set_event_loop` 后于 loop 中调用；伪码仅为示意，落地时用 `loop.run_until_complete(_init())` 包裹异步初始化。

---

## 10. CLI 改动

```bash
# 多线程全站（默认模式 A，--shard 切模式 B）
python -m novel crawl-all wenku8 --workers 4 [--shard] [--max-novels 200]

# 单本不变；--workers 1 即退化为当前行为（回滚开关）
python -m novel crawl wenku8 2877 --chapters 1-10
```

`crawl-all` 内部改为：建 `CrawlWorkerPool` → coordinator 发现/分片 → `pool.submit(id)` → `pool.join()`。

---

## 11. GIL 与多进程备选

- **GIL 影响**：仅 `await` 外的 CPU 段（lxml 解析、文本清洗）被串行化，占比极小，I/O 等待期间线程并行，吞吐近线性。
- **backend 可一行切换**：把 `threading.Thread` 换成 `multiprocessing.Process`、`queue.Queue` 换成 `multiprocessing.Queue`，抽象不变。多进程隔离更强、无 GIL，但内存 ×N、且 engine/config 需在子进程内重建（不可跨进程 pickle loop-bound 对象）。两者吞吐等价，建议先上线线程版，容量仍不足再换进程版。

---

## 12. 风险与回滚

- **前置硬依赖**：§5 的 `busy_timeout` + 写重试必须先合，否则多写即锁库。
- **合规**：每 worker 仍守 5s，属横向扩展；若想进一步提速需引入代理 IP 池（推高单进程实际速率），属踩线，需单独拍板。
- **回滚**：`--workers 1` 即为当前单进程行为，零风险退路。

---

## 13. 分阶段落地

| 阶段 | 内容 | 风险 |
|---|---|---|
| P0 | `SQLiteBackend` 加 `busy_timeout` + 写重试；新增 `registry.build()` 工厂与 `build_worker_engine()` | 低 |
| P1 | `CrawlWorkerPool`（线程版）+ 队列 + 单 worker 自测 | 低 |
| P2 | `crawl-all --workers N`（模式 A 发现-消费） | 低 |
| P3 | `--shard` 模式 B 分块领取 + 续爬直起块 | 中 |
| P4（可选） | 多进程 backend 一行切换 | 中 |

---

### 一句话总结

瓶颈不在「并发不够」，而在「一把域名限速锁把并发吸成串行」。多线程方案的精髓是 **给每个 worker 一份独立引擎实例（独立 `RateLimitMiddleware` → 独立 5s 预算）**，worker 间仅通过有界队列传递 `novel_id`、共享 WAL 数据库；配合 `busy_timeout`+写重试消除锁库风险，即可在守速前提下获得近 N× 吞吐。
