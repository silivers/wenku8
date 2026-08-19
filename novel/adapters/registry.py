"""适配器注册表：目录扫描自动加载

设计说明：
- `_factories` 保存「站点 -> 工厂函数」，每次调用返回一个**全新**适配器实例。
- `get()` 返回**缓存的单例**，供 CLI 轻量入口（search / sites info 等）复用；
- `build()` 返回**全新实例**，供多线程 worker 使用（每个 worker 必须拥有独立
  适配器，因为其内部的 _fetcher / _http_client / 限速锁都是 loop-bound，不可跨线程）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import yaml
from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.exceptions import AdapterNotFoundError


class AdapterRegistry:
    """适配器注册表。

    自动扫描内置代码适配器和 YAML 配置适配器。
    """

    def __init__(self, config_dir: str = "adapters/configs"):
        # site -> 工厂函数（返回全新 BaseAdapter 实例）
        self._factories: dict[str, Callable[[], BaseAdapter]] = {}
        # site -> 缓存单例（供 get 复用）
        self._instances: dict[str, BaseAdapter] = {}
        self._config_dir = config_dir

    def register(self, adapter: BaseAdapter) -> None:
        """注册一个适配器实例（兼容旧调用；内部转为工厂）。"""
        site = adapter.site_name
        self._factories[site] = lambda a=adapter: a
        logger.debug(f"注册适配器: {site}")

    def register_factory(self, site: str, factory: Callable[[], BaseAdapter]) -> None:
        """注册一个适配器工厂（每次调用返回新实例）。"""
        self._factories[site] = factory
        logger.debug(f"注册适配器工厂: {site}")

    def get(self, site_name: str) -> BaseAdapter:
        """返回站点适配器（缓存单例）。"""
        if site_name not in self._factories:
            available = ", ".join(self.list_sites()) or "(无)"
            raise AdapterNotFoundError(
                f"适配器 '{site_name}' 未找到。可用: {available}"
            )
        if site_name not in self._instances:
            self._instances[site_name] = self._factories[site_name]()
        return self._instances[site_name]

    def build(self, site_name: str) -> BaseAdapter:
        """返回站点适配器的**全新实例**（多线程 worker 必须用它）。

        与 get() 的区别：不缓存，每次返回独立对象，避免 _fetcher/_http_client/
        限速锁等 loop-bound 状态在多个事件循环/线程间共享。
        """
        if site_name not in self._factories:
            available = ", ".join(self.list_sites()) or "(无)"
            raise AdapterNotFoundError(
                f"适配器 '{site_name}' 未找到。可用: {available}"
            )
        return self._factories[site_name]()

    def list_sites(self) -> list[str]:
        return sorted(self._factories.keys())

    def load_all(self) -> None:
        """加载所有适配器：内置代码适配器 + YAML 配置适配器。"""
        # 1. 注册内置代码适配器
        self._load_builtin_adapters()

        # 2. 扫描 YAML 配置
        self._load_config_adapters()

    def _load_builtin_adapters(self) -> None:
        """注册内置代码适配器（以工厂形式，每次 build 拿到新实例）。"""
        try:
            from novel.adapters.code.wenku8 import Wenku8Adapter

            self.register_factory("wenku8", Wenku8Adapter)
        except ImportError as e:
            logger.warning(f"加载内置适配器失败: {e}")

    def _load_config_adapters(self) -> None:
        """扫描 YAML 配置文件，注册 ConfigAdapter 工厂。"""
        config_path = Path(self._config_dir)
        if not config_path.exists():
            # 也检查包内目录
            config_path = Path(__file__).parent / "configs"

        if not config_path.exists():
            return

        for yaml_file in config_path.glob("*.yaml"):
            try:
                config = yaml.safe_load(yaml_file.read_text("utf-8"))
                if config and "site_name" in config:
                    from novel.adapters.config_adapter import ConfigAdapter

                    site = config["site_name"]
                    # 默认参数绑定，避免 lambda 循环变量延迟绑定陷阱
                    self.register_factory(
                        site, lambda c=config: ConfigAdapter(c)
                    )
                    logger.info(f"加载配置适配器: {site} ({yaml_file.name})")
            except Exception as e:
                logger.error(f"加载配置适配器失败 {yaml_file.name}: {e}")
