# 代码评审：性能问题与安全隐患

> 评审范围：`novel/` 全部 42 个 Python 源文件（引擎、HTTP、存储、中间件、管道、适配器、导出、CLI）  
> 结论：42 个文件语法全部通过 `py_compile`。架构清晰、SQL 全部参数化（无 SQL 注入）。  
> 但发现 **2 个高危性能问题 + 1 个高危安全问题**，以及若干中低危项，按优先级列于下方。

---

## 修复状态（2026-08-18）

按用户要求，除 **H3 SSRF** 外全部修复完毕。核心改动是引入统一 `Fetcher`
（`novel/core/fetcher.py`）并注入适配器，使所有真实请求都走 UA 轮换 / 限速 /
编码 / 校验 / 重试中间件链——一次性解决 H1、H2、M1、M5 的根因；其余为中低危定点修复。

| 编号 | 问题 | 状态 |
|------|------|------|
| H1 | 每章下载两次 | ✅ 已修复 |
| H2 | 请求中间件对真实请求失效 | ✅ 已修复（Fetcher 注入适配器） |
| H3 | SSRF（图片 URL） | ⏸️ **按用户要求未修复** |
| M1 | 站点级限速被忽略 | ✅ 已修复 |
| M2 | 增量过滤 N+1 查询 | ✅ 已修复（批量 IN 查询） |
| M3 | 逐条 commit + 资源表重复插入 | ✅ 已修复（统一提交 + 图片去重） |
| M4 | 路径穿越（chapter_id 进路径） | ✅ 已修复（safe_path_segment） |
| M5 | 重试耗尽把错误页当正文 | ✅ 已修复（FetchError 抛出，计入 failed） |
| L3 | 引擎级与中间件级重复重试 | ✅ 顺带修复（重试统一收口到 Fetcher） |

验证：42 文件 `py_compile` 全通过；原有 12 个 pytest 用例全过；新增 Fetcher 集成
校验（UA 轮换 / gbk 解码 / 503 重试耗尽抛 FetchError / 200 仅请求一次）、
M1 限速抬高 / M2 批量过滤 / M3 图片去重 / M4 路径净化均通过。

---

## 🔴 高危（High）

### H1. 每个章节被下载两次（性能 / 带宽 / 限流双重浪费）

- **位置**：`novel/engine/crawler.py:296` 引擎先 `http_client.fetch(request)` 拉章节正文；随后 `crawler.py:321` 调用 `adapter.get_chapter_content()`，而 wenku8 / ConfigAdapter 内部又通过 `self.fetch_text(url)`（`wenku8.py:241`、`config_adapter.py:148`）**再次拉取同一个 URL**。
- **影响**：单本 1000 章的小说实际发 ~2000 个请求。引擎那次 fetch 的 `Response` 除"是否触发重试"外被完全丢弃（适配器重抓、重解码、重解析）。纯属浪费：带宽 2×、限流等待 2×、延迟 2×。
- **修复**：引擎不要再自己抓章节正文。改为把"已解码文本/Response"传给适配器，或给适配器注入一个**经过中间件链的 fetch**（`adapter.fetch` 走 UA/限流/编码中间件）。这是下面 H2 的根因之一。

### H2. 请求中间件（UA 轮换 + 限速）对所有真实请求形同虚设（性能+正确性+封禁风险）【✅ 已修复】

- **位置**：`novel/core/adapter.py:74-96` 的 `BaseAdapter.fetch_text` 直接 `client.fetch(request)`，**不经过任何请求中间件**；而引擎只在 H1 那次"被丢弃的 fetch"上套了 `UserAgentMiddleware` / `RateLimitMiddleware`（`crawler.py:289-296`）。
- **影响**：
  1. **UA 轮换失效**——`get_novel_info` / `get_chapter_list` / `get_chapter_content`（绝大多数 HTML 请求）全部用 `AsyncHTTPClient` 里硬编码的默认 UA（`http/client.py:24`），UA 列表白写了。
  2. **HTML 页面完全不限速**——`RateLimitMiddleware`（config 里 3.0s）只作用在 H1 那次无效 fetch 上；真实 HTML 请求无间隔约束，只有插图下载走了引擎级 `RateLimiter`（`crawler.py:408`）。对"频率敏感、声明 5s"的 wenku8 而言，章节页会被以并发数（默认 5）全速打，极易被封。
- **修复**：让适配器走统一的、带中间件链的 fetch（同 H1）。这是本框架限速/反爬设计的根本性断裂。

---

### H3. SSRF：图片下载未校验 URL（高危安全）⏸️ **按用户要求未修复**

- **位置**：`novel/engine/crawler.py` 的 `_download_chapter_images` 中 `self.http_client.fetch_bytes(img.url)`，URL 来自抓取页的 `<img src>`，配合 `follow_redirects=True` 可被引向 `http://169.254.169.254/`（云凭据）、内网甚至 `file://`。
- **状态**：**本次按用户要求未修复**。若后续处理，建议加协议白名单（仅 `http/https`）+ 私网/链路本地地址（RFC 1918、`169.254.0.0/16`、`127.0.0.0/8`、`::1` 等）黑名单，并在解析后校验仍指向预期主机。

---

## 🟠 中危（Medium）

### M1. 【✅ 已修复】 站点级限速被忽略

- **位置**：`crawler.py:83` 用 `rate_limit or adapter.get_rate_limit()` 构全局 `RateLimiter`，但 CLI 传入的是 `delay or config.engine.default_rate_limit`（`main.py:365`、`main.py:446`），**从不读取 `adapter.get_rate_limit()`**。而该全局 limiter 只用于插图（`crawler.py:408`）；正文 HTML 限速由 `RateLimitMiddleware.interval=3.0`（config）决定。
- **影响**：wenku8 声明的 `5.0s`（`wenku8.py:49`）从未生效，始终 3.0s。与 H2 叠加，限流逻辑整体不可信。

