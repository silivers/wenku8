# Novel Crawler — 通用轻小说爬虫框架

基于适配器模式 + 中间件管道 + 异步引擎的通用轻小说爬虫框架。

## 特性

- **适配器模式**：引擎与站点解耦，简单站点写 YAML 配置即可，复杂站点写代码适配器
- **中间件链**：请求/响应中间件可插拔（UA 轮换、限速、重试、编码检测、内容校验）
- **数据管道**：HTML 清洗 → 文本归一化 → 内容去重 → 质量校验，全部可组合
- **异步优先**：基于 asyncio + httpx，Semaphore 并发控制
- **编码处理**：三级检测（适配器声明 → HTTP 头 → meta 标签 → 自动检测 → UTF-8 回退）
- **图片独立爬取**：正文与插图两阶段解耦——爬正文只登记图片，`images crawl` 轮询遍历下载（失败图下一轮自动重试直至全部成功，下架书经 `book_status` 跳过），EPUB 离线可读
- **全站爬取**：ID 递增遍历站点所有小说（连续 100 个无效编号即止），自动续爬；默认多线程（`crawl: workers=4`，每 worker 独立引擎实例 ≈ N× 吞吐），分块领取默认开启
- **登录态支持**：wenku8 等需登录站点可凭凭据登录，会话 Cookie 贯穿全部请求（章节正文 / 封面 / 插图）
- **自动续爬**：站点进度持久化（`site_meta.last_novel_id`），中断后从上次位置继续，只补缺失章节
- **配置集中**：限速 / 线程 / 并发 / 分块等全部集中在 `config/default.yaml`（`crawl:` / `images:` / `sites:` 段），CLI 无散落默认值、代码无写死生效值
- **增量更新**：检查已存小说的新章节
- **广告噪声过滤**：自动剥离章节正文中的固定广告/署名段（如「本文来自 轻小说文库」「录入：夜殇」）
- **EPUB 导出**：保留排版标准，支持卷封面、插图嵌入、作者后记
- **零配置可用**：SQLite 单文件存储（WAL 模式，多线程并发写安全），开箱即用

## 安装

依赖统一在 `requirements.txt` 中管理（`pyproject.toml` 仅描述包元数据，不内联依赖）。
请先安装依赖，再以可编辑模式安装本包：

```bash
git clone <repo-url>
cd novel-crawler

# 1. 安装运行依赖（必须）
pip install -r requirements.txt

# 2. 以可编辑模式安装包本身（注册 novel 命令）
pip install -e .
```

> 注意：只执行 `pip install -e .` 不会自动安装运行依赖，必须先执行第 1 步。

## 快速开始

```bash
# 初始化配置文件（限速/线程/并发等默认值都在 config/default.yaml）
novel config init

# 查看可用站点
novel sites list

# 搜索小说
novel search wenku8 "刀剑神域"

# 全站爬取正文（默认 4 线程 + 分块领取，见 config crawl: 段；自动续爬）
novel crawl-all wenku8 --max-novels 50

# 全站爬取 + 自动导出 EPUB
novel crawl-all wenku8 --auto-export

# 需登录站点：先验证凭据，再爬取
novel sites login wenku8 --username "acc@qq.com" --password "pwd"
novel crawl-all wenku8 --username "acc@qq.com" --password "pwd"

# 图片独立阶段（正文爬取只登记图片，这里统一下载；失败图自动轮询重试）
novel images crawl wenku8            # 一轮全站 → 失败书下一轮，直到全部成功
novel images status wenku8           # 查看下载状态/轮次/待下载书目

# 导出 EPUB（含已下载插图）
novel export <novel-internal-id>

# 查看已爬取的小说列表
novel list

# 查看统计信息
novel stats
```

## 架构

