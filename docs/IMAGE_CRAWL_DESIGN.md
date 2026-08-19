# 图片独立爬取设计（v1 提案）

> 状态：**已实现（2026-08-19）** —— 迁移 004 + 存储接口 + crawler 减法 +
> image_crawler.py + CLI images 命令组全部落地；多线程池（ImageWorkerPool）与
> 配置集中（v3.2）已同步。本文为设计定稿，代码与之一致（含「按 novel_id 全量
> 回写」加固，见 §4.2）。注：项目已按用户要求移除 tests/，验证走 py_compile +
> 一次性临时脚本。
> 日期：2026-08-19

## 1. 背景与目标

**现状问题**：

1. 正文与插图在同一链路内串行下载（`crawler.py:359-363` `_crawl_chapter` 内联调用
   `_download_chapter_images`），图片下载失败只打 warning 并从章节剔除
   （`crawler.py:456` `content.images = downloaded`），**失败图片永久丢失，无重试机制**。
2. 插图下载持有章节并发槽位（semaphore），一本多图小说拖慢整体章节吞吐。
3. 图片双轨存储：`resources` 表记录与 `chapters.images` JSON 列并存，且
   `resources.chapter_id` 恒为 NULL（`crawler.py:426`），表间无真实关联。

**目标**：

- 文本爬取与图片爬取彻底解耦为两个独立阶段、独立命令。
- 图片阶段采用**轮询遍历**：一轮全站爬完 → 从头开始下一轮，只爬有失败图片的书，
   直到全部成功。
- 下架/删除书目通过数据库书籍状态字段跳过。
- 顺带消灭双轨存储：`images` 表成为图片唯一事实来源。

## 2. 总体架构

```
阶段 A：文本爬取（现有 crawl / crawl-all，砍掉图片逻辑）
  抓正文 → 解析出 ImageRef 列表 → 批量写入 images 表（status=pending）
  HTML 保持远程 URL 不改写
  下架检测命中 → novels.book_status='takedown'

阶段 B：图片爬取（新命令 images crawl）
  while 存在 pending/failed 图片:
      取一批「有待下载图片且 book_status='active'」的书
      逐书下载其 pending/failed 图片
      成功 → 落盘 + status='downloaded' + 回写 HTML 中的 URL
      失败 → status='failed'，attempt_count+1
  attempt_count 达到 max_rounds 仍 failed → status='skipped'（死链兜底，防死循环）
```

两阶段可独立运行、独立重跑、独立限速（文本 5s/请求 vs 图片走 CDN 域名可放宽）。

## 3. 数据库设计（migration `004_image_crawl.sql`）

### 3.1 书籍状态：`novels.book_status`

```
'active'   正常（默认）
'takedown' 下架/版权删除（crawl_novel 中 is_takedown() 命中时写入）
'deleted'  已删除（预留，未来 update 命令发现书页消失时写入）
```

图片轮询查询统一 `WHERE book_status='active'` 过滤；被标记的书**图片行保留不动**，
若未来恢复上架可直接补爬（只需把 book_status 改回 active）。

### 3.2 图片任务表：`images`（替代 `resources`，承载下载状态机）

```sql
CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id TEXT NOT NULL,                  -- novels.id（内部 UUID）
    chapter_site_id TEXT,                    -- 站点章节号；封面为 NULL
    resource_type TEXT NOT NULL DEFAULT 'illustration',  -- 'cover' | 'illustration'
    remote_url TEXT NOT NULL,
    local_path TEXT,                         -- NULL = 尚未成功
    file_hash TEXT,
    image_order INTEGER NOT NULL DEFAULT 0,  -- 章内序号，决定文件名 img_0001
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | downloaded | failed | skipped
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_attempt_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(novel_id, remote_url),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_images_status ON images(status);
CREATE INDEX IF NOT EXISTS idx_images_novel_status ON images(novel_id, status);
```

状态机：

```
pending ──下载成功──> downloaded（终态）
   │                        ↑
   └──下载失败──> failed ────┘（下一轮重试）
                    │
                    └──attempt_count≥max_rounds──> skipped（终态，死链兜底）
```

要点：

- **`UNIQUE(novel_id, remote_url)`** 是幂等写入的关键：文本阶段重爬/断点续爬时
  `INSERT OR IGNORE`，同一张图不会产生重复行。
- **`chapter_site_id` 存站点章节号而非内部 UUID**：图片下载成功后要用它回写
  `chapters.content`（`UPDATE ... WHERE novel_id=? AND site_chapter_id=?`），
  且文件路径 `images/{novel_id}/{chapter_site_id}/` 本来就用的站点号。
