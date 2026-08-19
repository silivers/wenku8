# 使用说明（USAGE）

通用轻小说爬虫框架 `novel` 的命令行使用指南。本文档聚焦"怎么用"，架构与扩展方式见 `README.md`。

> **重要前提**：项目只支持全站爬取（单本 `crawl` 命令已移除）。`crawl-all` 与
> `images crawl` 默认走 **多线程池**（config `crawl.workers` / `images.workers`，默认 4）
> ——每个 worker 在独立 OS 线程 + 独立事件循环内构建私有引擎实例（含独立的
> `RateLimitMiddleware` 限速预算），≈ N× 吞吐，且每个 worker 仍各自守限速
> （正文 `crawl.rate_limit` 默认 5s、图片 `images.interval` 默认 1s）。设为 1 即单线程。

---

## 0. 先解决最常见的报错

如果你遇到：

```
python novel sites info wenku8
ModuleNotFoundError: No module named 'novel'
```

原因是 `novel` 是个**包**不是脚本，`python novel` 会把 `novel/` 目录当包入口去跑，导致 `import novel` 失败。正确写法是下面两种之一：

**方式一（推荐，无需安装）：`python -m novel`**

```bash
cd F:\project\novel
python -m novel sites info wenku8
```

`python -m novel` 会把当前目录（`F:\project\novel`，即包含 `novel` 包的那一层）加入 `sys.path`，包即可导入。
⚠️ 必须在项目根目录 `F:\project\novel` 下执行，在子目录里跑会再次报同样的错。

**方式二（装一次，之后用 `novel` 命令）**

```bash
cd F:\project\novel
pip install -e .
novel sites info wenku8
```

本文后续示例统一用 `python -m novel`（最稳，不依赖是否装过包）。已 `pip install -e .` 的可自行把 `python -m novel` 替换为 `novel`。

---

## 1. 安装

```bash
cd F:\project\novel
pip install -r requirements.txt      # 必须先装运行依赖（httpx/aiosqlite/click/lxml/...）
pip install -e .                     # 可选：注册 novel 命令
```

依赖装好后**开箱即用**，无需额外配置——未指定配置文件时，程序会自动使用内置的 `config/default.yaml`。

---

## 2. 配置

配置文件优先级：**命令行 `--config` > 环境变量 > 找到的 YAML > 默认值**。

加载逻辑（`novel/config.py`）：未传 `--config` 时，先找 `./novel.yaml`，找不到则回落到 `./config/default.yaml`。

```bash
python -m novel config init           # 生成 ./novel.yaml（可选，基于默认配置导出可改项）
python -m novel config show           # 查看当前生效配置
```

`novel.yaml` / `config/default.yaml` 关键字段：

| 字段 | 默认 | 说明 |
|---|---|---|
| `engine.max_concurrency` | 5 | 章节同时爬取的协程上限（`crawler.py` 中用 `asyncio.Semaphore` 限制） |
| `engine.default_rate_limit` | 3.0 | 默认请求间隔（秒） |
| `storage.sqlite.path` | `data/novels.db` | SQLite 数据库路径 |
| `http.timeout_connect` / `timeout_read` | 10 / 30 | 连接 / 读取超时（秒） |
| `http.proxy` | 无 | 全局代理，也可用环境变量 `NOVEL_PROXY` |
| `middleware` | — | `user_agent` / `rate_limit` / `retry` / `encoding` / `content_verify` |
| `pipeline` | — | `html_cleaner` / `text_normalizer` / `deduplication` / `quality_validator` |

环境变量覆盖：`NOVEL_DB_PATH`（数据库路径）、`NOVEL_LOG_LEVEL`（日志级别）、`NOVEL_PROXY`（代理）。

部分站点（如 wenku8）需要登录才能获取完整数据，账号密码通过环境变量提供（**不建议写进配置文件**，避免明文泄露）：

```bash
export WENKU8_USERNAME="your_account@qq.com"
export WENKU8_PASSWORD="your_password"
```

---

## 3. 全局选项

所有子命令都支持：

```
--config <path>      指定配置文件
--db <path>          指定数据库路径（覆盖配置）
--log-level <level>  debug / info / warning / error
--json               以 JSON 格式输出（便于脚本解析）
```

---

## 4. 站点与搜索