### M2. 【✅ 已修复】 增量过滤 N+1 查询

- **位置**：`crawler.py` `crawl_novel` 逐章调用 `storage.chapter_exists(chapter_id, site)`（每章一次 SELECT），千章小说即千次查询。
- **修复**：新增 `filter_existing_chapters(chapter_ids, site)`，单次 `WHERE site_chapter_id IN (...)` 批量返回已存在集合，N+1 → 1 次查询。

### M3. 【✅ 已修复】 单连接逐条 commit，写入串行化 + 资源表重复插入

- **位置**：`sqlite_backend.py:217` `save_chapter` 每次 `commit()`；`sqlite_backend.py:282` `save_image` 每次 `commit()` 且无 upsert。
- **影响**：5 路并发下所有写都在同一个 aiosqlite 连接上串行、且每个小事务一次往返；重跑会在 `resources` 表产生重复图片记录。
- **修复**：批量提交（如每 N 章或每本提交一次）；`save_image` 加去重（按 `file_hash` 或 `(novel_id, remote_url)` 唯一）。

### M4. 【✅ 已修复】 路径穿越：ConfigAdapter 的 `chapter_id` 直接进入文件路径

- **位置**：`crawler.py:400` `img_dir = IMAGES_DIR / novel_internal_id / content.chapter_id`；`content.chapter_id` 来自抓取页（`config_adapter.py:226` 取 `data.get("chapter_id", data.get("url",""))`，可为任意字符串）。
- **影响**：构造 `chapter_id = "../../foo"` 可把插图写到 `data/images/{novel_id}/` 之外。`novel_internal_id` 是 UUID 安全，但 `chapter_id` 对配置型适配器是外部可控的。
- **修复**：对 `chapter_id` 做 `safe_filename` 式净化（仅 `[\w-]`），或 `Path` 解析后确认仍落在 `IMAGES_DIR` 下。

### M5. 【✅ 已修复】 重试耗尽后把错误页当正文存库

- **位置**：`retry.py:48` 重试次数耗尽时返回 `response`（非 None）；`crawler.py` 继续解析，把 5xx/封锁页当作正常章节内容写入。
- **修复**：耗尽时应返回 None 并计入 `failed`，而非当正文。

---

## 🟡 低危（Low）

- **L1. 响应体无大小上限**：`http/client.py:74` 直接 `resp.content`，超大/恶意响应会撑内存。建议加 `max_bytes` 保护。
- **L2. charset-normalizer 每响应全量统计**：`encoding.py:56-59` 对声明 `auto` 的站点全量分析（gbk/utf-8 配置会短路，影响小）。
- **L3. 引擎级重试与 RetryMiddleware 级重试重复计数**：`crawler.py:282-383` 与 `retry.py` 各自计数，导致额外 sleep。
- **L4. 明文凭据**：`config_adapter.py:250-275` 登录表单、`PostgresConfig.password` 以明文存 YAML/配置。本地工具风险低，建议文档提示。
- **L5. EPUB 未转义嵌入**：`epub.py` 把抓取的 `description`/`author_note`/标题原样塞进 XHTML，本地产物风险低，但导出前建议转义。
- **L6. 文档与实现漂移**：`TECHNICAL_DESIGN.md` 多处与代码不符——middleware 链"应用于所有请求"实际未覆盖适配器抓取（H2）；`ContentVerifyMiddleware` 文档说有字数检查但代码已移除；`crawler.py` 行号与文档对不上。建议同步。
- **L7. `iter_all_novels` 暴力 1..5000**：`wenku8.py:363` 对不存在 ID 也发整页请求，~小时级。设计使然，已有 `--start-id/--end-id` 兜底。

---

## ✅ 已确认安全/良好的点

- **无 SQL 注入**：`sqlite_backend.py` 全部使用 `?` 参数化；迁移用静态 `executescript`。
- **无 YAML 反序列化风险**：统一 `yaml.safe_load`（`config.py:89`、`registry.py:68`）。
- **FK 级联删除正确**：迁移里 `chapters/resources/volumes` 均带 `ON DELETE CASCADE`（`001_initial.sql`），`delete_novel` 可正确级联。
- **`safe_filename` 处理了 `/\\?*` 等**：EPUB 输出文件名安全（`text.py:81`）。

---

## 后续：2026-08-19 多线程重构（不影响本评审结论）

在本文评审之后，项目落地了多线程全站爬取（`novel/engine/worker_pool.py` + `crawl-all --workers N [--shard]`）。对安全/性能的补充结论：

- **未引入新的 SQL 注入**：多线程共享的仍是 `SQLiteBackend`，全部参数化查询（`?`）不变；WAL 并发写由 `isolation_level=None`（autocommit）+ 短事务提交 + `busy_timeout=200` + 模块级 `_INIT_LOCK` + `_commit_retry()` 兜底，已验证 6 worker × 2400 章 0 锁库、多线程并发写 288/288 零丢失。
- **未引入新的 SSRF/越权**：图片下载仍走 `http_client.fetch_bytes`，H3 状态不变（**仍未修复**，按用户要求排除）。
- **限速合规不变**：每个 worker 独立 `RateLimitMiddleware` 锁 + 5s 间隔，多线程是横向扩展而非提速破限；`--workers 1` 退化为原单线程。
- 本文其余 H1–H2、M1–M5、L3 修复结论仍然有效（基于统一 `Fetcher` 中间件链）。


