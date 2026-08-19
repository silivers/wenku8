# 通用轻小说爬虫框架 — 完善方案

**版本**：3.1
**日期**：2026年8月13日
**状态**：最终设计方案（精简版）
**定位**：从单站爬虫升级为通用轻小说爬虫框架

> **📌 落地状态（2026-08-19 校订）**：本方案为设计稿，下述能力已在代码中落地，其余为"已决定不做 / 暂缓"：
> **已落地**：适配器模式（ConfigAdapter + CodeAdapter）、请求/响应中间件链、数据管道（清洗→归一化→去重→校验）、SQLite 存储（WAL）、EPUB 3.0 导出、wenku8 登录态（`login()` + `sites login`）、章节 URL 分桶（`/novel/{id//1000}/{id}/index.htm`）、下架检测精确化、正文广告过滤、多线程全站爬取（`worker_pool.py` + `crawl-all --workers/--shard`）、全站 ID 递增遍历 + 自动续爬。
> **已决定不做 / 暂缓**：PostgreSQL 后端（仅 SQLite）、Proxy 中间件与代理池、syosetu 等额外代码适配器（仅 wenku8）、Web UI / REST API、Ruby 注音处理管道、竖排（`writing-mode`）、webhook 报警、Redis/布隆过滤器去重（精确 SHA-256 去重足够）。
> 设计稿与实现细节差异以 `README.md` / `USAGE.md` / `TECHNICAL_DESIGN.md` / `MULTITHREAD_DESIGN.md` / `WENKU8_LOGIN_REPORT.md` 为准。

---

## 目录

