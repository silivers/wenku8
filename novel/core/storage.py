"""存储后端抽象接口"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from novel.core.models import ChapterContent, NovelInfo


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
    async def chapter_exists(self, chapter_id: str, site: str) -> bool:
        """检查章节是否已爬取"""

    @abstractmethod
    async def filter_existing_chapters(
        self, chapter_ids: list[str], site: str
    ) -> set[str]:
        """批量检查哪些章节已存在，返回已存在的 site_chapter_id 集合（修复 M2 N+1）"""

    @abstractmethod
    async def commit(self) -> None:
        """提交当前未决事务（批量写入后统一提交，避免逐条 commit，修复 M3）"""

    @abstractmethod
    async def content_hash_exists(self, content_hash: str) -> bool:
        """检查内容哈希是否已存在（去重用）"""

    @abstractmethod
    async def get_chapters(self, novel_internal_id: str) -> list[dict[str, Any]]:
        """获取小说的所有章节"""

    @abstractmethod
    async def save_image(
        self, novel_internal_id: str, chapter_internal_id: str | None,
        resource_type: str, remote_url: str, local_path: str,
        caption: str = "", file_hash: str = "", image_order: int = 0,
    ) -> str:
        """保存图片资源记录"""

    @abstractmethod
    async def save_state(self, task_id: str, state: dict) -> None:
        """保存爬取任务状态"""

    @abstractmethod
    async def load_state(self, task_id: str) -> dict | None:
        """加载爬取任务状态"""

    @abstractmethod
    async def get_last_novel_id(self, site: str) -> int:
        """读取站点全站遍历的精确数量（最后一个有效小说编号，0 表示未爬过）。"""

    @abstractmethod
    async def set_last_novel_id(self, site: str, last_id: int) -> None:
        """保存站点全站遍历的精确数量（UPSERT），供下次续爬直接读取。"""

    @abstractmethod
    async def list_tasks(self, site: str | None = None) -> list[dict[str, Any]]:
        """列出任务"""

    @abstractmethod
    async def delete_task(self, task_id: str) -> None:
        """删除任务状态记录"""

    @abstractmethod
    async def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
