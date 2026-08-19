# 数据库设计文档（DATABASE_DESIGN）

> 版本：v4（对应 migration 001-004）| 更新：2026-08-19
> 存储后端：SQLite 单文件（默认 `data/novels.db`），WAL 模式，多线程并发写安全。
> 本文是数据库的**权威说明**；表结构以 `novel/storage/migrations/*.sql` 为准。

---

## 1. 概览

- **引擎**：SQLite + WAL（`journal_mode=WAL` / `synchronous=NORMAL` / `busy_timeout=200`），
  连接必须 `isolation_level=None`（真 autocommit，避免残留读事务触发 `SQLITE_LOCKED`）。
- **写入模型**：短事务——每个写操作（`save_chapter`/`save_images`/`mark_image_*`）写完即
  commit，写锁持有毫秒级；冲突由 `_run_with_lock_retry()` 指数退避兜底。
- **迁移机制**：`schema_migrations` 记录已应用版本，`novel/storage/migrations/{NNN}_*.sql`
  按版本号升序执行，仅执行 `version > MAX(version)` 的文件。

**迁移版本一览**

| 版本 | 文件 | 内容 |
|---|---|---|
| 001 | `001_initial.sql` | 初始六表：novels / volumes / chapters / resources / crawl_states / schema_migrations |
| 002 | `002_add_images.sql` | `chapters.images` 列（JSON 文本，已废弃） |
| 003 | `003_site_meta.sql` | `site_meta` 表（全站遍历精确数量） |
| 004 | `004_image_crawl.sql` | `novels.book_status`；新 `images` 表（替代 resources）；存量数据迁移；DROP `resources` 与 `chapters.images` |

---

## 2. 表设计

### 2.1 `novels` — 小说元信息

每本小说一行，全书关联枢纽。唯一键 `(site, site_novel_id)`。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 内部 UUID（跨表关联键） |
| site | TEXT NOT NULL | 站点名（如 wenku8） |
| site_novel_id | TEXT NOT NULL | 站内书号 |
| title / author / description | TEXT | 元信息 |
| cover_url / cover_local | TEXT | 封面远程 URL / 本地路径（图片阶段下载成功后写 cover_local） |
| status | TEXT | 站点侧连载状态（如 连载/完结），**非下架标记** |
| **book_status** | TEXT NOT NULL DEFAULT 'active' | **书籍状态**：`active` 正常 / `takedown` 下架（文本爬取 `is_takedown()` 命中写入）/ `deleted` 预留 |
| categories / tags / extra | TEXT | JSON 文本（数组/对象） |
| word_count / last_updated / crawl_time | INTEGER / TEXT | 统计与时间戳 |

约束与索引：`UNIQUE(site, site_novel_id)`；`idx_novels_site(site)`。

**关键点**：`book_status` 是图片轮询的过滤依据（`get_novels_with_pending_images` 只处理
active 的书）；图片行保留不动，恢复上架把状态改回 active 即可续补。

### 2.2 `volumes` — 卷信息（遗留，未使用）

| 字段 | 说明 |
|---|---|
| id / novel_id | 内部 UUID / 外键（级联删除） |
| name / idx / cover_url / cover_local | 卷名 / 序号 / 封面 |

**现状**：schema 遗留结构，代码从未写入；章节卷信息用 `chapters.volume` 文本列承载。

### 2.3 `chapters` — 章节正文

每章一行，唯一键 `(novel_id, site_chapter_id)`。**注意：章节 ID 是各书独立的相对编号，
查询必须限定 novel_id**（M6 修复：`filter_existing_chapters` 已加 novel_id 参数）。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | TEXT PK | 内部 UUID |
| novel_id | TEXT NOT NULL FK | → novels.id（级联删除） |
| volume_id | TEXT | 遗留外键，从未写入 |
| site_chapter_id | TEXT | 站点章节号（"1"、"2"…） |
| title / chapter_index | TEXT / REAL | 标题 / 章序（支持 1.5 卷间章） |
| content / content_format | TEXT | 正文 HTML / 格式（"html"）。**图片阶段成功后把远程 URL 回写为本地路径** |
| word_count / author_note | INTEGER / TEXT | 字数 / 作者语 |
| is_crawled / crawl_time / content_hash | INTEGER / TEXT / TEXT | 爬取状态 / 时间 / SHA-256（去重用） |
| volume | TEXT | 卷名（实际使用的卷信息） |

约束与索引：`UNIQUE(novel_id, site_chapter_id)`；`idx_chapters_novel(novel_id)`、
`idx_chapters_site(site_chapter_id, novel_id)`。

**关键点**：`chapters.images` 列已在 004 迁移 DROP——图片元数据只存 `images` 表，正文
HTML 中保留远程 URL 直到图片下载成功回写（单轨存储）。

### 2.4 `images` — 图片下载任务表（004 新增，替代 resources）

图片独立爬取的状态机载体。**本表是图片元数据的唯一事实来源**。

| 字段 | 类型 | 说明 |
|---|---|---|
| id | INTEGER PK AUTOINCREMENT | 行号 |
| novel_id | TEXT NOT NULL FK | → novels.id（级联删除） |
| chapter_site_id | TEXT | 站点章节号（落盘目录与回写定位用）；封面为 NULL |
| resource_type | TEXT NOT NULL DEFAULT 'illustration' | `cover` / `illustration` |
| remote_url | TEXT NOT NULL | 远程 URL |
| local_path | TEXT | 本地落盘路径（NULL = 未成功） |
| file_hash | TEXT | SHA-256 |
| image_order | INTEGER NOT NULL DEFAULT 0 | 章内序号（决定文件名 `img_{order:04d}`） |
| status | TEXT NOT NULL DEFAULT 'pending' | 见下方状态机 |
| attempt_count | INTEGER NOT NULL DEFAULT 0 | 已尝试轮数（= 当前轮次） |
| last_error | TEXT | 最近一次失败原因 |
| last_attempt_at / created_at / updated_at | TEXT | 时间戳 |

