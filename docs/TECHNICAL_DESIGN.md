# NovelCrawler 技术设计文档

> **版本**: v2.0 | **日期**: 2026-08-13
> 本文档描述程序的**实际实现细节**：数据如何爬取、如何流转、如何存储、如何导出。
> 文档目标：后续二次开发或扩展时，能根据本文档把握整体架构和技术细节，并快速定位需要修改的代码文件。

> **⚠️ 文档状态（2026-08-19 校订）**：本文档基于 v2.0 实现，已落后于当前代码（v3.0+ 多线程重构）。以下关键能力已落地但本文档未覆盖，请以代码为准：
> - **登录态**：`Wenku8Adapter.login()` + `http_client.set_cookies()`，CLI `sites login` 与 `crawl`/`crawl-all`/`update` 的 `--username/--password`（见 `WENKU8_LOGIN_REPORT.md`）。
> - **章节 URL 分桶**：真实路径为 `/novel/{id//1000}/{id}/index.htm`（非 `/book/{id}.htm`），由 `_build_chapter_index_url()` 生成；`id<1000` 凑巧正确，`id>=1000` 旧写法全 404。
> - **下架检测修复**：`is_takedown()` 只认精确提示语 `因版权问题，文库不再提供该小说的阅读`，不再把页脚版权声明当信号误杀正常章节。
> - **正文广告过滤**：模块级 `_AD_TEXT_PATTERNS` + `get_chapter_content` 内的 `_strip_ad_text()`。
> - **多线程全站爬取**：`novel/engine/worker_pool.py`（`CrawlWorkerPool`），`crawl-all --workers N [--shard]`，每 worker 独立引擎实例（含独立 `RateLimitMiddleware` 限速预算）≈ N× 吞吐（见 `MULTITHREAD_DESIGN.md`）。
> - **全站遍历**：ID 递增、连续 100 个无效编号即止、自动续爬（`--start-id` 覆盖起点），**无** `--class-id` / `--end-id`。
> - **SQLite 并发写**：WAL + `isolation_level=None`（真 autocommit）+ `PRAGMA busy_timeout=200` + 模块级 `_INIT_LOCK` + 每个写操作（save_chapter/save_images）写完即提交（短事务）+ `_commit_retry()` 退避重试。
> - **图片独立爬取（v3.1）**：正文与图片彻底解耦——`crawl-all` 只爬正文并把图片登记到新 `images` 表（pending），插图/封面由 `images crawl` 轮询下载（失败图下一轮重试直至全部成功，`images.max_rounds` 兜底置 skipped；下架书经 `novels.book_status` 跳过）。`resources` 表与 `chapters.images` 列已在 004 迁移中退役。图片链路的权威说明见 `IMAGE_CRAWL_DESIGN.md`（已实现）；本文 §7 / §9 的 resources/save_image 描述为历史快照。
> - **精简（v3.2）**：移除单本爬取（`crawl` 命令与 `--resume`/`--chapters`）、任务状态机（`tasks` 命令、`crawl_states` 读写、`engine/state.py` 已删）；限速 / 线程 / 并发等默认值全部集中到 `config/default.yaml` 的 `crawl:`（workers/shard/rate_limit）与 `images:`（workers/concurrency/interval/max_rounds）段，CLI 无散落默认参数。全站续爬只依赖 `site_meta.last_novel_id`。
> - **续爬完整性修复（方案 A，2026-08-19）**：`crawl_site` 与 worker 池收尾时重扫「缺章/整本失败」的书（`crawl_novel` 章节差集幂等，只补缺章）；重扫后仍失败的书把续爬起点回退到「最小失败书号 - 1」，防止「爬过但没爬完」的书被计入进度后缺章永久丢失。
> 正文个别行号/片段为历史快照，可能已偏移；命令与新增能力以 `USAGE.md`、`MULTITHREAD_DESIGN.md`、`IMAGE_CRAWL_DESIGN.md`、`WENKU8_LOGIN_REPORT.md` 为准。

---

## 目录

