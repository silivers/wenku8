# Novel Crawler — 通用轻小说爬虫框架

基于适配器模式 + 中间件管道 + asyncio 的轻小说爬虫，内置 wenku8 适配器：  
正文与图片两阶段解耦爬取（图片轮询重试至全部成功）、多线程全站遍历、SQLite（WAL）存储、EPUB 导出。  
限速 / 线程 / 并发等全部集中在 `config/default.yaml` 的 `crawl:` 与 `images:` 段。

## 快速开始

```bash
pip install -r requirements.txt && pip install -e .
python -m novel sites list                 # 查看可用站点
python -m novel crawl-all wenku8           # 全站爬正文（默认 8 线程 + 分块领取，自动续爬）
python -m novel images crawl wenku8        # 图片轮询下载（失败自动重试至全部成功）
                                          # 或在 config 设 crawl.auto_images: true 一步全搞定
python -m novel images status wenku8       # 图片下载状态
python -m novel export <novel-id>          # 导出 EPUB（含已下载插图）
```

完整命令列表、用法与 FAQ 见 `docs/USAGE.md`；架构见 `docs/TECHNICAL_DESIGN.md`。

## 文档（docs/）

| 文档                                                           | 内容                   |
| :----------------------------------------------------------- | :------------------- |
| [docs/README.md](docs/README.md)                             | 项目总览、特性、CLI 速查       |
| [docs/USAGE.md](docs/USAGE.md)                               | 命令行使用指南与 FAQ         |
| [docs/TECHNICAL\_DESIGN.md](docs/TECHNICAL_DESIGN.md)        | 技术设计（含实现偏差校订）        |
| [docs/DATABASE\_DESIGN.md](docs/DATABASE_DESIGN.md)          | 数据库设计（表结构/状态机/运维 SQL）  |
| [docs/CRAWLER\_GUIDE.md](docs/CRAWLER_GUIDE.md)              | 爬虫开发注意事项（踩坑沉淀，通用）      |
| [docs/MULTITHREAD\_DESIGN.md](docs/MULTITHREAD_DESIGN.md)    | 多线程 worker 池设计       |
| [docs/IMAGE\_CRAWL\_DESIGN.md](docs/IMAGE_CRAWL_DESIGN.md)   | 图片独立爬取设计（已实现）        |
| [docs/WENKU8\_LOGIN\_REPORT.md](docs/WENKU8_LOGIN_REPORT.md) | wenku8 登录/分桶/下架/广告修复 |
| [docs/CODE\_REVIEW.md](docs/CODE_REVIEW.md)                  | 历史代码评审记录             |
| [docs/wenku8/](docs/wenku8/)                                 | wenku8 早期设计稿（历史存档）   |