- 「该小说图片总数」不需要专门字段：`SELECT COUNT(*) FROM images WHERE novel_id=?`
  即得（文本阶段一次性登记完）。

### 3.3 为什么不需要单独的「轮次表」

轮次信息已隐含在 `attempt_count` 里：一轮全站遍历 = 每本失败书的图片
attempt_count 集体 +1。「第几轮」只是观测口径（`MAX(attempt_count)`），
不参与调度，因此**不建 image_crawl_meta 表**，砍掉一张表和一套读写。

### 3.4 存量数据迁移

`resources` 表的 `chapter_id` 恒为 NULL、与 chapters 无关联，数据不可信；
`chapters.images` JSON 里只留了当年下载成功的图（失败的已被剔除）。

**决策：不信任存量图片记录，全部按 pending 重新登记、重新下载一次**
（图片是 CDN 静态资源，重下成本远低于修复脏数据的复杂度）：

```sql
-- 004_image_crawl.sql 全文

-- 1. 书籍状态
ALTER TABLE novels ADD COLUMN book_status TEXT NOT NULL DEFAULT 'active';

-- 2. 图片任务表
CREATE TABLE IF NOT EXISTS images ( ...如上... );

-- 3. 存量迁移：chapters.images JSON → pending 行（已下载过的也会重下，
--    落盘文件按同名覆盖，幂等）
INSERT OR IGNORE INTO images
    (novel_id, chapter_site_id, resource_type, remote_url, image_order, status)
SELECT c.novel_id, c.site_chapter_id, 'illustration',
       json_extract(j.value, '$.url'),
       COALESCE(json_extract(j.value, '$.index'), 0),
       'pending'
FROM chapters c, json_each(c.images) j
WHERE json_extract(j.value, '$.url') IS NOT NULL
  AND json_extract(j.value, '$.url') != '';

-- 4. 存量封面也登记为 pending（从 novels.cover_url）
INSERT OR IGNORE INTO images
    (novel_id, chapter_site_id, resource_type, remote_url, status)
SELECT id, NULL, 'cover', cover_url, 'pending'
FROM novels
WHERE cover_url IS NOT NULL AND cover_url != '';

-- 5. 旧表/旧列退役
DROP TABLE IF EXISTS resources;
ALTER TABLE chapters DROP COLUMN images;   -- 需要 SQLite ≥3.35，Python3.13 自带 3.4x，满足
```

> 备选（如不愿重下）：第 3 步后从 `resources` 回填 local_path 并置 downloaded。
> 复杂度换来的只是省一次下载，不推荐。

## 4. 重构思路（文件级 + 行级）

### 4.1 `novel/engine/crawler.py` — 做减法

| 位置 | 改动 |
|---|---|
| L359-363 | 删除 `if content.images: content = await self._download_chapter_images(...)`，改为 `await self.storage.save_images(novel_internal_id, content.chapter_id, content.images)`（批量登记 pending，每章一次 commit，沿用短事务铁律） |
| L264-281 | 封面下载块整体删除，改为 `save_images(novel_internal_id, None, "cover", [ImageRef(url=novel_info.cover_url)])` |
| L332-338 | `is_takedown()` 命中处追加 `await self.storage.set_novel_status(novel_internal_id, "takedown")` |
| L386-474 | 删除 `_download_chapter_images`、`_detect_image_ext`（迁往 image_crawler） |
| L37-52 | `RateLimiter` 类迁往 `novel/engine/image_crawler.py`（文本引擎不再需要） |
| L75/L94-95/L114 | 构造参数删 `image_concurrency`、`rate_limit`（插图限速） |
| L377 | 日志 `({len(content.images)} 图)` 改为 `(登记 N 图)` 口径 |

注意：HTML **不再在文本阶段改写**，`chapters.content` 保留远程 URL，由图片阶段
成功后回写。`save_chapter` 相应删掉 images JSON 序列化（列已 DROP）。

### 4.2 新增 `novel/engine/image_crawler.py` — 核心轮询逻辑