```
┌─────────────────────────────────────────┐
│              CLI (click)                 │
├─────────────────────────────────────────┤
│           EPUB Exporter                  │
├─────────────────────────────────────────┤
│         SQLite Storage                   │
├─────────────────────────────────────────┤
│     Data Pipeline (清洗→归一化→去重→校验) │
├─────────────────────────────────────────┤
│    Engine + Middleware Chain (asyncio)   │
├─────────────────────────────────────────┤
│  CrawlWorkerPool (crawl-all 默认多线程)   │
├─────────────────────────────────────────┤
│    Adapters (ConfigAdapter / CodeAdapter)│
├─────────────────────────────────────────┤
│  HTTP Client · Encoding · Logging · Config│
└─────────────────────────────────────────┘
```

### 核心设计

**引擎不知道站点**：`CrawlerEngine` 只通过 `BaseAdapter` 接口与站点交互，不感知任何站点细节。

**适配器两种模式**：
- **ConfigAdapter**：写 YAML 配置即可创建适配器（零代码），适合简单静态站点
- **CodeAdapter**：继承 `BaseAdapter` 编写完整代码，适合复杂站点（GBK 编码、多页合并、登录态等）

**中间件可组合**：请求中间件（UA 轮换、限速、重试）和响应中间件（编码检测、内容校验）都是独立组件，可启用/禁用/自定义/调整顺序。

## 编写自定义适配器

### 方式一：YAML 配置适配器

在 `novel/adapters/configs/` 下创建 YAML 文件：

```yaml
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

chapter_list:
  selector: "//div[@id='chapter-list']//a"
  fields:
    title: "./text()"
    url: "./@href"

chapter_content:
  selector: "//div[@id='content']"
  remove_selectors:
    - "//div[@class='ad']"
    - "//script"
  image_selector: "//img/@src"
```

### 方式二：代码适配器

```python
from novel.core.adapter import BaseAdapter
from novel.core.models import NovelInfo, ChapterInfo, ChapterContent

class MySiteAdapter(BaseAdapter):
    @property
    def site_name(self) -> str:
        return "mysite"

    @property
    def base_url(self) -> str:
        return "https://www.mysite.com"

    @property
    def encoding(self) -> str:
        return "utf-8"

    def get_rate_limit(self) -> float:
        return 3.0

    async def search(self, keyword: str, page: int = 1):
        # 实现搜索逻辑
        ...

    async def get_novel_info(self, novel_id: str) -> NovelInfo:
        # 实现获取小说信息
        ...

    async def get_chapter_list(self, novel_id: str) -> list[ChapterInfo]:
        # 实现获取章节目录
        ...

    async def get_chapter_content(self, chapter_id: str, url: str = "") -> ChapterContent:
        # 实现获取章节正文
        ...
```

将文件放到 `novel/adapters/code/` 下，在 `AdapterRegistry._load_builtin_adapters()` 中注册即可。

## 配置

配置文件 `novel.yaml`（由 `novel config init` 生成）：

```yaml
engine:
  max_concurrency: 5
  default_rate_limit: 3.0

storage:
  backend: "sqlite"
  sqlite:
    path: "data/novels.db"

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
        fallback: "utf-8"
    - content_verify:
        min_word_count: 100

pipeline:
  html_cleaner:
    enabled: true
  text_normalizer:
    enabled: true
  deduplication:
    enabled: true
  quality_validator:
    enabled: true
    min_chapter_words: 100
```

优先级：命令行参数 > 环境变量 > 配置文件 > 默认值。

## CLI 命令