1. [现有设计分析](#1-现有设计分析)
2. [改进总览](#2-改进总览)
3. [通用框架架构设计](#3-通用框架架构设计)
4. [核心抽象与接口定义](#4-核心抽象与接口定义)
5. [适配器系统设计](#5-适配器系统设计)
6. [引擎与中间件设计](#6-引擎与中间件设计)
7. [数据处理管道设计](#7-数据处理管道设计)
8. [通用数据模型设计](#8-通用数据模型设计)
9. [存储系统设计](#9-存储系统设计)
10. [EPUB导出设计](#10-epub导出设计)
11. [任务调度与状态管理](#11-任务调度与状态管理)
12. [CLI 设计](#12-cli-设计)
13. [配置系统设计](#13-配置系统设计)
14. [内容质量保障](#14-内容质量保障)
15. [测试策略](#15-测试策略)
16. [安全与合规](#16-安全与合规)
17. [项目结构](#17-项目结构)
18. [实现路线图](#18-实现路线图)

---

## 1. 现有设计分析

### 1.1 原文档优点

| 方面 | 评价 |
|:---|:---|
| 文档结构 | 完整清晰，覆盖爬取→存储→导出全链路 |
| 模块划分 | 分层合理，职责定义明确 |
| 反爬策略 | 覆盖 IP 限频、UA 检测、验证码等常见场景 |
| EPUB 设计 | 排版参数细致，有竖排支持构想 |
| 配置化解析 | 提出用 YAML 管理选择器，方向正确 |
| 异常处理 | 有分级处理机制和报警集成 |
| 合规意识 | 有频率限制、免责声明 |

### 1.2 核心问题诊断

#### 问题一：名为"通用"实为"单站"

原文档标题提及"wenku8.net类"，但整个架构围绕单一站点设计：

- URL 结构、编码、分页方式都是隐式假设
- 数据模型字段（如 `novel_id`、`chapter_id`）直接取自 wenku8
- 选择器配置只支持单一 XPath 集，无法处理多站点结构差异
- 增量更新逻辑绑定 wenku8 的 `last_updated` 字段

**根因**：缺少适配器（Adapter）抽象层。框架引擎不应知道任何站点细节。

#### 问题二：配置化解析能力不足

YAML 选择器只能处理最简单的静态 HTML 页面，无法应对：

| 场景 | YAML 选择器的局限 |
|:---|:---|
| 复杂分页 | 游标分页、无限滚动需要代码逻辑 |
| 编码差异 | GBK / Shift-JIS / UTF-8 混用，需运行时检测 |
| 登录态内容 | 需要 Cookie 管理、登录流程代码 |
| 多页章节合并 | 需要跨页面组装逻辑 |
| 反爬蜜罐 | 需要内容校验逻辑，非选择器能解决 |

**结论**：配置化是手段之一，但不能是唯一手段。需要"配置适配器 + 代码适配器"双层设计。

#### 问题三：缺少中间件与管道体系

原文档的请求管理器和数据清洗器是硬编码的固定流程，缺少：

- **请求中间件**：UA 轮换、代理注入、限速、重试、认证等应可插拔
- **响应中间件**：编码检测、内容校验、解压缩等应可插拔
- **数据处理管道**：清洗、归一化、去重、校验等步骤应可组合

没有中间件体系，扩展新功能只能改核心代码，违反开闭原则。

#### 问题四：异步设计是事后补丁

原文档在"性能优化建议"中才提到 asyncio + aiohttp，但核心架构图和流程设计是同步思维。现代爬虫框架应以异步为一等公民：

- 引擎事件循环为核心
- 所有 I/O 操作（HTTP、数据库、文件）均为 async
- 并发控制基于 asyncio 原语

#### 问题五：数据模型不够通用

缺失的关键结构：

| 缺失项 | 影响 |
|:---|:---|
| 卷（Volume）层级 | 很多轻小说有"卷→章"两级结构，原文档只在 chapter 表加了 `volume_name` 字段，不够 |
| 插图说明文字 | 插图常有配文，原文档的 IMAGE 表只有路径 |
| 作者后记 | 很多轻小说有作者后记/插画师后记 |

#### 问题六：缺少编码处理设计

这是轻小说爬虫的高频痛点：

- wenku8.net 使用 GBK 编码
- 日文站点常用 Shift-JIS
- 部分站点页面编码不统一（列表页 UTF-8，内容页 GBK）

原文档完全未提及编码检测与转换策略。

#### 问题七：无内容质量校验

爬取可能得到不完整或伪造的内容：

- 反爬返回截断内容（正文不完整）
- 反爬返回假内容（蜜罐页面）
- 编码损坏导致乱码
- 章节内容为空或过短

缺少校验机制会导致数据质量问题被掩盖。

#### 问题八：无 CLI 接口设计

用户如何使用框架？原文档只在"运行模式"表中列了几种模式，但没有定义具体的命令行接口。

#### 问题九：无测试策略

只有"验收标准"，缺少：

- 单元测试框架设计
- HTML 解析快照测试
- 适配器契约测试
- 集成测试方案

---

## 2. 改进总览

### 2.1 设计理念转变

| 维度 | 原设计 | 改进后 |
|:---|:---|:---|
| 定位 | wenku8 爬虫工具 | 通用轻小说爬虫框架 |
| 站点支持 | 单站，配置化选择器 | 多站，适配器模式（ConfigAdapter + CodeAdapter） |
| 执行模型 | 同步为主 | 异步优先（asyncio） |
| 扩展机制 | 无 | 中间件链 + 数据管道 |
| 数据模型 | 面向 wenku8 | 通用模型，含卷/插图配文/后记 |
| 存储后端 | PostgreSQL 固定 | SQLite 默认（零配置），PostgreSQL 可选 |
| 导出格式 | EPUB | EPUB（保留原设计排版标准） |
| 编码处理 | 未设计 | 适配器声明 + 自动检测 + 回退 |
| 内容校验 | 未设计 | 完整性/真实性/编码 校验（记日志，不阻断） |
| 用户接口 | 未定义 | CLI |
| 测试策略 | 仅验收标准 | 单元 + 契约 + 快照 + 集成 |

### 2.2 核心设计原则

1. **引擎不知道站点**：所有站点知识封装在适配器中，引擎只负责调度和执行
2. **异步优先**：从架构层面以 asyncio 为核心，而非事后优化
3. **组合优于继承**：中间件、管道均为可组合的独立组件
4. **配置驱动 + 代码逃生**：简单站点用 YAML 配置，复杂站点用代码适配器
5. **零配置可用**：个人用户开箱即用（SQLite + 内置适配器），无需安装数据库
6. **不过度设计**：只解决真实问题，不预埋用不到的扩展点

---

## 3. 通用框架架构设计

### 3.1 分层架构

```
┌─────────────────────────────────────────────────────┐
│                  接口层 (Interface)                   │
│                    CLI                               │
├─────────────────────────────────────────────────────┤
│                  导出层 (Export)                      │
│                 EPUB Exporter                        │
├─────────────────────────────────────────────────────┤
│                  存储层 (Storage)                     │
│           SQLite（默认） · PostgreSQL（可选）          │
├─────────────────────────────────────────────────────┤
│               数据处理管道 (Pipeline)                 │
│         清洗 → 归一化 → 去重 → 校验 → 入库             │
├─────────────────────────────────────────────────────┤
│              引擎与中间件 (Engine)                    │
│   调度器 · 请求中间件链 · 响应中间件链 · 并发控制       │
├─────────────────────────────────────────────────────┤
│               适配器层 (Adapters)                    │
│   wenku8 Adapter · syosetu · 自定义...               │
│   (ConfigAdapter / CodeAdapter)                      │
├─────────────────────────────────────────────────────┤
│              基础设施 (Infrastructure)               │
│       HTTP Client · 编码检测 · 日志 · 配置            │
└─────────────────────────────────────────────────────┘
```

### 3.2 数据流

```
用户指令 (CLI)
  │
  ▼
任务调度器 ──→ 适配器选择
                 │
                 ▼
       ┌──→ 请求中间件链 ──→ HTTP下载
       │         │
       │         ▼
       │    响应中间件链
       │         │
       │         ▼
       │    适配器解析
       │         │
       │         ▼
       │    数据处理管道
       │         │
       │         ▼
       │    存储层写入
       │         │
       └──←── 是否继续？
                 │
                 ▼
             EPUB 导出（可选）
```

### 3.3 核心设计模式

| 模式 | 应用位置 | 解决的问题 |
|:---|:---|:---|
| **适配器模式** | 站点适配器层 | 引擎与站点解耦 |
| **中间件模式** | 请求/响应处理 | 可插拔的横切关注点 |
| **管道模式** | 数据处理 | 可组合的数据加工步骤 |
| **注册表模式** | 适配器注册 | 适配器发现与加载 |
| **状态机模式** | 爬取任务状态 | 断点续爬与任务恢复 |

---

## 4. 核心抽象与接口定义

### 4.1 适配器接口（BaseAdapter）

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class NovelInfo:
    """小说元信息"""
    site_id: str              # 站点内唯一标识
    title: str
    author: str
    description: str = ""
    cover_url: str | None = None
    status: str = ""          # ongoing / completed / hiatus
    categories: list[str] = None
    tags: list[str] = None
    last_updated: str | None = None
    word_count: int = 0
    extra: dict = None        # 站点特有字段

@dataclass
class ChapterInfo:
    """章节目录项"""
    chapter_id: str
    title: str
    volume: str | None = None
    index: float = 0
    url: str = ""
    word_count: int = 0

@dataclass
class ChapterContent:
    """章节正文"""
    chapter_id: str
    title: str
    content: str              # HTML 或纯文本
    content_format: str = "html"
    images: list["ImageRef"] = None
    author_note: str | None = None
    word_count: int = 0
    raw_encoding: str = ""

@dataclass
class ImageRef:
    """图片引用"""
    url: str
    caption: str = ""
    alt: str = ""
    index: int = 0

class BaseAdapter(ABC):
    """站点适配器抽象基类"""

    @property
    @abstractmethod
    def site_name(self) -> str: ...

    @property
    @abstractmethod
    def base_url(self) -> str: ...

    @property
    def encoding(self) -> str:
        """默认页面编码，子类可覆盖。设为 "auto" 则自动检测"""
        return "utf-8"

    @abstractmethod
    async def search(self, keyword: str, page: int = 1) -> tuple[list[NovelInfo], bool]: ...

    @abstractmethod
    async def get_novel_info(self, novel_id: str) -> NovelInfo: ...

    @abstractmethod
    async def get_chapter_list(self, novel_id: str) -> list[ChapterInfo]: ...

    @abstractmethod
    async def get_chapter_content(self, chapter_id: str) -> ChapterContent: ...

    async def get_cover(self, novel_id: str) -> bytes | None: ...
    async def get_illustrations(self, chapter_id: str) -> list[bytes]: ...

    def get_rate_limit(self) -> float:
        """该站点的请求间隔（秒）"""
        return 3.0
```

### 4.2 中间件接口

```python
class RequestMiddleware(ABC):
    """请求中间件：在 HTTP 请求发出前介入"""

    @abstractmethod
    async def process_request(self, request: Request, adapter: BaseAdapter) -> Request | None:
        """返回修改后的 Request 或 None（终止请求）"""

class ResponseMiddleware(ABC):
    """响应中间件：在 HTTP 响应到达后、解析前介入"""

    @abstractmethod
    async def process_response(self, response: Response, request: Request, adapter: BaseAdapter) -> Response | None:
        """返回修改后的 Response 或 None（触发重试）"""
```

### 4.3 管道接口

```python
class Pipeline(ABC):
    """数据处理管道：对解析出的结构化数据进行加工"""

    @abstractmethod
    async def process_item(self, item, context: PipelineContext):
        """返回处理后的数据或 None（丢弃）"""
```

### 4.4 存储接口

```python
class StorageBackend(ABC):
    """存储后端抽象"""

    @abstractmethod
    async def save_novel(self, novel: NovelInfo) -> bool: ...

    @abstractmethod
    async def save_chapter(self, chapter: ChapterContent) -> bool: ...

    @abstractmethod
    async def get_novel(self, novel_id: str, site: str) -> NovelInfo | None: ...

    @abstractmethod
    async def chapter_exists(self, chapter_id: str, site: str) -> bool: ...

    @abstractmethod
    async def save_image(self, image: ImageData) -> str: ...

    @abstractmethod
    async def get_novel_list(self, site: str | None = None) -> list[NovelInfo]: ...

    async def save_state(self, task_id: str, state: dict) -> None: ...
    async def load_state(self, task_id: str) -> dict | None: ...
```

---

## 5. 适配器系统设计

### 5.1 适配器分类

```
BaseAdapter (抽象基类)
  ├── ConfigAdapter (配置驱动，适合简单静态站点)
  │     └── 读取 YAML 配置，自动生成适配器实例
  │     └── 支持：静态 HTML、简单分页、固定编码
  │
  └── CodeAdapter (代码驱动，适合复杂站点)
        ├── Wenku8Adapter (wenku8.net：GBK编码、多页合并)
        ├── SyosetuAdapter (syosetu.com：登录态、Ruby注音)
        └── 自定义适配器...
```

### 5.2 配置适配器（ConfigAdapter）

对于结构简单的静态站点，用户只需编写 YAML 配置即可创建适配器：

```yaml
# adapters/sites/mysite.yaml
site_name: "mysite"
base_url: "https://www.mysite.com"
encoding: "utf-8"
rate_limit: 3.0

search:
  url_template: "/search?q={keyword}&page={page}"
  items_selector: "//div[@class='result-item']"
  fields:
    title: ".//h3/a/text()"
    author: ".//span[@class='author']/text()"
    novel_id: ".//h3/a/@href"
    novel_id_regex: "/book/(\\d+)"

novel_info:
  url_template: "/book/{novel_id}"
  fields:
    title: "//h1[@class='title']/text()"
    author: "//a[@class='author']/text()"
    description: "//div[@class='description']/text()"
    cover_url: "//img[@class='cover']/@src"
    status: "//span[@class='status']/text()"
    status_mapping:
      "連載中": "ongoing"
      "完結": "completed"

chapter_list:
  selector: "//div[@id='chapter-list']//a"
  fields:
    title: "./text()"
    url: "./@href"
    chapter_id_regex: "/chapter/(\\d+)"

chapter_content:
  selector: "//div[@id='content']"
  remove_selectors:
    - "//div[@class='ad']"
    - "//script"
  image_selector: "//img/@src"
```

### 5.3 代码适配器（CodeAdapter）

对于复杂站点，继承 BaseAdapter 编写完整代码：

```python
class Wenku8Adapter(BaseAdapter):
    """wenku8.net 适配器"""

    site_name = "wenku8"
    base_url = "https://www.wenku8.net"
    encoding = "gbk"  # 关键：wenku8 使用 GBK 编码

    def get_rate_limit(self) -> float:
        return 5.0  # wenku8 对频率敏感

    async def get_chapter_content(self, chapter_id: str) -> ChapterContent:
        # wenku8 章节可能分页，需要合并
        pages = []
        page = 1
        while True:
            url = f"{self.base_url}/read/{chapter_id}_{page}.html"
            html = await self._fetch(url)
            content = self._parse_page(html)
            pages.append(content)
            if not self._has_next_page(html):
                break
            page += 1

        merged = self._merge_pages(pages)
        return ChapterContent(
            chapter_id=chapter_id,
            title=merged.title,
            content=merged.html,
            content_format="html",
            images=merged.images,
            word_count=len(merged.text),
            raw_encoding="gbk"
        )
```

### 5.4 适配器注册表

```python
class AdapterRegistry:
    """适配器注册表：目录扫描自动加载"""

    def __init__(self, config_dir: str = "adapters/sites"):
        self._adapters: dict[str, BaseAdapter] = {}
        self._config_dir = config_dir

    def register(self, adapter: BaseAdapter):
        self._adapters[adapter.site_name] = adapter

    def get(self, site_name: str) -> BaseAdapter:
        if site_name not in self._adapters:
            raise AdapterNotFoundError(f"Adapter '{site_name}' not found")
        return self._adapters[site_name]

    def list_sites(self) -> list[str]:
        return list(self._adapters.keys())

    def load_all(self):
        """加载目录下所有 YAML 配置适配器 + 内置代码适配器"""
        # 1. 扫描 YAML 配置
        for path in Path(self._config_dir).glob("*.yaml"):
            config = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.register(ConfigAdapter(config))
        # 2. 注册内置代码适配器
        self.register(Wenku8Adapter())
        # ... 其他内置适配器
```

### 5.5 适配器开发规范

| 项目 | 要求 |
|:---|:---|
| `site_name` | 全局唯一的小写标识符 |
| `base_url` | 站点根 URL |
| `encoding` | 页面编码（不确定则 "auto"） |
| `get_rate_limit()` | 返回该站点的最小请求间隔 |
| 错误处理 | 网络错误、解析失败需抛出明确异常 |
| 测试 | 附带至少 3 个 HTML 快照测试 |

---

## 6. 引擎与中间件设计

### 6.1 引擎核心

```python
class CrawlerEngine:
    """异步爬虫引擎"""

    def __init__(self, adapter, storage, request_mw, response_mw,
                 pipelines, config):
        self.adapter = adapter
        self.storage = storage
        self.request_mw = request_mw
        self.response_mw = response_mw
        self.pipelines = pipelines
        self.semaphore = asyncio.Semaphore(config.max_concurrency)
        self.rate_limiter = RateLimiter(adapter.get_rate_limit())
        self.http_client = AsyncHTTPClient(config.http)

    async def crawl_novel(self, novel_id: str) -> CrawlResult:
        # 1. 获取元信息
        novel_info = await self.adapter.get_novel_info(novel_id)
        await self.storage.save_novel(novel_info)

        # 2. 获取章节目录
        chapters = await self.adapter.get_chapter_list(novel_id)

        # 3. 过滤已爬取的章节
        pending = [ch for ch in chapters
                   if not await self.storage.chapter_exists(ch.chapter_id, self.adapter.site_name)]

        # 4. 并发下载章节
        tasks = [self._crawl_chapter(ch) for ch in pending]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 5. 下载封面
        cover = await self.adapter.get_cover(novel_id)
        if cover:
            await self.storage.save_image(ImageData(...))

        return CrawlResult(novel=novel_info, chapters=results)

    async def _crawl_chapter(self, chapter: ChapterInfo) -> ChapterContent:
        async with self.semaphore:
            await self.rate_limiter.wait()

            request = Request(url=chapter.url, adapter=self.adapter)

            # 请求中间件链
            for mw in self.request_mw:
                request = await mw.process_request(request, self.adapter)
                if request is None:
                    raise RequestBlockedError(...)

            # 执行请求
            response = await self.http_client.fetch(request)

            # 响应中间件链
            for mw in self.response_mw:
                response = await mw.process_response(response, request, self.adapter)
                if response is None:
                    return await self._crawl_chapter(chapter)  # 重试

            # 适配器解析
            content = await self.adapter.get_chapter_content(chapter.chapter_id)

            # 数据处理管道
            for pipe in self.pipelines:
                content = await pipe.process_item(content, PipelineContext(...))
                if content is None:
                    raise ItemDroppedError(...)

            await self.storage.save_chapter(content)
            return content
```

### 6.2 内置中间件

#### 请求中间件

| 中间件 | 功能 | 配置项 |
|:---|:---|:---|
| `UserAgentMiddleware` | 随机轮换 User-Agent | `ua_list`, `rotate` |
| `RateLimitMiddleware` | 域名级限速 | `interval`, `jitter` |
| `RetryMiddleware` | 指数退避重试 | `max_retries`, `backoff_base`, `retry_status_codes` |
| `ProxyMiddleware` | 代理池轮换 | `api_url`, `max_failures` |
| `CookieMiddleware` | Cookie 管理 | `cookie_file` |

#### 响应中间件

| 中间件 | 功能 | 配置项 |
|:---|:---|:---|
| `EncodingMiddleware` | 编码检测与转换 | `fallback` |
| `DecompressMiddleware` | 自动解压 gzip/br | - |
| `ContentVerifyMiddleware` | 内容完整性校验 | `min_word_count`, `honeypot_patterns` |

### 6.3 编码处理设计

三级处理策略：

```
编码检测优先级：
1. 适配器明确声明（非 "auto"）→ 直接使用（站点级确定值最可靠）
2. HTTP 响应头 Content-Type charset
3. HTML meta 标签 <meta charset>
4. chardet 自动检测（置信度 > 0.9 时采用）
5. 回退到 UTF-8 并记录警告
```

```python
class EncodingMiddleware(ResponseMiddleware):
    async def process_response(self, response, request, adapter):
        encoding = self._detect_encoding(response, adapter)

        if response.encoding != encoding:
            try:
                response.text = response.raw_body.decode(encoding)
                response.encoding = encoding
            except (UnicodeDecodeError, LookupError):
                response.text = response.raw_body.decode("utf-8", errors="replace")
                response.encoding = "utf-8"
                logger.warning(f"Encoding fallback to utf-8 for {request.url}")

        return response

    def _detect_encoding(self, response, adapter) -> str:
        if adapter.encoding != "auto":
            return adapter.encoding
        if response.charset:
            return response.charset
        meta = re.search(rb'<meta[^>]+charset=["\']?([\w-]+)', response.raw_body[:1024])
        if meta:
            return meta.group(1).decode("ascii", errors="ignore")
        detected = chardet.detect(response.raw_body)
        if detected["confidence"] > 0.9:
            return detected["encoding"]
        return "utf-8"
```

### 6.4 限速器

```python
class RateLimiter:
    """请求限速：间隔 + 随机抖动"""

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

---

## 7. 数据处理管道设计

### 7.1 管道架构

```
适配器输出
    │
    ▼
┌─────────────┐     ┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ HTML 清洗   │ ──→ │ 文本归一化    │ ──→ │ 内容去重     │ ──→ │ 质量校验     │
│ HTMLCleaner │     │ Normalizer   │     │ Deduplicator│     │ Validator    │
└─────────────┘     └──────────────┘     └─────────────┘     └──────────────┘
                                                                    │
                                                                    ▼
                                                                 入库
```

每个管道可启用/禁用、可替换、可插入自定义管道。

### 7.2 内置管道

#### HTML 清洗管道

```python
class HTMLCleanerPipeline(Pipeline):
    DEFAULT_REMOVE_SELECTORS = [
        "//*[contains(@class, 'ad')]",
        "//*[contains(@class, 'recommend')]",
        "//*[contains(@class, 'share')]",
        "//script", "//style", "//iframe", "//comment()",
    ]

    async def process_item(self, item, context):
        if item.content_format != "html":
            return item

        tree = lxml.html.fromstring(item.content)
        for selector in self.remove_selectors:
            for elem in tree.xpath(selector):
                elem.getparent().remove(elem)

        # 清理属性，只保留 href/src/alt
        for elem in tree.iter():
            for attr in list(elem.attrib):
                if attr not in ("href", "src", "alt"):
                    del elem.attrib[attr]

        item.content = lxml.html.tostring(tree, encoding="unicode")
        return item
```

#### 文本归一化管道

```python
class TextNormalizerPipeline(Pipeline):
    async def process_item(self, item, context):
        text = item.content
        text = html.unescape(text)               # HTML 实体
        text = self._normalize_whitespace(text)   # 空白归一化
        if self.config.normalize_width:
            text = self._normalize_width(text)   # 全角/半角统一
        item.content = text
        return item
```

#### 内容去重管道

```python
class DeduplicationPipeline(Pipeline):
    """基于内容哈希的精确去重"""

    async def process_item(self, item, context):
        content_hash = hashlib.sha256(item.content.encode()).hexdigest()
        if await self.storage.content_hash_exists(content_hash):
            logger.info(f"Duplicate content: {item.chapter_id}")
            return None  # 丢弃
        item.content_hash = content_hash
        return item
```

#### 质量校验管道

```python
class QualityValidatorPipeline(Pipeline):
    """内容质量校验：发现问题记日志，不阻断流程"""

    async def process_item(self, item, context):
        issues = []

        # 1. 字数检查
        text_len = len(re.sub(r'<[^>]+>', '', item.content))
        if text_len < self.config.min_chapter_words:
            issues.append(("warning", f"Chapter too short: {text_len} chars"))

        # 2. 乱码检测
        if self._has_mojibake(item.content):
            issues.append(("error", "Possible encoding corruption"))

        # 3. 反爬蜜罐检测
        for pattern in self.config.honeypot_patterns:
            if pattern in item.content:
                issues.append(("error", f"Honeypot pattern: {pattern}"))
                break

        # 4. 内容截断检测
        if self._appears_truncated(item.content):
            issues.append(("warning", "Content may be truncated"))

        # 记录问题到日志
        for level, msg in issues:
            getattr(logger, level)(f"[{item.chapter_id}] {msg}")

        return item  # 始终返回，不丢弃
```

### 7.3 管道配置

```yaml
pipeline:
  html_cleaner:
    enabled: true
  text_normalizer:
    enabled: true
    normalize_width: true
  deduplication:
    enabled: true
  quality_validator:
    enabled: true
    min_chapter_words: 100
    honeypot_patterns:
      - "请刷新页面重试"
      - "检测到异常访问"
```

---

## 8. 通用数据模型设计

### 8.1 实体关系

```
┌──────────┐     ┌──────────┐     ┌──────────────┐
│  Novel   │1───*│ Volume   │1───*│  Chapter     │
│          │     │          │     │              │
│ id       │     │ id       │     │ id           │
│ site     │     │ novel_id │     │ volume_id    │
│ site_id  │     │ name     │     │ title        │
│ title    │     │ index    │     │ content      │
│ author   │     │ cover_url│     │ word_count   │
│ ...      │     └──────────┘     │ is_crawled   │
│          │                      └──────┬───────┘
│          │1───────────────────────────*│
│          │                             │
│          │1────*┌──────────────┐1─────*│
│          │      │  Resource    │       │
│          │      │ id           │       │
│          │      │ novel_id     │       │
│          │      │ chapter_id   │       │
│          │      │ type         │       │
│          │      │ local_path   │       │
│          │      │ remote_url   │       │
│          │      │ caption      │       │
│          │      │ file_hash    │       │
│          │      └──────────────┘       │
└──────────┘                             │
```

### 8.2 数据模型定义

#### novels 表

| 字段名 | 类型 | 约束 | 说明 |
|:---|:---|:---|:---|
| id | VARCHAR(36) | PK | UUID |
| site | VARCHAR(50) | NOT NULL | 来源站点标识 |
| site_novel_id | VARCHAR(50) | NOT NULL | 站点内小说 ID |
| title | VARCHAR(200) | NOT NULL | 标题 |
| author | VARCHAR(100) | | 作者 |
| description | TEXT | | 简介 |
| cover_url | VARCHAR(500) | | 原站封面 URL |
| cover_local | VARCHAR(255) | | 本地封面路径 |
| status | VARCHAR(20) | | ongoing / completed / hiatus |
| categories | JSON | | 分类列表 |
| tags | JSON | | 标签列表 |
| word_count | INT | | 总字数 |
| last_updated | VARCHAR(30) | | 站点显示的更新时间（原样保存） |
| crawl_time | DATETIME | | 爬取时间 |
| extra | JSON | | 站点特有字段 |
| **UNIQUE** | (site, site_novel_id) | | 联合唯一约束 |

#### volumes 表（新增）

| 字段名 | 类型 | 约束 | 说明 |
|:---|:---|:---|:---|
| id | VARCHAR(36) | PK | UUID |
| novel_id | VARCHAR(36) | FK | 关联 novels |
| name | VARCHAR(200) | | 卷名 |
| index | FLOAT | | 卷序号 |
| cover_url | VARCHAR(500) | | 卷封面 URL |
| cover_local | VARCHAR(255) | | 本地卷封面 |

#### chapters 表

| 字段名 | 类型 | 约束 | 说明 |
|:---|:---|:---|:---|
| id | VARCHAR(36) | PK | UUID |
| novel_id | VARCHAR(36) | FK | 关联 novels |
| volume_id | VARCHAR(36) | FK | 关联 volumes（可为空） |
| site_chapter_id | VARCHAR(50) | | 站点内章节 ID |
| title | VARCHAR(200) | NOT NULL | 章节标题 |
| chapter_index | FLOAT | | 排序序号 |
| content | MEDIUMTEXT | | 正文内容 |
| content_format | VARCHAR(10) | | html / text |
| word_count | INT | | 字数 |
| author_note | TEXT | | 作者后记 |
| is_crawled | BOOLEAN | DEFAULT FALSE | 是否已抓取 |
| crawl_time | DATETIME | | 抓取时间 |
| content_hash | VARCHAR(64) | | 内容哈希（去重用） |
| **UNIQUE** | (novel_id, site_chapter_id) | | 联合唯一 |

#### resources 表

| 字段名 | 类型 | 约束 | 说明 |
|:---|:---|:---|:---|
| id | VARCHAR(36) | PK | UUID |
| novel_id | VARCHAR(36) | FK | 所属小说 |
| chapter_id | VARCHAR(36) | FK | 所属章节（封面类可为空） |
| resource_type | VARCHAR(20) | | cover / illustration |
| remote_url | VARCHAR(500) | | 原站 URL |
| local_path | VARCHAR(255) | | 本地路径 |
| caption | TEXT | | 配文/说明 |
| file_hash | VARCHAR(64) | | SHA-256 |
| image_order | INT | | 在章节中的顺序 |

### 8.3 与原设计的对比

| 改进点 | 原设计 | 改进后 |
|:---|:---|:---|
| 卷结构 | chapter 表的 volume_name 字段 | 独立 volumes 表 |
| 插图配文 | IMAGE 表只有路径 | resources 表增加 caption 字段 |
| 作者后记 | 无 | chapters.author_note 字段 |
| 站点标识 | 无 | 所有表增加 site 字段 |
| 主键策略 | 站点 ID 直接做主键 | UUID + 站点 ID 联合唯一 |
| 内容去重 | 无 | content_hash 字段 |
| 内容格式 | 隐式 HTML | 显式 content_format 字段 |

---

## 9. 存储系统设计

### 9.1 存储后端

```
StorageBackend (抽象接口)
  ├── SQLiteBackend     — 默认，零配置，单文件，个人使用
  └── PostgreSQLBackend — 可选，生产环境，高并发
```

**设计决策**：只做 SQLite + PostgreSQL 两个后端。SQLite 覆盖 95% 的个人使用场景（零配置、单文件、性能足够），PostgreSQL 覆盖需要高并发的生产部署。不做 MongoDB（文档型对关系数据无优势）和 JSON 文件（SQLite 已经是单文件）。

### 9.2 SQLite 默认后端

```python
class SQLiteBackend(StorageBackend):
    def __init__(self, db_path: str = "data/novels.db"):
        self.db_path = db_path
        self._conn = None

    async def init(self):
        self._conn = await aiosqlite.connect(self.db_path)
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._run_migrations()

    async def _run_migrations(self):
        """版本化迁移，启动时自动检测执行"""
        current = await self._get_schema_version()
        for version, sql in self._load_migrations():
            if version > current:
                await self._conn.executescript(sql)
                await self._set_schema_version(version)
```

### 9.3 状态存储

断点续爬的状态持久化，复用业务数据库的 `crawl_states` 表：

```python
@dataclass
class CrawlState:
    task_id: str
    novel_id: str
    site: str
    status: str          # pending / running / paused / completed / failed
    total_chapters: int
    crawled_chapters: int
    failed_chapters: list[str]
    last_chapter_id: str | None
    started_at: datetime
    updated_at: datetime
    error_message: str | None
```

### 9.4 数据库迁移

```sql
-- migrations/001_initial.sql
CREATE TABLE novels (...);
CREATE TABLE volumes (...);
CREATE TABLE chapters (...);
CREATE TABLE resources (...);
CREATE TABLE crawl_states (...);
```

迁移版本记录在 `schema_migrations` 表中，启动时自动检测并执行。

---

## 10. EPUB导出设计

### 10.1 设计目标

保留原设计的 EPUB 3.0 排版标准，在原基础上增强：

| 增强项 | 说明 |
|:---|:---|
| 卷封面 | 每卷可独立封面 |
| 插图配文 | 图片下方显示 caption |
| 作者后记 | 独立页面放在卷末 |
| 章节拆分 | 超长章节自动拆分（原设计已考虑） |
| 自定义 CSS | 用户可覆盖默认样式 |
| 元数据完整 | DC 元数据包含 source URL、爬取时间 |

### 10.2 EPUB 导出流程

1. **元数据封装**：将小说信息映射为 EPUB 元数据（DC 格式）
2. **封面处理**：
   - 有图片：下载、裁剪为合适比例（宽高比约 1:1.5），转换为 JPEG
   - 无图片：用 Pillow 生成简约封面（渐变背景 + 居中文字）
3. **样式注入**：将设计好的 CSS 作为独立样式表加入 EPUB
4. **内容组装**：
   - 遍历章节数据，为每章生成 XHTML 文档
   - 引用统一样式表
   - 将章节内插图引用转换为 EPUB 内部资源路径
5. **目录生成**：创建 NCX（EPUB 2）和 Nav（EPUB 3）文件，支持卷→章结构
6. **资源打包**：按 OEBPS/ 目录结构组织，zip 压缩为 .epub

### 10.3 排版参数（保留原设计）

| 元素 | 字体 | 大小 | 行距 | 对齐 | 缩进 |
|:---|:---|:---|:---|:---|:---|
| 书籍主标题 | Georgia/Noto Serif SC | 2.2em | 1.4 | 居中 | 无 |
| 卷标题 | Georgia/Noto Serif SC | 1.8em | 1.6 | 居中 | 无 |
| 章标题 | Georgia/Noto Serif SC | 1.5em | 1.6 | 居中 | 无 |
| 正文段落 | Georgia/Noto Serif SC | 1em | 1.8 | 两端对齐 | 2em |

### 10.4 导出选项

```yaml
export:
  output_dir: "./output"
  epub:
    style: "default"          # default / custom
    custom_css: null          # 自定义 CSS 文件路径
    split_by_volume: true     # 按卷分文件
    include_toc: true
    include_illustrations: true
    cover_style: "image"      # image / gradient / simple
    font_family: "Georgia, Noto Serif SC"
    base_font_size: "14pt"
    line_height: 1.8
    paragraph_indent: "2em"
```

### 10.5 质量保障

- **标准校验**：导出后调用 EPUBCheck 验证
- **字符集**：强制 UTF-8 编码
- **阅读器兼容**：在 Apple Books、Calibre、Thorium 中测试

---

## 11. 任务调度与状态管理

### 11.1 任务类型

| 类型 | 说明 | 触发方式 |
|:---|:---|:---|
| 单本爬取 | 爬取指定小说 | CLI `crawl` 命令 |
| 增量更新 | 检查已存小说的新章节 | CLI `update` 命令 |
| EPUB 导出 | 从已存数据生成电子书 | CLI `export` 命令 |

### 11.2 任务状态机

```
┌──────────┐     ┌──────────┐     ┌──────────┐
│ PENDING  │───→│ RUNNING  │───→│COMPLETED │
└──────────┘     └────┬─────┘     └──────────┘
                      │
              ┌───────┴───────┐
              │               │
         ┌────▼────┐    ┌────▼────┐
         │ PAUSED  │    │ FAILED  │
         └────┬────┘    └────┬────┘
              │ resume        │ resume
              └───────────────┘
```

### 11.3 断点续爬

```python
class TaskManager:
    async def create_task(self, novel_id: str, site: str) -> str:
        task_id = str(uuid.uuid4())
        await self.storage.save_state(task_id, CrawlState(
            task_id=task_id, novel_id=novel_id, site=site,
            status="pending", ...
        ))
        return task_id

    async def resume_task(self, task_id: str):
        state = await self.storage.load_state(task_id)
        if state["status"] not in ("paused", "failed"):
            raise InvalidTaskStateError(...)

        state["status"] = "running"
        await self.storage.save_state(task_id, state)

        # 获取未完成的章节，从断点继续
        all_chapters = await self.adapter.get_chapter_list(state["novel_id"])
        failed = set(state["failed_chapters"])
        pending = [ch for ch in all_chapters
                   if ch.chapter_id not in failed
                   and not await self.storage.chapter_exists(ch.chapter_id, state["site"])]

        await self._crawl_chapters(task_id, pending)
```

### 11.4 增量更新

```python
class IncrementalUpdater:
    async def check_updates(self, site: str) -> list[UpdateInfo]:
        novels = await self.storage.get_novel_list(site)
        updates = []

        for novel in novels:
            adapter = self.registry.get(novel.site)
            latest = await adapter.get_novel_info(novel.site_novel_id)

            if self._has_update(novel, latest):
                new_chapters = await self._find_new_chapters(novel, adapter)
                updates.append(UpdateInfo(novel=novel, new_chapter_count=len(new_chapters)))

        return updates

    def _has_update(self, stored, latest) -> bool:
        if latest.last_updated != stored.last_updated:
            return True
        if latest.status != stored.status:
            return True
        return False
```

---

## 12. CLI 设计

### 12.1 命令列表

```bash
# 站点管理
novel sites list                       # 列出可用站点
novel sites info wenku8                # 查看站点信息

# 搜索
novel search wenku8 "刀剑神域"          # 搜索小说

# 爬取
novel crawl wenku8 12345               # 爬取指定小说
novel crawl wenku8 12345 --chapters 1-10  # 指定章节范围
novel crawl wenku8 12345 --resume      # 断点续爬

# 增量更新
novel update                           # 更新所有已存小说
novel update --site wenku8             # 仅更新某站点

# 导出
novel export 12345                     # 导出 EPUB
novel export 12345 --output ./books/   # 指定输出目录
novel export 12345 --style custom.css  # 自定义样式

# 数据管理
novel list                             # 列出已爬取的小说
novel info 12345                       # 查看小说详情
novel stats                            # 统计信息
novel delete 12345                     # 删除小说及关联数据

# 任务管理
novel tasks list                       # 列出任务
novel tasks status <task_id>           # 查看任务状态
novel tasks resume <task_id>           # 恢复任务

# 配置
novel config init                      # 初始化配置文件
novel config show                      # 查看当前配置
```

### 12.2 全局选项

```bash
novel [全局选项] <命令> [命令选项]

全局选项：
  --config PATH        指定配置文件路径
  --db PATH            指定数据库路径
  --log-level LEVEL    日志级别 (debug/info/warning/error)
  --json               JSON 格式输出（便于脚本调用）
```

---

## 13. 配置系统设计

### 13.1 配置层级

```
优先级从高到低：
1. 命令行参数
2. 环境变量          NOVEL_DB_PATH=/data/novels.db
3. 项目配置文件      ./novel.yaml
4. 默认值（dataclass）
```

### 13.2 配置文件

```yaml
# novel.yaml

engine:
  max_concurrency: 5           # 最大并发数
  default_rate_limit: 3.0      # 默认请求间隔（秒）

http:
  timeout_connect: 10
  timeout_read: 30

storage:
  backend: "sqlite"            # sqlite / postgresql
  sqlite:
    path: "data/novels.db"
  postgresql:                  # 仅在使用 PG 时需要
    host: "localhost"
    port: 5432
    database: "novel_db"

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
        retry_status_codes: [429, 500, 502, 503, 504]
    - proxy:
        enabled: false
  response:
    - encoding:
        fallback: "utf-8"
    - content_verify:
        min_word_count: 100
        honeypot_patterns: ["请刷新页面重试", "检测到异常访问"]

pipeline:
  html_cleaner:
    enabled: true
  text_normalizer:
    enabled: true
    normalize_width: true
  deduplication:
    enabled: true
  quality_validator:
    enabled: true
    min_chapter_words: 100

export:
  output_dir: "./output"
  epub:
    style: "default"
    split_by_volume: true
    font_family: "Georgia, Noto Serif SC"
    base_font_size: "14pt"
    line_height: 1.8
    paragraph_indent: "2em"

logging:
  level: "info"
  file: "logs/novel.log"
```

### 13.3 配置加载

使用 dataclass 定义默认值，YAML 覆盖默认值，不做复杂的 schema 验证：

```python
@dataclass
class EngineConfig:
    max_concurrency: int = 5
    default_rate_limit: float = 3.0

@dataclass
class AppConfig:
    engine: EngineConfig = EngineConfig()
    storage: StorageConfig = StorageConfig()
    # ...

    @classmethod
    def load(cls, yaml_path: str | None = None) -> "AppConfig":
        config = cls()
        if yaml_path and Path(yaml_path).exists():
            data = yaml.safe_load(Path(yaml_path).read_text("utf-8"))
            # 递归覆盖默认值
            config = _merge(config, data)
        return config
```

---

## 14. 内容质量保障

### 14.1 校验机制

质量校验在管道中执行，发现问题记日志，不阻断流程（除非配置 `drop_failed: true`）：

| 校验类型 | 检查内容 | 级别 |
|:---|:---|:---|
| 完整性 | 字数是否合理、是否有结尾标点、是否截断 | warning |
| 真实性 | 反爬蜜罐模式匹配 | error |
| 编码 | 乱码检测、替换字符比例 | error |

### 14.2 乱码检测

```python
class MojibakeDetector:
    MOJIBAKE_PATTERNS = [
        r'[\ufffd]{3,}',           # Unicode 替换字符
        r'[\u00c0-\u00ff]{5,}',    # Latin-1 补充区连续
        r'Ã[\u0080-\u00bf]',       # UTF-8 被当作 Latin-1
    ]

    def detect(self, text: str) -> bool:
        for pattern in self.MOJIBAKE_PATTERNS:
            if re.search(pattern, text):
                return True
        if text.count('\ufffd') / max(len(text), 1) > 0.01:
            return True
        return False
```

### 14.3 内容截断检测

```python
class TruncationDetector:
    ENDING_PATTERNS = [
        r'[。．.！？!?」』)\s]+$',
        r'（完）|(全文完)|（终）',
    ]
    TRUNCATION_SIGNALS = [r'<[^>]+$', r'[\w]+$']

    def detect(self, content: str) -> bool:
        stripped = content.rstrip()
        has_ending = any(re.search(p, stripped[-20:]) for p in self.ENDING_PATTERNS)
        has_signal = any(re.search(p, stripped[-50:]) for p in self.TRUNCATION_SIGNALS)
        return not has_ending and has_signal
```

---

## 15. 测试策略

### 15.1 测试分层

```
┌────────────────────────────────────────────────┐
│           集成测试 (Integration)                │  ← 少量，端到端
├────────────────────────────────────────────────┤
│           契约测试 (Contract)                   │  ← 每个适配器
├────────────────────────────────────────────────┤
│           快照测试 (Snapshot)                   │  ← HTML 解析防回归
├────────────────────────────────────────────────┤
│           单元测试 (Unit)                       │  ← 大量
└────────────────────────────────────────────────┘
```

### 15.2 适配器契约测试

每个适配器必须通过以下契约测试：

```python
class AdapterContractTest:
    adapter: BaseAdapter  # 子类注入

    async def test_search_returns_results(self):
        results, has_next = await self.adapter.search("测试")
        assert isinstance(results, list)
        assert isinstance(has_next, bool)

    async def test_get_novel_info_returns_valid_data(self):
        novel = await self.adapter.get_novel_info(self.test_novel_id)
        assert novel.title and novel.author

    async def test_get_chapter_list_not_empty(self):
        chapters = await self.adapter.get_chapter_list(self.test_novel_id)
        assert len(chapters) > 0

    async def test_get_chapter_content_has_text(self):
        content = await self.adapter.get_chapter_content(self.test_chapter_id)
        assert len(content.content) > 100

    def test_rate_limit_is_reasonable(self):
        assert self.adapter.get_rate_limit() >= 1.0
```

### 15.3 HTML 快照测试

保存真实 HTML 样本，确保解析不回归：

```python
async def test_parse_novel_page():
    html = Path("tests/snapshots/wenku8/novel_page.html").read_text("gbk")
    result = adapter._parse_novel_info(html)
    expected = json.loads(Path("tests/snapshots/wenku8/novel_page.expected.json").read_text())
    assert result.title == expected["title"]
    assert result.author == expected["author"]
```

### 15.4 覆盖率目标

| 模块 | 目标 | 重点 |
|:---|:---|:---|
| 适配器 | ≥ 85% | 契约 + 快照 |
| 中间件 | ≥ 90% | 编码检测、重试 |
| 管道 | ≥ 90% | 清洗、去重、校验 |
| 存储层 | ≥ 85% | CRUD、迁移 |
| 引擎 | ≥ 80% | 并发、恢复 |

---

## 16. 安全与合规

| 项目 | 措施 |
|:---|:---|
| 请求频率 | 中间件强制最小间隔，配置可控 |
| 联系方式 | User-Agent 包含联系方式 |
| 内容声明 | 导出 EPUB 中标注来源 |
| 停止机制 | 收到停止请求立即停止 |

---

## 17. 项目结构

```
novel-crawler/
├── novel/
│   ├── __init__.py
│   ├── __main__.py                 # CLI 入口
│   ├── cli/                        # CLI 命令
│   │   ├── main.py
│   │   ├── crawl.py
│   │   ├── export.py
│   │   └── search.py
│   ├── core/                       # 核心抽象
│   │   ├── adapter.py              # BaseAdapter, ConfigAdapter
│   │   ├── middleware.py           # 中间件基类
│   │   ├── pipeline.py             # 管道基类
│   │   ├── storage.py              # 存储后端基类
│   │   └── models.py               # 数据模型
│   ├── engine/                     # 引擎
│   │   ├── crawler.py              # CrawlerEngine
│   │   ├── scheduler.py            # 任务调度
│   │   └── state.py                # 状态管理
│   ├── middlewares/                # 内置中间件
│   │   ├── user_agent.py
│   │   ├── rate_limit.py
│   │   ├── retry.py
│   │   ├── proxy.py
│   │   ├── encoding.py
│   │   └── content_verify.py
│   ├── pipelines/                  # 内置管道
│   │   ├── html_cleaner.py
│   │   ├── text_normalizer.py
│   │   ├── deduplication.py
│   │   └── quality_validator.py
│   ├── storage/                    # 存储后端
│   │   ├── sqlite_backend.py
│   │   ├── postgres_backend.py
│   │   └── migrations/
│   ├── exporters/                  # 导出器
│   │   ├── epub.py
│   │   └── templates/
│   │       └── epub/
│   │           ├── style.css
│   │           └── chapter.xhtml
│   ├── adapters/                   # 站点适配器
│   │   ├── registry.py
│   │   ├── code/
│   │   │   ├── wenku8.py
│   │   │   └── syosetu.py
│   │   └── configs/                # YAML 配置适配器
│   ├── http/                       # HTTP 客户端
│   │   └── client.py
│   ├── utils/
│   │   ├── encoding.py
│   │   ├── text.py
│   │   └── mojibake.py
│   └── config.py                   # 配置管理
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── contract/
│   └── snapshots/
├── config/
│   └── default.yaml
├── pyproject.toml
└── README.md
```

---

## 18. 实现路线图

### Phase 1：核心框架（MVP）

**目标**：能爬取单本小说并导出 EPUB

| 任务 | 产出 |
|:---|:---|
| 核心抽象 | BaseAdapter, Middleware, Pipeline, Storage 接口 |
| 引擎 | CrawlerEngine 异步引擎 + Semaphore 并发控制 |
| HTTP 客户端 | 基于 httpx 的异步客户端 |
| SQLite 存储 | 基础 CRUD + 迁移 |
| 编码中间件 | 三级编码检测 |
| HTML 清洗管道 | 基础清洗 |
| EPUB 导出器 | 保留原设计排版标准 |
| wenku8 适配器 | GBK 编码 + 多页合并 |
| CLI 基础 | crawl, export, search 命令 |

### Phase 2：多站点与健壮性

**目标**：支持多站点，具备断点续爬和增量更新

| 任务 | 产出 |
|:---|:---|
| 配置适配器 | ConfigAdapter + YAML 解析 |
| 适配器注册表 | 目录扫描自动加载 |
| 断点续爬 | 任务状态机 + 状态持久化 |
| 增量更新 | UpdateChecker |
| 重试中间件 | 指数退避 |
| 代理中间件 | 代理池管理 |
| 质量校验管道 | 乱码/蜜罐/截断检测 |
| CLI 完善 | update, tasks, config 命令 |

### Phase 3：稳定性与优化

**目标**：生产级稳定性

| 任务 | 产出 |
|:---|:---|
| 更多适配器 | syosetu 等站点 |
| PostgreSQL 后端 | 可选生产存储 |
| 更多测试 | 契约测试 + 快照测试 |
| 性能优化 | 连接池调优 |
| Docker 部署 | 容器化 |

---

## 附录 A：技术选型

| 组件 | 方案 | 选择理由 |
|:---|:---|:---|
| 异步 HTTP | httpx | HTTP/2 支持，API 友好 |
| HTML 解析 | lxml | 性能好，XPath 支持 |
| 异步 SQLite | aiosqlite | 异步原生 |
| EPUB 生成 | ebooklib | 成熟稳定 |
| 图片处理 | Pillow | 封面生成必需 |
| 编码检测 | charset-normalizer | 准确，维护活跃 |
| CLI 框架 | click | 子命令支持好 |
| 配置管理 | dataclass + YAML | 简单，无额外依赖 |
| 日志 | loguru | 开箱即用 |
| 测试 | pytest + pytest-asyncio | 异步测试支持 |

## 附录 B：v3.0 → v3.1 精简记录

v3.0 版本存在过度设计，v3.1 做了以下精简：

| 砍掉项 | 砍掉理由 |
|:---|:---|
| TXT/Markdown/PDF 导出器 | EPUB 是目标格式，其他格式用 Calibre 转换即可 |
| MongoDB/JSON 存储后端 | SQLite 覆盖个人场景，PostgreSQL 覆盖生产，足够 |
| REST API + Web UI | CLI 够用，API 是另一个项目的复杂度 |
| novel_aliases 跨站标识表 | 同一本小说爬两次就行，合并逻辑是伪需求 |
| quality_reports 独立表 | 质量校验保留，记日志即可 |
| SimHash 模糊去重 | 内容哈希精确去重足够 |
| 三级并发管理器 | 单个 Semaphore 搞定 |
| 插件分发机制（entry_points） | 目录扫描自动加载足够 |
| 配置 Schema 验证器 | dataclass 默认值足够 |
| Ruby 注音处理管道 | 保留数据字段，不做专门处理管道 |

---

**文档结束**

*本方案聚焦于让框架真正"通用"（适配器模式）和"可维护"（中间件+管道），同时保持最小复杂度。每个设计决策都对应一个真实问题，不预埋用不到的扩展点。*