```bash
python -m novel sites list                 # 列出所有已注册适配器
python -m novel sites info wenku8          # 查看站点信息（URL / 编码 / 限速）
python -m novel sites login wenku8         # 登录站点，验证账号密码 / 获取会话 Cookie
python -m novel search wenku8 "刀剑神域"      # 搜索，拿到 novel_id
python -m novel search wenku8 "刀剑神域" --page 2
```

---

## 5. 全站爬取正文

项目只支持全站爬取（单本爬取命令已移除）；线程 / 分块 / 限速等默认值全部在
`config/default.yaml` 的 `crawl:` 段配置。

```bash
# 全站爬取正文（默认 4 线程 + 分块领取，ID 递增遍历、自动续爬）
python -m novel crawl-all wenku8

# 最多爬 50 本 / 从指定 ID 开始（不填则从 site_meta 记录的精确数量 +1 续爬）
python -m novel crawl-all wenku8 --max-novels 50
python -m novel crawl-all wenku8 --start-id 1

# 登录后爬取（账号密码：CLI 参数 或 环境变量 WENKU8_USERNAME/WENKU8_PASSWORD）
python -m novel crawl-all wenku8 --username "acc@qq.com" --password "pwd"

# 爬完自动导出 EPUB
python -m novel crawl-all wenku8 --auto-export
```

> **多线程与分块（config `crawl:` 段）**：`workers`（默认 4）= 线程数——每个 worker
> 在独立 OS 线程 + 独立事件循环内构建**完整且私有**的引擎（含独立的限速预算），
> 因此 N 个 worker ≈ N× 吞吐，且每个 worker 仍各自守 `rate_limit`（默认 5s）限速
> （合规横向扩展）。`shard: true`（默认）启用分块领取：外层按 500 分块
> （1-500、501-1000…），所有 worker 在当前块内按「序号最低的空闲书号」细粒度领取
> （单本粒度、块内负载均衡），整块领完切下一块；单个整空白块判定全局末尾，
> 记录最后有效书号 = 网站总书数（不依赖固定上界、不漏爬）。共享的 WAL 数据库
> 已加 `busy_timeout` + 写重试 + 初始化锁，多线程并发写安全。
>
> **登录（wenku8 等需登录站点）**：提供 `--username`/`--password`（或环境变量
> `WENKU8_USERNAME`/`WENKU8_PASSWORD`）后，命令开始时会先调用 `adapter.login()`，
> 登录态 Cookie 会贯穿本次爬取的全部请求（章节正文、封面、插图）。也可先单独跑
> `python -m novel sites login wenku8` 验证凭据。

爬取结果：章节入库 `data/novels.db`，正文内保留远程图片 URL；图片下载后存
`data/covers/`（封面）与 `data/images/`（插图），正文 URL 同步回写为本地路径。

> wenku8 全站遍历采用 **ID 递增、不设上限**：从起始 ID 到无穷，连续 100 个无效
> 编号即判定到站点末尾；结束后把最后一个有效编号作为精确数量存入 `site_meta`，
> 下次自动从「精确数量 + 1」续爬（自动续爬，无需 --resume）。

---

## 5.1 图片轮询爬取（独立阶段）

```bash
# 一轮全站遍历，下载全部待下载图片；失败图下一轮自动重试，直到全部成功
python -m novel images crawl wenku8

# 查看下载状态：各状态计数 / 当前轮次 / 待下载书目
python -m novel images status wenku8
```

> 线程 / 并发 / 间隔 / 轮数由 config `images:` 段控制：`workers`（默认 4，>=2 走
> 多线程池 ≈ N× 吞吐）、`concurrency`（单本图片并发，默认 4）、`interval`
> （图片请求间隔秒，默认 1.0——走 CDN，与正文 5s 限速无关）、`max_rounds`
> （单张图最大尝试轮数，默认 10，超过置 skipped 防死循环）。
>
> 轮询语义：一轮 = 全站所有「有 pending/failed 图片且 book_status='active'」的书
> 各下载一遍；失败图片 attempt_count+1，参与下一轮；达到 `max_rounds` 仍失败
> 置 `skipped`（死链兜底）。下架书（`book_status='takedown'`，文本爬取时
> `is_takedown()` 自动标记）整书跳过。

---

## 8. 增量更新与导出

