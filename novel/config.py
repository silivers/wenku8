"""配置管理：dataclass 默认值 + YAML 覆盖"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EngineConfig:
    """正文章节并发（每 worker 内 asyncio.Semaphore）。"""

    max_concurrency: int = 8


@dataclass
class CrawlConfig:
    """全站正文爬取（crawl-all）。"""

    workers: int = 4          # 默认线程数（每 worker 独立限速预算 ≈ N× 吞吐）
    shard: bool = True        # 默认分块领取（块内单本粒度）
    rate_limit: float = 5.0   # 正文请求间隔（秒）——wenku8 站点限速
    block_size: int = 500     # 分块大小（shard 模式：1-500、501-1000…）
    blank_limit: int = 100    # ID 递增遍历中「连续空白编号」判定站点末尾的阈值
    auto_images: bool = False # 正文爬取结束后自动执行图片轮询（images crawl）


@dataclass
class ImagesConfig:
    """图片独立爬取（images crawl）。"""

    workers: int = 4          # 默认线程数
    concurrency: int = 4      # 单本小说内图片并发下载数
    interval: float = 1.0     # 图片请求间隔（秒），走 CDN 可放宽
    max_rounds: int = 10      # 单张图片最大尝试轮数，超过置 skipped（死链兜底）


@dataclass
class HTTPConfig:
    timeout_connect: float = 10
    timeout_read: float = 30
    follow_redirects: bool = True
    proxy: str | None = None  # 全局 HTTP 代理（如 http://127.0.0.1:7890）


@dataclass
class SQLiteConfig:
    path: str = "data/novels.db"


@dataclass
class PostgresConfig:
    host: str = "localhost"
    port: int = 5432
    database: str = "novel_db"
    user: str = ""
    password: str = ""


@dataclass
class StorageConfig:
    backend: str = "sqlite"
    sqlite: SQLiteConfig = field(default_factory=SQLiteConfig)
    postgresql: PostgresConfig = field(default_factory=PostgresConfig)


@dataclass
class ExportConfig:
    output_dir: str = "./output"
    epub_style: str = "default"
    epub_split_by_volume: bool = True
    epub_font_family: str = "Georgia, Noto Serif SC"
    epub_base_font_size: str = "14pt"
    epub_line_height: float = 1.8
    epub_paragraph_indent: str = "2em"


@dataclass
class LoggingConfig:
    level: str = "info"
    file: str = "logs/novel.log"


@dataclass
class AppConfig:
    engine: EngineConfig = field(default_factory=EngineConfig)
    crawl: CrawlConfig = field(default_factory=CrawlConfig)
    images: ImagesConfig = field(default_factory=ImagesConfig)
    http: HTTPConfig = field(default_factory=HTTPConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # 中间件和管道配置保留为原始 dict，按需读取
    middleware: dict[str, Any] = field(default_factory=dict)
    pipeline: dict[str, Any] = field(default_factory=dict)
    # 站点级配置：{站点名: {rate_limit: 秒}}。rate_limit 覆盖适配器声明，
    # 未配置时回退 adapter.get_rate_limit()。
    sites: dict[str, Any] = field(default_factory=dict)

    def site_rate_limit(self, site: str) -> float | None:
        """返回站点配置的限速（秒）；未配置返回 None（由调用方回退适配器声明）。"""
        cfg = self.sites.get(site)
        if isinstance(cfg, dict) and cfg.get("rate_limit"):
            return float(cfg["rate_limit"])
        return None

    @classmethod
    def load(cls, yaml_path: str | None = None) -> "AppConfig":
        """加载配置。优先级：环境变量 > YAML > 默认值。"""
        config = cls()

        # 查找 YAML 文件
        if yaml_path is None:
            for candidate in ["./novel.yaml", "./config/default.yaml"]:
                if Path(candidate).exists():
                    yaml_path = candidate
                    break

        if yaml_path and Path(yaml_path).exists():
            data = yaml.safe_load(Path(yaml_path).read_text("utf-8"))
            if data:
                config = _merge_dataclass(config, data)

        # 环境变量覆盖
        env_db = os.environ.get("NOVEL_DB_PATH")
        if env_db:
            config.storage.sqlite.path = env_db

        env_log = os.environ.get("NOVEL_LOG_LEVEL")
        if env_log:
            config.logging.level = env_log

        env_proxy = os.environ.get("NOVEL_PROXY")
        if env_proxy:
            config.http.proxy = env_proxy

        return config


def _merge_dataclass(instance: Any, data: dict) -> Any:
    """递归地将 dict 合并到 dataclass 实例。"""
    for key, value in data.items():
        if not hasattr(instance, key):
            continue
        current = getattr(instance, key)
        if is_dataclass(current) and isinstance(value, dict):
            setattr(instance, key, _merge_dataclass(current, value))
        elif isinstance(value, dict):
            # 非 dataclass 的 dict 字段（如 middleware/pipeline），直接替换
            setattr(instance, key, value)
        else:
            setattr(instance, key, value)
    return instance
