"""中间件基类与请求/响应对象"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from novel.core.adapter import BaseAdapter


@dataclass
class Request:
    """HTTP 请求对象"""

    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    data: Any = None  # 表单数据（application/x-www-form-urlencoded）
    json_body: Any = None  # JSON 请求体
    timeout: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    retry_count: int = 0


@dataclass
class Response:
    """HTTP 响应对象"""

    url: str
    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    raw_body: bytes = b""
    text: str = ""
    encoding: str = "utf-8"
    charset: str | None = None  # 从 Content-Type 头解析
    cookies: dict[str, str] = field(default_factory=dict)
    request: Request | None = None
    from_cache: bool = False


class RequestMiddleware(ABC):
    """请求中间件：在 HTTP 请求发出前介入。"""

    @abstractmethod
    async def process_request(
        self, request: Request, adapter: BaseAdapter
    ) -> Request | None:
        """处理请求。返回修改后的 Request，或 None 表示终止请求。"""


class ResponseMiddleware(ABC):
    """响应中间件：在 HTTP 响应到达后、解析前介入。"""

    @abstractmethod
    async def process_response(
        self, response: Response, request: Request, adapter: BaseAdapter
    ) -> Response | None:
        """处理响应。返回修改后的 Response，或 None 表示触发重试。"""