1. [系统总览](#1-系统总览)
2. [核心爬取流程](#2-核心爬取流程)
3. [适配器系统：如何对接不同站点](#3-适配器系统如何对接不同站点)
4. [HTTP 层与中间件链](#4-http-层与中间件链)
5. [编码处理机制](#5-编码处理机制)
6. [数据管道：清洗→归一化→去重→校验](#6-数据管道清洗归一化去重校验)
7. [插图处理：下载→存储→EPUB 嵌入](#7-插图处理下载存储epub-嵌入)
8. [全站爬取](#8-全站爬取)
9. [数据存储：SQLite Schema 与读写逻辑](#9-数据存储sqlite-schema-与读写逻辑)
10. [并发控制与限速](#10-并发控制与限速)
11. [断点续爬与增量更新](#11-断点续爬与增量更新)
12. [EPUB 导出](#12-epub-导出)
13. [配置系统](#13-配置系统)
14. [项目文件索引](#14-项目文件索引)
15. [二次开发指南：修改某功能时查找哪些文件](#15-二次开发指南修改某功能时查找哪些文件)

---

## 1. 系统总览

### 1.1 技术栈

| 层级 | 技术选型 | 用途 |
|:---|:---|:---|
| 运行时 | Python 3.13+ | 异步优先 |
| HTTP | httpx (AsyncClient) | 异步 HTTP 请求 |
| HTML 解析 | lxml | XPath 选择器 |
| 存储 | aiosqlite + SQLite | 异步 SQLite，单文件零配置 |
| 导出 | ebooklib | EPUB 3.0 生成 |
| CLI | click | 命令行交互 |
| 编码检测 | charset-normalizer | 自动编码识别 |
| 日志 | loguru | 结构化日志 |
| 配置 | PyYAML + dataclass | YAML 覆盖默认值 |

### 1.2 分层架构

```
┌──────────────────────────────────────────────────┐
│                 CLI (click)                       │  ← 用户交互层
│  crawl / crawl-all / search / export / list ...  │
├──────────────────────────────────────────────────┤
│                CrawlerEngine                      │  ← 调度层
│  (asyncio.gather + Semaphore + RateLimiter)      │
│  crawl_novel() / crawl_site()                    │
├───────────┬───────────┬───────────┬──────────────┤
│  Adapter  │ Middleware │ Pipeline  │  Exporter   │  ← 可插拔层
│  (站点)   │ (请求/响应)│ (数据处理)│  (EPUB)     │
├───────────┴───────────┴───────────┴──────────────┤
│          AsyncHTTPClient (httpx)                  │  ← 网络层
│          fetch() / fetch_bytes()                  │
├──────────────────────────────────────────────────┤
│          SQLiteBackend (aiosqlite)                │  ← 存储层
│          novels / chapters / resources / states   │
├──────────────────────────────────────────────────┤
│     文件系统: data/covers/ data/images/ output/   │  ← 文件存储
└──────────────────────────────────────────────────┘
```

引擎（`CrawlerEngine`）是唯一调度者。它持有适配器、中间件链、管道链、存储后端、HTTP 客户端五个组件，但**不感知任何站点细节**——所有站点知识封装在适配器中。

### 1.3 核心数据模型

定义在 `novel/core/models.py`，全部为 dataclass：

| 模型 | 用途 | 关键字段 |
|:---|:---|:---|
| `NovelInfo` | 小说元信息 | `site_id`, `title`, `author`, `cover_url`, `status`, `tags`, `categories` |
| `ChapterInfo` | 章节目录项 | `chapter_id`, `title`, `volume`, `index`, `url` |
| `ChapterContent` | 章节正文 | `chapter_id`, `title`, `content`, `images: list[ImageRef]`, `word_count`, `content_hash`, `volume` |
| `ImageRef` | 图片引用 | `url`, `caption`, `alt`, `index` |
| `VolumeInfo` | 卷信息 | `name`, `index`, `cover_url`, `chapter_ids` |
| `CrawlResult` | 爬取结果 | `novel`, `chapters`, `failed_chapters`, `skipped_chapters` |
| `CrawlState` | 断点续爬状态 | `task_id`, `status`, `total_chapters`, `crawled_chapters`, `failed_chapters` |

---

## 2. 核心爬取流程

用户执行 `python -m novel crawl wenku8 1234` 后，程序执行以下步骤：

### 2.1 完整流程图

```
CLI crawl 命令
    │
    ▼
create_engine_from_config()
    │  加载 YAML 配置 → 构建 6 个组件：
    │  storage, http_client, registry, req_mw, resp_mw, pipelines
    │
    ▼
registry.get("wenku8") → 返回 Wenku8Adapter 实例
    │
    ▼
CrawlerEngine(adapter, storage, http_client, req_mw, resp_mw, pipelines)
    │
    ▼
engine.crawl_novel("1234")
    │
    ├──→ 步骤1: adapter.get_novel_info("1234")
    │         → HTTP GET /novel/{1234//1000}/1234/index.htm（分桶路径，GBK 解码）
    │         → XPath 提取标题/作者/简介/封面/状态
    │         → storage.save_novel() → 返回 internal_id (UUID)
    │
    ├──→ 步骤2: adapter.get_chapter_list("1234")
    │         → HTTP GET /novel/{1234//1000}/1234/index.htm（分桶路径，GBK 解码）
    │         → XPath 遍历表格行，识别卷标题 + 章节链接
    │         → 返回 List[ChapterInfo]
    │
    ├──→ 步骤3: 章节范围过滤 (--chapters 1-10)
    │         → chapters[start-1 : end]
    │
    ├──→ 步骤4: 增量过滤
    │         → 对每章调用 storage.chapter_exists(chapter_id, site)
    │         → 已存在的跳过，未存在的加入 pending 列表
    │
    ├──→ 步骤5: 并发下载章节
    │         │  asyncio.gather(*[self._crawl_chapter(ch) for ch in pending])
    │         │
    │         │  每个 _crawl_chapter 内部（见 §2.2 详图）：
    │         │  ┌─ semaphore.acquire() (最多 5 并发)
    │         │  ├─ rate_limiter.wait() (间隔 + 随机抖动)
    │         │  ├─ 构造 Request(url=chapter.url)
    │         │  ├─ 请求中间件链: UA轮换 → 限速 → 重试
    │         │  ├─ http_client.fetch(request) → Response
    │         │  ├─ 响应中间件链: 编码检测 → 内容校验
    │         │  ├─ adapter.get_chapter_content() → ChapterContent
    │         │  │    (wenku8: 多页合并，GBK 解码，提取图片)
    │         │  ├─ 数据管道: HTML清洗 → 文本归一化 → SHA-256去重 → 质量校验
    │         │  ├─ 插图下载: 遍历 content.images → 下载到 data/images/ → 替换 HTML src
    │         │  └─ storage.save_chapter() → 写入 SQLite（含 images JSON）
    │         │
    │         └─ 异常的章节记入 failed 列表
    │
    ├──→ 步骤6: 下载封面
    │         → adapter.get_cover() → bytes
    │         → 写入 data/covers/{internal_id}.jpg
    │         → storage.save_image() 记录元数据
    │
    └──→ 返回 CrawlResult(novel, chapters, failed, skipped)
```

### 2.2 单章节爬取的中间件+管道+插图链（最核心）

这是每个章节从 URL 到入库的完整数据流：

```
chapter.url
    │
    ▼ Request(url=url)
    │
    ├─ UserAgentMiddleware.process_request()
    │    → 随机选一个 UA 写入 request.headers["User-Agent"]
    │
    ├─ RateLimitMiddleware.process_request()
    │    → 按域名查 last_request 时间，不足间隔则 asyncio.sleep()
    │
    ├─ RetryMiddleware.process_request()
    │    → 透传（仅记录 retry_count）
    │
    ▼ http_client.fetch(request)
    │    → httpx.AsyncClient.request() → httpx.Response
    │    → 提取 Content-Type 中的 charset
    │    → 封装为 Response(raw_body=bytes, text=str, charset=...)
    │
    ├─ EncodingMiddleware.process_response()
    │    → detect_encoding(raw_body, adapter.encoding, charset)
    │    → 如果编码不匹配，safe_decode 重新解码
    │    → 更新 response.text 和 response.encoding
    │
    ├─ ContentVerifyMiddleware.process_response()
    │    → 蜜罐模式检测（"请刷新页面重试"等）
    │    → 字数检查（纯文本 < 100 字 → 警告日志）
    │    → 返回 None 则触发重试
    │
    ├─ RetryMiddleware.process_response()
    │    → 状态码 429/500/502/503/504 → 返回 None 触发重试
    │    → 指数退避: sleep(2^attempt) 后重试
    │
    ▼ adapter.get_chapter_content(chapter_id, url)
    │    (wenku8 适配器内部逻辑，见 §3.2)
    │    → 返回 ChapterContent(content=HTML, images=[ImageRef...], word_count=N)
    │
    ├─ HTMLCleanerPipeline.process_item()
    │    → lxml 解析 HTML
    │    → 移除 script/style/iframe/广告 class
    │    → 清理属性（只保留 href/src/alt/class）
    │
    ├─ TextNormalizerPipeline.process_item()
    │    → html.unescape() 反转 HTML 实体
    │    → 统一换行符 \r\n → \n
    │    → 压缩连续空行（3+ → 2）
    │    → 压缩非换行空白
    │
    ├─ DeduplicationPipeline.process_item()
    │    → SHA-256(content.encode("utf-8"))
    │    → storage.content_hash_exists(hash) → 已存在则返回 None 丢弃
    │
    ├─ QualityValidatorPipeline.process_item()
    │    → 字数检查（< 100 → 警告）
    │    → 乱码检测（U+FFFD 连续出现、Latin-1 区段异常等）
    │    → 蜜罐检测（"请刷新页面重试"等）
    │    → 截断检测（无结尾标点 + 孤立词片段）
    │    → 问题记日志，默认不丢弃
    │
    ├─ _download_chapter_images()  ← 新增：插图下载
    │    → 遍历 content.images
    │    → 对每张图片：rate_limiter.wait() → http_client.fetch_bytes(url)
    │    → SHA-256 去重 → 写入 data/images/{novel_id}/{chapter_id}/img_NNNN.{ext}
    │    → _detect_image_ext() 按文件头判断 PNG/JPG/GIF/WebP
    │    → storage.save_image() 记录到 resources 表
    │    → 将 HTML 中远程 URL 替换为本地相对路径 images/{chapter_id}/img_NNNN.{ext}
    │
    ▼ storage.save_chapter(content, novel_id, site, index, volume)
         → images 列表序列化为 JSON 存入 chapters.images 列
         → INSERT INTO chapters (...) VALUES (...)
         → 返回 chapter internal_id (UUID)
```

---

## 3. 适配器系统：如何对接不同站点

### 3.1 适配器抽象基类

`BaseAdapter`（`novel/core/adapter.py`）定义了引擎与站点交互的全部接口：

```python
class BaseAdapter(ABC):
    # ---- 站点属性 ----
    @property
    @abstractmethod
    def site_name(self) -> str: ...      # 站点唯一标识，如 "wenku8"

    @property
    @abstractmethod
    def base_url(self) -> str: ...       # 站点根 URL

    @property
    def encoding(self) -> str: ...       # 默认编码，"auto" 则自动检测
        return "utf-8"

    def get_rate_limit(self) -> float: ...  # 最小请求间隔（秒）
        return 3.0

    # ---- 单本小说操作 ----
    @abstractmethod
    async def search(self, keyword, page=1) -> tuple[list[NovelInfo], bool]: ...

    @abstractmethod
    async def get_novel_info(self, novel_id) -> NovelInfo: ...

    @abstractmethod
    async def get_chapter_list(self, novel_id) -> list[ChapterInfo]: ...

    @abstractmethod
    async def get_chapter_content(self, chapter_id, url="") -> ChapterContent: ...

    # ---- 图片下载 ----
    async def get_cover(self, novel_id) -> bytes | None: ...  # 可选

    # ---- 全站遍历 ----
    async def iter_all_novels(
        self, filter_params: dict | None = None
    ) -> AsyncIterator[NovelInfo]: ...  # 可选，支持全站爬取

    async def close(self) -> None: ...
```

引擎只调用这些方法，不知道 URL 长什么样、编码是什么、HTML 结构如何。

### 3.2 代码适配器示例：Wenku8Adapter

`novel/adapters/code/wenku8.py` 是一个完整的代码适配器，处理 wenku8.net 的三个特殊问题：

**问题 1：GBK 编码**

wenku8 全站使用 GBK 编码。适配器声明 `encoding = "gbk"`，在 `_fetch_text()` 中调用编码检测工具强制 GBK 解码：

```python
async def _fetch_text(self, url, params=None) -> str:
    request = Request(url=url, params=params or {})
    response = await self._http_client.fetch(request)
    encoding, _ = detect_encoding(response.raw_body, self.encoding, response.charset)
    text, _ = safe_decode(response.raw_body, encoding)
    return text
```

**问题 2：多页章节**

wenku8 的长章节会分页，URL 格式为 `xxx.htm`、`xxx_2.htm`、`xxx_3.htm`。适配器用循环抓取所有分页，然后合并：

```python
async def get_chapter_content(self, chapter_id, url="") -> ChapterContent:
    pages_content = []
    pages_images = []
    page = 1
    while True:
        page_url = url if page == 1 else url.replace(".htm", f"_{page}.htm")
        html_text = await self._fetch_text(page_url)
        tree = lxml_html.fromstring(html_text)

        # 提取正文
        content_elems = tree.xpath('//div[@id="content"]')
        if content_elems:
            for elem in content_elems[0].xpath('.//script'):
                elem.getparent().remove(elem)
            page_html = lxml_html.tostring(content_elems[0], encoding="unicode")
            pages_content.append(page_html)

            # 提取插图
            for img in content_elems[0].xpath('.//img[@src]'):
                src = img.get("src", "")
                if "imgpic" in src:
                    pages_images.append(ImageRef(url=src, ...))

        # 检查下一页
        next_page = tree.xpath('//a[contains(text(), "下一页")]')
        if next_page and f"_{page+1}" in next_page[0].get("href", ""):
            page += 1
            continue
        break

    return ChapterContent(
        content="\n".join(pages_content),  # 多页合并
        content_format="html",
        images=pages_images,
        raw_encoding="gbk",
    )
```

**问题 3：频率敏感**

wenku8 对请求频率敏感，适配器声明 `get_rate_limit() -> 5.0`（5 秒间隔），引擎的 RateLimiter 会据此控制节奏。

**全站遍历：`iter_all_novels()`**

通过 `articlelist.php` 分页遍历全站小说列表：

```python
async def iter_all_novels(self, filter_params=None) -> AsyncIterator[NovelInfo]:
    page = 1
    while True:
        url = f"{self.base_url}/modules/article/articlelist.php"
        params = {**base_params, "page": str(page)}
        html_text = await self._fetch_text(url, params=params)
        tree = lxml_html.fromstring(html_text)

        items = tree.xpath('//table[@class="grid"]//tr[position()>1]')
        # ... 提取小说 ID 和标题 ...
        for item in items:
            yield NovelInfo(site_id=..., title=...)

        # 检查下一页
        if not has_next:
            break
        page += 1
```

### 3.3 配置适配器：YAML 驱动（零代码）

对于结构简单的站点，放一个 YAML 文件到 `novel/adapters/configs/` 即可创建适配器，无需写任何 Python 代码：

```yaml
# novel/adapters/configs/example.yaml
site_name: "example"
base_url: "https://www.example.com"
encoding: "utf-8"
rate_limit: 3.0

search:
  url_template: "/search?q={keyword}&page={page}"
  items_selector: '//div[@class="result"]'
  fields:
    title: './/h2/a/text()'
    author: './/span[@class="author"]/text()'
    url: './/h2/a/@href'

novel_info:
  url_template: "/novel/{novel_id}"
  fields:
    title: '//h1/text()'
    author: '//span[@class="author"]/text()'
    description: '//div[@class="desc"]/text()'

chapter_list:
  selector: '//ul[@class="chapters"]//a'
  fields:
    title: './text()'
    url: './@href'

chapter_content:
  selector: '//div[@id="content"]'
  title_selector: '//h1/text()'
  remove_selectors:
    - '//div[@class="ad"]'
    - '//script'
```

`ConfigAdapter`（`novel/adapters/config_adapter.py`）读取 YAML 配置，用 lxml XPath 执行选择器，动态构造 `NovelInfo` / `ChapterInfo` / `ChapterContent`。

### 3.4 注册表：自动发现

`AdapterRegistry`（`novel/adapters/registry.py`）在 `load_all()` 时执行两步扫描：

1. **内置代码适配器**：硬编码 `import Wenku8Adapter`，实例化并注册
2. **YAML 配置适配器**：扫描 `novel/adapters/configs/*.yaml`，每个文件创建一个 `ConfigAdapter` 并注册

注册后通过 `registry.get("wenku8")` 按名称获取适配器实例。

---

## 4. HTTP 层与中间件链

### 4.1 HTTP 客户端

`AsyncHTTPClient`（`novel/http/client.py`）封装 httpx.AsyncClient：

- 连接超时 10s，读取超时 30s，自动跟随重定向
- 支持 gzip/deflate 压缩
- `fetch(request)` 方法接收 `Request` 对象，返回 `Response` 对象（用于网页）
- `fetch_bytes(url)` 方法直接下载二进制数据（用于图片下载）
- 从 `Content-Type` 头提取 `charset` 存入 `Response.charset`

### 4.2 请求/响应对象

```python
@dataclass
class Request:
    url: str
    method: str = "GET"
    headers: dict[str, str] = {}
    params: dict = {}
    cookies: dict = {}
    timeout: float | None = None
    meta: dict = {}        # 中间件间传递数据
    retry_count: int = 0

@dataclass
class Response:
    url: str
    status_code: int
    headers: dict[str, str] = {}
    raw_body: bytes = b""  # 原始字节
    text: str = ""          # 解码后文本
    encoding: str = "utf-8"
    charset: str | None = None  # HTTP 头声明的编码
    request: Request | None = None
    from_cache: bool = False
```

### 4.3 中间件链执行顺序

引擎在 `_crawl_chapter()` 中按固定顺序执行中间件：

**请求中间件链**（发出 HTTP 请求前）：

| 顺序 | 中间件 | 作用 |
|:---|:---|:---|
| 1 | `UserAgentMiddleware` | 随机选一个 User-Agent（5 个预设 UA） |
| 2 | `RateLimitMiddleware` | 域名级限速：间隔 + 0~30% 随机抖动 |
| 3 | `RetryMiddleware` | 透传，仅记录 retry_count |

中间件返回 `None` 表示拦截请求（不发出）。返回修改后的 `Request` 继续传递。

**响应中间件链**（收到 HTTP 响应后，解析前）：

| 顺序 | 中间件 | 作用 |
|:---|:---|:---|
| 1 | `EncodingMiddleware` | 三级编码检测，重新解码 response.text |
| 2 | `ContentVerifyMiddleware` | 蜜罐检测 + 字数检查 |
| 3 | `RetryMiddleware` | 状态码 429/500/502/503/504 → 返回 None 触发重试 |

响应中间件返回 `None` 表示触发重试。引擎在收到 `None` 后执行指数退避等待（`2^attempt` 秒），然后重新走整个请求-响应链。最多重试 3 次。

### 4.4 中间件的配置化组装

CLI 在 `create_engine_from_config()` 中根据 YAML 配置动态组装中间件链：

```python
# config/default.yaml 中的中间件配置
middleware:
  request:
    - user_agent:
        rotate: true
    - rate_limit:
        interval: 3.0
        jitter: 0.3
    - retry:
        max_retries: 3
        backoff_base: 1.0
  response:
    - encoding:
        fallback: utf-8
    - content_verify:
        min_word_count: 100
        drop_on_honeypot: false
```

`_build_request_middlewares()` 和 `_build_response_middlewares()` 读取配置，按名称动态导入对应类并实例化。用户可以通过修改 YAML 来启用/禁用/调整中间件，无需改代码。

---

## 5. 编码处理机制

### 5.1 为什么需要专门设计

轻小说站点编码不统一：
- wenku8.net → **GBK**
- 日本站点 → **Shift-JIS** 或 **EUC-JP**
- 多数现代站点 → **UTF-8**
- 部分站点 HTTP 头声明 UTF-8 但实际是 GBK

错误的编码会导致全文乱码。这是轻小说爬虫的高频痛点。

### 5.2 三级检测算法

`detect_encoding()`（`novel/utils/encoding.py`）按优先级依次尝试：

```
输入: raw_body (bytes), declared_encoding (适配器声明), http_charset (HTTP头)

1. 适配器明确声明（非 "auto"）→ 直接使用，置信度 1.0
   例: Wenku8Adapter 声明 "gbk" → 永远用 GBK 解码

2. HTTP 头 Content-Type: charset=xxx → 使用，置信度 0.95
   例: Content-Type: text/html; charset=shift_jis

3. HTML meta 标签 <meta charset="xxx"> → 使用，置信度 0.9
   扫描 raw_body 前 2048 字节

4. charset-normalizer 自动检测 → 使用检测结果的 encoding 和 coherence
   基于字节频率分布的统计检测

5. 全部失败 → 回退 UTF-8，置信度 0.0
```

### 5.3 安全解码

`safe_decode()` 尝试用检测到的编码解码，失败则用 UTF-8 + replace 容错：

```python
def safe_decode(raw_body: bytes, encoding: str) -> tuple[str, str]:
    try:
        return raw_body.decode(encoding), encoding
    except (UnicodeDecodeError, LookupError):
        return raw_body.decode("utf-8", errors="replace"), "utf-8"
```

### 5.4 乱码检测

`MojibakeDetector`（`novel/utils/mojibake.py`）通过正则模式检测已解码文本中的乱码痕迹：

- `U+FFFD` 连续出现 3 次以上（UTF-8 解码失败的替换字符）
- Latin-1 补充区（`\u00c0-\u00ff`）连续出现 8 个以上
- UTF-8 被当作 Latin-1 的典型模式 `\xc3[\x80-\xbf]`
- 不完整的 UTF-8 序列 `\xe4[\x00-\xff]{2}`
- 替换字符占比超过 1%

质量校验管道（`QualityValidatorPipeline`）会调用此检测器，发现乱码记 error 日志。

---

## 6. 数据管道：清洗→归一化→去重→校验

### 6.1 管道基类

```python
class Pipeline(ABC):
    @abstractmethod
    async def process_item(self, item: Any, context: PipelineContext) -> Any:
        """返回处理后的数据继续流转，返回 None 表示丢弃。"""
```

管道是线性链式执行。引擎在 `_crawl_chapter()` 中按顺序调用每个管道的 `process_item()`：

```python
for pipe in self.pipelines:
    content = await pipe.process_item(content, ctx)
    if content is None:
        logger.info(f"章节被管道丢弃: {chapter.title}")
        return None
```

### 6.2 四个内置管道

#### HTMLCleanerPipeline

| 项目 | 内容 |
|:---|:---|
| 文件 | `novel/pipelines/html_cleaner.py` |
| 输入 | 原始 HTML 正文 |
| 输出 | 清洗后的 HTML |

处理逻辑：
1. 用 lxml 解析 HTML
2. 移除匹配选择器的元素（默认移除 script/style/iframe/广告 class/推荐/分享/评论）
3. 清理元素属性，只保留 `href`、`src`、`alt`、`class`
4. 重新序列化为 HTML 字符串

```python
DEFAULT_REMOVE_SELECTORS = [
    "//*[contains(@class, 'ad')]",
    "//*[contains(@class, 'advert')]",
    "//*[contains(@class, 'recommend')]",
    "//script", "//style", "//iframe", "//ins", "//noscript",
]
```

注意：HTML 清洗**保留 `<img>` 标签**，只清理 `src` 属性值，不删除图片元素。这确保插图能被后续的插图下载逻辑处理。

#### TextNormalizerPipeline

| 项目 | 内容 |
|:---|:---|
| 文件 | `novel/pipelines/text_normalizer.py` |
| 输入 | HTML 文本 |
| 输出 | 归一化后的文本 |

处理逻辑：
1. `html.unescape()` — 反转 HTML 实体（`&amp;` → `&`，`&#x3000;` → `　`）
2. 统一换行符（`\r\n` → `\n`，`\r` → `\n`）
3. 行尾空白去除
4. 连续空行压缩（3+ 个 → 2 个）
5. 非换行空白压缩（多个空格 → 1 个空格）
6. 可选：NFKC 全角半角统一（默认关闭）

#### DeduplicationPipeline

| 项目 | 内容 |
|:---|:---|
| 文件 | `novel/pipelines/deduplication.py` |
| 输入 | ChapterContent |
| 输出 | 带 content_hash 的 ChapterContent，或 None（重复时丢弃） |

处理逻辑：
1. `content_hash = SHA-256(content.encode("utf-8"))`
2. 查询数据库 `content_hash_exists(hash)`
3. 已存在 → 返回 None（丢弃）
4. 不存在 → 设置 `item.content_hash = hash`，继续传递

#### QualityValidatorPipeline

| 项目 | 内容 |
|:---|:---|
| 文件 | `novel/pipelines/quality_validator.py` |
| 输入 | ChapterContent |
| 输出 | ChapterContent（默认始终返回），或 None（配置 drop_on_error 时） |

检测四类问题：

| 检查项 | 级别 | 判定条件 |
|:---|:---|:---|
| 字数过短 | warning | `count_text_length(content) < min_chapter_words` (默认 100) |
| 乱码 | error | MojibakeDetector 检测到乱码模式 |
| 蜜罐 | error | 内容包含 "请刷新页面重试"/"检测到异常访问" 等 |
| 截断 | warning | 无结尾标点 + 末尾有未闭合标签或孤立词 |

问题记日志，默认不阻断流程。配置 `drop_on_error: true` 时遇到 error 级别问题会丢弃章节。

---

## 7. 插图处理：下载→存储→EPUB 嵌入

插图处理是一个跨越引擎、存储、导出三层的完整链路。

### 7.1 数据流总览

```
适配器提取 <img src="远程URL">
    │
    ▼ ChapterContent.images = [ImageRef(url, alt, index), ...]
    │
    ▼ 数据管道处理（HTML 清洗保留 <img> 标签，只清理属性）
    │
    ▼ 引擎 _download_chapter_images()
    │    ├─ 遍历 content.images
    │    ├─ rate_limiter.wait() → http_client.fetch_bytes(url) 下载图片二进制
    │    ├─ hashlib.sha256(img_bytes) 计算 SHA-256 哈希
    │    ├─ _detect_image_ext(bytes, url) 按文件头判断格式
    │    │    PNG: \x89PNG\r\n\x1a\n
    │    │    JPG: \xff\xd8\xff
    │    │    GIF: GIF8
    │    │    WebP: RIFF....WEBP
    │    ├─ 写入文件: data/images/{novel_id}/{chapter_id}/img_{index:04d}.{ext}
    │    ├─ storage.save_image() 记录到 resources 表
    │    └─ HTML 中远程 URL → 替换为 images/{chapter_id}/img_NNNN.{ext}
    │
    ▼ storage.save_chapter() — images 列表序列化为 JSON 存入 chapters.images 列
    │
    ▼ EPUB 导出 _embed_images()
         ├─ 正则扫描 XHTML 中 <img src="images/..."> 引用
         ├─ 验证本地文件存在: data/images/{novel_id}/{chapter_id}/{filename}
         ├─ 文件存在 → 保留 <img> 标签，记录 (epub_path, media_type)
         ├─ 文件不存在 → 移除 <img> 标签，记 warning 日志
         └─ 用 EpubItem 嵌入图片文件到 EPUB 包
```

### 7.2 插图下载细节

`CrawlerEngine._download_chapter_images()`（`novel/engine/crawler.py` 第 273-333 行）：

```python
async def _download_chapter_images(self, content, novel_internal_id):
    img_dir = Path(f"data/images/{novel_internal_id}/{content.chapter_id}")
    img_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    local_paths = {}  # remote_url -> epub_relative_path

    for img in content.images:
        # 限速（复用引擎级 RateLimiter）
        await self.rate_limiter.wait()

        # 下载图片
        img_bytes = await self.http_client.fetch_bytes(img.url)

        # SHA-256 去重
        file_hash = hashlib.sha256(img_bytes).hexdigest()

        # 扩展名检测
        ext = self._detect_image_ext(img_bytes, img.url)
        filename = f"img_{img.index:04d}{ext}"
        local_path = str(img_dir / filename)
        Path(local_path).write_bytes(img_bytes)

        # EPUB 内部相对路径
        epub_path = f"images/{content.chapter_id}/{filename}"
        local_paths[img.url] = epub_path

        # 记录到数据库
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
        downloaded.append(img)

    # 替换 HTML 中的远程 src 为本地相对路径
    for remote_url, local_url in local_paths.items():
        content.content = content.content.replace(remote_url, local_url)

    content.images = downloaded
    return content
```

### 7.3 文件存储结构

```
data/
├── novels.db                              # SQLite 数据库
├── covers/
│   └── {novel_internal_id}.jpg            # 封面图片
└── images/
    └── {novel_internal_id}/
        ├── {chapter_id}/
        │   ├── img_0000.png               # 章节插图，按 index 编号
        │   ├── img_0001.jpg
        │   └── img_0002.gif
        └── {another_chapter_id}/
            └── img_0000.webp
```

### 7.4 EPUB 图片嵌入

`EPUBExporter._embed_images()`（`novel/exporters/epub.py` 第 208-237 行）：

1. 用正则 `<img[^>]+src="([^"]+)"[^>]*>` 扫描章节 XHTML
2. 只处理 `images/` 开头的本地路径（远程 URL 跳过）
3. 验证本地文件存在：`data/images/{novel_id}/{chapter_id}/{filename}`
4. 文件存在 → 保留 `<img>` 标签，记录 `(epub_path, media_type)` 到待嵌入列表
5. 文件不存在 → 移除 `<img>` 标签，记 debug 日志
6. 遍历待嵌入列表，用 `epub.EpubItem` 将图片文件嵌入 EPUB 包

Media Type 映射：

| 扩展名 | media_type |
|:---|:---|
| .jpg / .jpeg | image/jpeg |
| .png | image/png |
| .gif | image/gif |
| .webp | image/webp |
| .svg | image/svg+xml |

---

## 8. 全站爬取

### 8.1 全站爬取流程

```
CLI: novel crawl-all wenku8 --max-novels 50 --auto-export
    │
    ▼
CrawlerEngine.crawl_site(max_novels=50, novel_filter={...})
    │
    ├──→ adapter.iter_all_novels(filter_params) → AsyncIterator[NovelInfo]
    │    (Wenku8Adapter: 通过 articlelist.php 分页遍历)
    │
    ├──→ 对每本小说：
    │    ├─ 检查 _cancelled 标志
    │    ├─ 检查 max_novels 上限
    │    └─ engine.crawl_novel(novel_info.site_id)
    │         → 走完整的单本爬取流程（见 §2.1）
    │         → 包括：元信息 → 章节目录 → 增量过滤 → 并发下载 → 插图 → 封面
    │
    ├──→ 每本小说的爬取结果收集到 results 列表
    │
    └──→ 返回 list[CrawlResult]
         │
         ▼ (如果 --auto-export)
    遍历 results，对每本有章节的小说执行 EPUBExporter.export()
```

### 8.2 适配器的全站遍历接口

`BaseAdapter.iter_all_novels()` 是一个异步生成器，yield `NovelInfo` 对象：

```python
async def iter_all_novels(
    self, filter_params: dict[str, Any] | None = None
) -> AsyncIterator[NovelInfo]:
    """遍历站点所有小说。子类应实现此方法以支持全站爬取。"""
    raise NotImplementedError(...)
```

Wenku8Adapter 的全站遍历实际通过 **ID 递增遍历**实现（不是上面的 `articlelist.php` 分页伪码——那段仅示意 `iter_all_novels` 的 `AsyncIterator` 契约）：
- 从 `start_id` 起按 `id` 递增请求小说页，连续 100 个无效编号（404 / 非小说页）即判定到站点末尾并停止
- 最后一个有效编号回写 `site_meta.last_novel_id`，下次 `crawl-all` 自动从「精确数量 + 1」续爬（也可用 `--start-id` 手动覆盖起点）
- 无 `class` / `order` 参数（早期 `articlelist.php` 分页 + `--class-id` 方案已废弃）
- `supports_site_wide()` 返回 `True` 时才允许 `crawl-all`

### 8.3 CLI 命令

```bash
# 全站爬取（限制 50 本）
novel crawl-all wenku8 --max-novels 50

# 全站爬取 + 自动导出 EPUB
novel crawl-all wenku8 --auto-export

# ID 递增遍历，从指定 ID 起（不填则自动从已存精确数量+1 续爬）
novel crawl-all wenku8 --start-id 1

# 多线程池（每 worker 独立引擎实例 ≈ N× 吞吐）
novel crawl-all wenku8 --workers 4
novel crawl-all wenku8 --workers 4 --shard        # --shard：分块领取（500/块，块内单本粒度、整空白块判末尾）

# 需登录站点
novel crawl-all wenku8 --username "acc@qq.com" --password "pwd"

# 不限制数量，爬取全站
novel crawl-all wenku8
```

### 8.4 为新站点添加全站爬取

只需在适配器中实现 `iter_all_novels()` 方法：

```python
class MySiteAdapter(BaseAdapter):
    # ... 其他方法 ...

    async def iter_all_novels(self, filter_params=None):
        page = 1
        while True:
            # 1. 请求小说列表页
            html = await self._fetch_list_page(page)
            # 2. 解析小说列表
            novels = self._parse_novel_list(html)
            if not novels:
                break
            # 3. yield 每本小说
            for novel in novels:
                yield novel
            page += 1
```

引擎的 `crawl_site()` 会自动调用这个方法，无需改动引擎代码。

---

## 9. 数据存储：SQLite Schema 与读写逻辑

### 9.1 存储抽象接口

`StorageBackend`（`novel/core/storage.py`）定义了存储层的全部接口。当前唯一实现是 `SQLiteBackend`，未来可实现 PostgreSQL 等后端而无需改动引擎代码。

接口方法一览：

| 方法 | 用途 |
|:---|:---|
| `init()` / `close()` | 初始化/关闭连接 |
| `save_novel(novel, site)` | 保存小说元信息（upsert），返回内部 ID |
| `get_novel(novel_id, site)` | 按站点 ID 查询 |
| `get_novel_by_internal_id(id)` | 按内部 UUID 查询 |
| `get_novel_list(site=None)` | 获取小说列表 |
| `delete_novel(id)` | 删除小说（级联删除章节和资源） |
| `save_chapter(chapter, novel_id, site, index, volume)` | 保存章节（upsert），images 序列化为 JSON |
| `chapter_exists(chapter_id, site)` | 检查章节是否已爬取 |
| `content_hash_exists(hash)` | 检查内容哈希是否存在（去重用） |
| `get_chapters(novel_id)` | 获取小说所有章节 |
| `save_image(novel_id, chapter_id, type, url, path, ...)` | 保存图片资源记录 |
| `save_state(task_id, state)` / `load_state(task_id)` | 任务状态管理 |
| `list_tasks(site=None)` | 列出任务 |
| `get_stats()` | 统计信息 |

### 9.2 数据库表结构

数据库文件默认在 `data/novels.db`，包含 5 张业务表 + 1 张迁移记录表：

```sql
-- 小说元信息表
CREATE TABLE novels (
    id TEXT PRIMARY KEY,           -- 内部 UUID
    site TEXT NOT NULL,            -- 站点标识，如 "wenku8"
    site_novel_id TEXT NOT NULL,   -- 站点内的小说 ID，如 "1234"
    title TEXT NOT NULL,
    author TEXT,
    description TEXT,
    cover_url TEXT,
    cover_local TEXT,
    status TEXT,                   -- ongoing / completed / hiatus
    categories TEXT,               -- JSON 数组，如 ["轻小说","奇幻"]
    tags TEXT,                     -- JSON 数组
    word_count INTEGER DEFAULT 0,
    last_updated TEXT,             -- 站点声明的最后更新时间
    crawl_time TEXT,               -- 本地爬取时间
    extra TEXT,                    -- JSON 对象，存放额外字段
    UNIQUE(site, site_novel_id)    -- 同一站点同一小说只存一条
);

-- 卷信息表
CREATE TABLE volumes (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    name TEXT,                     -- 卷名，如 "第一卷"
    idx REAL DEFAULT 0,            -- 卷序号
    cover_url TEXT,
    cover_local TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- 章节内容表
CREATE TABLE chapters (
    id TEXT PRIMARY KEY,           -- 内部 UUID
    novel_id TEXT NOT NULL,
    volume_id TEXT,                -- 关联卷（可为空）
    site_chapter_id TEXT,          -- 站点内的章节 ID
    title TEXT NOT NULL,
    chapter_index REAL DEFAULT 0,  -- 全局序号
    content TEXT,                  -- HTML 正文
    content_format TEXT DEFAULT 'html',
    word_count INTEGER DEFAULT 0,
    author_note TEXT,              -- 作者后记
    is_crawled INTEGER DEFAULT 0,  -- 1=已爬取
    crawl_time TEXT,
    content_hash TEXT,             -- SHA-256，用于去重
    volume TEXT,                   -- 卷名冗余字段（方便查询）
    images TEXT DEFAULT '[]',      -- JSON 数组，存储 ImageRef 列表
    UNIQUE(novel_id, site_chapter_id),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- 图片资源表
CREATE TABLE resources (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_id TEXT,               -- 关联章节（封面为空）
    resource_type TEXT,            -- "cover" / "illustration"
    remote_url TEXT,
    local_path TEXT,               -- 本地文件路径
    caption TEXT,
    file_hash TEXT,                -- SHA-256
    image_order INTEGER DEFAULT 0,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE CASCADE
);

-- 爬取任务状态表（断点续爬用）
CREATE TABLE crawl_states (
    task_id TEXT PRIMARY KEY,
    novel_id TEXT,
    site TEXT,
    status TEXT,                   -- pending / running / paused / completed / failed
    total_chapters INTEGER DEFAULT 0,
    crawled_chapters INTEGER DEFAULT 0,
    failed_chapters TEXT,          -- JSON 数组
    last_chapter_id TEXT,
    started_at TEXT,
    updated_at TEXT,
    error_message TEXT
);

-- 迁移记录表
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);
```

索引：
```sql
CREATE INDEX idx_chapters_novel ON chapters(novel_id);
CREATE INDEX idx_chapters_site ON chapters(site_chapter_id, novel_id);
CREATE INDEX idx_resources_novel ON resources(novel_id);
CREATE INDEX idx_resources_chapter ON resources(chapter_id);
CREATE INDEX idx_novels_site ON novels(site);
```

### 9.3 数据写入逻辑

#### 小说元信息（upsert）

`save_novel()` 先查 `(site, site_novel_id)` 是否已存在：
- **存在** → UPDATE 全部字段（覆盖旧数据）
- **不存在** → 生成 UUID，INSERT 新记录

`categories`、`tags`、`extra` 用 `json.dumps()` 序列化为 JSON 字符串存储。读取时用 `json.loads()` 反序列化。

#### 章节内容（upsert）

`save_chapter()` 先查 `(novel_id, site_chapter_id)` 是否已存在：
- **存在** → UPDATE 内容、字数、哈希、images 等字段
- **不存在** → 生成 UUID，INSERT 新记录

`images` 列表序列化为 JSON 存储：
```python
images_json = json.dumps([
    {"url": img.url, "caption": img.caption, "alt": img.alt, "index": img.index}
    for img in chapter.images
], ensure_ascii=False) if chapter.images else "[]"
```

#### 章节存在检查（增量爬取）

```sql
SELECT 1 FROM chapters c
JOIN novels n ON c.novel_id = n.id
WHERE c.site_chapter_id = ? AND n.site = ?
```

引擎在爬取前对每章执行此查询，已存在的跳过。

#### 内容哈希去重

```sql
SELECT 1 FROM chapters WHERE content_hash = ?
```

去重管道用此查询判断内容是否与已存储的章节完全相同。

#### 图片资源

`save_image()` 记录图片的远程 URL、本地路径、SHA-256 哈希、图片类型（cover/illustration）。图片文件本身存储在文件系统（`data/covers/` 和 `data/images/`），数据库只存路径。

### 9.4 数据库初始化与迁移

`SQLiteBackend.init()` 执行以下操作：

1. 创建数据目录（`data/`）
2. `aiosqlite.connect()` 建立连接
3. `PRAGMA journal_mode=WAL` — 写前日志模式，提升并发读写性能
4. `PRAGMA foreign_keys=ON` — 启用外键约束
5. `_run_migrations()` — 执行迁移脚本

迁移机制：
- 扫描 `novel/storage/migrations/*.sql`
- 每个文件名前缀是版本号（如 `001_initial.sql` → 版本 1，`002_add_images.sql` → 版本 2）
- 查 `schema_migrations` 表获取当前版本
- 只执行版本号大于当前的脚本
- 执行后记录版本号到 `schema_migrations`

当前迁移文件：

| 文件 | 版本 | 内容 |
|:---|:---|:---|
| `001_initial.sql` | 1 | 创建全部初始表和索引 |
| `002_add_images.sql` | 2 | 给 chapters 表添加 `images TEXT DEFAULT '[]'` 列 |

### 9.5 SQLite 配置优化

- **WAL 模式**：读写不互斥，爬取时写入和查询可并行
- **外键约束**：删除小说时自动级联删除章节和资源
- **UNIQUE 约束**：防止同一站点同一小说/章节重复插入

---

## 10. 并发控制与限速

### 10.1 并发模型

引擎使用 `asyncio.gather()` 并发下载所有待爬章节，用 `asyncio.Semaphore` 控制最大并发数（默认 5）：

```python
# engine/crawler.py
self.semaphore = asyncio.Semaphore(max_concurrency)

async def _crawl_chapter(self, chapter, novel_internal_id):
    async with self.semaphore:          # 最多 5 个协程同时执行
        await self.rate_limiter.wait()  # 限速
        # ... 爬取逻辑
```

所有待爬章节作为独立的 asyncio Task 同时启动，Semaphore 确保同一时刻最多 5 个在执行 HTTP 请求。其余在 Semaphore 队列中等待。

插图下载也复用同一个 RateLimiter 进行限速，确保图片请求不会额外增加站点负担。

### 10.2 限速机制

有两层限速，互相补充：

**引擎级 RateLimiter**（`engine/crawler.py`）：

```python
class RateLimiter:
    def __init__(self, interval: float = 3.0):
        self._interval = interval
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            jitter = random.uniform(0, self._interval * 0.3)
            wait_time = self._interval + jitter
            elapsed = time.monotonic() - self._last_request
            if elapsed < wait_time:
                await asyncio.sleep(wait_time - elapsed)
            self._last_request = time.monotonic()
```

全局单一锁，确保所有并发协程串行等待间隔。间隔 = 适配器声明值 + 0~30% 随机抖动。插图下载也调用 `rate_limiter.wait()`。

**中间件级 RateLimitMiddleware**（`middlewares/rate_limit.py`）：

域名级限速，每个域名独立计时。如果未来支持跨站爬取，不同站点的请求不会互相阻塞。

### 10.3 重试机制

两层重试：

**引擎级重试**（`_crawl_chapter` 内部）：

```python
for attempt in range(max_retries + 1):  # 默认 3+1=4 次
    try:
        # ... 请求 + 中间件 + 管道 + 插图 + 存储
        return content
    except Exception as e:
        if attempt < max_retries:
            await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s 指数退避
        else:
            raise
```

**中间件级重试**（`RetryMiddleware`）：

响应中间件检查状态码，429/500/502/503/504 返回 None 触发引擎级重试。退避时间 = `backoff_base * 2^retry_count`。

### 10.4 多线程全站爬取（worker_pool）

单线程下，无论起多少协程，`www.wenku8.net` 域名上的请求都被一把 `RateLimitMiddleware` 锁 + 5s 间隔串行化（实际 ≈ 1 请求/5s）。提速的唯一干净杠杆是**让每个 worker 拥有独立的限速预算**。

`novel/engine/worker_pool.py`（`CrawlWorkerPool`）的实现：

- `crawl-all --workers N`（N≥2）启动 N 个 OS 线程，每个线程跑独立的 `asyncio` 事件循环，并在线程内构建**完整且私有**的引擎（`registry.build(site)` 工厂返回新适配器实例，禁用 `get()` 单例）：`SQLiteBackend` / `AsyncHTTPClient` / 中间件链（含独立 `RateLimitMiddleware` 锁）/ `CrawlerEngine`。
- 每 worker 独立 5s 限速预算 ⇒ N 个 worker ≈ N× 吞吐；每个 worker 仍守 5s，属合规横向扩展。
- 两种模式：默认（discover）单 coordinator 发现并投递队列、N 个 worker 消费；`--shard` 为**分块领取**：外层按 `BLOCK_SIZE=500` 分块（1-500、501-1000…），所有 worker 在当前块内原子领取「序号最低的空闲书号」（单本粒度、块内负载均衡），整块领完切下一块；**单个整空白块**（块内 0 有效）判定全局末尾，确认 worker 把最后有效书号写入 `site_meta`（= 网站总书数，**不依赖固定上界**，原 `DEFAULT_SCAN_UPPER=8000` 已废弃），发现+爬取均并行、无尾段空等。
- 唯一跨线程可变状态 = 有界 `queue.Queue` + `Lock`；唯一共享文件 = WAL 数据库。
- SQLite 并发写安全：`init()` 用模块级 `_INIT_LOCK` 序列化；连接必须 `isolation_level=None`（真 autocommit，避免默认 `isolation_level=""` 下每次 `SELECT` 开残留读事务，导致并发写升级触发 `SQLITE_LOCKED`）；每个写操作（`save_chapter`/`save_image`）写完即提交（短事务，写锁持有毫秒级）；`PRAGMA busy_timeout=200`（小值，冲突快速失败，避免大值下自旋 30s 退化为 livelock）+ `_commit_retry()`/`_run_with_lock_retry()` 指数退避兜底，彻底消除 `database is locked`。`--workers 1` 退化为原单线程，零风险。
- 详细设计与验证见 `MULTITHREAD_DESIGN.md`。

---

## 11. 断点续爬与增量更新

### 11.1 增量爬取（自动跳过已爬章节）

引擎在步骤 4 对每章调用 `storage.chapter_exists(chapter_id, site)`。已入库的章节直接跳过，不发出 HTTP 请求。这意味着：

- 爬取中断后重新运行同一命令 → 自动跳过已完成的章节
- 只爬取失败的或新增的章节

### 11.2 任务状态管理

`TaskManager`（`novel/engine/state.py`）提供任务的创建、更新、恢复：

```python
# 创建任务
task_id = await task_manager.create_task(novel_id="1234", site="wenku8")
# → 写入 crawl_states 表，status="pending"

# 更新进度
await task_manager.update_task(
    task_id, status="running",
    total=200, crawled=150,
    failed=["ch099", "ch100"],
    last_chapter_id="ch150",
)

# 恢复任务
state = await task_manager.resume_task(task_id)
# → 检查 status 必须是 "paused" 或 "failed"
# → 改为 "running" 并返回状态
```

任务状态存储在 `crawl_states` 表中，`failed_chapters` 字段以 JSON 数组形式存储失败章节 ID 列表。

### 11.3 增量更新命令

`python -m novel update` 命令遍历数据库中所有小说：

```python
novels = await storage.get_novel_list(site_name)
for novel_row in novels:
    latest = await adapter.get_novel_info(site_novel_id)
    if latest.last_updated != stored_updated:
        # 站点有更新 → 重新爬取（增量跳过已有章节）
        engine = CrawlerEngine(...)
        await engine.crawl_novel(site_novel_id)
    else:
        # 无更新 → 跳过
```

---

## 12. EPUB 导出

### 12.1 导出流程

`EPUBExporter`（`novel/exporters/epub.py`）从存储层读取已爬取的数据，生成 EPUB 3.0 文件：

```
export(novel_internal_id)
    │
    ├──→ storage.get_novel_by_internal_id() → NovelInfo
    ├──→ storage.get_chapters() → List[dict] (按 chapter_index 排序)
    │
    ├──→ 创建 EPUB 书籍
    │    ├── set_identifier() — 唯一标识
    │    ├── set_title() / set_language("zh") / add_author()
    │    ├── add_metadata("DC", "description", ...)
    │    └── 加载 CSS 样式表 (exporters/templates/epub/style.css)
    │
    ├──→ 设置封面
    │    └── 读取 data/covers/{internal_id}.jpg → set_cover()
    │
    ├──→ 生成目录页 (toc.xhtml)
    │
    ├──→ 遍历章节，生成 XHTML 内容
    │    ├── 卷分隔：遇到新卷名 → 插入卷标题页
    │    ├── HTML → XHTML 转换 (lxml 解析 + 重新序列化)
    │    ├── 纯文本 → <p> 段落包装
    │    ├── 插图嵌入：_embed_images() 扫描 <img src="images/...">
    │    │    → 验证本地文件 → 记录到 embedded_images 字典
    │    └── 作者后记 → 追加到章节末尾
    │
    ├──→ 嵌入所有图片文件
    │    └── 遍历 embedded_images → EpubItem(file_name=images/..., media_type=...)
    │
    ├──→ 设置目录 (book.toc) 和阅读顺序 (book.spine)
    ├──→ 添加 NCX 和 Nav 导航文件
    │
    └──→ epub.write_epub(output_path, book)
         → 输出到 ./output/{安全文件名}.epub
```

### 12.2 EPUB 包含的内容

按阅读顺序排列：

| 序号 | 内容 | 说明 |
|:---|:---|:---|
| 1 | 元数据 (Dublin Core) | 书名、作者、语言(zh)、简介、标签、来源站点 |
| 2 | 封面图片 | 从 `data/covers/{id}.jpg` 读取，嵌入为 `cover.jpg` |
| 3 | 目录页 | 独立 XHTML，标题"目录" |
| 4 | 卷分隔页 | 按卷拆分时插入，`<h1 class="volume-title">` |
| 5 | 章节正文 | 每章一个 XHTML 文件，含嵌入插图 |
| 6 | 作者后记 | 追加在章节正文末尾，`<div class="author-note">` |
| 7 | 导航文件 | NCX (旧阅读器) + Nav (EPUB 3) |
| 8 | CSS 样式表 | `style/default.css`，所有页面引用 |
| 9 | 插图文件 | `images/{chapter_id}/img_NNNN.{ext}`，EpubItem 嵌入 |

### 12.3 排版标准

CSS 样式（`exporters/templates/epub/style.css`）：

| 参数 | 默认值 | 说明 |
|:---|:---|:---|
| font-family | Georgia, "Noto Serif SC", "Source Han Serif SC", serif | 衬线字体 |
| font-size | 14pt | 基础字号 |
| line-height | 1.8 | 行高 |
| text-indent | 2em | 段首缩进 |
| text-align | justify | 两端对齐 |
| color | #1a1a1a | 正文文字色 |
| background | #fefefe | 背景色 |
| .chapter-title | 1.5em, 居中, 粗体 | 章节标题 |
| .volume-title | 1.8em, 居中, 粗体, page-break-before | 卷标题 |
| .author-note | 0.9em, #444, 顶部分隔线, page-break-before | 作者后记 |
| .illustration | 居中, max-width: 100% | 插图样式 |

### 12.4 构造函数可调参数

```python
EPUBExporter(
    storage,              # 存储后端
    output_dir="./output",
    font_family="Georgia, Noto Serif SC",
    base_font_size="14pt",
    line_height=1.8,
    paragraph_indent="2em",
    split_by_volume=True, # 是否按卷分页
)
```

> **注意**：构造函数的排版参数目前作为实例属性存储，但实际 CSS 从模板文件 `style.css` 读取。如需让参数动态生效，需在 `_load_css()` 中做模板字符串替换。

### 12.5 文件名安全化

`safe_filename()`（`novel/utils/text.py`）将小说标题转为安全的文件名：
- 替换 `<>:"/\|?*` 为 `_`
- 压缩空白
- 限制长度 100 字符

---

## 13. 配置系统

### 13.1 配置层级

优先级从高到低：

1. **环境变量**：`NOVEL_DB_PATH`、`NOVEL_LOG_LEVEL`
2. **YAML 文件**：`./novel.yaml` 或 `./config/default.yaml`
3. **dataclass 默认值**

### 13.2 配置结构

```python
@dataclass
class AppConfig:
    engine: EngineConfig        # max_concurrency=5, default_rate_limit=3.0
    http: HTTPConfig            # timeout_connect=10, timeout_read=30
    storage: StorageConfig      # backend="sqlite", sqlite.path="data/novels.db"
    export: ExportConfig        # output_dir="./output", epub 参数
    logging: LoggingConfig      # level="info", file="logs/novel.log"
    middleware: dict            # 中间件配置（原始 dict）
    pipeline: dict              # 管道配置（原始 dict）
```

### 13.3 配置加载流程

```python
AppConfig.load(yaml_path=None)
    │
    ├──→ 创建默认实例 (所有 dataclass 默认值)
    ├──→ 查找 YAML 文件 (novel.yaml → config/default.yaml)
    ├──→ yaml.safe_load() → dict
    ├──→ _merge_dataclass() — 递归合并 dict 到 dataclass
    │    └── 嵌套 dataclass 递归合并，dict 字段直接替换
    └──→ 环境变量覆盖 (NOVEL_DB_PATH, NOVEL_LOG_LEVEL)
```

### 13.4 默认配置文件

`config/default.yaml` 包含完整的默认配置，包括中间件和管道的启用/参数：

```yaml
engine:
  max_concurrency: 5
  default_rate_limit: 3.0

http:
  timeout_connect: 10
  timeout_read: 30
  follow_redirects: true

storage:
  backend: sqlite
  sqlite:
    path: data/novels.db

middleware:
  request:
    - user_agent: { rotate: true }
    - rate_limit: { interval: 3.0, jitter: 0.3 }
    - retry: { max_retries: 3, backoff_base: 1.0 }
  response:
    - encoding: { fallback: utf-8 }
    - content_verify: { min_word_count: 100, drop_on_honeypot: false }

pipeline:
  html_cleaner: { enabled: true }
  text_normalizer: { enabled: true, normalize_width: false }
  deduplication: { enabled: true }
  quality_validator:
    enabled: true
    min_chapter_words: 100
    drop_on_error: false
```

---

## 14. 项目文件索引

```
novel-crawler/
├── pyproject.toml                          # 项目元数据 + 依赖声明
├── config/
│   └── default.yaml                        # 默认配置文件
├── README.md                               # 使用说明
├── TECHNICAL_DESIGN.md                     # 本文档
│
├── novel/                                  # 主包
│   ├── __init__.py                         # 版本号
│   ├── __main__.py                         # python -m novel 入口
│   ├── config.py                           # 配置加载 (dataclass + YAML)
│   │
│   ├── core/                               # 核心抽象层
│   │   ├── __init__.py
│   │   ├── models.py                       # 7 个数据模型 (NovelInfo/ChapterInfo/...)
│   │   ├── exceptions.py                   # 自定义异常
│   │   ├── adapter.py                      # BaseAdapter 抽象基类
│   │   ├── middleware.py                   # Request/Response + 中间件基类
│   │   ├── pipeline.py                     # Pipeline 基类 + PipelineContext
│   │   └── storage.py                      # StorageBackend 抽象接口
│   │
│   ├── http/
│   │   ├── __init__.py
│   │   └── client.py                       # AsyncHTTPClient (httpx 封装)
│   │
│   ├── engine/                             # 调度层
│   │   ├── __init__.py
│   │   ├── crawler.py                      # CrawlerEngine + RateLimiter
│   │   │                                  #   crawl_novel() — 单本爬取
│   │   │                                  #   crawl_site() — 全站爬取
│   │   │                                  #   _crawl_chapter() — 单章爬取
│   │   │                                  #   _download_chapter_images() — 插图下载
│   │   │                                  #   _detect_image_ext() — 图片格式检测
│   │   └── state.py                        # TaskManager (断点续爬)
│   │
│   ├── middlewares/                        # 内置中间件
│   │   ├── __init__.py
│   │   ├── user_agent.py                   # UA 轮换
│   │   ├── rate_limit.py                   # 域名级限速
│   │   ├── retry.py                        # 指数退避重试
│   │   ├── encoding.py                     # 三级编码检测
│   │   └── content_verify.py              # 蜜罐 + 字数校验
│   │
│   ├── pipelines/                          # 数据处理管道
│   │   ├── __init__.py
│   │   ├── html_cleaner.py                 # HTML 清洗（保留 <img> 标签）
│   │   ├── text_normalizer.py              # 文本归一化
│   │   ├── deduplication.py                # SHA-256 去重
│   │   └── quality_validator.py            # 质量校验（乱码/蜜罐/截断）
│   │
│   ├── storage/                            # 存储层
│   │   ├── __init__.py
│   │   ├── sqlite_backend.py               # SQLiteBackend 实现
│   │   └── migrations/
│   │       ├── 001_initial.sql             # 初始表结构
│   │       └── 002_add_images.sql          # chapters 表添加 images 列
│   │
│   ├── exporters/                          # 导出层
│   │   ├── __init__.py
│   │   ├── epub.py                         # EPUBExporter
│   │   │                                  #   export() — 主流程
│   │   │                                  #   _embed_images() — 插图嵌入
│   │   │                                  #   _get_media_type() — MIME 映射
│   │   │                                  #   _html_to_xhtml_body() — HTML→XHTML
│   │   │                                  #   _load_css() — 加载样式表
│   │   │                                  #   _get_cover_path() — 封面路径
│   │   └── templates/
│   │       └── epub/
│   │           └── style.css               # EPUB 排版样式
│   │
│   ├── adapters/                           # 站点适配器
│   │   ├── __init__.py
│   │   ├── registry.py                     # AdapterRegistry (自动扫描)
│   │   ├── config_adapter.py               # YAML 配置适配器
│   │   ├── code/
│   │   │   ├── __init__.py
│   │   │   └── wenku8.py                   # wenku8.net 代码适配器
│   │   │                                  #   _fetch_text() — GBK 解码
│   │   │                                  #   get_chapter_content() — 多页合并
│   │   │                                  #   get_cover() — 封面下载
│   │   │                                  #   iter_all_novels() — 全站遍历
│   │   └── configs/
│   │       └── example.yaml                # YAML 配置适配器示例
│   │
│   ├── utils/                              # 工具函数
│   │   ├── __init__.py
│   │   ├── encoding.py                     # 编码检测 + 安全解码
│   │   ├── mojibake.py                     # 乱码检测
│   │   └── text.py                         # HTML剥离/空白归一化/字数统计/截断检测/安全文件名
│   │
│   └── cli/
│       ├── __init__.py
│       └── main.py                         # CLI 入口 (12 个命令)
│                                          #   crawl / crawl-all / search / export
│                                          #   list / info / stats / delete
│                                          #   sites list / sites info
│                                          #   tasks list / config init / config show
│
├── data/                                   # 运行时数据 (自动创建)
│   ├── novels.db                           # SQLite 数据库
│   ├── covers/                             # 封面图片
│   │   └── {novel_id}.jpg
│   └── images/                             # 章节插图
│       └── {novel_id}/{chapter_id}/img_NNNN.{ext}
│
├── output/                                 # EPUB 输出 (自动创建)
│
└── logs/                                   # 日志文件 (自动创建)
    └── novel.log
```

### CLI 命令一览

| 命令 | 作用 |
|:---|:---|
| `sites list` | 列出所有可用站点 |
| `sites info <name>` | 查看站点详情 |
| `search <site> <keyword>` | 搜索小说 |
| `crawl <site> <novel_id> [--chapters 1-10]` | 爬取小说 |
| `crawl-all <site> [--max-novels N] [--class-id N] [--auto-export]` | 全站爬取 |
| `update [--site <name>]` | 增量更新已存小说 |
| `export <novel_id> [--output path]` | 导出 EPUB |
| `list` | 列出已爬取小说 |
| `info <novel_id>` | 查看小说详情 |
| `stats` | 统计信息 |
| `delete <novel_id>` | 删除小说及关联数据 |
| `tasks list` | 列出爬取任务 |
| `tasks status <task_id>` | 查看任务状态 |
| `config init` | 初始化配置文件 |
| `config show` | 查看当前配置 |

---

## 15. 二次开发指南：修改某功能时查找哪些文件

### 15.1 功能 → 文件速查表

| 我想修改... | 主要文件 | 关联文件 | 说明 |
|:---|:---|:---|:---|
| **添加新站点** | `novel/adapters/code/{站点名}.py` | `novel/adapters/registry.py` (注册)、`novel/core/adapter.py` (接口) | 代码适配器需继承 BaseAdapter，在 registry 的 `_load_builtin_adapters()` 中注册 |
| **添加 YAML 配置站点** | `novel/adapters/configs/{站点名}.yaml` | `novel/adapters/config_adapter.py` (解析逻辑) | 放 YAML 文件即可，无需改代码。ConfigAdapter 自动发现 |
| **修改请求头/UA** | `novel/middlewares/user_agent.py` | `config/default.yaml` (middleware.request) | UA 列表硬编码在中间件类中 |
| **修改限速策略** | `novel/engine/crawler.py` (RateLimiter) | `novel/middlewares/rate_limit.py` | 引擎级全局限速 + 中间件级域名限速，两层独立 |
| **修改重试逻辑** | `novel/engine/crawler.py` (第 176-269 行) | `novel/middlewares/retry.py` | 引擎级异常重试 + 中间件级状态码重试 |
| **修改编码检测** | `novel/utils/encoding.py` | `novel/middlewares/encoding.py` | 检测算法在 utils，中间件调用它 |
| **修改乱码检测** | `novel/utils/mojibake.py` | `novel/pipelines/quality_validator.py` | 检测器在 utils，质量校验管道调用它 |
| **修改 HTML 清洗规则** | `novel/pipelines/html_cleaner.py` | — | `DEFAULT_REMOVE_SELECTORS` 列表和属性白名单 |
| **修改文本归一化** | `novel/pipelines/text_normalizer.py` | — | 换行/空行/空白处理逻辑 |
| **修改去重策略** | `novel/pipelines/deduplication.py` | `novel/storage/sqlite_backend.py` (`content_hash_exists`) | 当前用 SHA-256 精确去重 |
| **修改质量校验** | `novel/pipelines/quality_validator.py` | `novel/utils/mojibake.py`、`novel/utils/text.py` | 字数/乱码/蜜罐/截断四类检查 |
| **修改并发数** | `config/default.yaml` (engine.max_concurrency) | `novel/engine/crawler.py` (Semaphore) | 配置化，无需改代码 |
| **修改插图下载逻辑** | `novel/engine/crawler.py` (`_download_chapter_images`) | `novel/http/client.py` (`fetch_bytes`)、`novel/storage/sqlite_backend.py` (`save_image`) | 下载→哈希→存储→替换 HTML |
| **修改图片格式检测** | `novel/engine/crawler.py` (`_detect_image_ext`) | — | 按文件头 magic bytes 判断 |
| **修改 EPUB 排版** | `novel/exporters/templates/epub/style.css` | `novel/exporters/epub.py` (构造函数参数) | CSS 在模板文件中，导出器加载后嵌入 |
| **修改 EPUB 导出逻辑** | `novel/exporters/epub.py` | `novel/exporters/templates/epub/style.css` | 导出流程、插图嵌入、HTML→XHTML 转换 |
| **修改 EPUB 图片嵌入** | `novel/exporters/epub.py` (`_embed_images`) | — | 正则扫描 `<img src="images/...">` + EpubItem 嵌入 |
| **修改数据库表结构** | `novel/storage/migrations/003_{描述}.sql` (新建) | `novel/storage/sqlite_backend.py` (读写逻辑) | 新建迁移文件，版本号递增 |
| **修改小说存储逻辑** | `novel/storage/sqlite_backend.py` (`save_novel`/`get_novel`) | `novel/core/models.py` (NovelInfo) | upsert 逻辑 + JSON 序列化 |
| **修改章节存储逻辑** | `novel/storage/sqlite_backend.py` (`save_chapter`/`get_chapters`) | `novel/core/models.py` (ChapterContent) | images 序列化为 JSON 存入 chapters.images |
| **添加新存储后端** | `novel/storage/{后端名}_backend.py` (新建) | `novel/core/storage.py` (实现接口) | 继承 StorageBackend，实现全部抽象方法 |
| **修改断点续爬** | `novel/engine/state.py` | `novel/storage/sqlite_backend.py` (`save_state`/`load_state`) | TaskManager 管理任务状态 |
| **添加新 CLI 命令** | `novel/cli/main.py` | — | 用 click.command() 装饰器添加 |
| **修改全站爬取** | `novel/engine/crawler.py` (`crawl_site`) | `novel/adapters/code/wenku8.py` (`iter_all_novels`)、`novel/cli/main.py` (crawl_all 命令) | 引擎遍历 + 适配器 yield + CLI 触发 |
| **修改配置加载** | `novel/config.py` | `config/default.yaml` | dataclass + YAML 合并 |
| **添加新中间件** | `novel/middlewares/{名称}.py` (新建) | `novel/core/middleware.py` (基类)、`novel/cli/main.py` (注册) | 继承 RequestMiddleware 或 ResponseMiddleware |
| **添加新管道** | `novel/pipelines/{名称}.py` (新建) | `novel/core/pipeline.py` (基类)、`novel/cli/main.py` (注册) | 继承 Pipeline，实现 process_item() |
| **添加新导出格式** | `novel/exporters/{格式}.py` (新建) | `novel/core/storage.py` (数据读取) | 新建导出器类，在 CLI 中调用 |
| **修改数据模型** | `novel/core/models.py` | 所有引用该模型的文件 | 改 dataclass 后需同步更新存储层的序列化/反序列化 |

### 15.2 扩展场景指南

#### 场景 1：添加一个新站点（最常见）

```
1. 判断站点复杂度
   ├─ 简单（UTF-8、单页、标准 HTML）→ 写 YAML 配置
   │    → novel/adapters/configs/{站点名}.yaml
   │    → 参考 example.yaml 格式
   │    → 完成，自动注册
   │
   └─ 复杂（非 UTF-8、多页、特殊逻辑）→ 写代码适配器
        → novel/adapters/code/{站点名}.py
        → 继承 BaseAdapter，实现 6 个抽象方法
        → 在 novel/adapters/registry.py 的 _load_builtin_adapters() 中注册
        → 如需全站爬取，实现 iter_all_novels()
```

#### 场景 2：修改爬取行为（如添加代理、Cookie）

```
1. 是否可以通过中间件实现？
   ├─ 是 → novel/middlewares/ 下新建中间件
   │       → 继承 RequestMiddleware 或 ResponseMiddleware
   │       → 在 config/default.yaml 的 middleware 中注册
   │
   └─ 否 → 修改 novel/engine/crawler.py 的 _crawl_chapter()
```

#### 场景 3：修改数据处理流程

```
1. 是否可以通过管道实现？
   ├─ 是 → novel/pipelines/ 下新建管道
   │       → 继承 Pipeline，实现 process_item()
   │       → 在 config/default.yaml 的 pipeline 中注册
   │
   └─ 否 → 修改 novel/engine/crawler.py 中管道调用部分（第 226-236 行）
```

#### 场景 4：修改数据库结构

```
1. 新建迁移文件：novel/storage/migrations/003_{描述}.sql
2. 修改 novel/storage/sqlite_backend.py 中对应的读写方法
3. 如果新增字段在 models.py 的 dataclass 中，同步更新序列化逻辑
4. 迁移会自动执行（版本号递增即可）
```

#### 场景 5：修改 EPUB 输出

```
1. 只改排版 → 修改 novel/exporters/templates/epub/style.css
2. 改导出流程 → 修改 novel/exporters/epub.py
3. 添加新导出格式 → 新建 novel/exporters/{格式}.py，在 CLI 中调用
```

### 15.3 代码依赖关系图

理解模块间的调用方向，避免循环依赖：

```
cli/main.py
    │
    ├──→ config.py (AppConfig)
    ├──→ adapters/registry.py (AdapterRegistry)
    │       ├──→ adapters/code/wenku8.py (Wenku8Adapter)
    │       │       ├──→ core/adapter.py (BaseAdapter)
    │       │       ├──→ core/models.py
    │       │       ├──→ core/middleware.py (Request)
    │       │       ├──→ http/client.py (AsyncHTTPClient)
    │       │       └──→ utils/encoding.py
    │       └──→ adapters/config_adapter.py (ConfigAdapter)
    │               └──→ (同上依赖)
    ├──→ engine/crawler.py (CrawlerEngine)
    │       ├──→ core/adapter.py (BaseAdapter) ← 引擎只调接口
    │       ├──→ core/middleware.py (Request/Response/Middleware)
    │       ├──→ core/pipeline.py (Pipeline/PipelineContext)
    │       ├──→ core/storage.py (StorageBackend) ← 引擎只调接口
    │       ├──→ core/models.py
    │       ├──→ http/client.py (AsyncHTTPClient)
    │       └──→ utils/text.py (count_text_length)
    ├──→ middlewares/*.py (5 个中间件)
    │       ├──→ core/middleware.py (基类)
    │       ├──→ utils/encoding.py
    │       └──→ utils/mojibake.py
    ├──→ pipelines/*.py (4 个管道)
    │       ├──→ core/pipeline.py (基类)
    │       ├──→ core/models.py
    │       ├──→ utils/text.py
    │       └──→ utils/mojibake.py
    ├──→ storage/sqlite_backend.py (SQLiteBackend)
    │       ├──→ core/storage.py (基类)
    │       ├──→ core/models.py
    │       └──→ storage/migrations/*.sql
    └──→ exporters/epub.py (EPUBExporter)
            ├──→ core/storage.py (StorageBackend) ← 导出器只调接口
            ├──→ utils/text.py (safe_filename)
            └──→ exporters/templates/epub/style.css
```

**关键原则**：
- `core/` 是纯抽象，不依赖任何具体实现
- `engine/` 只依赖 `core/` 的抽象接口，不导入具体适配器或存储后端
- `adapters/` 依赖 `core/adapter.py` 和 `http/client.py`
- `middlewares/` 和 `pipelines/` 依赖 `core/` 和 `utils/`
- `storage/` 和 `exporters/` 依赖 `core/` 的抽象接口
- `cli/` 是组装层，依赖所有组件

### 15.4 关键代码位置速查

| 功能点 | 文件 | 行号范围（约） | 函数/方法名 |
|:---|:---|:---|:---|
| 引擎主入口 | `engine/crawler.py` | 86-162 | `crawl_novel()` |
| 单章爬取 | `engine/crawler.py` | 164-271 | `_crawl_chapter()` |
| 插图下载 | `engine/crawler.py` | 273-333 | `_download_chapter_images()` |
| 图片格式检测 | `engine/crawler.py` | 335-350 | `_detect_image_ext()` |
| 全站爬取 | `engine/crawler.py` | 352-404 | `crawl_site()` |
| 限速器 | `engine/crawler.py` | 33-48 | `RateLimiter.wait()` |
| 中间件链执行 | `engine/crawler.py` | 182-211 | `_crawl_chapter()` 内部循环 |
| 管道链执行 | `engine/crawler.py` | 226-236 | `_crawl_chapter()` 内部循环 |
| 封面下载 | `engine/crawler.py` | 133-151 | `crawl_novel()` 步骤 6 |
| EPUB 主流程 | `exporters/epub.py` | 40-206 | `export()` |
| EPUB 插图嵌入 | `exporters/epub.py` | 208-237 | `_embed_images()` |
| EPUB MIME 映射 | `exporters/epub.py` | 239-250 | `_get_media_type()` |
| EPUB HTML→XHTML | `exporters/epub.py` | 264-277 | `_html_to_xhtml_body()` |
| 适配器全站遍历 | `adapters/code/wenku8.py` | 320-410 | `iter_all_novels()` |
| 适配器多页合并 | `adapters/code/wenku8.py` | ~200-280 | `get_chapter_content()` |
| 存储 upsert 章节 | `storage/sqlite_backend.py` | 159-218 | `save_chapter()` |
| 存储 images JSON | `storage/sqlite_backend.py` | 173-185 | `save_chapter()` 内序列化 |
| 数据库迁移 | `storage/sqlite_backend.py` | 45-76 | `_run_migrations()` |
| CLI crawl 命令 | `cli/main.py` | 295-352 | `crawl()` |
| CLI crawl-all 命令 | `cli/main.py` | 358-447 | `crawl_all()` |
| 引擎组装 | `cli/main.py` | ~130-170 | `create_engine_from_config()` |
