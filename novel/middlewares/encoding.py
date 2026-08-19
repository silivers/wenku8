"""编码检测中间件"""

from __future__ import annotations

from loguru import logger

from novel.core.adapter import BaseAdapter
from novel.core.middleware import Request, Response, ResponseMiddleware
from novel.utils.encoding import detect_encoding, safe_decode


class EncodingMiddleware(ResponseMiddleware):
    """三级编码检测与转换。

    优先级：适配器声明 > HTTP 头 > meta 标签 > 自动检测 > UTF-8 回退
    """

    def __init__(self, fallback: str = "utf-8"):
        self.fallback = fallback

    async def process_response(
        self, response: Response, request: Request, adapter: BaseAdapter
    ) -> Response | None:
        encoding, confidence = detect_encoding(
            response.raw_body,
            declared_encoding=adapter.encoding,
            http_charset=response.charset,
        )

        # 如果检测到的编码与当前不同，重新解码
        if encoding != response.encoding or not response.text:
            text, used_encoding = safe_decode(response.raw_body, encoding)
            response.text = text
            response.encoding = used_encoding

            if confidence < 0.9 and adapter.encoding == "auto":
                logger.debug(
                    f"编码检测: {encoding} (置信度 {confidence:.2f}) for {request.url}"
                )

            if used_encoding == self.fallback and encoding != self.fallback:
                logger.warning(
                    f"编码回退到 {self.fallback} for {request.url} "
                    f"(检测到 {encoding} 但解码失败)"
                )

        return response