| 命令 | 说明 |
|:---|:---|
| `novel sites list` | 列出可用站点 |
| `novel sites info <site>` | 查看站点信息 |
| `novel sites login <site> [--username U] [--password P]` | 登录站点，验证凭据 / 获取会话 Cookie |
| `novel search <site> <keyword>` | 搜索小说 |
| `novel crawl-all <site> [--max-novels N] [--start-id N] [--auto-export] [--username U] [--password P]` | 全站爬取正文；线程/分块/限速由 config `crawl:` 段控制（默认 4 线程 + 分块） |
| `novel images crawl <site>` | 图片轮询爬取：一轮全站，失败图下一轮重试直至全部成功（下架书跳过）；参数由 config `images:` 段控制 |
| `novel images status <site>` | 图片下载状态：各状态计数 / 当前轮次 / 待下载书目 |
| `novel update [--site <site>] [--username U] [--password P]` | 增量更新 |
| `novel export <novel_id> [--output PATH]` | 导出 EPUB（含已下载插图） |
| `novel list` | 列出已爬取的小说 |
| `novel info <novel_id>` | 查看小说详情 |
| `novel stats` | 统计信息 |
| `novel delete <novel_id>` | 删除小说 |
| `novel config init` | 初始化配置文件 |
| `novel config show` | 查看当前配置 |

全局选项：`--config <path>` `--db <path>` `--log-level <level>` `--json`

## 项目结构

```
novel-crawler/
├── novel/
│   ├── __main__.py              # CLI 入口 (python -m novel)
│   ├── config.py                # 配置管理
│   ├── core/                    # 核心抽象
│   │   ├── adapter.py           # BaseAdapter
│   │   ├── middleware.py        # 中间件基类 + Request/Response
│   │   ├── pipeline.py          # 管道基类
│   │   ├── storage.py           # 存储后端抽象
│   │   ├── models.py            # 数据模型 (dataclass)
│   │   └── exceptions.py        # 异常体系
│   ├── engine/                  # 引擎
│   │   ├── crawler.py           # CrawlerEngine + RateLimiter
│   │   └── state.py             # 任务状态管理
│   ├── middlewares/             # 内置中间件
│   │   ├── user_agent.py        # UA 轮换
│   │   ├── rate_limit.py        # 域名级限速
│   │   ├── retry.py             # 指数退避重试
│   │   ├── encoding.py          # 编码检测
│   │   └── content_verify.py    # 内容校验
│   ├── pipelines/               # 内置管道
│   │   ├── html_cleaner.py      # HTML 清洗
│   │   ├── text_normalizer.py   # 文本归一化
│   │   ├── deduplication.py     # 内容去重
│   │   └── quality_validator.py # 质量校验
│   ├── storage/                 # 存储后端
│   │   ├── sqlite_backend.py    # SQLite 异步存储
│   │   └── migrations/          # 数据库迁移
│   ├── exporters/               # 导出器
│   │   ├── epub.py              # EPUB 导出
│   │   └── templates/epub/      # EPUB 模板
│   ├── adapters/                # 站点适配器
│   │   ├── registry.py          # 适配器注册表
│   │   ├── config_adapter.py    # YAML 配置适配器
│   │   └── code/wenku8.py       # wenku8 代码适配器
│   ├── http/client.py           # 异步 HTTP 客户端
│   ├── utils/                   # 工具函数
│   │   ├── encoding.py          # 编码检测
│   │   ├── mojibake.py          # 乱码检测
│   │   └── text.py              # 文本处理
│   └── cli/main.py              # CLI 命令
├── config/default.yaml          # 默认配置
├── pyproject.toml
└── README.md
```

## 技术选型

| 组件 | 方案 | 理由 |
|:---|:---|:---|
| 异步 HTTP | httpx | HTTP/2 支持，API 友好 |
| HTML 解析 | lxml | 性能好，XPath 支持 |
| 异步 SQLite | aiosqlite | 异步原生 |
| EPUB 生成 | ebooklib | 成熟稳定 |
| 图片处理 | Pillow | 封面生成 |
| 编码检测 | charset-normalizer | 准确，维护活跃 |
| CLI | click | 子命令支持好 |
| 日志 | loguru | 开箱即用 |

## 合规声明

- 强制最小请求间隔（由中间件 `rate_limit` 配置控制）
- User-Agent 包含框架标识
- 导出的 EPUB 标注数据来源
- 仅供个人学习研究使用，请遵守当地法律法规

## License

MIT