```python
class ImageCrawler:
    """图片独立爬取：轮询遍历直到全部成功。"""

    def __init__(self, adapter, storage, http_client, *,
                 image_concurrency=4, interval=1.0, max_rounds=10):
        self.rate_limiter = RateLimiter(interval)   # 从 crawler.py 迁入
        ...

    async def run(self, site: str) -> ImageCrawlReport:
        while True:
            novels = await self.storage.get_novels_with_pending_images(site)
            # SQL: SELECT n.id, n.title, COUNT(*) pending
            #      FROM images i JOIN novels n ON i.novel_id=n.id
            #      WHERE i.status IN ('pending','failed')
            #        AND i.attempt_count < ?
            #        AND n.book_status='active'
            #      GROUP BY n.id
            if not novels:
                break                              # 全部成功，收工
            round_no = novels_max_attempt + 1      # 仅日志口径
            for nv in novels:
                await self._crawl_novel_images(nv["id"])
        # 兜底：把 attempt_count>=max_rounds 仍 failed 的置 skipped
        await self.storage.skip_exhausted_images(self.max_rounds)

    async def _crawl_novel_images(self, novel_internal_id: str) -> None:
        rows = await self.storage.get_pending_images(novel_internal_id)
        sem = asyncio.Semaphore(self.image_concurrency)
        await asyncio.gather(*[self._download_one(sem, r) for r in rows])

    async def _download_one(self, sem, row) -> None:
        async with sem:
            await self.rate_limiter.wait()
            try:
                data = await self.http_client.fetch_bytes(row["remote_url"])
                if not data:
                    raise FetchError("empty body")
                # 落盘：沿用 data/images/{novel_id}/{safe(chapter)}/img_{order:04d}{ext}
                # 封面：data/covers/{novel_id}.jpg + UPDATE novels.cover_local
                local, epub_rel, file_hash = self._write_file(row, data)
                await self.storage.mark_image_downloaded(
                    row["id"], local, file_hash)
                # 回写 HTML：UPDATE chapters SET content=replace(content,?,?)
                #            WHERE novel_id=?（全量 REPLACE，不带章节条件）
                await self.storage.rewrite_chapter_image_url(
                    row["novel_id"], row["remote_url"], epub_rel)
            except Exception as e:
                await self.storage.mark_image_failed(row["id"], str(e))
```

> **加固（复盘结论，代码已按此落地）**：回写按 `WHERE novel_id=?` 全量 REPLACE，
> **不带 site_chapter_id 条件**——`images` 表 `UNIQUE(novel_id, remote_url)` 意味着
> 同一 URL 跨章只登记一行，按章节回写会漏；全量 REPLACE 对已替换章节是 no-op
> （URL 已不存在），幂等且把跨章重复 URL 一并覆盖。`rewrite_chapter_image_url`
> 签名相应为 `(novel_id, remote_url, local_url)` 三参。

`_write_file` 内含原 `_detect_image_ext`（按文件头魔数）+ `safe_path_segment`
路径净化（防穿越逻辑原样迁移，M4 修复不回退）。

**防死循环兜底**：死链（书真删了但未被标记 takedown、CDN 404）会永远 failed。
`max_rounds` 默认 10，耗尽置 `skipped` 并在报告里列出，人工决定是否处理。
「直到全部成功」在工程上必须收敛为「直到全部 downloaded 或 skipped」。

### 4.3 `core/storage.py` + `sqlite_backend.py` — 接口变更

新增抽象方法（全部短事务、每调用一次 commit，沿用 WAL 三铁律）：

```python
async def save_images(novel_internal_id, chapter_site_id, resource_type,
                      images: list[ImageRef]) -> int
    # INSERT OR IGNORE INTO images(...)（UNIQUE 幂等），返回插入条数

async def get_novels_with_pending_images(site, max_attempts=10,
                                         limit=None) -> list[dict]
    # 只返回 attempt_count < max_attempts 且 book_status='active' 的书
async def get_pending_images(novel_internal_id) -> list[dict]
    # status IN ('pending','failed') ORDER BY chapter_site_id, image_order
async def count_pending_images(site, site_novel_id) -> int   # CLI 提示用

async def mark_image_downloaded(image_id, local_path, file_hash) -> None
async def mark_image_failed(image_id, error) -> None
    # status/attempt_count/last_error/last_attempt_at 一次 UPDATE
async def skip_exhausted_images(max_rounds) -> int

async def rewrite_chapter_image_url(novel_id, remote_url, local_url) -> None
    # UPDATE chapters SET content=replace(content, remote_url, local_url)
    # WHERE novel_id=? （全量 REPLACE，幂等；见 §4.2 加固）

async def set_novel_status(novel_internal_id, status) -> None
async def set_novel_cover(novel_internal_id, local_path) -> None
    # 封面下载成功后写 novels.cover_local
async def get_image_stats(site) -> dict   # 各 status 计数 + 当前轮次 + 待下载书目数
```

