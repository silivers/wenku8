"""数据处理管道基类"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PipelineContext:
    """管道上下文"""

    site: str = ""
    novel_id: str = ""
    storage: Any = None
    config: Any = None
    extra: dict[str, Any] = field(default_factory=dict)


class Pipeline(ABC):
    """数据处理管道：对解析出的结构化数据进行加工。

    每个管道可启用/禁用、可替换、可插入自定义管道。
    返回处理后的数据继续流转，返回 None 表示丢弃该数据项。
    """

    @abstractmethod
    async def process_item(self, item: Any, context: PipelineContext) -> Any:
        """处理数据项。返回处理后的数据或 None（丢弃）。"""