约束与索引：`UNIQUE(novel_id, remote_url)`（幂等登记关键）；`idx_images_status(status)`、
`idx_images_novel_status(novel_id, status)`。

**状态机**：

```
pending ──下载成功──> downloaded（终态）
   │                        ↑
   └──下载失败──> failed ───┘（下一轮重试，attempt_count+1）
                    │
                    └──attempt_count ≥ max_rounds──> skipped（终态，死链兜底）
```

**关键点**：
- `UNIQUE(novel_id, remote_url)` + `INSERT OR IGNORE`：文本阶段断点续爬/重爬不产生重复行。
- 轮次隐含于 `attempt_count`（无轮次表）：一轮全站遍历 = 失败图 attempt_count 集体 +1。
- 回写 HTML 按 novel_id 全量 REPLACE（同一 URL 跨章只登记一行，按章回写会漏）。
- 封面与插图同表：`resource_type='cover'` 且 `chapter_site_id IS NULL`。

### 2.5 `site_meta` — 站点遍历元数据（003 新增）

| 字段 | 类型 | 说明 |
|---|---|---|
| site | TEXT PK | 站点名 |
| last_novel_id | INTEGER NOT NULL DEFAULT 0 | **最后一个有效书号**（= 站点精确数量）；续爬起点 = 该值 + 1 |
| updated_at | TEXT | 回写时间 |

**关键点**：全站遍历只在**完整收尾**时更新（异常中断不写，下次重扫中断后的部分）；
续爬完整性由 crawl_site/worker 池收尾重扫缺章书保证（方案 A）——若重扫后仍有书
缺章，该值回退到「最小失败书号 - 1」。

### 2.6 `crawl_states` — 任务状态（遗留，无写入者）

| 字段 | 说明 |
|---|---|
| task_id / novel_id / site | 任务标识 |
| status / total_chapters / crawled_chapters | 状态与进度 |
| failed_chapters / last_chapter_id | JSON 失败列表 / 断点 |
| started_at / updated_at / error_message | 时间与错误 |

**现状**：001 建表，历史数据保留；v3.2 移除单本爬取与任务状态机后**无任何代码写入**，
后续可考虑在迁移中 DROP。

### 2.7 `schema_migrations` — 迁移版本

`version` INTEGER PK / `applied_at` TEXT。由 `SQLiteBackend._run_migrations()` 维护。

---

## 3. 表关系

```
novels 1 ──── n chapters        (novel_id, 级联删除)
novels 1 ──── n images          (novel_id, 级联删除)
novels 1 ──── 1 site_meta       (site, 按站点聚合，非外键)
volumes   (遗留，未关联写入)
crawl_states (遗留，无写入者)
```

所有查询跨表时均以 `novels.id`（内部 UUID）为键；`images.chapter_site_id` 与
`chapters.site_chapter_id` 是对应关系，但不设外键（章节可能先于图片登记）。

---

## 4. 读写路径（谁写哪张表）

| 阶段 | 写入 |
|---|---|
| 文本爬取 `crawl-all` | `novels`（save_novel UPSERT）、`chapters`（save_chapter）、`images`（save_images 登记 pending 插图+封面）、`novels.book_status`（is_takedown 命中 → takedown） |
| 收尾重扫（方案 A） | 同上（章节差集幂等，只补缺章）、`site_meta.last_novel_id` |
| 图片爬取 `images crawl` | `images`（mark_image_downloaded / mark_image_failed / skip_exhausted_images）、`chapters.content`（rewrite_chapter_image_url 回写本地路径）、`novels.cover_local`（封面成功） |
| 数据管理 | `novels`（delete 级联 chapters/images） |
| 增量更新 `update` | 同文本爬取（新章节/新图自动纳入） |

---

## 5. 常见运维 SQL

```sql
-- 某站图片状态一览
SELECT status, COUNT(*) FROM images i JOIN novels n ON i.novel_id=n.id
WHERE n.site='wenku8' GROUP BY status;

-- 待下载图片的书目（对应 images crawl 的轮询查询）
SELECT n.id, n.title, COUNT(*) FROM images i JOIN novels n ON i.novel_id=n.id
WHERE n.site='wenku8' AND n.book_status='active'
  AND i.status IN ('pending','failed') AND i.attempt_count < 10
GROUP BY n.id, n.title;

-- 查看失败图片原因
SELECT n.title, i.remote_url, i.attempt_count, i.last_error
FROM images i JOIN novels n ON i.novel_id=n.id
WHERE i.status='failed' ORDER BY i.last_attempt_at DESC LIMIT 50;

-- 把下架书恢复为可爬（续补图片）
UPDATE novels SET book_status='active' WHERE site='wenku8' AND site_novel_id='1234';
```

---

## 6. 与历史 schema 的差异（004 迁移要点）

1. `resources` 表已 DROP（原 `chapter_id` 恒为 NULL、与 chapters 无真实关联）；
   图片元数据全部收敛到 `images` 表。
2. `chapters.images` JSON 列已 DROP（双轨存储废弃，避免与 images 表不一致）。
3. `novels` 新增 `book_status` 列（默认 active）。
4. 存量数据：老 `chapters.images` JSON 与 `novels.cover_url` 在 004 迁移中统一登记为
   `images` 表 pending 行（重下策略——历史 resources 数据不可信，图片是 CDN 静态
   资源，重下成本远低于修脏数据）。
