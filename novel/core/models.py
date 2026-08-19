"""通用数据模型定义"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NovelInfo:
    """小说元信息"""

    site_id: str
    title: str
    author: str
    description: str = ""
    cover_url: str | None = None
    status: str = ""  # ongoing / completed / hiatus
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    last_updated: str | None = None
    word_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChapterInfo:
    """章节目录项"""

    chapter_id: str
    title: str
    volume: str | None = None
    index: float = 0
    url: str = ""
    word_count: int = 0


@dataclass
class ImageRef:
    """图片引用"""

    url: str
    caption: str = ""
    alt: str = ""
    index: int = 0


@dataclass
class ChapterContent:
    """章节正文"""

    chapter_id: str
    title: str
    content: str
    content_format: str = "html"
    images: list[ImageRef] = field(default_factory=list)
    author_note: str | None = None
    word_count: int = 0
    raw_encoding: str = ""
    content_hash: str | None = None
    volume: str | None = None


@dataclass
class CrawlResult:
    """爬取结果"""

    novel: NovelInfo
    chapters: list[ChapterContent]
    failed_chapters: list[str] = field(default_factory=list)
    skipped_chapters: list[str] = field(default_factory=list)
