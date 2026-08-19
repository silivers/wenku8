"""文本处理工具"""

from __future__ import annotations

import html
import re
import unicodedata


def strip_html(text: str) -> str:
    """去除 HTML 标签，返回纯文本。"""
    return re.sub(r"<[^>]+>", "", text)


def normalize_whitespace(text: str) -> str:
    """归一化空白字符。"""
    # 统一换行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 去除行尾空白
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    # 连续空行压缩为最多两个
    text = re.sub(r"\n{3,}", "\n\n", text)
    # 非换行空白压缩
    text = re.sub(r"[^\S\n]+", " ", text)
    return text.strip()


def normalize_width(text: str) -> str:
    """全角/半角统一（NFKC 归一化）。"""
    return unicodedata.normalize("NFKC", text)


def unescape_html(text: str) -> str:
    """反转 HTML 实体。"""
    return html.unescape(text)


def count_words(text: str) -> int:
    """估算字数（中文字符数 + 英文单词数）。"""
    plain = strip_html(text)
    # 中文字符
    chinese = len(re.findall(r"[\u4e00-\u9fff\u3040-\u30ff]", plain))
    # 英文单词
    english = len(re.findall(r"[a-zA-Z]+", plain))
    return chinese + english


def count_text_length(text: str) -> int:
    """计算纯文本字符数（不含 HTML 标签）。"""
    return len(strip_html(text))


def truncate_text(text: str, max_len: int = 200) -> str:
    """截断文本到指定长度，添加省略号。"""
    plain = strip_html(text).strip()
    if len(plain) <= max_len:
        return plain
    return plain[:max_len] + "..."


def appears_truncated(content: str) -> bool:
    """检测内容是否可能被截断。"""
    stripped = content.rstrip()
    if not stripped:
        return False

    # 检查是否有结尾标点
    ending_patterns = [
        r"[。．.！？!?」』)\s]+$",
        r"（完）|(全文完)|（终）",
    ]
    has_ending = any(re.search(p, stripped[-20:]) for p in ending_patterns)

    # 截断信号：未闭合的标签或孤立的词
    truncation_signals = [r"<[^>]+$", r"\b\w{1,3}$"]
    has_signal = any(re.search(p, stripped[-50:]) for p in truncation_signals)

    return not has_ending and has_signal


def safe_filename(name: str) -> str:
    """将标题转为安全的文件名。"""
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(".")
    return name[:100] if len(name) > 100 else name


def safe_path_segment(name: str, fallback: str = "chapter") -> str:
    """将任意字符串净化为『单个』安全的路径片段（防路径穿越，修复 M4）。

    仅保留 [A-Za-z0-9_-]，其余一律替换为 _；去掉首尾的 _/-；
    空结果回退到 fallback。由于不含任何路径分隔符，拼接后必然停留在
    IMAGES_DIR/{novel_id}/ 之下，无法逃出。
    """
    seg = re.sub(r"[^A-Za-z0-9_-]", "_", name or "")
    seg = seg.strip("_-") or fallback
    return seg[:100]
