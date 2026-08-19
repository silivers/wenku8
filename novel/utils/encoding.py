"""编码检测工具"""

from __future__ import annotations

import re

try:
    from charset_normalizer import from_bytes
    _HAS_CHARSET_NORMALIZER = True
except ImportError:
    _HAS_CHARSET_NORMALIZER = False


def detect_encoding(
    raw_body: bytes,
    declared_encoding: str = "auto",
    http_charset: str | None = None,
) -> tuple[str, float]:
    """三级编码检测。

    优先级：
    1. 适配器明确声明（非 "auto"）→ 直接使用
    2. HTTP 响应头 Content-Type charset
    3. HTML meta 标签
    4. charset-normalizer 自动检测
    5. 回退到 UTF-8

    返回 (编码名, 置信度 0~1)
    """
    # 1. 适配器明确声明
    if declared_encoding and declared_encoding.lower() != "auto":
        return declared_encoding, 1.0

    # 2. HTTP 头
    if http_charset:
        try:
            "".encode(http_charset)  # 验证编码名有效
            return http_charset, 0.95
        except (LookupError, TypeError):
            pass

    # 3. HTML meta 标签
    meta = re.search(
        rb'<meta[^>]+charset=["\']?([\w-]+)',
        raw_body[:2048],
    )
    if meta:
        enc = meta.group(1).decode("ascii", errors="ignore")
        try:
            "".encode(enc)
            return enc, 0.9
        except (LookupError, TypeError):
            pass

    # 4. charset-normalizer
    if _HAS_CHARSET_NORMALIZER and raw_body:
        result = from_bytes(raw_body).best()
        if result and result.encoding:
            return result.encoding, result.coherence

    # 5. 回退
    return "utf-8", 0.0


def safe_decode(raw_body: bytes, encoding: str) -> tuple[str, str]:
    """安全解码字节流。返回 (解码文本, 实际使用的编码)。"""
    try:
        return raw_body.decode(encoding), encoding
    except (UnicodeDecodeError, LookupError):
        text = raw_body.decode("utf-8", errors="replace")
        return text, "utf-8"
