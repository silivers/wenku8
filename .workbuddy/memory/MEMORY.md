# 项目长期记忆（MEMORY.md）

## wenku8 适配器关键坑（非显而易见，务必遵守）

1. **章节目录路径按 ID 分桶**：真实路径是 `/novel/{id//1000}/{id}/index.htm`，
   **不是** `/novel/0/{id}/`。后者只对 `id<1000` 凑巧正确，`id>=1000` 全 404。
   生成一律走 `Wenku8Adapter._build_chapter_index_url()`。

2. **下架检测不要误伤正常页**：wenku8 每篇正常章节页页脚都带
   `本文来自 轻小说文库`，**绝不能**拿它当下架信号，否则每章都被跳过、
   永远抓不到正文。只认精确提示语 `因版权问题，文库不再提供该小说的阅读` /
   `不再提供该小说的阅读`（`is_takedown()` 当前实现）。

3. **登录态机制**：`login()` 把会话 Cookie 写入共享 `http_client`
   （`set_cookies`）并缓存 `self._session_cookies`，`fetch_response()` 合并进
   每次请求 → 章节正文/封面/插图全链路带登录态。公共章节其实无需登录即可读。

4. **凭据不写死**：账号密码只从 CLI `--username/--password` 或环境变量
   `WENKU8_USERNAME`/`WENKU8_PASSWORD` 读取；验证用 `novel sites login <site>`。

5. **全站遍历无上限**：`iter_all_novels` 从 `start_id` 递增，连续 100 个错误页
   终止；精确数量存 `site_meta.last_novel_id`，下次从 `+1` 续爬（无 `--end-id`）。

6. **多线程并发写 WAL 库的两道保险 + 根除 LOCKED 三原则**：
   - `SQLiteBackend.init()` 用模块级 `_INIT_LOCK`（threading.Lock）序列化，
     否则多 worker 并发连同一尚不存在的库时 `PRAGMA journal_mode=WAL` 互相
     `database is locked` 崩溃（实测 4 worker 崩 2）。
   - 写提交走 `_commit_retry()`（遇 locked 指数退避重试）。
   - 多线程入口见 `novel/engine/worker_pool.py`：每 worker 独立事件循环+独立引擎
     实例（独立 `RateLimitMiddleware` 限速预算）= N× 吞吐；CLI `--workers>=2`
     + `--shard`。跨线程只传标量，唯一共享态 = 有界 queue + Lock，唯一共享文件 = WAL 库。
   - 分块领取（模式 B，用户 2026-08-19 定稿方案）：外层按 `BLOCK_SIZE=500` 分块
     （1-500、501-1000…），所有 worker 在当前块内原子领取「序号最低的空闲书号」
     （单本粒度、块内负载均衡），整块领完切下一块；**单个整空白块**判定全局末尾，
     确认 worker 显式 `set_last_novel_id`（勿依赖 `crawl_site` 的 `write_last_id`，
     已废弃静态等分与 `DEFAULT_SCAN_UPPER`）。无效书号 = `crawl_novel` 抛 ValueError。
   - **根除 `database is locked` 的三条铁律（2026-08-19 实测踩坑结论）**：
     1. `aiosqlite.connect` 必须 `isolation_level=None`（真 autocommit）。默认
        `isolation_level=""` 会让每次 `SELECT` 开读事务且不自动关闭；`gather` 并发
        多个 `save_chapter` 时同一连接堆叠多个残留读事务，写升级触发 `SQLITE_LOCKED`
        （`busy_timeout` 对 LOCKED 无效），重试也救不回。
     2. 每个写操作（`save_chapter`/`save_image`）**写完即 commit**（短事务）。
        绝不能"整本小说批量攒到末尾再 commit"——WAL 单写者下长事务会横跨几十秒
        网络抓取独占写锁，饿死其余 worker。
     3. `busy_timeout` 必须**小**（200ms）+ `connect timeout=5s` 兜底。**绝不能 30s**：
        大 busy_timeout 在多线程 writer 下把每次写冲突放大成 SQLite 内部自旋等 30s，
        退化为 livelock。小 busy_timeout 让冲突快速失败，交给我方
        `_run_with_lock_retry`（退避封顶 0.5s）兜底。

7. **页面编码实际是 GB18030 而非纯 GBK**（2026-08-19 实测）：wenku8 正文页
   含 GB18030 四字节扩展字符，Python `gbk` codec 解码会抛
   `UnicodeDecodeError`（如 `can't decode byte 0xfe`），而 `gb18030` 是超集
   可完全解出。`safe_decode()` 已做 GB 系→`gb18030` 兜底链——解码失败**勿直接
   回退 utf-8**（会产生乱码并触发 MojibakeDetector 误报"编码损坏"）。

## 工程约定（用户偏好）
- 极简风格：砍非核心特性（如曾移除 Markdown/PDF/TXT 导出、robots 合规）。
- 先提案 → 再复盘砍过度设计，是固定两轮工作流。
- 改动后跑 `pytest tests/` + `py_compile` 验证；CLI 用 `python -m novel`（非 `python novel`）。
