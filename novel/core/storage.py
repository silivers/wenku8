"""存储后端抽象接口"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from novel.core.models import ChapterContent, ImageRef, NovelInfo


class StorageBackend(ABC):
    """存储后端抽象。SQLite / PostgreSQL 均实现此接口。"""

    @abstractmethod
    async def init(self) -> None:
        """初始化数据库连接和表结构"""

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""

    @abstractmethod
    async def save_novel(self, novel: NovelInfo, site: str) -> str:
        """保存小说元信息，返回内部 ID"""

    @abstractmethod
    async def get_novel(self, novel_id: str, site: str) -> NovelInfo | None:
        """获取小说信息"""

    @abstractmethod
    async def get_novel_by_internal_id(self, internal_id: str) -> NovelInfo | None:
        """通过内部 UUID 获取小说"""

    @abstractmethod
    async def get_novel_list(self, site: str | None = None) -> list[dict[str, Any]]:
        """获取小说列表"""

    @abstractmethod
    async def delete_novel(self, internal_id: str) -> None:
        """删除小说及关联数据"""

    @abstractmethod
    async def save_chapter(
        self, chapter: ChapterContent, novel_internal_id: str, site: str,
        chapter_index: float = 0, volume: str | None = None,
    ) -> str:
        """保存章节内容，返回内部 ID"""

    @abstractmethod
    async def chapter_exists(self, chapter_id: str, novel_id: str, site: str) -> bool:
        """检查章节是否已爬取（限定小说，防止跨书章节 ID 撞号误判）"""

    @abstractmethod
    async def filter_existing_chapters(
        self, chapter_ids: list[str], site: str, novel_id: str
    ) -> set[str]:
        """批量检查哪些章节已存在，返回已存在的 site_chapter_id 集合。
        必须限定 novel_id：wenku8 章节 ID 是各书独立的相对编号，
        不限定会导致 B 书章节被 A 书同名章节误判已爬而跳过。
        """

    @abstractmethod
    async def commit(self) -> None:
        """提交当前未决事务（批量写入后统一提交，避免逐条 commit，修复 M3）"""

    @abstractmethod
    async def content_hash_exists(self, content_hash: str) -> bool:
        """检查内容哈希是否已存在（去重用）"""

    @abstractmethod
    async def get_chapters(self, novel_internal_id: str) -> list[dict[str, Any]]:
        """获取小说的所有章节"""

    # ---- Image（独立图片爬取，替代原 save_image）----

    @abstractmethod
    async def save_images(
        self, novel_internal_id: str, chapter_site_id: str | None,
        resource_type: str, images: list[ImageRef],
    ) -> int:
        """批量登记图片为 pending（UNIQUE(novel_id, remote_url) 幂等）。
        chapter_site_id 为 None 时表示封面（resource_type='cover'）。
        返回本次实际插入的条数。"""

    @abstractmethod
    async def get_novels_with_pending_images(
        self, site: str, max_attempts: int = 10, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """返回有待下载图片（status IN pending/failed 且 attempt<max_attempts、
        book_status='active'）的小说列表：[{id, title, pending_count}]。"""

    @abstractmethod
    async def get_pending_images(self, novel_internal_id: str) -> list[dict[str, Any]]:
        """返回某小说的全部待下载图片行（按章节、序号排序）。"""

    @abstractmethod
    async def count_pending_images(self, site: str, site_novel_id: str) -> int:
        """某小说尚未下载成功的图片数（给 CLI 提示用；按站内书号定位）。"""

    @abstractmethod
    async def mark_image_downloaded(
        self, image_id: int, local_path: str, file_hash: str
    ) -> None:
        """图片下载成功：置 downloaded + 记录本地路径/哈希。"""

    @abstractmethod
    async def mark_image_failed(self, image_id: int, error: str) -> None:
        """图片下载失败：置 failed + attempt_count+1 + 记录错误。"""

    @abstractmethod
    async def skip_exhausted_images(self, max_rounds: int) -> int:
        """把 attempt_count>=max_rounds 仍失败的图片置 skipped（死链兜底）。
        返回受影响行数。"""

    @abstractmethod
    async def rewrite_chapter_image_url(
        self, novel_id: str, remote_url: str, local_url: str
    ) -> None:
        """把章节 HTML 中的远程图片 URL 替换为本地相对路径。
        按 novel_id 全量 REPLACE（同一 URL 跨章出现时一并覆盖；对已替换
        章节为 no-op，幂等）。"""

    @abstractmethod
    async def set_novel_status(self, novel_internal_id: str, status: str) -> None:
        """更新书籍状态（active/takedown/deleted）。"""

    @abstractmethod
    async def set_novel_cover(
        self, novel_internal_id: str, local_path: str
    ) -> None:
        """封面下载成功后记录本地路径（novels.cover_local）。"""

    @abstractmethod
    async def get_image_stats(self, site: str) -> dict[str, Any]:
        """图片爬取统计：各 status 计数、当前轮次（max attempt_count）、待下载书目数。"""

    @abstractmethod
    async def get_last_novel_id(self, site: str) -> int:
        """读取站点全站遍历的精确数量（最后一个有效小说编号，0 表示未爬过）。"""

    @abstractmethod
    async def set_last_novel_id(self, site: str, last_id: int) -> None:
        """保存站点全站遍历的精确数量（UPSERT），供下次续爬直接读取。"""

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