```bash
python -m novel update                 # 检查所有已存小说的新章节
python -m novel update --site wenku8   # 仅某站点

python -m novel export <novel_id>                       # 导出 EPUB（含嵌入插图）
python -m novel export <novel_id> --output out.epub      # 指定输出路径
```

---

## 9. 查看与管理

```bash
python -m novel list                    # 已爬小说列表
python -m novel info <novel_id>        # 小说详情 / 章节数
python -m novel stats                   # 统计：小说数 / 章节数 / 总字数 / 分站点
python -m novel delete <novel_id>      # 删除小说及关联数据（需二次确认）
```

---

## 10. 完整命令一览

| 命令 | 说明 |
|---|---|
| `sites list` | 列出可用站点 |
| `sites info <site>` | 查看站点信息 |
| `sites login <site> [--username U] [--password P]` | 登录站点，验证凭据 / 获取会话 Cookie |
| `search <site> <keyword> [--page N]` | 搜索小说 |
| `crawl-all <site> [--max-novels N] [--start-id N] [--auto-export] [--username U] [--password P]` | 全站爬取正文（可登录；线程/分块/限速由 config `crawl:` 段控制，默认 4 线程 + 分块） |
| `images crawl <site>` | 图片轮询爬取：一轮全站，失败图下一轮重试直至全部成功（下架书跳过；参数由 config `images:` 段控制） |
| `images status <site>` | 图片下载状态：各状态计数 / 当前轮次 / 待下载书目 |
| `update [--site <site>] [--username U] [--password P]` | 增量更新 |
| `export <novel_id> [--output PATH]` | 导出 EPUB |
| `list` | 已爬小说列表 |
| `info <novel_id>` | 小说详情 |
| `stats` | 统计信息 |
| `delete <novel_id>` | 删除小说（需确认） |
| `config init [--path PATH]` | 生成配置文件 |
| `config show` | 查看当前配置 |

---

## 11. 扩展新站点（两种适配器）

**ConfigAdapter（零代码，适合静态站点）**：在 `novel/adapters/configs/` 下写 YAML，声明 `search / novel_info / chapter_list / chapter_content` 的 URL 模板与 XPath 字段即可。

**CodeAdapter（写代码，适合 GBK、多页合并、登录态等复杂站点）**：继承 `novel.core.adapter.BaseAdapter`，实现 `search / get_novel_info / get_chapter_list / get_chapter_content`，放到 `novel/adapters/code/` 并在 `registry._load_builtin_adapters()` 注册。wenku8 即此模式。

---

## 12. 常见问题

**Q：`ModuleNotFoundError: No module named 'novel'`**
A：用了 `python novel ...`。改用 `python -m novel ...`（或先 `pip install -e .` 再用 `novel ...`），且必须在项目根目录 `F:\project\novel` 下执行。

**Q：单线程还是多线程？并发怎么控制？**
A：默认多线程——`crawl-all` 与 `images crawl` 的线程数由 config `crawl.workers` /
`images.workers` 控制（默认 4，>=2 走多线程池；设为 1 即单线程）。N 个 worker 各跑
独立引擎实例 ≈ N× 吞吐。并发由 `asyncio.Semaphore` 限制（章节 `engine.max_concurrency`、
图片 `images.concurrency`），频率由 config `crawl.rate_limit`（正文，默认 5s）与
`images.interval`（图片，默认 1s）控制。每个 worker 仍各自守限速，属合规横向扩展。

**Q：图片下载失败会重试吗？**
A：会。正文与图片已解耦：失败图片在 images 表标记 failed（attempt_count+1），
重跑 `python -m novel images crawl <site>` 会自动进入下一轮、只爬失败书的图片，
直到全部成功；超过 `images.max_rounds`（默认 10）仍失败才置 skipped（死链兜底）。

**Q：下架/版权删除的书还会爬图片吗？**
A：不会。文本爬取检测到下架页会把该书 `book_status` 置 `takedown`，图片轮询
查询按 `book_status='active'` 过滤自动跳过；恢复上架后把状态改回 active 即可续补。

**Q：爬到一半断了怎么办？**
A：无需任何操作，直接重跑 `python -m novel crawl-all <site>` 即可——全站遍历从
`site_meta.last_novel_id + 1` 自动续爬，已爬章节按差集跳过，只补缺失章节。

**Q：失败章节会重爬吗？**
A：会。失败的章节未入库，续爬时不在"已存在"集合内，自动重爬。
