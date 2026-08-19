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
    """安全解码字节流。返回 (解码文本, 实际使用的编码)。

    GB 系列（gbk/gb2312）解码失败时自动升级到 gb18030 超集：wenku8 等站点
    页面常含 GB18030 四字节扩展字符，Python 的 gbk codec 会抛
    UnicodeDecodeError（实测 byte 0xfe 非法序列），而 gb18030 完全兼容 GBK；
    仅当 gb18030 也失败才回退 utf-8（replace 保底，避免裸抛）。
    """
    candidates: list[str] = [encoding]
    if encoding.lower() in _GB_FAMILY:
        candidates.append("gb18030")
    candidates.append("utf-8")

    seen: set[str] = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return raw_body.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_body.decode("utf-8", errors="replace"), "utf-8"


# GB 系编码：解码失败时用 gb18030（GBK/GB2312 超集）兜底
_GB_FAMILY = ("gbk", "gb2312", "cp936")