删除：`save_image()`（被 save_images/mark_* 取代）。`get_stats()` 扩展图片状态计数。
顺带修复 M6：`filter_existing_chapters` / `chapter_exists` 增加 `novel_id` 限定，
杜绝跨书章节 ID 撞号误跳。

### 4.4 多线程（**已实现 2026-08-19**，`ImageWorkerPool` 位于 `image_crawler.py`）

轮次天然是 barrier（已按此落地）：

- 每轮主线程查 `get_novels_with_pending_images` 把 novel_id 入队；
- worker 线程各自在自己的事件循环里构建 `ImageCrawler`（独立 RateLimiter =
  N× 吞吐，与现有 worker_pool 提速原理一致）；
- `queue.get_nowait` 原子领取（队列排空即本轮结束，无需毒丸），主线程再查下一轮；
- 线程红线不变：跨线程只传 novel_id 标量、每线程独立 storage/client 实例、
  唯一共享是 WAL 库。
- CLI：`images crawl`（无参数）——线程/并发/间隔/轮数由 config `images:` 段读取；
  `workers>=2` 走池（默认 4），设为 1 即单线程。

离线验证（本地 http.server 打桩，2 worker / 3 书 5 图 / 2 死链）：2 轮收敛、
3 成功、2 skipped、0 剩余，文件落盘正确。

### 4.5 CLI（`novel/cli/main.py`）

```
python -m novel images crawl <site>   # 无参数：线程/并发/间隔/轮数全从 config images: 段读取
python -m novel images status <site>  # 各状态计数、当前轮次、待下载书目数
```

- 配置项（config/default.yaml `images:` 段）：`workers`（默认 4，>=2 走
  ImageWorkerPool）、`concurrency`（单本并发，默认 4）、`interval`（间隔秒，
  默认 1.0）、`max_rounds`（最大轮数，默认 10）——CLI 无散落默认参数。
- `crawl-all`：正文阶段只登记图片，流程结束后提示「N 张图片待下载，运行
  `novel images crawl` 补齐」。
- `export`：导出前查该小说 pending 图片数，>0 时警告「图片未全部就绪」。
- `update`：新章节产生的 ImageRef 同样走 `save_images` 登记，天然纳入图片轮询。

### 4.6 `exporters/epub.py` — 基本不动

HTML 回写方案保证：图片成功后 `chapters.content` 中的 URL 已是
`images/{chapter}/{file}` 本地相对路径，与现有 `_embed_images` 的正则匹配
（`epub.py:209-238`）完全兼容，导出器零改动。

> 备选方案「导出时经 images 表动态解析远程 URL」被否：要改导出器、HTML 里
> 长期残留远程 URL、且导出性能依赖表 JOIN——收益不抵复杂度。

## 5. 关键决策摘要

| 决策 | 理由 |
|---|---|
| HTML 回写而非导出时解析 | 导出器零改动；数据自洽（content 即最终形态） |
| 轮次隐含于 attempt_count，无轮次表 | 少一张表一套读写，轮次仅是日志口径 |
| 存量图片全部按 pending 重下 | resources 数据不可信，重下成本 < 修数据复杂度 |
| failed 图片行保留而非删除 | book_status 恢复 active 后可自动续补 |
| max_rounds=10 兜底置 skipped | 「直到全部成功」需收敛，防死链死循环 |
| 下架书在查询侧过滤，不改图片行 | 数据可逆，逻辑集中在一条 SQL |

## 6. 顺带发现的存量 bug（建议一并修）

`sqlite_backend.py:394-407` `filter_existing_chapters`（及 `chapter_exists`
L384-392）只按 `site_chapter_id + site` 匹配、**未限定 novel_id**。wenku8 章节
ID 是各书独立的相对编号（"1"、"2"...），多书爬取时 B 书章节会被 A 书的同名
章节误判为已爬而跳过。shard 模式全站爬取**必然触发**。修法：查询加
`c.novel_id=?`（调用方 `crawler.py:224` 传入 novel_internal_id）。
本方案不依赖此修复，但动 storage 层时顺手改掉。

## 7. 实施步骤（均已落地）

1. **migration 004** + sqlite_backend 新接口（含 6. 的 bug 修复）
2. **crawler.py 做减法**（删图片链路、加登记与 takedown 状态写入）
3. **image_crawler.py**（单线程版 + RateLimiter 迁移）
4. **CLI images 命令组** + crawl-all 参数清理 + export 警告
5. 验证（py_compile + 一次性临时脚本；tests/ 已按用户要求移除）
6. （二期）ImageWorkerPool 多线程池接入图片轮询
7. （v3.2）配置集中：images 段参数移入 config/default.yaml
