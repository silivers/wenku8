"""配置管理：dataclass 默认值 + YAML 覆盖"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class EngineConfig:
    max_concurrency: int = 5
    default_rate_limit: float = 3.0


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
    http: HTTPConfig = field(default_factory=HTTPConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # 中间件和管道配置保留为原始 dict，按需读取
    middleware: dict[str, Any] = field(default_factory=dict)
    pipeline: dict[str, Any] = field(default_factory=dict)

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
